import unittest

from frontier_monitor.db import Database
from frontier_monitor.utils import fingerprint, utcnow


class DatabaseTests(unittest.TestCase):
    def test_source_upsert_is_idempotent(self):
        db = Database("sqlite:///:memory:")
        item = {
            "fingerprint": fingerprint("https://example.com/a", "A"),
            "url": "https://example.com/a",
            "title": "A",
            "publisher": "Example",
            "published_at": utcnow(),
            "category_hint": "autonomous_agents",
            "source_type": "test",
            "snippet": "hello",
        }
        first = db.upsert_source(item)
        second = db.upsert_source(item)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
