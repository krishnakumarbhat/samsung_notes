"""Unit tests for the bounded, read-only sync quiescence check."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wait_sync_quiescence import read_snapshot, wait_for_quiescence


class QuiescenceTests(unittest.TestCase):
    """Exercise read-only snapshots and clean/dirty wait behavior."""

    def make_store(self, root: Path, dirty: int = 0) -> Path:
        """Create an isolated fixture with the minimum Samsung Notes schema."""
        local_state = root / "LocalState"
        wdoc = local_state / "wdoc"
        wdoc.mkdir(parents=True)
        with sqlite3.connect(local_state / "Storage.sqlite") as connection:
            connection.execute(
                "CREATE TABLE NoteDB (UUID TEXT, DeletedStatus INTEGER, IsDirty INTEGER, LastModifiedAt INTEGER)"
            )
            connection.execute(
                "INSERT INTO NoteDB VALUES ('fixture', 0, ?, 42)", (dirty,)
            )
        return local_state

    def test_snapshot_reads_dirty_count_and_epoch(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_state = self.make_store(Path(temporary), dirty=0)
            dirty, modified, wdoc_mtime = read_snapshot(local_state)
            self.assertEqual((dirty, modified), (0, 42))
            self.assertEqual(wdoc_mtime, (local_state / "wdoc").stat().st_mtime_ns)

    def test_wait_returns_only_when_clean_and_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_state = self.make_store(Path(temporary), dirty=0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = wait_for_quiescence(local_state, 5, 1, 1)
            self.assertEqual(result, 0)
            self.assertIn("SYNC_QUIESCENT=YES", output.getvalue())

    def test_dirty_note_times_out_without_claiming_quiescence(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_state = self.make_store(Path(temporary), dirty=1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = wait_for_quiescence(local_state, 1, 1, 1)
            self.assertEqual(result, 2)
            self.assertIn("SYNC_QUIESCENT=NO", output.getvalue())


if __name__ == "__main__":
    unittest.main()
