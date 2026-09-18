from __future__ import annotations

import logging
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from mail_archiver.config import ExtractConfig
from mail_archiver.util import sanitize_filename

LOG = logging.getLogger("mail_archiver")


def archive_kind(filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".tar")):
        return "tar"
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith((".rar", ".7z")):
        return "ext"
    return None


def extract_stem(filename: str) -> str:
    lower = filename.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2"):
        if lower.endswith(ext):
            return filename[: -len(ext)]
    return Path(filename).stem


def _safe_join(dest: Path, member: str) -> Path | None:
    dest = dest.resolve()
    cleaned = member.replace("\\", "/").lstrip("/")
    parts: list[str] = []
    for part in Path(cleaned).parts:
        if part in ("", ".", ".."):
            if part == "..":
                return None
            continue
        parts.append(sanitize_filename(part, max_len=120))
    if not parts:
        return None
    target = dest.joinpath(*parts)
    try:
        target.resolve().relative_to(dest)
    except ValueError:
        return None
    return target


def extract_archive(src: Path, dest: Path, cfg: ExtractConfig) -> tuple[bool, str]:
    kind = archive_kind(src.name)
    if not kind:
        return False, "not-archive"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "zip":
            return _extract_zip(src, dest, cfg)
        if kind == "tar":
            return _extract_tar(src, dest, cfg)
        return _extract_external(src, dest)
    except Exception as exc:
        LOG.warning("解压失败 %s: %s", src.name, exc)
        return False, str(exc)


def _extract_zip(src: Path, dest: Path, cfg: ExtractConfig) -> tuple[bool, str]:
    files = 0
    total = 0
    skipped = 0
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if files >= cfg.max_files:
                LOG.warning("压缩包文件数超过上限 %s，停止继续解压: %s", cfg.max_files, src.name)
                break
            if info.flag_bits & 0x1:
                skipped += 1
                LOG.warning("跳过加密条目（仅保留原压缩包）: %s -> %s", src.name, info.filename)
                continue
            target = _safe_join(dest, info.filename)
            if target is None:
                skipped += 1
                LOG.warning("已拦截危险路径: %s -> %s", src.name, info.filename)
                continue
            size = info.file_size
            if total + size > cfg.max_bytes:
                LOG.warning("压缩包解压体积超过上限，停止继续解压: %s", src.name)
                break
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src_fh, target.open("wb") as out_fh:
                    shutil.copyfileobj(src_fh, out_fh)
            except RuntimeError as exc:
                skipped += 1
                LOG.warning("解压条目失败 %s -> %s: %s", src.name, info.filename, exc)
                if target.exists() and target.stat().st_size == 0:
                    target.unlink()
                continue
            files += 1
            total += size
    return files > 0, f"files={files}, skipped={skipped}"


def _extract_tar(src: Path, dest: Path, cfg: ExtractConfig) -> tuple[bool, str]:
    files = 0
    total = 0
    skipped = 0
    with tarfile.open(src) as tf:
        for info in tf:
            if not info.isfile():
                continue
            if files >= cfg.max_files:
                break
            target = _safe_join(dest, info.name)
            if target is None:
                skipped += 1
                LOG.warning("已拦截危险路径: %s -> %s", src.name, info.name)
                continue
            if total + max(info.size, 0) > cfg.max_bytes:
                break
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(info)
            if extracted is None:
                skipped += 1
                continue
            with extracted, target.open("wb") as out_fh:
                shutil.copyfileobj(extracted, out_fh)
            files += 1
            total += max(info.size, 0)
    return files > 0, f"files={files}, skipped={skipped}"


def _extract_external(src: Path, dest: Path) -> tuple[bool, str]:
    dest.mkdir(parents=True, exist_ok=True)
    commands: list[list[str]] = []
    if shutil.which("7z"):
        commands.append(["7z", "x", "-y", f"-o{dest}", str(src)])
    elif shutil.which("7za"):
        commands.append(["7za", "x", "-y", f"-o{dest}", str(src)])
    if src.suffix.lower() == ".rar" and shutil.which("unrar"):
        commands.append(["unrar", "x", "-y", str(src), str(dest) + "\\"])
    if not commands:
        return False, "no-extractor"
    last_err = "no-extractor"
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
            if proc.returncode == 0:
                return True, "external"
            last_err = (proc.stderr or proc.stdout).decode("utf-8", errors="replace")[:200]
        except Exception as exc:
            last_err = str(exc)
    LOG.warning("外部解压失败 %s: %s", src.name, last_err)
    return False, last_err
