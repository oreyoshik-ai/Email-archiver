from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("mail_archiver")

WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def configure_stdio() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def setup_logging(verbose: bool = False) -> None:
    configure_stdio()
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


_TZ_CACHE: dict = {}


def resolve_timezone(name: str):
    name = (name or "Asia/Shanghai").strip()
    if name in _TZ_CACHE:
        return _TZ_CACHE[name]
    try:
        tz = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        LOG.warning("本机缺少时区数据 %s，回退到 UTC+8（中国无夏令时，与上海时区等价）", name)
        tz = timezone(timedelta(hours=8), name="UTC+8")
    except Exception:
        tz = timezone(timedelta(hours=8), name="UTC+8")
    _TZ_CACHE[name] = tz
    return tz


def to_tz(dt: datetime, tz) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def imap_date(day) -> str:
    """IMAP 日期必须用英文月份，不能用系统 locale。"""
    return f"{day.day:02d}-{IMAP_MONTHS[day.month - 1]}-{day.year}"


def sanitize_filename(name: str, max_len: int = 80) -> str:
    name = (name or "").strip() or "untitled"
    name = name.replace("\x00", "")
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = "".join(ch if ord(ch) >= 32 else "_" for ch in name)
    name = name.rstrip(" .")
    if not name:
        name = "untitled"
    stem = name
    suffix = ""
    if "." in name and not name.startswith("."):
        stem, suffix = name.rsplit(".", 1)
        suffix = "." + suffix
    if stem.upper() in WINDOWS_RESERVED:
        stem = f"_{stem}"
    out = (stem + suffix)[:max_len].rstrip(" .")
    return out or "untitled"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    for i in range(2, 1000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为 {path} 生成不冲突的文件名")


def unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.parent / f"{path.name}_{i}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为 {path} 生成不冲突的目录名")
