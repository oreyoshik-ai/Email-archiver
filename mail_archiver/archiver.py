from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from mail_archiver.config import AppConfig
from mail_archiver.extract import archive_kind, extract_archive, extract_stem
from mail_archiver.index import ArchiveIndex
from mail_archiver.parse import ParsedMail, parse_raw_email
from mail_archiver.report import append_daily_row
from mail_archiver.screenshot import render_screenshot, set_hidden
from mail_archiver.util import sanitize_filename, to_tz, unique_dir, unique_path

LOG = logging.getLogger("mail_archiver")


@dataclass
class ArchiveResult:
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    note: str = ""


class MailArchiver:
    def __init__(self, cfg: AppConfig, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.tz = None
        self.index = ArchiveIndex(cfg.archive_root)

    def setup(self) -> None:
        from mail_archiver.util import resolve_timezone

        self.tz = resolve_timezone(self.cfg.timezone)
        if not self.dry_run:
            self.cfg.archive_root.mkdir(parents=True, exist_ok=True)

    def archive_raw(self, raw: bytes, received: datetime | None = None) -> str:
        if self.tz is None:
            self.setup()
        fallback = received or datetime.now(self.tz)
        mail = parse_raw_email(raw, fallback_date=fallback)
        return self.archive_mail(mail, received=received)

    def archive_mail(self, mail: ParsedMail, received: datetime | None = None) -> str:
        if self.tz is None:
            self.setup()
        if self.index.contains(mail.message_id):
            LOG.info("跳过已归档: %s | %s", mail.from_email, mail.subject)
            return "skipped"

        when = to_tz(received or mail.date, self.tz)
        day: date = when.date()
        day_dir = self.cfg.archive_root / day.isoformat()

        sender_label = (mail.from_name or mail.from_raw or mail.from_email or "未知发件人").strip()
        sender_dir_name = sanitize_filename(f"{sender_label}{mail.from_email}", max_len=120)
        sender_dir = day_dir / sender_dir_name

        subject_slug = sanitize_filename(mail.subject, max_len=80)
        serial = self._next_serial(sender_dir)
        folder_name = f"{subject_slug}_{serial:03d}"
        dest = sender_dir / folder_name

        if self.dry_run:
            LOG.info("[dry-run] %s  <-  %s | %s", dest, mail.from_email, mail.subject)
            self.index.add(mail.message_id, str(dest.relative_to(self.cfg.archive_root)))
            return "dry-run"

        dest = unique_dir(dest)
        dest.mkdir(parents=True, exist_ok=False)
        try:
            self._write_mail(dest, mail, when)
        except Exception:
            LOG.exception("写入失败，已尝试保留目录: %s", dest)
            raise

        rel = dest.relative_to(self.cfg.archive_root).as_posix()
        self.index.add(mail.message_id, rel)
        self.index.save()
        append_daily_row(
            self.cfg.archive_root / day.isoformat() / "_日报.csv",
            datetime.now(self.tz).isoformat(timespec="seconds"),
            mail,
            rel,
        )
        LOG.info("已归档 %s", rel)
        return "saved"

    def _next_serial(self, sender_dir: Path) -> int:
        if not sender_dir.is_dir():
            return 1
        max_serial = 0
        for entry in sender_dir.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if "_" in name:
                suffix = name.rsplit("_", 1)[-1]
                if suffix.isdigit():
                    max_serial = max(max_serial, int(suffix))
        return max_serial + 1

    def _write_mail(self, dest: Path, mail: ParsedMail, when: datetime) -> None:
        preview_png = dest / "邮件预览.png"
        render_screenshot(mail, preview_png)

        attach_dir = dest / "attachments"
        saved_attachments: list[dict] = []
        used_names: set[str] = set()
        for item in mail.attachments:
            attach_dir.mkdir(parents=True, exist_ok=True)
            filename = item.filename
            if filename.lower() in used_names:
                filename = unique_path(attach_dir / filename).name
            used_names.add(filename.lower())
            file_path = attach_dir / filename
            file_path.write_bytes(item.content)
            extracted = False
            extract_note = ""
            if self.cfg.extract.enabled and archive_kind(filename):
                out_dir = unique_dir(attach_dir / sanitize_filename(extract_stem(filename), max_len=80))
                extracted, extract_note = extract_archive(file_path, out_dir, self.cfg.extract)
                if extracted:
                    LOG.info("已解压 %s -> %s", filename, out_dir.name)
                elif out_dir.exists() and not any(out_dir.iterdir()):
                    try:
                        out_dir.rmdir()
                    except OSError:
                        pass
            saved_attachments.append(
                {
                    "filename": filename,
                    "size": len(item.content),
                    "content_type": item.content_type,
                    "extracted": extracted,
                    "extract_note": extract_note,
                }
            )

        meta = {
            "message_id": mail.message_id,
            "subject": mail.subject,
            "from": mail.from_raw,
            "from_name": mail.from_name,
            "from_email": mail.from_email,
            "to": mail.to_raw,
            "date": mail.date.isoformat(timespec="seconds"),
            "folder_date": when.date().isoformat(),
            "folder_time": when.strftime("%H:%M:%S"),
            "attachments": saved_attachments,
        }
        (dest / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        set_hidden(dest / "meta.json")
