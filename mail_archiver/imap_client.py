from __future__ import annotations

import imaplib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

from mail_archiver.config import ImapConfig
from mail_archiver.util import imap_date

LOG = logging.getLogger("mail_archiver")

_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


class ImapError(RuntimeError):
    pass


def _ensure_id_command() -> None:
    imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")


def _send_id(conn: imaplib.IMAP4) -> None:
    payload = (
        '("name" "mail-archiver" "version" "1.0.0" '
        '"vendor" "mail-archiver" "support-email" "noreply@localhost")'
    )
    try:
        typ, _ = conn._simple_command("ID", payload)
        try:
            conn._untagged_response("ID")
        except Exception:
            pass
        LOG.debug("IMAP ID 状态: %s", typ)
    except Exception as exc:
        LOG.debug("IMAP ID 发送失败（部分服务器可忽略）: %s", exc)


def _parse_internaldate(header: bytes) -> datetime | None:
    match = _INTERNALDATE_RE.search(header)
    if not match:
        return None
    try:
        dt = parsedate_to_datetime(match.group(1).decode("ascii", errors="replace"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class ImapSource:
    def __init__(self, cfg: ImapConfig):
        self.cfg = cfg
        self.conn: imaplib.IMAP4 | None = None

    def connect(self) -> None:
        _ensure_id_command()
        LOG.info("连接 IMAP %s:%s ...", self.cfg.host, self.cfg.port)
        try:
            if self.cfg.ssl:
                self.conn = imaplib.IMAP4_SSL(self.cfg.host, self.cfg.port, timeout=30)
            else:
                self.conn = imaplib.IMAP4(self.cfg.host, self.cfg.port, timeout=30)
        except Exception as exc:
            raise ImapError(f"无法连接 {self.cfg.host}:{self.cfg.port}: {exc}") from exc

        _send_id(self.conn)
        try:
            typ, _ = self.conn.login(self.cfg.username, self.cfg.auth_code)
        except imaplib.IMAP4.error as exc:
            raise ImapError(
                "IMAP 登录失败。163/126 必须使用网页邮箱生成的「授权码」，"
                "不能用登录密码；并请先在设置中开启 IMAP。原始错误: "
                f"{exc}"
            ) from exc
        if typ != "OK":
            raise ImapError("IMAP 登录被拒绝")
        LOG.info("IMAP 登录成功: %s", self.cfg.username)

    def close(self) -> None:
        if not self.conn:
            return
        try:
            self.conn.logout()
        except Exception:
            try:
                self.conn.shutdown()
            except Exception:
                pass
        self.conn = None

    def __enter__(self) -> "ImapSource":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def fetch_on_date(self, day: date) -> Iterator[tuple[bytes, datetime | None]]:
        if self.conn is None:
            raise ImapError("IMAP 未连接")
        typ, _ = self.conn.select(self.cfg.folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"无法打开文件夹 {self.cfg.folder}")

        uids = self._search_uids(day)
        LOG.info("日期 %s 在 %s 中匹配到 %s 封邮件", day.isoformat(), self.cfg.folder, len(uids))
        for uid in uids:
            raw, internal = self._fetch_uid(uid)
            if raw:
                yield raw, internal

    def _search_uids(self, day: date) -> list[str]:
        assert self.conn is not None
        day_s = imap_date(day)
        queries = [
            f"(ON {day_s})",
            f"(SINCE {day_s} BEFORE {imap_date(day + timedelta(days=1))})",
        ]
        for query in queries:
            try:
                typ, data = self.conn.uid("SEARCH", None, query)
            except Exception as exc:
                LOG.debug("SEARCH %s 失败: %s", query, exc)
                continue
            if typ != "OK":
                continue
            token = data[0] if data else b""
            if not token:
                continue
            uids = token.decode("ascii", errors="ignore").split()
            if uids:
                return uids
        return []

    def _fetch_uid(self, uid: str) -> tuple[bytes | None, datetime | None]:
        assert self.conn is not None
        try:
            typ, data = self.conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")
        except Exception as exc:
            LOG.warning("拉取 UID %s 失败: %s", uid, exc)
            return None, None
        if typ != "OK" or not data:
            return None, None
        raw = None
        internal = None
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            header, payload = item[0], item[1]
            if isinstance(header, bytes):
                internal = _parse_internaldate(header) or internal
            if isinstance(payload, (bytes, bytearray)):
                raw = bytes(payload)
        if raw is None:
            LOG.warning("UID %s 未取到正文", uid)
        return raw, internal
