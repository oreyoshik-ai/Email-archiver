from __future__ import annotations

import base64
import html as html_lib
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mail_archiver.parse import ParsedMail

LOG = logging.getLogger("mail_archiver")

_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    str(Path.home() / "AppData/Local/Microsoft/Edge/Application/msedge.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

_BROWSER_CACHE: str | None = None


def _find_browser() -> str | None:
    global _BROWSER_CACHE
    if _BROWSER_CACHE:
        return _BROWSER_CACHE
    for candidate in _BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            _BROWSER_CACHE = candidate
            return candidate
    if sys.platform != "win32":
        from shutil import which

        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            found = which(name)
            if found:
                _BROWSER_CACHE = found
                return found
    return None


def _replace_cid_images(body_html: str, mail: ParsedMail) -> str:
    def make_data_uri(att) -> str:
        b64 = base64.b64encode(att.content).decode("ascii")
        return f"data:{att.content_type};base64,{b64}"

    cid_map: dict[str, str] = {}
    for att in mail.attachments:
        if att.content_id:
            cid_map[att.content_id.lower()] = make_data_uri(att)
        cid_filename = att.filename.rsplit(".", 1)[0] if "." in att.filename else att.filename
        if cid_filename:
            cid_map.setdefault(cid_filename.lower(), make_data_uri(att))

    def replacer(match):
        ref = match.group(1).strip().lower()
        return f'src="{cid_map.get(ref, match.group(0))}"'

    return re.sub(r'src="cid:([^"]+)"', replacer, body_html)


def _build_email_html(mail: ParsedMail) -> str:
    date_str = mail.date.strftime("%Y年%m月%d日 %H:%M") if mail.date else ""

    header = f"""
    <div class="mail-header">
      <h1 class="mail-subject">{html_lib.escape(mail.subject or "(无主题)")}</h1>
      <div class="mail-meta">
        <div><span class="label">发件人</span> {html_lib.escape(mail.from_raw or mail.from_email)}</div>
        <div><span class="label">收件人</span> {html_lib.escape(mail.to_raw or "")}</div>
        <div><span class="label">时间</span> {html_lib.escape(date_str)}</div>
      </div>
      <hr class="divider" />
    </div>"""

    if mail.body_html.strip():
        body_content = _replace_cid_images(mail.body_html, mail)
    else:
        escaped = html_lib.escape(mail.body_text or "")
        body_content = f'<div class="text-body">{escaped.replace(chr(10), "<br>\n")}</div>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
    font-size: 14px; line-height: 1.7; color: #1c1916; background: #fff;
    padding: 24px 28px; width: 760px;
  }}
  .mail-subject {{
    font-size: 20px; font-weight: 700; margin-bottom: 12px; color: #1c1916;
  }}
  .mail-meta {{ font-size: 13px; color: #6b635a; margin-bottom: 14px; }}
  .mail-meta div {{ margin: 3px 0; }}
  .mail-meta .label {{
    display: inline-block; width: 48px; color: #9b9388; font-weight: 600;
  }}
  .divider {{ border: none; border-top: 1px solid #e4dcd0; margin: 0 0 16px; }}
  .text-body {{ white-space: pre-wrap; }}
  img {{ max-width: 100%; }}
</style></head><body>
{header}
<div class="mail-body">{body_content}</div>
</body></html>"""


def _measure_height(browser: str, html_path: Path, width: int) -> int:
    measure_html = html_path.read_text(encoding="utf-8")
    # 等 load（图片等加载完）之后再量高度，并用 setTimeout 兜底再量一次；
    # 同时取 body 与 documentElement 的最大值，避免漏算溢出内容。
    measure_html = measure_html.replace(
        "</body>",
        """<script>
  function _report() {
    var h = Math.max(
      document.body.scrollHeight, document.body.offsetHeight,
      document.documentElement.scrollHeight, document.documentElement.offsetHeight
    );
    if (h > 0) document.title = 'HEIGHT:' + h;
  }
  if (document.readyState === 'complete') { _report(); }
  else { window.addEventListener('load', _report); }
  setTimeout(_report, 800);
</script></body>""",
    )
    tmp_measure = html_path.parent / "measure.html"
    tmp_measure.write_text(measure_html, encoding="utf-8")

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--virtual-time-budget=3000",
        "--dump-dom",
        f"--window-size={width},100",
        str(tmp_measure),
    ]
    try:
        # 用字节捕获再按 UTF-8 解码：中文 Windows 默认 GBK 解码 DOM 会抛
        # UnicodeDecodeError，导致测量始终失败、回退到固定高度而截断长邮件。
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        match = re.search(r"HEIGHT:(\d+)", stdout)
        if not match:
            # Win7 上最后的浏览器（Chrome 109/Edge 110）不认识 --headless=new（112 才加入），
            # 会正常启动且不出 DOM，换旧 --headless 再量一次。
            cmd[1] = "--headless"
            proc = subprocess.run(cmd, capture_output=True, timeout=20)
            stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
            match = re.search(r"HEIGHT:(\d+)", stdout)
        if match:
            return int(match.group(1)) + 64
    except Exception:
        pass
    return 0


def render_screenshot(mail: ParsedMail, output_png: Path, width: int = 820) -> bool:
    browser = _find_browser()
    if not browser:
        LOG.warning("未找到 Edge 或 Chrome，跳过截图")
        return False

    content = _build_email_html(mail)
    if not content.strip():
        LOG.debug("邮件内容为空，跳过截图")
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="mail-shot-"))
    tmp_html = tmp_dir / "page.html"
    tmp_html.write_text(content, encoding="utf-8")

    measured = _measure_height(browser, tmp_html, width)
    height = max(measured, 200) if measured > 0 else 1200

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--screenshot={output_png}",
        f"--window-size={width},{height}",
        str(tmp_html),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        # 只要没出图就换旧 --headless 重试：Win7 最后版浏览器（Chrome 109/Edge 110）
        # 不认识 --headless=new，会正常启动、退出码为 0 但不截图，不能只看 returncode。
        if not output_png.is_file():
            cmd[1] = "--headless"
            cmd[-2] = f"--window-size={width},{height}"
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception as exc:
        LOG.warning("截图失败: %s", exc)
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if output_png.is_file() and output_png.stat().st_size > 0:
        LOG.debug("已生成截图: %s (%sx%s)", output_png.name, width, height)
        return True
    LOG.warning("截图未生成，Edge/Chrome 可能版本过低")
    return False


def set_hidden(path: Path) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass
