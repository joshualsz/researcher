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
