from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = logging.getLogger("mail_archiver")


class ArchiveIndex:
    def __init__(self, archive_root: Path):
        self.path = archive_root / ".archive_index.json"
        self.data: dict[str, Any] = {"message_ids": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("message_ids", {})
        except Exception as exc:
            LOG.warning("索引文件损坏，将重建: %s", exc)
            self.data = {"message_ids": {}}

    def contains(self, message_id: str) -> bool:
        return message_id in self.data["message_ids"]

    def add(self, message_id: str, relative_path: str) -> None:
        self.data["message_ids"][message_id] = {
            "path": relative_path,
            "archived_at": datetime.now().isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
