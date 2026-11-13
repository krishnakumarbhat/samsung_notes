"""Wait for a quiet Samsung Notes wdoc tree using read-only SQLite checks."""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


def read_snapshot(local_state: Path) -> tuple[int, int, int]:
    """Read dirty-note count, latest modification epoch, and wdoc mtime."""
    database = local_state / "Storage.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(NoteDB)")}
        if "IsDirty" not in columns:
            raise RuntimeError("NoteDB.IsDirty is unavailable; refusing a false-clean result.")
        dirty = connection.execute(
            "SELECT COUNT(*) FROM NoteDB WHERE DeletedStatus=0 AND COALESCE(IsDirty, 0)<>0"
        ).fetchone()[0]
        latest_modified = connection.execute(
            "SELECT COALESCE(MAX(LastModifiedAt), 0) FROM NoteDB WHERE DeletedStatus=0"
        ).fetchone()[0]
        return int(dirty), int(latest_modified), (local_state / "wdoc").stat().st_mtime_ns
    finally:
        connection.close()


def wait_for_quiescence(
    local_state: Path,
    timeout_seconds: int,
    interval_seconds: int,
    stable_intervals: int,
) -> int:
    """Require clean notes and unchanged wdoc/modified epochs across samples."""
    deadline = time.monotonic() + timeout_seconds
    previous: tuple[int, int] | None = None
    stable = 0
    sample = 0
    while time.monotonic() < deadline:
        sample += 1
        try:
            dirty, latest_modified, wdoc_mtime = read_snapshot(local_state)
            current = (latest_modified, wdoc_mtime)
            stable = stable + 1 if dirty == 0 and current == previous else 0
            print(
                f"sample={sample} dirty={dirty} stable={stable}/{stable_intervals} "
                f"wdoc_mtime_ns={wdoc_mtime} max_modified={latest_modified}",
                flush=True,
            )
            if dirty == 0 and stable >= stable_intervals:
                print("SYNC_QUIESCENT=YES", flush=True)
                return 0
            previous = current
        except (OSError, sqlite3.Error, RuntimeError) as error:
            stable = 0
            previous = None
            print(f"sample={sample} read_error={type(error).__name__}", flush=True)
        time.sleep(interval_seconds)
    print("SYNC_QUIESCENT=NO; timed out", flush=True)
    return 2


def main() -> int:
    """Parse bounded wait settings and check the current Samsung Notes store."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--stable", type=int, default=3)
    args = parser.parse_args()
    if min(args.timeout, args.interval, args.stable) < 1:
        parser.error("timeout, interval, and stable must be positive")
    local_state = (
        Path.home()
        / "AppData/Local/Packages/SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState"
    )
    return wait_for_quiescence(local_state, args.timeout, args.interval, args.stable)


if __name__ == "__main__":
    raise SystemExit(main())
