from __future__ import annotations

import csv
import logging
from pathlib import Path

from mail_archiver.parse import ParsedMail

LOG = logging.getLogger("mail_archiver")

FIELDS = ["归档时间", "邮件时间", "发件人", "邮箱", "主题", "相对路径", "附件数", "Message-ID"]


def append_daily_row(csv_path: Path, archived_at: str, mail: ParsedMail, relative: str) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids: set[str] = set()
    rows: list[dict[str, str]] = []
    if csv_path.is_file():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                mid = row.get("Message-ID") or ""
                if mid:
                    existing_ids.add(mid)
                rows.append(row)
    if mail.message_id in existing_ids:
        return
    rows.append(
        {
            "归档时间": archived_at,
            "邮件时间": mail.date.isoformat(timespec="seconds"),
            "发件人": mail.from_name or mail.from_raw,
            "邮箱": mail.from_email,
            "主题": mail.subject,
            "相对路径": relative,
            "附件数": str(len(mail.attachments)),
            "Message-ID": mail.message_id,
        }
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
