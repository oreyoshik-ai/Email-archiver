from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def list_day(archive_root: Path, day: date | str) -> list[dict]:
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    root = Path(archive_root) / day_s
    items: list[dict] = []
    if not root.is_dir():
        return items
    for meta in sorted(root.glob("*/*/meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        folder = meta.parent
        items.append(
            {
                "subject": data.get("subject") or "",
                "from_name": data.get("from_name") or "",
                "from_email": data.get("from_email") or folder.parent.name,
                "date": data.get("date") or "",
                "attachments": len(data.get("attachments") or []),
                "path": str(folder),
                "relative": folder.relative_to(archive_root).as_posix(),
            }
        )
    items.sort(key=lambda row: row.get("date") or "", reverse=True)
    return items


def list_days(archive_root: Path) -> list[str]:
    root = Path(archive_root)
    if not root.is_dir():
        return []
    days = [p.name for p in root.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-"]
    days.sort(reverse=True)
    return days