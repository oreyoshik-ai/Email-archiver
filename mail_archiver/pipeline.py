from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import date
from pathlib import Path

from mail_archiver.archiver import ArchiveResult, MailArchiver
from mail_archiver.config import AppConfig
from mail_archiver.imap_client import ImapSource
from mail_archiver.local_import import iter_local_messages
from mail_archiver.selftest import build_sample_dir
from mail_archiver.util import resolve_timezone, to_tz

LOG = logging.getLogger("mail_archiver")


def consume(archiver: MailArchiver, result: ArchiveResult, raw: bytes, received=None) -> None:
    try:
        status = archiver.archive_raw(raw, received=received)
        if status == "skipped":
            result.skipped += 1
        else:
            result.saved += 1
    except Exception:
        result.failed += 1
        LOG.exception("归档一封邮件时出错")


def _make_archiver(cfg: AppConfig) -> MailArchiver:
    archiver = MailArchiver(cfg)
    archiver.setup()
    return archiver


def run_imap(cfg: AppConfig, start: date, end: date, progress=None) -> ArchiveResult:
    tz = resolve_timezone(cfg.timezone)
    archiver = _make_archiver(cfg)
    result = ArchiveResult()
    if start == end:
        LOG.info("开始收取 %s 的收件箱（%s）", start.isoformat(), cfg.imap.username)
    else:
        LOG.info("开始收取 %s~%s 的收件箱（%s）", start.isoformat(), end.isoformat(), cfg.imap.username)
    found = False
    with ImapSource(cfg.imap) as client:
        for raw, internal in client.fetch_in_range(start, end, tz):
            found = True
            received = to_tz(internal, tz) if internal else None
            consume(archiver, result, raw, received=received)
            if progress:
                progress(result.saved, result.skipped, result.failed)
    if not found:
        if start == end:
            result.note = f"{start.isoformat()} 当日收件箱没有邮件，已停止，未导出任何内容。"
        else:
            result.note = f"{start.isoformat()}~{end.isoformat()} 这段时间收件箱没有邮件，已停止，未导出任何内容。"
    return result


def run_local_paths(cfg: AppConfig, paths: list[Path], progress=None) -> ArchiveResult:
    """逐封归档本地文件；progress(saved, skipped, failed) 每封回调一次，供任务状态实时计数。"""
    archiver = _make_archiver(cfg)
    result = ArchiveResult()
    for path in paths:
        path = path.expanduser().resolve()
        LOG.info("导入: %s", path)
        for raw in iter_local_messages(path):
            consume(archiver, result, raw)
            if progress:
                progress(result.saved, result.skipped, result.failed)
    return result


def run_demo(cfg: AppConfig) -> ArchiveResult:
    tmp = Path(tempfile.mkdtemp(prefix="mail-demo-"))
    try:
        sample = build_sample_dir(tmp)
        LOG.info("使用内置示例邮件演示归档（不连接邮箱）")
        return run_local_paths(cfg, [sample])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def summarize(result: ArchiveResult) -> str:
    if getattr(result, "note", ""):
        return result.note
    if result.failed:
        return f"完成：新增 {result.saved} 封，跳过 {result.skipped} 封，失败 {result.failed} 封"
    if result.saved == 0 and result.skipped:
        return f"整理完成：这 {result.skipped} 封以前已经存过了，没有重复保存"
    if result.saved and result.skipped:
        return f"整理完成：新存 {result.saved} 封，另外 {result.skipped} 封以前存过"
    if result.saved:
        return f"整理完成：新存 {result.saved} 封"
    return "没有找到可整理的邮件"