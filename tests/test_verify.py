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


class WordBoundaryTests(unittest.TestCase):
    SRC = ("Training remained unstable throughout, and accuracy improved by 12% overall. "
           "It is impossible to scale this approach.")

    def test_fragment_starting_mid_word_rejected(self):
        r = papers.verify_quote(self.SRC, "stable throughout, and accuracy improved")
        self.assertFalse(r["found"])

    def test_fragment_ending_mid_number_rejected(self):
        self.assertFalse(papers.verify_quote(self.SRC, "accuracy improved by 1")["found"])

    def test_fragment_that_flips_meaning_rejected(self):
        self.assertFalse(papers.verify_quote(self.SRC, "possible to scale this approach")["found"])

    def test_quote_ending_in_symbol_still_found(self):
        r = papers.verify_quote(self.SRC, "accuracy improved by 12%")
        self.assertTrue(r["found"])


if __name__ == "__main__":
    unittest.main()
