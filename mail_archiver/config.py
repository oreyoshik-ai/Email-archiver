from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
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
    auth_codes: dict = field(default_factory=dict)
    config_path: Path | None = None


# 邮箱后缀 → (服务商名称, IMAP 主机)。新增邮箱在此追加即可。
DOMAIN_INFO: dict[str, tuple[str, str]] = {
    # 网易
    "163.com": ("网易 163 邮箱", "imap.163.com"),
    "126.com": ("网易 126 邮箱", "imap.126.com"),
    "yeah.net": ("网易 yeah 邮箱", "imap.yeah.net"),
    # 腾讯
    "qq.com": ("QQ 邮箱", "imap.qq.com"),
    "foxmail.com": ("Foxmail 邮箱", "imap.qq.com"),
    "vip.qq.com": ("QQ VIP 邮箱", "imap.qq.com"),
    "exmail.qq.com": ("腾讯企业邮箱", "imap.exmail.qq.com"),
    # 谷歌
    "gmail.com": ("Gmail", "imap.gmail.com"),
    "googlemail.com": ("Gmail", "imap.gmail.com"),
    # 微软
    "outlook.com": ("Outlook", "outlook.office365.com"),
    "hotmail.com": ("Hotmail", "outlook.office365.com"),
    "live.com": ("Live", "outlook.office365.com"),
    "msn.com": ("MSN", "outlook.office365.com"),
    # 苹果
    "icloud.com": ("iCloud 邮箱", "imap.mail.me.com"),
    "me.com": ("iCloud 邮箱", "imap.mail.me.com"),
    "mac.com": ("iCloud 邮箱", "imap.mail.me.com"),
    # 新浪
    "sina.com": ("新浪邮箱", "imap.sina.com"),
    "sina.cn": ("新浪邮箱", "imap.sina.com"),
    # 阿里
    "aliyun.com": ("阿里云邮箱", "imap.aliyun.com"),
    # 中国移动 / 电信
    "139.com": ("139 邮箱", "imap.139.com"),
    "189.cn": ("189 邮箱", "imap.189.cn"),
    # 其它
    "sohu.com": ("搜狐邮箱", "imap.sohu.com"),
    "tom.com": ("Tom 邮箱", "imap.tom.com"),
    "yahoo.com": ("Yahoo 邮箱", "imap.mail.yahoo.com"),
}

# 兼容旧调用：域名 → IMAP 主机
HOST_BY_DOMAIN = {domain: host for domain, (_name, host) in DOMAIN_INFO.items()}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_archive_root() -> Path:
    home = Path.home()
    for name in ("Desktop", "桌面"):
        desktop = home / name
        if desktop.is_dir():
            return desktop / "邮件归档"
    return home / "Desktop" / "邮件归档"


def infer_imap_info(username: str, fallback: str = "imap.163.com") -> dict:
    """根据邮箱地址推断 IMAP 服务商与主机。

    返回 ``{"provider", "host", "known", "domain"}``。``known=False`` 表示
    该域名不在内置表里，``host`` 取 ``fallback``，需用户手动填写 IMAP 服务器。
    """
    name = (username or "").strip()
    if "@" not in name:
        return {"provider": "", "host": fallback, "known": False, "domain": ""}
    domain = name.rsplit("@", 1)[-1].strip().lower()
    info = DOMAIN_INFO.get(domain)
    if info:
        provider, host = info
        return {"provider": provider, "host": host, "known": True, "domain": domain}
    return {"provider": "", "host": fallback, "known": False, "domain": domain}


def infer_imap_host(username: str, fallback: str = "imap.163.com") -> str:
    """仅返回 IMAP 主机，保留给旧调用方用。"""
    return infer_imap_info(username, fallback)["host"]


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
    auth_raw = data.get("auth_codes") or {}
    auth_codes = {
        str(k): str(v) for k, v in auth_raw.items()
    } if isinstance(auth_raw, dict) else {}
    return AppConfig(
        archive_root=archive_root,
        timezone=(data.get("timezone") or "Asia/Shanghai").strip(),
        imap=imap,
        extract=extract,
        auth_codes=auth_codes,
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
    info = infer_imap_info(cfg.imap.username)
    return {
        "archive_root": str(cfg.archive_root),
        "timezone": cfg.timezone,
        "username": "" if not cfg.imap.username or cfg.imap.username.startswith("yourname") else cfg.imap.username,
        "auth_code_set": imap_credentials_ready(cfg),
        "host": cfg.imap.host,
        "auto_host": info["host"],
        "provider": info["provider"],
        "folder": cfg.imap.folder,
        "ready": imap_credentials_ready(cfg),
        "config_path": str(cfg.config_path) if cfg.config_path else "",
    }


def get_saved_auth_code(username: str, dest: Path | None = None) -> str:
    """返回该邮箱已保存的授权码；没有则空串。兼容旧配置（仅 imap.auth_code）。"""
    username = (username or "").strip()
    if not username:
        return ""
    cfg = load_config(str(dest) if dest and dest.is_file() else None)
    code = (cfg.auth_codes or {}).get(username)
    if code:
        return code
    # 旧配置迁移：只有 imap.auth_code，若邮箱一致则视作已保存
    if (cfg.imap.username or "") == username and cfg.imap.auth_code:
        return cfg.imap.auth_code
    return ""


def save_config(
    *,
    username: str,
    auth_code: str | None,
    archive_root: str,
    timezone: str = "Asia/Shanghai",
    folder: str = "INBOX",
    host: str | None = None,
    dest: Path | None = None,
) -> AppConfig:
    """保存配置。

    ``host`` 留空或为 ``"auto"`` 时按邮箱地址自动推断 IMAP 主机。
    授权码按邮箱存入 ``auth_codes`` 字典：用户重新输入则更新，未输入则带入该邮箱已存码，
    下次输入同一邮箱时前端可自动填入。切换邮箱不会把别的邮箱的授权码带过去。
    """
    dest = dest or (project_root() / LOCAL_NAME)
    current = load_config(str(dest) if dest.is_file() else None)
    username = (username or "").strip()
    archive_path = Path(archive_root).expanduser() if archive_root else default_archive_root()
    if not archive_path.is_absolute():
        archive_path = (dest.parent / archive_path).resolve()
    else:
        archive_path = archive_path.resolve()

    saved_map = dict(current.auth_codes or {})
    new_auth = (auth_code or "").strip()
    if not new_auth or new_auth == "********":
        # 用户未重新输入：优先取该邮箱已存授权码；其次仅当邮箱未变时沿用当前 imap.auth_code
        if username and username in saved_map:
            new_auth = saved_map[username]
        elif current.config_path and (current.imap.username or "") == username and username:
            new_auth = current.imap.auth_code
        else:
            new_auth = ""
    if username and new_auth:
        saved_map[username] = new_auth

    resolved_host = (host or "").strip()
    if not resolved_host or resolved_host.lower() == "auto":
        resolved_host = infer_imap_host(username, current.imap.host or "imap.163.com")

    payload = {
        "archive_root": str(archive_path),
        "timezone": timezone or current.timezone or "Asia/Shanghai",
        "imap": {
            "enabled": True,
            "host": resolved_host,
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
        "auth_codes": saved_map,
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
