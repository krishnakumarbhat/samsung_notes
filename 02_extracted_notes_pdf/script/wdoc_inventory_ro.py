"""Read-only metadata inventory helper for the Windows Samsung Notes guest."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def main() -> None:
    """Emit note/folder/WDoc-size metadata without opening a write connection."""
    local_state = (
        Path.home()
        / "AppData/Local/Packages/SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState"
    )
    database = local_state / "Storage.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    categories = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT UUID,ParentUUID,DisplayName FROM CategoryTreeDB WHERE IsDeleted=0"
        )
    }
    rows = []
    query = "SELECT UUID,Title,CategoryUUID,LastModifiedAt,IsDirty FROM NoteDB WHERE DeletedStatus=0"
    for uuid, title, category, modified, dirty in connection.execute(query):
        folder_parts, seen = [], set()
        current = category
        while current in categories and current not in seen:
            seen.add(current)
            parent, name = categories[current]
            if name and name != "Folders":
                folder_parts.append(name)
            current = parent

        wdoc = local_state / "wdoc" / uuid
        files = [path for path in wdoc.rglob("*") if path.is_file()] if wdoc.is_dir() else []
        body = wdoc / "note.note"
        rows.append(
            {
                "uuid": uuid,
                "title": title or "Untitled",
                "folder": "/".join(reversed(folder_parts)),
                "modified": int(modified),
                "dirty": int(dirty or 0),
                "wdoc_exists": wdoc.is_dir(),
                "page_files": sorted(path.name for path in wdoc.glob("*.page")) if wdoc.is_dir() else [],
                "note_body_bytes": body.stat().st_size if body.is_file() else None,
                "payload_bytes": sum(path.stat().st_size for path in files),
            }
        )
    connection.close()
    print(json.dumps(rows, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
