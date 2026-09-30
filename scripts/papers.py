#!/usr/bin/env python3
"""Helper for the paper-researcher agent: search, fetch and verify quotes."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "papers"
TEXT_DIR = CACHE_DIR / "text"
META_DIR = CACHE_DIR / "meta"

MIN_QUOTE_CHARS = 20


def normalize(text, join_hyphens=True):
    """Lowercase and flatten text so PDF/HTML artefacts don't defeat matching."""
    text = unicodedata.normalize("NFKC", text)  # also expands ligatures
    text = text.replace("­", "")  # soft hyphen
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2" if join_hyphens else r"\1-\2", text)
    for old, new in (("‘", "'"), ("’", "'"), ("“", '"'),
                     ("”", '"'), ("–", "-"), ("—", "-")):
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().lower()


def segments(text):
    """Split text into (label, body): PDF pages if form feeds exist, else ## sections."""
    if "\f" in text:
        return [("p.%d" % i, body) for i, body in enumerate(text.split("\f"), 1)]
    segs, label, buf = [], "start", []
    for line in text.splitlines():
        if line.startswith("## "):
            segs.append((label, "\n".join(buf)))
            label, buf = line[3:].strip(), []
        else:
            buf.append(line)
    segs.append((label, "\n".join(buf)))
    return segs


def verify_quote(source, quote):
    q = normalize(quote, join_hyphens=False)
    if len(q) < MIN_QUOTE_CHARS:
        return {"found": False, "location": None, "reason": "too short"}
    variants = (True, False)
    if not any(q in normalize(source, j) for j in variants):
        return {"found": False, "location": None, "reason": "not found in text"}
    for label, body in segments(source):
        if any(q in normalize(body, j) for j in variants):
            return {"found": True, "location": label, "reason": None}
    return {"found": True, "location": None, "reason": None}


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
S2_FIELDS = "title,authors,year,venue,citationCount,externalIds,abstract,url,openAccessPdf"


def safe_name(paper_id):
    return re.sub(r"[^A-Za-z0-9._-]", "_", paper_id)


def save_meta(paper):
    META_DIR.mkdir(parents=True, exist_ok=True)
    (META_DIR / (safe_name(paper["id"]) + ".json")).write_text(
        json.dumps(paper, ensure_ascii=False), encoding="utf-8")


def load_meta(paper_id):
    path = META_DIR / (safe_name(paper_id) + ".json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def http_get(url, retries=1, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-researcher/0.1"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(2)


def _terms(query):
    terms = re.findall(r"[\w+#.-]+", query)
    if not terms:
        raise ValueError("empty query")
    return terms


def arxiv_url(query, limit):
    search_query = " AND ".join("all:" + t for t in _terms(query))
    params = {"search_query": search_query, "start": 0,
              "max_results": limit, "sortBy": "relevance"}
    return "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)


def arxiv_id_url(arxiv_id):
    return "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"id_list": arxiv_id})


def s2_url(query, limit):
    if not query.strip():
        raise ValueError("empty query")
    params = {"query": query.strip(), "limit": limit, "fields": S2_FIELDS}
    return "https://api.semanticscholar.org/graph/v1/paper/search?" + \
        urllib.parse.urlencode(params)


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_arxiv(xml_text):
    out = []
    for entry in ET.fromstring(xml_text).findall(ATOM + "entry"):
        raw_id = entry.findtext(ATOM + "id", "")
        aid = re.sub(r"v\d+$", "", raw_id.split("/abs/")[-1])
        published = entry.findtext(ATOM + "published", "")
        doi = entry.findtext(ARXIV_NS + "doi", "")
        out.append({
            "id": "arxiv:" + aid, "arxiv_id": aid, "doi": doi.lower(),
            "title": _clean(entry.findtext(ATOM + "title")),
            "authors": [_clean(a.findtext(ATOM + "name"))
                        for a in entry.findall(ATOM + "author")],
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "venue": "arXiv", "citations": None,
            "url": "https://arxiv.org/abs/" + aid,
            "abstract": _clean(entry.findtext(ATOM + "summary")),
            "pdf_url": "https://arxiv.org/pdf/" + aid,
        })
    return out


def parse_s2(json_text):
    out = []
    for item in json.loads(json_text).get("data") or []:
        ext = item.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv") or ""
        doi = (ext.get("DOI") or "").lower()
        pid = ("arxiv:" + arxiv_id) if arxiv_id else \
              ("doi:" + doi) if doi else "s2:" + item["paperId"]
        out.append({
            "id": pid, "arxiv_id": arxiv_id, "doi": doi,
            "title": _clean(item.get("title")),
            "authors": [a["name"] for a in item.get("authors") or []],
            "year": item.get("year"), "venue": item.get("venue") or "",
            "citations": item.get("citationCount"),
            "url": item.get("url") or "",
            "abstract": _clean(item.get("abstract")),
            "pdf_url": (item.get("openAccessPdf") or {}).get("url") or "",
        })
    return out


def _canonical_id(p):
    if p.get("arxiv_id"):
        return "arxiv:" + p["arxiv_id"]
    if p.get("doi"):
        return "doi:" + p["doi"]
    return p.get("id")


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        if k == "citations":
            vals = [x for x in (a.get(k), v) if x is not None]
            out[k] = max(vals) if vals else None
        elif k == "venue":
            if not a.get(k) or a[k] == "arXiv":
                out[k] = v or a.get(k)
        elif not out.get(k):
            out[k] = v
    out["id"] = _canonical_id(out)
    return out


def _keys(p):
    keys = []
    if p.get("doi"):
        keys.append("doi:" + p["doi"].lower())
    if p.get("arxiv_id"):
        keys.append("arxiv:" + p["arxiv_id"])
    if p.get("title"):
        keys.append("title:" + re.sub(r"[^a-z0-9]", "", p["title"].lower()))
    return keys


def dedupe(papers):
    merged, index = [], {}
    for p in papers:
        hit = next((index[k] for k in _keys(p) if k in index), None)
        if hit is None:
            merged.append(p)
            hit = len(merged) - 1
        else:
            merged[hit] = _merge(merged[hit], p)
        for k in _keys(merged[hit]):
            index[k] = hit
    return merged


def search_arxiv(query, limit):
    return parse_arxiv(http_get(arxiv_url(query, limit)).decode("utf-8"))


def search_s2(query, limit):
    return parse_s2(http_get(s2_url(query, limit)).decode("utf-8"))


def search(query, limit=10):
    _terms(query)  # raise a clear error on empty queries
    found, warnings = [], []
    for name, fn in (("arxiv", search_arxiv), ("semanticscholar", search_s2)):
        try:
            found += fn(query, limit)
        except Exception as e:  # skip a failing source, report it
            warnings.append("%s failed: %s" % (name, e))
    result = dedupe(found)
    for p in result:
        save_meta(p)
    return {"papers": result, "warnings": warnings}
