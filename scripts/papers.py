#!/usr/bin/env python3
"""Helper for the paper-researcher agent: search, fetch and verify quotes."""
import argparse
import json
import os
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


SUPERSCRIPTS = str.maketrans("\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b",
                             "0123456789+-")
SUPERSCRIPT_RUN = re.compile("[" + "".join(chr(c) for c in SUPERSCRIPTS) + "]+")


def normalize(text, join_hyphens=True):
    """Lowercase and flatten text so PDF/HTML artefacts don't defeat matching."""
    # 10\u2076 -> 10^6, so NFKC can't silently turn a superscript into a plain digit
    text = SUPERSCRIPT_RUN.sub(lambda m: "^" + m.group().translate(SUPERSCRIPTS), text)
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


def _contains(haystack, needle):
    """Substring match that must not start or end in the middle of a word or number."""
    pre = r"(?<!\w)" if re.match(r"\w", needle) else ""
    post = r"(?!\w)" if re.search(r"\w$", needle) else ""
    return re.search(pre + re.escape(needle) + post, haystack) is not None


def verify_quote(source, quote):
    q = normalize(quote, join_hyphens=False)
    if len(q) < MIN_QUOTE_CHARS:
        return {"found": False, "location": None, "reason": "too short"}
    variants = (True, False)
    if not any(_contains(normalize(source, j), q) for j in variants):
        return {"found": False, "location": None, "reason": "not found in text"}
    for label, body in segments(source):
        if any(_contains(normalize(body, j), q) for j in variants):
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


def _retry_delay(retry_after, attempt):
    """Seconds to wait before retrying: Retry-After if numeric, else 2, 4, 8... (max 30)."""
    if retry_after and retry_after.isdigit():
        return min(int(retry_after), 30)
    return min(2 ** (attempt + 1), 30)


def http_get(url, retries=3, timeout=30, headers=None):
    req = urllib.request.Request(
        url, headers=dict({"User-Agent": "paper-researcher/0.1"}, **(headers or {})))
    for attempt in range(retries + 1):
        retry_after = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or attempt == retries:
                raise
            retry_after = e.headers.get("Retry-After")
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(_retry_delay(retry_after, attempt))


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
    key = os.environ.get("S2_API_KEY")
    headers = {"x-api-key": key} if key else None
    return parse_s2(http_get(s2_url(query, limit), headers=headers).decode("utf-8"))


def search(query, limit=10):
    _terms(query)  # raise a clear error on empty queries
    found, warnings = [], []
    sources = (("arxiv", search_arxiv), ("semanticscholar", search_s2))
    for name, fn in sources:
        try:
            found += fn(query, limit)
        except Exception as e:  # skip a failing source, report it
            warnings.append("%s failed: %s" % (name, e))
    result = dedupe(found)
    for p in result:
        save_meta(p)
    return {"papers": result, "warnings": warnings, "ok": len(warnings) < len(sources)}


MIN_FULLTEXT_CHARS = 2000


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "noscript"}
    HEADINGS = {"h1", "h2", "h3", "h4"}
    BLOCK = {"p", "div", "li", "br", "tr", "section", "figure", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0
        self._math = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "math":
            self._math += 1
            if self._math == 1 and not self._skip:
                self.parts.append(dict(attrs).get("alttext") or "")
        elif self._skip or self._math:
            return
        elif tag in self.HEADINGS:
            self.parts.append("\n## ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "math":
            self._math = max(0, self._math - 1)
        elif self._skip or self._math:
            return
        elif tag in self.HEADINGS or tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not (self._skip or self._math):
            self.parts.append(data)


def html_to_text(html):
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def try_arxiv_html(aid):
    try:
        html = http_get("https://arxiv.org/html/" + aid).decode("utf-8", "replace")
        return html_to_text(html)
    except Exception:
        return None


def try_pdf(url):
    if not shutil.which("pdftotext"):
        return None
    try:
        data = http_get(url, timeout=60)
        proc = subprocess.run(["pdftotext", "-", "-"], input=data,
                              capture_output=True, timeout=120)
    except Exception:
        return None
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def get_abstract(paper_id):
    meta = load_meta(paper_id)
    if meta and meta.get("abstract"):
        return meta["abstract"]
    if paper_id.startswith("arxiv:"):
        try:
            found = parse_arxiv(http_get(arxiv_id_url(paper_id[6:])).decode("utf-8"))
        except Exception:
            return ""
        if found:
            save_meta(found[0])
            return found[0]["abstract"]
    return ""


def _write_text(paper_id, kind, text):
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    path = TEXT_DIR / ("%s.%s.txt" % (safe_name(paper_id), kind))
    path.write_text(text, encoding="utf-8")
    return path


def _cached(paper_id):
    for kind, source_type in (("full", "full_text"), ("abstract", "abstract_only")):
        path = TEXT_DIR / ("%s.%s.txt" % (safe_name(paper_id), kind))
        if path.exists():
            return path, source_type
    return None, None


def _fetch_full_text(paper_id):
    text = None
    if paper_id.startswith("arxiv:"):
        aid = paper_id[6:]
        text = try_arxiv_html(aid)
        if not text or len(text) < MIN_FULLTEXT_CHARS:
            text = try_pdf("https://arxiv.org/pdf/" + aid)
    else:
        meta = load_meta(paper_id)
        if meta and meta.get("pdf_url"):
            text = try_pdf(meta["pdf_url"])
    return text if text and len(text) >= MIN_FULLTEXT_CHARS else None


def fetch(paper_id):
    path, source_type = _cached(paper_id)
    if source_type == "full_text":
        return {"id": paper_id, "path": str(path), "source_type": source_type}
    text = _fetch_full_text(paper_id)  # an abstract-only cache may be a transient failure
    if text:
        path = _write_text(paper_id, "full", text)
        return {"id": paper_id, "path": str(path), "source_type": "full_text"}
    if path:
        return {"id": paper_id, "path": str(path), "source_type": source_type}
    abstract = get_abstract(paper_id)
    if abstract:
        path = _write_text(paper_id, "abstract", abstract)
        return {"id": paper_id, "path": str(path), "source_type": "abstract_only"}
    return {"id": paper_id, "path": None, "source_type": "unavailable"}


def verify_paper(paper_id, quote):
    path, source_type = _cached(paper_id)
    if not path:
        return {"found": False, "location": None, "reason": "not fetched",
                "source_type": "unavailable"}
    result = verify_quote(path.read_text(encoding="utf-8"), quote)
    result["source_type"] = source_type
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(prog="papers.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    f = sub.add_parser("fetch")
    f.add_argument("id")
    v = sub.add_parser("verify")
    v.add_argument("id")
    v.add_argument("quote", nargs="?", default="-",
                   help="quote text, or - (default) to read it from stdin")
    args = ap.parse_args(argv)
    code = 0
    try:
        if args.cmd == "search":
            out = search(args.query, args.limit)
            code = 0 if out["ok"] else 3
        elif args.cmd == "fetch":
            out = fetch(args.id)
            code = 0 if out["path"] else 1
        else:
            quote = sys.stdin.read() if args.quote == "-" else args.quote
            out = verify_paper(args.id, quote)
            code = 0 if out["found"] else 1
    except ValueError as e:
        out, code = {"error": str(e)}, 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
