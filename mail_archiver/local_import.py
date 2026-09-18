from __future__ import annotations

import logging
import mailbox
from pathlib import Path
from typing import Iterator

LOG = logging.getLogger("mail_archiver")

MAIL_SUFFIXES = {".eml", ".emlx"}


def iter_local_messages(path: Path) -> Iterator[bytes]:
    if path.is_file():
        yield from _from_file(path)
        return
    if not path.is_dir():
        raise FileNotFoundError(f"找不到导入路径: {path}")
    files = sorted(p for p in path.rglob("*") if p.is_file())
    if not files:
        LOG.warning("目录中没有文件: %s", path)
        return
    for file in files:
        suffix = file.suffix.lower()
        if suffix in MAIL_SUFFIXES or suffix == ".mbox" or file.name.lower() == "mbox":
            yield from _from_file(file)
        elif suffix in {".txt", ".msg"}:
            LOG.debug("跳过不支持的文件: %s", file)
        else:
            # 无后缀文件可能是 mbox；仅当看起来像邮件时再读
            if suffix == "" and _looks_like_mbox(file):
                yield from _from_file(file)


def _from_file(path: Path) -> Iterator[bytes]:
    suffix = path.suffix.lower()
    if suffix in MAIL_SUFFIXES:
        data = path.read_bytes()
        if data.strip():
            yield data
        return
    try:
        box = mailbox.mbox(path)
        count = 0
        for message in box:
            raw = message.as_bytes()
            if raw.strip():
                count += 1
                yield raw
        if count:
            LOG.info("从 mbox 导入 %s 封: %s", count, path)
    except Exception as exc:
        LOG.warning("无法解析 %s: %s", path, exc)


def _looks_like_mbox(path: Path) -> bool:
    try:
        head = path.read_bytes()[:8]
    except Exception:
        return False
    return head.startswith(b"From ")
