import unittest

from frontier_monitor.pipeline import balanced_select, dedupe_candidates
from frontier_monitor.utils import extract_json, fingerprint, title_similarity


class UtilsTests(unittest.TestCase):
    def test_extract_json_from_fence(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```')["a"], 1)

    def test_fingerprint_stable_ignores_query(self):
        a = fingerprint("https://example.com/story?utm_source=x", "Title")
        b = fingerprint("https://example.com/story?utm_source=y", "Title")
        self.assertEqual(a, b)

    def test_title_similarity(self):
        self.assertGreater(title_similarity("Major AI court ruling", "Major AI court ruling announced"), 0.7)

    def test_dedupe_candidates_by_source(self):
        items = [
            {"canonical_title": "A", "source_ids": [1], "materiality": 8, "novelty": 7, "category": "x"},
            {"canonical_title": "B", "source_ids": [1, 2], "materiality": 7, "novelty": 6, "category": "x"},
        ]
        out = dedupe_candidates(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0]["source_ids"]), {1, 2})

    def test_balanced_selection(self):
        items = [
            {"canonical_title": "A1", "category": "a", "materiality": 9, "novelty": 8},
            {"canonical_title": "A2", "category": "a", "materiality": 8, "novelty": 8},
            {"canonical_title": "B1", "category": "b", "materiality": 7, "novelty": 9},
        ]
        out = balanced_select(items, 2)
        self.assertEqual({x["category"] for x in out}, {"a", "b"})


if __name__ == "__main__":
    unittest.main()
