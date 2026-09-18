from __future__ import annotations

import imaplib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

from mail_archiver.config import ImapConfig
from mail_archiver.util import imap_date, to_tz

LOG = logging.getLogger("mail_archiver")

_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
_UID_RE = re.compile(rb"UID (\d+)")


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
                "IMAP 登录失败。必须使用邮箱网页设置里生成的「授权码」，"
                "不能用登录密码；并请先在邮箱设置中开启 IMAP。"
                "若刚切换了邮箱，请确认填的是新邮箱的授权码。原始错误: "
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

    def fetch_on_date(self, day: date, tz=None) -> Iterator[tuple[bytes, datetime | None]]:
        """归档单日（等价于 fetch_in_range(day, day)）。"""
        yield from self.fetch_in_range(day, day, tz)

    def fetch_in_range(self, start: date, end: date, tz=None) -> Iterator[tuple[bytes, datetime | None]]:
        """归档 [start, end] 闭区间内所有邮件。

        一次 SEARCH SINCE start BEFORE end+1 取候选 UID，再用 INTERNALDATE 二次校验落在区间内，
        避免服务端 SEARCH 不可靠（个别邮箱空范围会返回整箱）。
        """
        if self.conn is None:
            raise ImapError("IMAP 未连接")
        typ, _ = self.conn.select(self.cfg.folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"无法打开文件夹 {self.cfg.folder}")

        candidates = self._search_range(start, end)
        LOG.info("%s~%s 在 %s 中初筛到 %s 封", start.isoformat(), end.isoformat(), self.cfg.folder, len(candidates))
        uids = self._filter_in_range(candidates, start, end, tz)
        LOG.info("按 INTERNALDATE 校验后 %s~%s 实有 %s 封", start.isoformat(), end.isoformat(), len(uids))
        for uid in uids:
            raw, internal = self._fetch_uid(uid)
            if raw:
                yield raw, internal

    def _search_range(self, start: date, end: date) -> list[str]:
        assert self.conn is not None
        # 严格 SINCE start BEFORE end+1 区间。ON 不可靠（个别邮箱空范围会返回整箱）。
        query = f"(SINCE {imap_date(start)} BEFORE {imap_date(end + timedelta(days=1))})"
        try:
            typ, data = self.conn.uid("SEARCH", None, query)
        except Exception as exc:
            LOG.debug("SEARCH %s 失败: %s", query, exc)
            return []
        if typ != "OK":
            return []
        token = data[0] if data else b""
        if not token:
            return []
        return token.decode("ascii", errors="ignore").split()

    def _filter_in_range(self, uids: list[str], start: date, end: date, tz=None) -> list[str]:
        """服务端 SEARCH 不可靠，用 INTERNALDATE 二次校验，只留落在 [start, end] 的。"""
        if not uids:
            return []
        dated = self._fetch_internaldates(uids)
        kept: list[str] = []
        for uid in uids:
            internal = dated.get(uid)
            if internal is None:
                continue
            received_day = to_tz(internal, tz).date() if tz is not None else internal.date()
            if start <= received_day <= end:
                kept.append(uid)
            else:
                LOG.debug("UID %s INTERNALDATE 为 %s，不在 %s~%s，跳过", uid, received_day, start, end)
        return kept

    def _fetch_internaldates(self, uids: list[str]) -> dict[str, datetime]:
        """批量取 INTERNALDATE，返回 {uid: internal_dt}。"""
        assert self.conn is not None
        result: dict[str, datetime] = {}
        if not uids:
            return result
        for i in range(0, len(uids), 500):
            batch = uids[i:i + 500]
            seqset = ",".join(batch)
            try:
                typ, data = self.conn.uid("FETCH", seqset, "(INTERNALDATE)")
            except Exception as exc:
                LOG.warning("批量取 INTERNALDATE 失败(%s): %s", seqset, exc)
                continue
            if typ != "OK" or not data:
                continue
            for item in data:
                if isinstance(item, tuple):
                    header = item[0] if item else b""
                elif isinstance(item, (bytes, bytearray)):
                    header = bytes(item)
                else:
                    continue
                if not isinstance(header, (bytes, bytearray)):
                    continue
                uid_m = _UID_RE.search(header)
                dt = _parse_internaldate(header)
                if uid_m and dt is not None:
                    result[uid_m.group(1).decode("ascii", errors="ignore")] = dt
        return result

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
