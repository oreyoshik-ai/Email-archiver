from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


LOCAL_NAME = "config.local.json"


@dataclass
class ImapConfig:
    enabled: bool = True
    host: str = "imap.163.com"
    port: int = 993
    ssl: bool = True
    username: str = ""
    auth_code: str = ""
    folder: str = "INBOX"


@dataclass
class ExtractConfig:
    enabled: bool = True
    max_files: int = 500
    max_bytes: int = 524_288_000


@dataclass
class AppConfig:
    archive_root: Path
    timezone: str
    imap: ImapConfig
    extract: ExtractConfig
    config_path: Path | None = None


HOST_BY_DOMAIN = {
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
    "yeah.net": "imap.yeah.net",
    "qq.com": "imap.qq.com",
    "foxmail.com": "imap.qq.com",
    "gmail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_archive_root() -> Path:
    home = Path.home()
    for name in ("Desktop", "桌面"):
        desktop = home / name
        if desktop.is_dir():
            return desktop / "邮件归档"
    return home / "Desktop" / "邮件归档"


def infer_imap_host(username: str, fallback: str = "imap.163.com") -> str:
    if "@" not in (username or ""):
        return fallback
    domain = username.rsplit("@", 1)[-1].strip().lower()
    return HOST_BY_DOMAIN.get(domain, fallback)


def default_config_candidates(explicit: str | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    cwd = Path.cwd()
    root = project_root()
    return [
        cwd / LOCAL_NAME,
        cwd / "config.json",
        root / LOCAL_NAME,
        root / "config.json",
    ]


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(explicit: str | None = None) -> AppConfig:
    data: dict = {}
    used: Path | None = None
    for path in default_config_candidates(explicit):
        if path.is_file():
            used = path
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            break

    imap_raw = data.get("imap") or {}
    extract_raw = data.get("extract") or {}
    archive_root = Path(
        os.environ.get("COMPLAINT_ARCHIVE_ROOT")
        or data.get("archive_root")
        or str(default_archive_root())
    ).expanduser()
    if not archive_root.is_absolute():
        base = used.parent if used else Path.cwd()
        archive_root = (base / archive_root).resolve()

    imap = ImapConfig(
        enabled=_as_bool(imap_raw.get("enabled"), True),
        host=os.environ.get("COMPLAINT_IMAP_HOST") or imap_raw.get("host") or "imap.163.com",
        port=int(os.environ.get("COMPLAINT_IMAP_PORT") or imap_raw.get("port") or 993),
        ssl=_as_bool(imap_raw.get("ssl"), True),
        username=(os.environ.get("COMPLAINT_IMAP_USER") or imap_raw.get("username") or "").strip(),
        auth_code=(os.environ.get("COMPLAINT_IMAP_AUTH") or imap_raw.get("auth_code") or "").strip(),
        folder=(os.environ.get("COMPLAINT_IMAP_FOLDER") or imap_raw.get("folder") or "INBOX").strip()
        or "INBOX",
    )
    extract = ExtractConfig(
        enabled=_as_bool(extract_raw.get("enabled"), True),
        max_files=int(extract_raw.get("max_files") or 500),
        max_bytes=int(extract_raw.get("max_bytes") or 524_288_000),
    )
    return AppConfig(
        archive_root=archive_root,
        timezone=(data.get("timezone") or "Asia/Shanghai").strip(),
        imap=imap,
        extract=extract,
        config_path=used,
    )


def imap_credentials_ready(cfg: AppConfig) -> bool:
    if not cfg.imap.enabled:
        return False
    user = cfg.imap.username
    auth = cfg.imap.auth_code
    if not user or user.startswith("yourname"):
        return False
    if not auth or "授权码" in auth or "不是登录" in auth:
        return False
    return True


def config_public_dict(cfg: AppConfig) -> dict:
    return {
        "archive_root": str(cfg.archive_root),
        "timezone": cfg.timezone,
        "username": "" if not cfg.imap.username or cfg.imap.username.startswith("yourname") else cfg.imap.username,
        "auth_code_set": imap_credentials_ready(cfg),
        "host": cfg.imap.host,
        "folder": cfg.imap.folder,
        "ready": imap_credentials_ready(cfg),
        "config_path": str(cfg.config_path) if cfg.config_path else "",
    }


def save_config(
    *,
    username: str,
    auth_code: str | None,
    archive_root: str,
    timezone: str = "Asia/Shanghai",
    folder: str = "INBOX",
    dest: Path | None = None,
) -> AppConfig:
    dest = dest or (project_root() / LOCAL_NAME)
    current = load_config(str(dest) if dest.is_file() else None)
    username = (username or "").strip()
    archive_path = Path(archive_root).expanduser() if archive_root else default_archive_root()
    if not archive_path.is_absolute():
        archive_path = (dest.parent / archive_path).resolve()
    else:
        archive_path = archive_path.resolve()

    new_auth = (auth_code or "").strip()
    if not new_auth or new_auth == "********":
        new_auth = current.imap.auth_code if current.config_path else ""

    payload = {
        "archive_root": str(archive_path),
        "timezone": timezone or current.timezone or "Asia/Shanghai",
        "imap": {
            "enabled": True,
            "host": infer_imap_host(username, current.imap.host or "imap.163.com"),
            "port": 993,
            "ssl": True,
            "username": username,
            "auth_code": new_auth,
            "folder": (folder or "INBOX").strip() or "INBOX",
        },
        "extract": {
            "enabled": True,
            "max_files": current.extract.max_files,
            "max_bytes": current.extract.max_bytes,
        },
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dest)
    return load_config(str(dest))


def init_local_config(dest: Path | None = None) -> Path:
    dest = dest or (project_root() / LOCAL_NAME)
    if dest.exists():
        return dest
    payload = {
        "archive_root": str(default_archive_root()),
        "timezone": "Asia/Shanghai",
        "imap": {
            "enabled": True,
            "host": "imap.163.com",
            "port": 993,
            "ssl": True,
            "username": "",
            "auth_code": "",
            "folder": "INBOX",
        },
        "extract": {"enabled": True, "max_files": 500, "max_bytes": 524288000},
    }
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
