"""Tests for safe recall DB migration and FSRS-v5 scheduling persistence."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import memory, review


class RecallMigrationFsrsTests(unittest.TestCase):
    """Preserve the legacy memory DB and schedule FSRS grades 1–4."""

    def test_legacy_memory_db_is_copied_not_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "memory.db"
            with sqlite3.connect(legacy) as connection:
                connection.execute("CREATE TABLE qa(id INTEGER PRIMARY KEY,ts REAL,direction TEXT,topic TEXT,question TEXT,answer TEXT,correct INTEGER)")
                connection.execute("INSERT INTO qa VALUES(1,1.0,'asked','topic','q','a',NULL)")
                connection.execute("CREATE TABLE mastery(topic TEXT PRIMARY KEY,prereq TEXT,correct INTEGER,total INTEGER)")
                connection.execute("INSERT INTO mastery VALUES('topic','[]',1,2)")
                connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
                connection.execute("INSERT INTO meta VALUES('ingested','[]')")
            recall = root / "recall.sqlite"
            with patch.object(memory, "DATA", root), patch.object(memory, "DB", recall), patch.object(memory, "LEGACY_MEMORY_DB", legacy):
                connection = memory._conn()
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM qa").fetchone()[0], 1)
                self.assertEqual(tuple(connection.execute("SELECT correct,total FROM mastery WHERE topic='topic'").fetchone()), (1,2))
                connection.close()
            self.assertTrue(legacy.is_file())
            self.assertTrue(recall.is_file())

    def test_fsrs_ratings_persist_with_due_and_retrievability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "recall.sqlite"
            with patch.object(memory, "DATA", root), patch.object(memory, "DB", db), patch.object(memory, "LEGACY_MEMORY_DB", root/'memory.db'):
                results=[]
                now=datetime.now(timezone.utc)
                for rating in (1,2,3,4):
                    results.append(review.update(f"topic-{rating}",rating,folder="maths",review_datetime=now))
                self.assertEqual([r["rating"] for r in results],[1,2,3,4])
                self.assertTrue(all(r["due"] and 0<=r["retrievability"]<=1 for r in results))
                due=review.due_topics(limit=2)
                self.assertLessEqual(len(due),2)
                connection=sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)
                connection.execute("PRAGMA query_only=ON")
                rows=connection.execute("SELECT topic,card_json FROM fsrs_cards ORDER BY topic").fetchall()
                connection.close()
                self.assertEqual(len(rows),4)
                self.assertEqual(json.loads(rows[0][1])["state"],1)


if __name__ == "__main__":
    unittest.main()
