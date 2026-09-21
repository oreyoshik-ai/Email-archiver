from __future__ import annotations

import imaplib
import logging
import re
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

from mail_archiver.config import ImapConfig
from mail_archiver.util import IMAP_MONTHS, imap_date, to_tz

LOG = logging.getLogger("mail_archiver")

_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
# 标准 IMAP INTERNALDATE："DD-Mon-YYYY HH:MM:SS +ZZZZ"，时间与偏移部分可缺省。
_INTERNALDATE_FMT = re.compile(
    r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})"
    r"(?:\s+(\d{1,2}):(\d{2}):(\d{2}))?"
    r"(?:\s+([+-]\d{4}))?$"
)
_INTERNALDATE_MONTHS = {name.lower(): idx for idx, name in enumerate(IMAP_MONTHS, start=1)}
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
    """解析 FETCH 响应中的 INTERNALDATE。

    格式 "DD-Mon-YYYY HH:MM:SS +ZZZZ"。带偏移时返回带时区时间；不带偏移时按
    RFC 3501 是服务器本地时间（中文邮箱即北京时间），保持 naive 返回，由调用方
    用 to_tz 按配置时区解释——不能当 UTC 处理，否则 16:00 之后收到的邮件会被
    错算到第二天。
    """
    match = _INTERNALDATE_RE.search(header)
    if not match:
        return None
    fmt = _INTERNALDATE_FMT.match(match.group(1).decode("ascii", errors="replace").strip())
    if not fmt:
        return None
    day, mon, year, hh, mi, ss, offset = fmt.groups()
    month = _INTERNALDATE_MONTHS.get(mon.lower())
    if month is None:
        return None
    try:
        dt = datetime(int(year), month, int(day))
        if hh is not None:
            dt = dt.replace(hour=int(hh), minute=int(mi), second=int(ss))
    except ValueError:
        return None
    if offset:
        sign = 1 if offset[0] == "+" else -1
        dt = dt.replace(
            tzinfo=timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
        )
    return dt


class ImapSource:
    def __init__(self, cfg: ImapConfig):
        self.cfg = cfg
        self.conn: imaplib.IMAP4 | None = None

    def connect(self) -> None:
        _ensure_id_command()
        LOG.info("连接 IMAP %s:%s ...", self.cfg.host, self.cfg.port)
        if sys.version_info < (3, 9):
            # Python 3.8 的 imaplib 没有 timeout 参数（3.9 才加入，Win7 只能装 3.8），
            # 退而求其次用全局 socket 默认超时兜底。
            socket.setdefaulttimeout(30)
            kwargs: dict = {}
        else:
            kwargs = {"timeout": 30}
        try:
            if self.cfg.ssl:
                self.conn = imaplib.IMAP4_SSL(self.cfg.host, self.cfg.port, **kwargs)
            else:
                self.conn = imaplib.IMAP4(self.cfg.host, self.cfg.port, **kwargs)
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
