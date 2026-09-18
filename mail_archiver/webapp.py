from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mail_archiver.config import (
    AppConfig,
    config_public_dict,
    infer_imap_info,
    imap_credentials_ready,
    load_config,
    save_config,
)
from mail_archiver.imap_client import ImapError
from mail_archiver.jobs import RUNNER
from mail_archiver.pipeline import run_demo, run_imap, run_local_paths
from mail_archiver.results import list_day, list_days
from mail_archiver.util import resolve_timezone, sanitize_filename, unique_path

LOG = logging.getLogger("mail_archiver")
WEB_DIR = Path(__file__).resolve().parent / "web"

_PICKER_SCRIPT = """
import os
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
initial = os.environ.get("COMPLAINT_PICK_DIR", "")
path = filedialog.askdirectory(initialdir=initial or None, title="选择保存文件夹")
print(path, end="")
root.destroy()
"""


def pick_folder(initial: str = "") -> str:
    env = os.environ.copy()
    start = Path(initial).expanduser() if initial else None
    if start is not None:
        if start.is_file():
            start = start.parent
        if start.exists():
            env["COMPLAINT_PICK_DIR"] = str(start)
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "env": env,
        "timeout": 600,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.run([sys.executable, "-c", _PICKER_SCRIPT], **kwargs)
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        err = (proc.stderr or "").strip() or "无法打开文件夹窗口"
        raise RuntimeError(err)
    return (proc.stdout or "").strip()



def _today(cfg: AppConfig) -> str:
    return datetime.now(resolve_timezone(cfg.timezone)).date().isoformat()


def _json_bytes(payload: dict, code: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return code, body, "application/json; charset=utf-8"


def _parse_multipart(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type, re.I)
    if not match:
        return []
    boundary = (match.group(1) or match.group(2)).encode("ascii", errors="ignore")
    files: list[tuple[str, bytes]] = []
    for part in body.split(b"--" + boundary):
        if not part or part in (b"--", b"--\r\n") or part.startswith(b"--"):
            continue
        header, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        header_text = header.decode("utf-8", errors="replace")
        name = re.search(r'filename="([^"]+)"', header_text)
        if not name:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        files.append((name.group(1), content))
    return files


def _open_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _safe_under(archive_root: Path, target: Path) -> Path:
    root = archive_root.resolve()
    resolved = target.expanduser().resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("只能打开归档目录里的文件夹")
    return resolved


def _state_payload() -> dict:
    cfg = load_config()
    day = _today(cfg)
    job = RUNNER.state.snapshot()
    return {
        "config": config_public_dict(cfg),
        "today": day,
        "job": job,
        "days": list_days(cfg.archive_root),
        "results": list_day(cfg.archive_root, day),
    }


class AppHandler(BaseHTTPRequestHandler):
    server_version = "mail-archiver"

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("http " + fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            html = (WEB_DIR / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            qs = parse_qs(parsed.query)
            cfg = load_config()
            day = (qs.get("date") or [_today(cfg)])[0]
            payload = _state_payload()
            payload["results"] = list_day(cfg.archive_root, day)
            payload["view_date"] = day
            self._send(*_json_bytes(payload))
            return
        if path == "/api/infer-imap":
            qs = parse_qs(parsed.query)
            username = (qs.get("username") or [""])[0]
            self._send(*_json_bytes(infer_imap_info(username)))
            return
        if path == "/api/job":
            self._send(*_json_bytes(RUNNER.state.snapshot()))
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/config":
                self._handle_save_config()
                return
            if path == "/api/run":
                self._handle_run()
                return
            if path == "/api/import-files":
                self._handle_import_files()
                return
            if path == "/api/open":
                self._handle_open()
                return
            if path == "/api/pick-folder":
                self._handle_pick_folder()
                return
        except RuntimeError as exc:
            self._send(*_json_bytes({"ok": False, "error": str(exc)}, 409))
            return
        except ValueError as exc:
            self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
            return
        except ImapError as exc:
            self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
            return
        except Exception as exc:
            LOG.exception("接口失败")
            self._send(*_json_bytes({"ok": False, "error": str(exc)}, 500))
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _handle_save_config(self) -> None:
        data = self._read_json()
        cfg = save_config(
            username=str(data.get("username") or ""),
            auth_code=data.get("auth_code"),
            archive_root=str(data.get("archive_root") or ""),
            host=str(data.get("host") or ""),
        )
        self._send(*_json_bytes({"ok": True, "config": config_public_dict(cfg)}))

    def _handle_run(self) -> None:
        data = self._read_json()
        kind = (data.get("kind") or "imap").strip()
        cfg = self._cfg_from_request(data)
        if kind == "demo":
            RUNNER.start("demo", lambda: run_demo(cfg))
        elif kind == "imap":
            if not imap_credentials_ready(cfg):
                raise ValueError("请先填写邮箱和授权码，点「保存」后再开始")
            day_s = data.get("date") or _today(cfg)
            day = date.fromisoformat(day_s)
            RUNNER.start("imap", lambda: run_imap(cfg, day))
        else:
            raise ValueError("未知操作")
        self._send(*_json_bytes({"ok": True, "job": RUNNER.state.snapshot()}))

    def _handle_import_files(self) -> None:
        ctype = self.headers.get("Content-Type") or ""
        files = _parse_multipart(ctype, self._read_body())
        blobs = [(name, content) for name, content in files if content.strip()]
        if not blobs:
            raise ValueError("没有读到邮件文件，请选择 .eml 或 .mbox")
        tmp = Path(tempfile.mkdtemp(prefix="mail-upload-"))
        paths: list[Path] = []
        for name, content in blobs:
            dest = unique_path(tmp / sanitize_filename(name or "mail.eml", max_len=120))
            dest.write_bytes(content)
            paths.append(dest)
        cfg = load_config()

        def job():
            try:
                return run_local_paths(cfg, paths)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        RUNNER.start("upload", job)
        self._send(*_json_bytes({"ok": True, "count": len(paths), "job": RUNNER.state.snapshot()}))

    def _handle_open(self) -> None:
        data = self._read_json()
        cfg = load_config()
        raw_path = (data.get("path") or "").strip()
        target = Path(raw_path) if raw_path else cfg.archive_root
        opened = _safe_under(cfg.archive_root, target)
        _open_dir(opened)
        self._send(*_json_bytes({"ok": True, "path": str(opened)}))

    def _handle_pick_folder(self) -> None:
        data = self._read_json()
        initial = str(data.get("initial") or load_config().archive_root)
        chosen = pick_folder(initial)
        if not chosen:
            self._send(*_json_bytes({"ok": True, "cancelled": True, "path": ""}))
            return
        path = str(Path(chosen).expanduser().resolve())
        self._send(*_json_bytes({"ok": True, "cancelled": False, "path": path}))

    def _cfg_from_request(self, data: dict) -> AppConfig:
        username = str(data.get("username") or "").strip()
        archive_root = str(data.get("archive_root") or "").strip()
        auth_code = data.get("auth_code")
        host = str(data.get("host") or "").strip()
        if username or archive_root or auth_code or host:
            current = load_config()
            return save_config(
                username=username or current.imap.username,
                auth_code=auth_code,
                archive_root=archive_root or str(current.archive_root),
                host=host,
            )
        return load_config()


def pick_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用端口")


def run_web(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    if not (WEB_DIR / "index.html").is_file():
        raise FileNotFoundError(f"缺少页面文件: {WEB_DIR / 'index.html'}")
    port = pick_port(host, port)
    server = ThreadingHTTPServer((host, port), AppHandler)
    url = f"http://{host}:{port}/"
    print()
    print("邮件归档页面已启动", flush=True)
    print(f"请用浏览器打开: {url}", flush=True)
    print("整理时请不要关闭这个窗口。按 Ctrl+C 可退出。", flush=True)
    print()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:
            LOG.warning("无法自动打开浏览器: %s", exc)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        server.server_close()
    return 0
