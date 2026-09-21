from __future__ import annotations

import logging
import mailbox
import tempfile
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


def iter_message_bytes(name: str, data: bytes) -> Iterator[bytes]:
    """把一份上传内容（文件名 + 原始字节）解析成邮件字节流，普通邮件不落盘。

    网页批量上传几百封 .eml 时逐批直接归档内存字节，不再写 C 盘临时中转
    文件；只有 mbox 容器（标准库只支持按路径解析）才落一个临时文件。
    """
    if not data or not data.strip():
        return
    suffix = Path(name).suffix.lower()
    if suffix in MAIL_SUFFIXES:
        yield data
        return
    if suffix in {".txt", ".msg"}:
        LOG.debug("跳过不支持的文件: %s", name)
        return
    if suffix == ".mbox" or name.lower() == "mbox" or (suffix == "" and data[:5] == b"From "):
        with tempfile.NamedTemporaryFile(suffix=".mbox", delete=False) as fh:
            fh.write(data)
            tmp = Path(fh.name)
        try:
            yield from _from_file(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        return
    # 其余后缀按普通邮件交给解析器自辨，解析失败按失败计数
    yield data


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
