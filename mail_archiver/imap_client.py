from __future__ import annotations

import imaplib
import logging
import re
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

from mail_archiver.config import ImapConfig
from mail_archiver.util import IMAP_MONTHS, to_tz

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
# Date 信头行（BODY.PEEK[HEADER.FIELDS (DATE)] 的字面量，或拼入响应 blob 后按行匹配）
_DATE_HEADER_RE = re.compile(rb"^date:\s*(.+)$", re.I | re.M)


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


def _parse_date_header(literal: bytes) -> datetime | None:
    """解析 Date 信头（网页/客户端列表显示的发信时间），缺失或解析失败返回 None。

    naive 值（信头无时区）原样返回，由调用方按配置时区解释——与 INTERNALDATE
    裸值同一约定。
    """
    match = _DATE_HEADER_RE.search(literal or b"")
    if not match:
        return None
    try:
        return parsedate_to_datetime(match.group(1).decode("ascii", errors="replace").strip())
    except (ValueError, TypeError, IndexError):
        return None


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

        不用服务端日期 SEARCH 当初筛：各服务商 SEARCH 实现参差（有整箱截断、
        空范围返回整箱等行为），结果不可全信。改为按序号 1..EXISTS 全量枚举
        (UID INTERNALDATE)——序号连续，不存在被服务器漏发——再在本地按
        INTERNALDATE 筛出区间。
        """
        if self.conn is None:
            raise ImapError("IMAP 未连接")
        typ, data = self.conn.select(self.cfg.folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"无法打开文件夹 {self.cfg.folder}")
        total = self._exists_count(data)

        uids = self._mails_in_range(start, end, tz, total)
        LOG.info("%s~%s 在 %s 中筛出 %s 封（全箱共 %s 封）", start.isoformat(), end.isoformat(), self.cfg.folder, len(uids), total)
        for uid, eff in uids:
            raw, _internal = self._fetch_uid(uid)
            if raw:
                # 交付 _mails_in_range 算好的归档日期（信头优先），不用 _fetch_uid 的
                # INTERNALDATE——归档目录日期与筛选口径必须一致。
                yield raw, eff

    @staticmethod
    def _exists_count(data) -> int:
        """从 SELECT 响应（* n EXISTS）解析邮箱总数。"""
        for item in data or []:
            if isinstance(item, (bytes, bytearray)):
                try:
                    return int(bytes(item).split()[-1])
                except (ValueError, IndexError):
                    continue
        return 0

    def _effective_date(self, header_dt: datetime | None, internal_dt: datetime | None, tz) -> datetime | None:
        """邮件的归档日期：优先 Date 信头（客户端列表显示的发信时间），不可信时回退入箱时间。

        搬家/迁移进来的邮件，信头是原始发信时间（如 6 月）、INTERNALDATE 是入箱
        时间（如 8 月下旬），用户在客户端里看到的是前者——按入箱时间归目录会把
        它们全放进 8 月。信头缺失或明显离谱（解析失败、早于 1990 年、晚于明天，
        垃圾邮件常伪造）时才用 INTERNALDATE。
        """
        if header_dt is not None:
            try:
                utc = to_tz(header_dt, timezone.utc)
                if datetime(1990, 1, 1, tzinfo=timezone.utc) <= utc <= datetime.now(timezone.utc) + timedelta(days=1):
                    return to_tz(header_dt, tz)
            except (ValueError, OverflowError, OSError):
                pass
        if internal_dt is not None:
            return to_tz(internal_dt, tz)
        return None

    def _mails_in_range(self, start: date, end: date, tz, total: int) -> list[tuple[str, datetime | None]]:
        """按序号全量枚举 UID+入箱时间+Date 信头，本地筛出归档日期落在 [start, end] 的邮件。

        序号 1..EXISTS 连续，逐批 FETCH 只拉元数据（每批 500 封，几千封也就几个来回），
        相比服务端 SEARCH 不会有任何一封被服务器静默漏掉。返回 [(uid, 归档日期)]。
        """
        kept: list[tuple[str, datetime | None]] = []
        batch = 500
        for i in range(1, total + 1, batch):
            seqset = f"{i}:{min(i + batch - 1, total)}"
            try:
                typ, data = self.conn.fetch(seqset, "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE)])")
            except Exception as exc:
                LOG.warning("批量取 %s 元数据失败: %s", seqset, exc)
                continue
            if typ != "OK" or not data:
                # 个别服务器不支持按字段取信头：退回只取 UID+入箱时间（信头回退逻辑照常生效）
                try:
                    typ, data = self.conn.fetch(seqset, "(UID INTERNALDATE)")
                except Exception as exc:
                    LOG.warning("批量取 %s 元数据失败: %s", seqset, exc)
                    continue
            if typ != "OK" or not data:
                LOG.warning("批次 %s 返回异常（typ=%s），这一批邮件可能漏筛", seqset, typ)
                continue
            for item in data:
                if isinstance(item, tuple):
                    # 带字面量的真实响应：各段拼起来再匹配，兼容服务器把字段放在字面量前后的差异
                    blob = b"\n".join(bytes(part) for part in item if isinstance(part, (bytes, bytearray)))
                elif isinstance(item, (bytes, bytearray)):
                    blob = bytes(item)
                else:
                    continue
                uid_m = _UID_RE.search(blob)
                if not uid_m:
                    continue
                internal = _parse_internaldate(blob)
                eff = self._effective_date(_parse_date_header(blob), internal, tz)
                if eff is None:
                    continue
                if start <= eff.date() <= end:
                    kept.append((uid_m.group(1).decode("ascii", errors="ignore"), eff))
        return kept

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
