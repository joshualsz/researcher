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
