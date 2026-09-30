# Paper Researcher Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Claude Code subagent that researches a topic via arXiv and Semantic Scholar and writes a report of verbatim, verified excerpts, each with a reference.

**Architecture:** A stdlib-only Python helper (`scripts/papers.py`) does the deterministic work: `search`, `fetch` and `verify`. A subagent definition (`.claude/agents/paper-researcher.md`) orchestrates it and writes `research/<topic-slug>.md`. Quotes are only kept if `verify` confirms they appear in the fetched text.

**Tech Stack:** Python 3.9+ (stdlib only: `urllib`, `xml.etree`, `html.parser`, `unittest`), optional `pdftotext` (poppler) for PDF text, Claude Code subagents.

**Spec:** `docs/superpowers/specs/2026-09-30-paper-researcher-agent-design.md`

## Global Constraints

- Helper script is stdlib-only Python; no `pip install`. PDF extraction uses the optional external `pdftotext` binary; if it is missing, papers fall back to arXiv HTML or abstract-only (labelled).
- Sources: arXiv and Semantic Scholar only (no web search, no local PDFs).
- Never invent a quote; never cite a paper that was not fetched; every kept quote must pass `verify`.
- Failed API calls: retry once, then skip and report as a warning.
- Paper ids are canonical strings: `arxiv:<id>` (preferred), else `doi:<doi>`, else `s2:<paperId>`.
- Source type is always reported as `full_text`, `abstract_only` or `unavailable`.
- Tests run with `python3 -m unittest discover -s tests -v` from the repo root.

## Review Focus

- A very short quote (e.g. `12%`) would match almost anywhere: `verify` must reject quotes under 20 normalised characters. (Task 1)
- A quote that spans a PDF page break: `verify` must still find it (location may be unknown). (Task 1)
- An empty or whitespace-only quote must not "verify". (Task 1)
- The same paper returned by both APIs with a different title case or arXiv version suffix must merge into one entry. (Task 2)
- A topic containing `&`, quotes or `+` must not break the query URLs; an empty topic must give a clear error. (Task 2)

---

### Task 1: Text normalisation and quote verification

**Files:**
- Create: `scripts/papers.py`
- Create: `tests/test_verify.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: `normalize(text: str, join_hyphens: bool = True) -> str`
- Produces: `segments(text: str) -> list[tuple[str, str]]` returning `(label, body)` pairs
- Produces: `verify_quote(source: str, quote: str) -> dict` with keys `found: bool`, `location: str | None`, `reason: str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify.py`:

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import papers

SOURCE = (
    "We propose a new method for state-of-the-art retrieval. "
    "Our ap-\nproach uses ﬁne-grained “attention” — it works.\n\n"
    "## 4.2 Results\nAccuracy improved by 12% over the baseline."
)


class NormalizeTests(unittest.TestCase):
    def test_collapses_whitespace_and_case(self):
        self.assertEqual(papers.normalize("Hello \n  World"), "hello world")

    def test_joins_hyphenated_linebreak(self):
        self.assertEqual(papers.normalize("ap-\nproach"), "approach")

    def test_keeps_hyphen_when_not_joining(self):
        self.assertEqual(papers.normalize("state-\nof", join_hyphens=False), "state-of")

    def test_ligatures_and_smart_punctuation(self):
        self.assertEqual(
            papers.normalize("ﬁne “x” – y’s"), "fine \"x\" - y's"
        )


class VerifyTests(unittest.TestCase):
    def test_found_with_section_location(self):
        r = papers.verify_quote(SOURCE, "Accuracy improved by 12% over the baseline")
        self.assertTrue(r["found"])
        self.assertEqual(r["location"], "4.2 Results")

    def test_found_across_hyphenation_ligature_and_quotes(self):
        r = papers.verify_quote(SOURCE, 'Our approach uses fine-grained "attention"')
        self.assertTrue(r["found"])
        self.assertEqual(r["location"], "start")

    def test_fabricated_quote_not_found(self):
        r = papers.verify_quote(SOURCE, "Accuracy dropped dramatically in every setting")
        self.assertFalse(r["found"])

    def test_too_short_quote_rejected(self):
        r = papers.verify_quote(SOURCE, "12%")
        self.assertFalse(r["found"])
        self.assertEqual(r["reason"], "too short")

    def test_empty_quote_rejected(self):
        for q in ("", "   \n "):
            self.assertFalse(papers.verify_quote(SOURCE, q)["found"])

    def test_pdf_page_location(self):
        src = "alpha beta gamma delta epsilon\fzeta eta theta iota kappa lambda"
        r = papers.verify_quote(src, "zeta eta theta iota kappa")
        self.assertEqual((r["found"], r["location"]), (True, "p.2"))

    def test_quote_spanning_page_break_found_without_location(self):
        src = "first half of the sentence here\fsecond half continues on"
        r = papers.verify_quote(src, "sentence here second half continues")
        self.assertEqual((r["found"], r["location"]), (True, None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'papers'`

- [ ] **Step 3: Write minimal implementation**

Create `.gitignore`:

```
.cache/
__pycache__/
```

Create `scripts/papers.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: 11 tests, all PASS

- [ ] **Step 5: Commit**

```bash
git add .gitignore scripts/papers.py tests/test_verify.py
git commit -m "feat: add quote normalisation and verification"
```

---

### Task 2: Search (arXiv + Semantic Scholar) and dedupe

**Files:**
- Modify: `scripts/papers.py` (append)
- Create: `tests/test_search.py`

**Interfaces:**
- Consumes: `META_DIR` from Task 1
- Produces: paper dicts with keys `id, arxiv_id, doi, title, authors (list[str]), year (int|None), venue, citations (int|None), url, abstract, pdf_url`
- Produces: `parse_arxiv(xml_text) -> list[dict]`, `parse_s2(json_text) -> list[dict]`, `dedupe(papers) -> list[dict]`
- Produces: `arxiv_url(query, limit) -> str`, `arxiv_id_url(arxiv_id) -> str`, `s2_url(query, limit) -> str`
- Produces: `http_get(url, retries=1, timeout=30) -> bytes`
- Produces: `search(query, limit=10) -> {"papers": [...], "warnings": [...]}`; saves each paper via `save_meta(paper)`; `load_meta(paper_id) -> dict | None`; `safe_name(paper_id) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search.py`:

```python
import json
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import papers

ARXIV_XML = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
 <entry>
  <id>http://arxiv.org/abs/2301.12345v2</id>
  <published>2023-01-29T00:00:00Z</published>
  <title>A  Study of
   Things</title>
  <summary>  We study things. </summary>
  <author><name>Ada Lovelace</name></author>
  <author><name>Alan Turing</name></author>
  <arxiv:doi>10.1000/xyz</arxiv:doi>
 </entry>
</feed>"""

S2_JSON = json.dumps({"data": [
    {"paperId": "abc123", "title": "a study of THINGS", "year": 2023,
     "authors": [{"name": "Ada Lovelace"}], "venue": "NeurIPS", "citationCount": 42,
     "externalIds": {"ArXiv": "2301.12345", "DOI": "10.1000/XYZ"},
     "abstract": "We study things.",
     "url": "https://www.semanticscholar.org/paper/abc123",
     "openAccessPdf": {"url": "https://example.org/p.pdf"}},
    {"paperId": "zzz", "title": "Sparse Record", "year": None, "authors": None,
     "venue": "", "citationCount": None, "externalIds": None, "abstract": None,
     "url": "https://www.semanticscholar.org/paper/zzz", "openAccessPdf": None},
]})


class ParseTests(unittest.TestCase):
    def test_parse_arxiv(self):
        [p] = papers.parse_arxiv(ARXIV_XML)
        self.assertEqual(p["id"], "arxiv:2301.12345")
        self.assertEqual(p["arxiv_id"], "2301.12345")
        self.assertEqual(p["title"], "A Study of Things")
        self.assertEqual(p["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(p["year"], 2023)
        self.assertEqual(p["abstract"], "We study things.")
        self.assertEqual(p["doi"], "10.1000/xyz")
        self.assertEqual(p["url"], "https://arxiv.org/abs/2301.12345")

    def test_parse_s2_full_record(self):
        p = papers.parse_s2(S2_JSON)[0]
        self.assertEqual(p["id"], "arxiv:2301.12345")
        self.assertEqual(p["doi"], "10.1000/xyz")
        self.assertEqual(p["citations"], 42)
        self.assertEqual(p["venue"], "NeurIPS")
        self.assertEqual(p["pdf_url"], "https://example.org/p.pdf")

    def test_parse_s2_sparse_record(self):
        p = papers.parse_s2(S2_JSON)[1]
        self.assertEqual(p["id"], "s2:zzz")
        self.assertEqual(p["authors"], [])
        self.assertEqual(p["abstract"], "")
        self.assertEqual(p["pdf_url"], "")


class DedupeTests(unittest.TestCase):
    def test_same_paper_from_both_sources_merges(self):
        merged = papers.dedupe(
            papers.parse_arxiv(ARXIV_XML) + papers.parse_s2(S2_JSON)[:1]
        )
        self.assertEqual(len(merged), 1)
        p = merged[0]
        self.assertEqual(p["id"], "arxiv:2301.12345")
        self.assertEqual(p["citations"], 42)
        self.assertEqual(p["venue"], "NeurIPS")
        self.assertEqual(p["title"], "A Study of Things")

    def test_title_only_match_ignores_case_and_punctuation(self):
        a = {"id": "s2:1", "title": "Deep Learning!", "citations": 1}
        b = {"id": "s2:2", "title": "deep learning", "citations": 5}
        merged = papers.dedupe([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["citations"], 5)

    def test_different_papers_stay_separate(self):
        a = {"id": "s2:1", "title": "Alpha"}
        b = {"id": "s2:2", "title": "Beta"}
        self.assertEqual(len(papers.dedupe([a, b])), 2)


class UrlTests(unittest.TestCase):
    def test_special_characters_are_encoded(self):
        url = papers.arxiv_url('c++ "quotes" & more', 5)
        self.assertNotIn(" ", url)
        self.assertNotIn('"', url)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["search_query"], ["all:c++ AND all:quotes AND all:more"])
        self.assertEqual(qs["max_results"], ["5"])

    def test_s2_url_encodes_query(self):
        url = papers.s2_url("a & b", 3)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["query"], ["a & b"])

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError):
            papers.arxiv_url("  ", 5)
        with self.assertRaises(ValueError):
            papers.s2_url("", 5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_search -v`
Expected: FAIL with `AttributeError: module 'papers' has no attribute 'parse_arxiv'`

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/papers.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests PASS (Task 1 and Task 2)

- [ ] **Step 5: Live smoke check (network)**

Run: `python3 -c "import sys; sys.path.insert(0,'scripts'); import papers, json; r=papers.search('retrieval augmented generation', 3); print(len(r['papers']), r['warnings']); print(r['papers'][0]['id'], r['papers'][0]['title'])"`
Expected: prints a count of at least 1, a warnings list (an empty list, or a Semantic Scholar rate-limit message), and the first paper's id and title.

- [ ] **Step 6: Commit**

```bash
git add scripts/papers.py tests/test_search.py
git commit -m "feat: add arXiv and Semantic Scholar search with dedupe"
```

---

### Task 3: Fetch, verify command and CLI

**Files:**
- Modify: `scripts/papers.py` (append)
- Create: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `http_get, load_meta, save_meta, safe_name, parse_arxiv, arxiv_id_url, verify_quote, TEXT_DIR, search` from Tasks 1-2
- Produces: `html_to_text(html: str) -> str`, `try_arxiv_html(aid) -> str | None`, `try_pdf(url) -> str | None`, `get_abstract(paper_id) -> str`
- Produces: `fetch(paper_id) -> {"id", "path": str | None, "source_type": "full_text" | "abstract_only" | "unavailable"}`
- Produces: `verify_paper(paper_id, quote) -> dict` (`found, location, reason, source_type`)
- Produces: `main(argv=None) -> int`, CLI commands `search <query> [--limit N]`, `fetch <id>`, `verify <id> <quote>`; JSON on stdout; exit code 1 when fetch is unavailable or a quote is not found

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fetch.py`:

```python
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import papers

LONG_TEXT = "## Intro\n" + ("The quick brown fox jumps over the lazy dog. " * 60)

HTML = """<html><head><script>var x = 1;</script></head><body>
<header><h1>Site Banner</h1></header>
<article><h2>Results</h2><p>Loss was <math alttext="L = 0.5"><mi>L</mi></math> after training.</p>
<p>Second paragraph.</p></article></body></html>"""


class HtmlToTextTests(unittest.TestCase):
    def test_extracts_headings_math_and_skips_chrome(self):
        text = papers.html_to_text(HTML)
        self.assertIn("## Results", text)
        self.assertIn("Loss was L = 0.5 after training.", text)
        self.assertIn("Second paragraph.", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("Site Banner", text)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name, value in (("TEXT_DIR", base / "text"), ("META_DIR", base / "meta")):
            p = mock.patch.object(papers, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_full_text_from_arxiv_html(self):
        with mock.patch.object(papers, "try_arxiv_html", return_value=LONG_TEXT):
            r = papers.fetch("arxiv:2301.12345")
        self.assertEqual(r["source_type"], "full_text")
        self.assertTrue(r["path"].endswith(".full.txt"))
        self.assertEqual(Path(r["path"]).read_text(encoding="utf-8"), LONG_TEXT)

    def test_falls_back_to_pdf_when_no_html(self):
        with mock.patch.object(papers, "try_arxiv_html", return_value=None), \
             mock.patch.object(papers, "try_pdf", return_value=LONG_TEXT) as pdf:
            r = papers.fetch("arxiv:2301.12345")
        self.assertEqual(r["source_type"], "full_text")
        pdf.assert_called_once_with("https://arxiv.org/pdf/2301.12345")

    def test_tiny_text_is_not_treated_as_full_text(self):
        with mock.patch.object(papers, "try_arxiv_html", return_value="Not found."), \
             mock.patch.object(papers, "try_pdf", return_value=None), \
             mock.patch.object(papers, "get_abstract", return_value="An abstract."):
            r = papers.fetch("arxiv:2301.12345")
        self.assertEqual(r["source_type"], "abstract_only")

    def test_abstract_only_fallback(self):
        with mock.patch.object(papers, "try_arxiv_html", return_value=None), \
             mock.patch.object(papers, "try_pdf", return_value=None), \
             mock.patch.object(papers, "get_abstract", return_value="An abstract."):
            r = papers.fetch("arxiv:2301.12345")
        self.assertEqual(r["source_type"], "abstract_only")
        self.assertTrue(r["path"].endswith(".abstract.txt"))

    def test_unavailable_when_nothing_found(self):
        with mock.patch.object(papers, "try_arxiv_html", return_value=None), \
             mock.patch.object(papers, "try_pdf", return_value=None), \
             mock.patch.object(papers, "get_abstract", return_value=""):
            r = papers.fetch("arxiv:2301.12345")
        self.assertEqual(r, {"id": "arxiv:2301.12345", "path": None,
                             "source_type": "unavailable"})

    def test_non_arxiv_paper_uses_pdf_url_from_search_meta(self):
        papers.save_meta({"id": "doi:10.1/x", "pdf_url": "https://e.org/p.pdf",
                          "abstract": ""})
        with mock.patch.object(papers, "try_pdf", return_value=LONG_TEXT) as pdf:
            r = papers.fetch("doi:10.1/x")
        self.assertEqual(r["source_type"], "full_text")
        pdf.assert_called_once_with("https://e.org/p.pdf")

    def test_second_fetch_uses_cache(self):
        with mock.patch.object(papers, "try_arxiv_html", return_value=LONG_TEXT):
            papers.fetch("arxiv:2301.12345")
        with mock.patch.object(papers, "try_arxiv_html") as html:
            r = papers.fetch("arxiv:2301.12345")
        html.assert_not_called()
        self.assertEqual(r["source_type"], "full_text")


class VerifyPaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(papers, "TEXT_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_verifies_against_cached_text(self):
        (Path(self.tmp.name) / "arxiv_1.full.txt").write_text(LONG_TEXT, encoding="utf-8")
        r = papers.verify_paper("arxiv:1", "The quick brown fox jumps over the lazy dog")
        self.assertTrue(r["found"])
        self.assertEqual(r["source_type"], "full_text")
        self.assertEqual(r["location"], "Intro")

    def test_abstract_only_is_labelled(self):
        (Path(self.tmp.name) / "arxiv_2.abstract.txt").write_text(
            "We study things at great length.", encoding="utf-8")
        r = papers.verify_paper("arxiv:2", "We study things at great length")
        self.assertTrue(r["found"])
        self.assertEqual(r["source_type"], "abstract_only")

    def test_unfetched_paper_fails_clearly(self):
        r = papers.verify_paper("arxiv:999", "Some long enough quote text here")
        self.assertFalse(r["found"])
        self.assertEqual(r["reason"], "not fetched")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_fetch -v`
Expected: FAIL with `AttributeError: module 'papers' has no attribute 'html_to_text'`

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/papers.py`:

```python
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


def fetch(paper_id):
    path, source_type = _cached(paper_id)
    if path:
        return {"id": paper_id, "path": str(path), "source_type": source_type}
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
    if text and len(text) >= MIN_FULLTEXT_CHARS:
        path = _write_text(paper_id, "full", text)
        return {"id": paper_id, "path": str(path), "source_type": "full_text"}
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
    v.add_argument("quote")
    args = ap.parse_args(argv)
    code = 0
    try:
        if args.cmd == "search":
            out = search(args.query, args.limit)
        elif args.cmd == "fetch":
            out = fetch(args.id)
            code = 0 if out["path"] else 1
        else:
            out = verify_paper(args.id, args.quote)
            code = 0 if out["found"] else 1
    except ValueError as e:
        out, code = {"error": str(e)}, 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests PASS

- [ ] **Step 5: Live CLI check (network)**

Run:
```bash
python3 scripts/papers.py search "attention is all you need" --limit 3
python3 scripts/papers.py fetch arxiv:1706.03762
python3 scripts/papers.py verify arxiv:1706.03762 "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks"
python3 scripts/papers.py verify arxiv:1706.03762 "This sentence was never written in the paper at all"; echo "exit=$?"
```
Expected:
- The search prints JSON that includes `arxiv:1706.03762`.
- The fetch prints `"source_type": "full_text"`.
- The first verify prints `"found": true`.
- The second verify prints `"found": false` and `exit=1`.

If `fetch` reports `abstract_only`, check whether `pdftotext` is installed (`which pdftotext`). Then check whether the arXiv HTML page for that paper exists. The fallback is expected behaviour, so it isn't a bug.

- [ ] **Step 6: Commit**

```bash
git add scripts/papers.py tests/test_fetch.py
git commit -m "feat: add fetch, verify and CLI"
```

---

### Task 4: Agent definition, README and end-to-end run

**Files:**
- Create: `.claude/agents/paper-researcher.md`
- Create: `README.md`
- Create (by the run): `research/<topic-slug>.md`

**Interfaces:**
- Consumes: CLI commands `search`, `fetch` and `verify` from Task 3, with the JSON shapes defined there.

- [ ] **Step 1: Write the agent definition**

Create `.claude/agents/paper-researcher.md`:

````markdown
---
name: paper-researcher
description: Researches a topic across arXiv and Semantic Scholar and writes a report of verbatim, verified excerpts from the papers, each with a numbered reference. Use when the user wants sourced research points or quotes on a topic.
tools: Bash, Read, Write
---

You are a research assistant. Given a topic, you find relevant academic papers and produce a report of **verbatim excerpts, each traceable to its source**. Accuracy of quotes and references matters more than coverage.

All paper work goes through the helper script, run from the repository root:

- `python3 scripts/papers.py search "<query>" --limit 10` prints JSON `{papers: [...], warnings: [...]}`. Each paper has `id, title, authors, year, venue, citations, url, abstract`.
- `python3 scripts/papers.py fetch "<id>"` prints JSON `{id, path, source_type}`. `source_type` is `full_text`, `abstract_only` or `unavailable`. Read the file at `path` to get the text.
- `python3 scripts/papers.py verify "<id>" "<quote>"` prints JSON with `found`, `location` and `source_type`. It exits 1 if the quote is not found.

## Workflow

1. Turn the topic into 3-5 distinct search queries (synonyms, sub-topics, key methods).
2. Run `search` for each query. Combine the results and drop duplicate ids. Note any `warnings`; they go in the report.
3. Shortlist 5-10 papers by relevance to the topic, favouring recent and well-cited ones. Prefer variety of viewpoints over near-duplicates.
4. Run `fetch` on each shortlisted paper and read the text file. If `source_type` is `unavailable`, drop the paper and note it.
5. Extract 3-6 excerpts per paper that directly help answer the topic: definitions, key claims, results with numbers, stated limitations. Copy each one **exactly** from the text. Prefer one to three sentences. Never paraphrase inside quote marks and never stitch sentences together with "...".
6. Run `verify` on **every** excerpt. If it fails, re-copy it from the text and verify again once. If it still fails, drop it. Use the `location` that `verify` returns (`p.N` or a section title) in the reference when it is not null.
7. Write the report to `research/<topic-slug>.md` (lowercase, hyphens), creating the folder if needed.

## Hard rules

- Every quote in the report must have passed `verify`. Do not include unverified text as a quote.
- Never cite a paper you did not `fetch`. Never invent authors, years, titles or locations. Take bibliographic details from the `search` output only.
- If a paper is `abstract_only`, mark every excerpt from it `(abstract only)` and say so in the bibliography.
- Do not present your own synthesis as if a paper said it. Synthesis sentences carry no quote marks. Every claim in them must be backed by an excerpt shown under it.

## Report format

```markdown
# <Topic>

_Generated <date>. Sources: arXiv, Semantic Scholar. <N> papers, <M> verified excerpts._

## <Theme 1>
<One or two sentences of synthesis, no quote marks.>

> "<verbatim excerpt>" — [3, §4.2]

> "<verbatim excerpt>" — [5, p.7]

## <Theme 2>
...

## Notes
- Search warnings, dropped papers, abstract-only papers, and gaps in coverage.

## References
1. <Authors>. (<year>). *<Title>*. <venue>. <url> — full text
2. ...
```

Number references in order of first appearance. Group excerpts by theme, not by paper. End your reply to the user with the report path and a two-line summary. Do not paste the whole report into the chat.
````

- [ ] **Step 2: Write the README**

Create `README.md`:

````markdown
# Paper Researcher

A Claude Code subagent that researches a topic on arXiv and Semantic Scholar. It writes a report of verbatim excerpts, each with a reference. Every quote is checked against the paper's text before it goes into the report.

## Use

In Claude Code, from this folder (restart the session or run `/agents` after cloning so the agent loads):

> Use the paper-researcher agent to research "retrieval augmented generation for code".

The report is saved to `research/<topic-slug>.md`.

## How it works

- `.claude/agents/paper-researcher.md`: the agent's workflow and citation rules.
- `scripts/papers.py`: the helper it calls (stdlib-only Python).
  - `search "<query>" [--limit N]` searches arXiv and Semantic Scholar and dedupes the results.
  - `fetch <id>` downloads a paper's full text (arXiv HTML, else PDF via `pdftotext`) or falls back to the abstract.
  - `verify <id> "<quote>"` checks that a quote appears in the fetched text.

`pdftotext` (poppler, `brew install poppler`) is optional. Without it, papers that lack an arXiv HTML version fall back to abstract-only, and the report labels them.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
````

- [ ] **Step 3: Load the agent and run it end to end**

Restart the Claude Code session or run `/agents` so the new agent is picked up. Then ask:

> Use the paper-researcher agent to research "retrieval augmented generation".

Expected: the agent finishes, and `research/retrieval-augmented-generation.md` exists.

- [ ] **Step 4: Spot-check the report**

Open the report and check:
- It has themed sections, blockquotes that each end with a `[n, location]` reference, and a numbered References section.
- The Notes section lists any warnings, dropped papers and abstract-only papers.
- Every `[n]` matches an entry in References.

Then independently verify three quotes copied from the report. Use the id from the fetch output for the paper each quote came from:

```bash
python3 scripts/papers.py verify "<id>" "<quote copied from the report>"
```

Expected: `"found": true` for all three. If any fails, the agent prompt needs tightening, so fix it in `.claude/agents/paper-researcher.md` and re-run.

- [ ] **Step 5: Commit**

```bash
git add .claude/agents/paper-researcher.md README.md research/
git commit -m "feat: add paper-researcher agent, README and sample report"
```
