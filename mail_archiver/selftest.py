from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from pathlib import Path

from mail_archiver.archiver import MailArchiver
from mail_archiver.config import AppConfig, ExtractConfig, ImapConfig
from mail_archiver.local_import import iter_local_messages
from mail_archiver.util import setup_logging

LOG = logging.getLogger("mail_archiver")

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\r\xd8\x00\x00\x00\x00IEND\xaeB`\x82"
)

TZ = timezone(timedelta(hours=8), name="UTC+8")
FIXED = datetime(2026, 9, 16, 14, 32, 5, tzinfo=TZ)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _build_mail(
    *,
    subject: str,
    from_name: str,
    from_email: str,
    message_id: str,
    when: datetime,
    text: str,
    html: str | None = None,
    attachments: list[tuple[str, bytes, str, str]] | None = None,
    inline_images: list[tuple[str, bytes, str]] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = "support@163.com"
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = message_id
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, data, maintype, subtype in attachments or []:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    for cid, data, subtype in inline_images or []:
        msg.add_attachment(data, maintype="image", subtype=subtype, filename=cid + "." + subtype)
        last = list(msg.walk())[-1]
        last["Content-ID"] = f"<{cid}>"
        last.replace_header("Content-Disposition", "inline")
    return msg.as_bytes()


def build_sample_dir(root: Path) -> Path:
    mail_dir = root / "inbox"
    mail_dir.mkdir(parents=True, exist_ok=True)

    zip_ok = _zip_bytes({"error.log": b"disk full\n", "subdir/info.txt": "中文日志".encode("utf-8")})
    zip_evil = _zip_bytes({"../../escape.txt": b"pwned\n", "ok.txt": b"safe\n"})

    mail_a = _build_mail(
        subject="无法登录生产环境",
        from_name="张三",
        from_email="zhangsan@example.com",
        message_id="<case-a@example.com>",
        when=FIXED,
        text="客户端打不开，见附件。",
        html="<p>客户端<strong>打不开</strong>，见附件。</p>",
        attachments=[
            ("截图.png", PNG_1X1, "image", "png"),
            ("日志.zip", zip_ok, "application", "zip"),
        ],
    )
    mail_b = _build_mail(
        subject="发票请尽快开",
        from_name="张三",
        from_email="zhangsan@example.com",
        message_id="<case-b@example.com>",
        when=FIXED.replace(hour=16, minute=1, second=9),
        text="第二封邮件，只要文本。",
    )
    mail_c = _build_mail(
        subject="压缩包路径测试",
        from_name="李四",
        from_email="lisi@example.com",
        message_id="<case-c@example.com>",
        when=FIXED.replace(hour=9, minute=10, second=11),
        text="请检查压缩包",
        attachments=[("payload.zip", zip_evil, "application", "zip")],
    )
    (mail_dir / "a.eml").write_bytes(mail_a)
    (mail_dir / "b.eml").write_bytes(mail_b)
    (mail_dir / "c.eml").write_bytes(mail_c)

    mail_d = _build_mail(
        subject="mbox 导入测试",
        from_name="王五",
        from_email="wangwu@example.com",
        message_id="<case-d@example.com>",
        when=FIXED.replace(hour=11, minute=0, second=0),
        text="来自 mbox",
    )
    (mail_dir / "extra.mbox").write_bytes(_as_mbox(mail_d, "wangwu@example.com"))

    mail_e = _build_mail(
        subject="内嵌图片测试",
        from_name="赵六",
        from_email="zhaoliu@example.com",
        message_id="<case-e@example.com>",
        when=FIXED.replace(hour=13, minute=20, second=0),
        text="正文有内嵌图片",
        html="<p>这是内嵌图片：</p><img src='cid:logo' alt='logo' />",
        inline_images=[("logo", PNG_1X1, "png")],
    )
    (mail_dir / "e.eml").write_bytes(mail_e)

    return mail_dir


def _as_mbox(raw: bytes, sender: str) -> bytes:
    text = raw.replace(b"\r\n", b"\n")
    escaped = []
    for line in text.split(b"\n"):
        escaped.append(b">" + line if line.startswith(b"From ") else line)
    body = b"\n".join(escaped)
    if not body.endswith(b"\n"):
        body += b"\n"
    header = f"From {sender} Wed Sep 16 03:00:00 2026\n".encode("ascii")
    return header + body + b"\n"


def _must_exist(path: Path) -> None:
    if not path.exists():
        raise AssertionError(f"缺少文件: {path}")


def _check_infer() -> None:
    """校验邮箱地址 → IMAP 服务商推断。"""
    from mail_archiver.config import infer_imap_host, infer_imap_info

    cases = {
        "name@163.com": ("网易 163 邮箱", "imap.163.com", True),
        "name@126.com": ("网易 126 邮箱", "imap.126.com", True),
        "name@qq.com": ("QQ 邮箱", "imap.qq.com", True),
        "name@foxmail.com": ("Foxmail 邮箱", "imap.qq.com", True),
        "name@gmail.com": ("Gmail", "imap.gmail.com", True),
        "name@outlook.com": ("Outlook", "outlook.office365.com", True),
        "name@sina.com": ("新浪邮箱", "imap.sina.com", True),
        "name@aliyun.com": ("阿里云邮箱", "imap.aliyun.com", True),
        "name@unknown.tld": ("", "imap.163.com", False),
    }
    for email, (provider, host, known) in cases.items():
        info = infer_imap_info(email)
        assert info["provider"] == provider, f"{email} provider 期望 {provider}，实际 {info['provider']}"
        assert info["host"] == host, f"{email} host 期望 {host}，实际 {info['host']}"
        assert info["known"] is known, f"{email} known 期望 {known}，实际 {info['known']}"
    assert infer_imap_info("no-atmark")["known"] is False
    assert infer_imap_host("name@unknown.tld", fallback="imap.custom.com") == "imap.custom.com"
    LOG.info("邮箱识别检查通过")


def _check_save_config(tmp_path: Path) -> None:
    """校验 save_config 的 host 自动推断与手动覆盖。"""
    from mail_archiver.config import save_config

    dest = tmp_path / "config.local.json"
    arc = str(tmp_path / "arc")
    # 不传 host：按邮箱自动推断
    cfg = save_config(username="name@qq.com", auth_code="CODE", archive_root=arc, dest=dest)
    assert cfg.imap.host == "imap.qq.com", f"自动推断应为 imap.qq.com，实际 {cfg.imap.host}"
    # 手动覆盖 host
    cfg2 = save_config(username="name@qq.com", auth_code="CODE", archive_root=arc, host="imap.custom.com", dest=dest)
    assert cfg2.imap.host == "imap.custom.com", f"手动覆盖未生效，实际 {cfg2.imap.host}"
    # 切换邮箱且不传 host：重新推断
    cfg3 = save_config(username="name@163.com", auth_code="CODE", archive_root=arc, dest=dest)
    assert cfg3.imap.host == "imap.163.com", f"切换邮箱后应重新推断，实际 {cfg3.imap.host}"
    # host=auto 哨兵也应触发推断
    cfg4 = save_config(username="name@gmail.com", auth_code="CODE", archive_root=arc, host="auto", dest=dest)
    assert cfg4.imap.host == "imap.gmail.com", f"auto 哨兵应触发推断，实际 {cfg4.imap.host}"
    LOG.info("配置保存与 host 推断检查通过")


def _check_auth_memory(tmp_path: Path) -> None:
    """校验按邮箱记忆授权码：保存→带入→更新→切换不串码。"""
    from mail_archiver.config import get_saved_auth_code, save_config

    dest = tmp_path / "config.local.json"
    arc = str(tmp_path / "arc")
    save_config(username="a@163.com", auth_code="CODE_A", archive_root=arc, dest=dest)
    assert get_saved_auth_code("a@163.com", dest) == "CODE_A", "A 邮箱授权码应已记忆"
    save_config(username="b@qq.com", auth_code="CODE_B", archive_root=arc, dest=dest)
    assert get_saved_auth_code("b@qq.com", dest) == "CODE_B"
    assert get_saved_auth_code("a@163.com", dest) == "CODE_A", "切到 B 后 A 的授权码仍应被记忆"
    # 重新输入 A 的授权码 → 更新
    save_config(username="a@163.com", auth_code="CODE_A2", archive_root=arc, dest=dest)
    assert get_saved_auth_code("a@163.com", dest) == "CODE_A2", "重新输入应更新授权码"
    # 切回 A 但不传码 → 应自动带入 A 的已存码（不能串成 B 的）
    cfg = save_config(username="a@163.com", auth_code=None, archive_root=arc, dest=dest)
    assert cfg.imap.auth_code == "CODE_A2", "不重填时应带入该邮箱已存授权码"
    # 切到一个没存过的邮箱且不传码 → 不能把别的邮箱的码带过去
    cfg2 = save_config(username="c@qq.com", auth_code=None, archive_root=arc, dest=dest)
    assert cfg2.imap.auth_code == "", "没存过的邮箱不传码时不应带入他人授权码"
    LOG.info("按邮箱记忆授权码检查通过")


def _check_summarize_note() -> None:
    """校验空结果提示：设了 note 优先返回 note。"""
    from mail_archiver.archiver import ArchiveResult
    from mail_archiver.pipeline import summarize

    r = ArchiveResult()
    r.note = "2026-09-18 当日收件箱没有邮件，已停止，未导出任何内容。"
    assert summarize(r) == r.note, "设置 note 时应优先返回 note"
    assert summarize(ArchiveResult()) == "没有找到可整理的邮件"
    LOG.info("空结果提示检查通过")


def _check_empty_day_filter() -> None:
    """回归：当日无邮件时不能导出全部，必须按 INTERNALDATE 校验后停止。"""
    from datetime import date, timezone, timedelta
    from mail_archiver.config import ImapConfig
    from mail_archiver.imap_client import ImapSource

    class FakeConn:
        def __init__(self, search_uids, internaldate_resp):
            self._search = search_uids
            self._internaldate = internaldate_resp

        def select(self, folder, readonly=True):
            return ("OK", [b"1"])

        def uid(self, command, *args):
            if command == "SEARCH":
                # 模拟 163 空日期故障性地返回整箱 UID
                return ("OK", [self._search])
            if command == "FETCH":
                return ("OK", self._internaldate)
            return ("NO", None)

        def logout(self):
            pass

    day = date(2026, 9, 18)
    tz = timezone(timedelta(hours=8))
    # SEARCH 返回 3 个 UID，但它们的 INTERNALDATE 都不在 9-18
    internaldate_resp = [
        b"1 (UID 1 INTERNALDATE \"17-Sep-2026 10:00:00 +0800\")",
        b"2 (UID 2 INTERNALDATE \"19-Sep-2026 10:00:00 +0800\")",
        b"3 (UID 3 INTERNALDATE \"20-Sep-2026 10:00:00 +0800\")",
        b")",
    ]
    conn = FakeConn(b"1 2 3", internaldate_resp)
    src = ImapSource(ImapConfig(username="x@163.com", auth_code="c"))
    src.conn = conn
    yielded = list(src.fetch_on_date(day, tz))
    assert yielded == [], f"当日无邮件时不应导出任何内容，却导出了 {len(yielded)} 封"
    LOG.info("空日期防误导检查通过")


def _check_range_filter() -> None:
    """回归：区间归档只保留 INTERNALDATE 落在 [start,end] 内的邮件。"""
    from datetime import date, timezone, timedelta
    from mail_archiver.config import ImapConfig
    from mail_archiver.imap_client import ImapSource

    class FakeConn:
        def __init__(self, internaldate_resp):
            self._internaldate = internaldate_resp

        def select(self, folder, readonly=True):
            return ("OK", [b"1"])

        def uid(self, command, *args):
            if command == "FETCH":
                return ("OK", self._internaldate)
            return ("NO", None)

        def logout(self):
            pass

    tz = timezone(timedelta(hours=8))
    # 5 封：边界前 / 起点 / 中间 / 终点 / 边界后
    internaldate_resp = [
        b"1 (UID 1 INTERNALDATE \"14-Sep-2026 10:00:00 +0800\")",
        b"2 (UID 2 INTERNALDATE \"15-Sep-2026 10:00:00 +0800\")",
        b"3 (UID 3 INTERNALDATE \"16-Sep-2026 23:59:00 +0800\")",
        b"4 (UID 4 INTERNALDATE \"17-Sep-2026 00:01:00 +0800\")",
        b"5 (UID 5 INTERNALDATE \"18-Sep-2026 10:00:00 +0800\")",
        b")",
    ]
    conn = FakeConn(internaldate_resp)
    src = ImapSource(ImapConfig(username="x@163.com", auth_code="c"))
    src.conn = conn
    kept = src._filter_in_range(["1", "2", "3", "4", "5"], date(2026, 9, 15), date(2026, 9, 17), tz)
    assert kept == ["2", "3", "4"], f"区间 9-15~9-17 应只留 2/3/4，实际 {kept}"
    # 单日等价：start==end 只留当天
    kept_day = src._filter_in_range(["1", "2", "3", "4", "5"], date(2026, 9, 16), date(2026, 9, 16), tz)
    assert kept_day == ["3"], f"单日 9-16 应只留 3，实际 {kept_day}"
    # 空区间：无候选
    assert src._filter_in_range([], date(2026, 9, 1), date(2026, 9, 10), tz) == []
    LOG.info("区间筛选检查通过")


def _check_internaldate_tz() -> None:
    """回归：INTERNALDATE 无时区偏移时按服务器本地时间（配置时区）解释。

    旧实现交给 RFC 2822 解析器，无偏移格式（部分服务器返回裸值）直接解析失败，
    邮件会被区间筛选整个丢掉；带偏移的虽然正确，但裸值当 UTC 处理会把
    16:00 之后收到的邮件错算到第二天。
    """
    from mail_archiver.config import ImapConfig
    from mail_archiver.imap_client import ImapSource, _parse_internaldate
    from mail_archiver.util import to_tz

    tz = timezone(timedelta(hours=8))

    # 带偏移：解析为带时区时间
    aware = _parse_internaldate(b'1 (UID 1 INTERNALDATE "18-Sep-2026 18:30:00 +0800")')
    assert aware is not None and aware.utcoffset() == timedelta(hours=8), f"带偏移解析失败: {aware}"
    # 无偏移：RFC 3501 服务器本地时间，保持 naive，由 to_tz 按配置时区解释
    naive = _parse_internaldate(b'1 (UID 1 INTERNALDATE "18-Sep-2026 18:30:00")')
    assert naive is not None and naive.tzinfo is None, f"无偏移应返回 naive，实际 {naive}"
    local = to_tz(naive, tz)
    assert (local.year, local.month, local.day) == (2026, 9, 18), (
        f"18:30 收到的邮件应留在 9-18，实际被算到 {local.date()}"
    )
    assert local.hour == 18 and local.minute == 30, f"本地时间应保持 18:30，实际 {local.time()}"
    # 省略时间部分（只有日期）也应能解析
    day_only = _parse_internaldate(b'1 (UID 1 INTERNALDATE "18-Sep-2026")')
    assert day_only is not None and (day_only.year, day_only.month, day_only.day) == (2026, 9, 18)
    # 乱格式与无 INTERNALDATE 都返回 None
    assert _parse_internaldate(b'1 (UID 1 INTERNALDATE "not-a-date")') is None
    assert _parse_internaldate(b"no date here") is None

    # 端到端：区间筛选不再丢掉无偏移的邮件
    class FakeConn:
        def __init__(self, resp):
            self._resp = resp

        def uid(self, command, *args):
            if command == "FETCH":
                return ("OK", self._resp)
            return ("NO", None)

        def logout(self):
            pass

    resp = [
        b"1 (UID 1 INTERNALDATE \"16-Sep-2026 17:05:00\")",  # 无偏移，旧版会被丢弃
        b"2 (UID 2 INTERNALDATE \"17-Sep-2026 09:00:00 +0800\")",
        b"3 (UID 3 INTERNALDATE \"14-Sep-2026 23:00:00\")",  # 区间外
        b")",
    ]
    src = ImapSource(ImapConfig(username="x@163.com", auth_code="c"))
    src.conn = FakeConn(resp)
    kept = src._filter_in_range(["1", "2", "3"], date(2026, 9, 15), date(2026, 9, 17), tz)
    assert kept == ["1", "2"], f"应保留 1/2，实际 {kept}"
    LOG.info("INTERNALDATE 时区检查通过")


CAL_TEST_SCRIPT = """
<script>
window.addEventListener('load', function () {
  var out = [];
  try {
    var cal = document.getElementById('calendar');
    document.getElementById('date-wrap').click();
    out.push('open: hidden=' + cal.hidden);
    var t0 = document.querySelector('.cal-title').textContent.trim();
    document.getElementById('cal-prev').click();
    var t1 = document.querySelector('.cal-title').textContent.trim();
    out.push('prev: hidden=' + cal.hidden + ' | ' + t0 + ' -> ' + t1);
    document.getElementById('cal-next').click();
    out.push('next: hidden=' + cal.hidden + ' | -> ' + document.querySelector('.cal-title').textContent.trim());
    cal.querySelector('.day[data-day="15"]').click();
    out.push('day15: hidden=' + cal.hidden + ' date=' + document.getElementById('date').value);
    cal.querySelector('.day[data-day="18"]').click();
    out.push('day18: hidden=' + cal.hidden + ' date=' + document.getElementById('date').value);
  } catch (e) { out.push('ERR ' + e.message); }
  document.title = 'TEST:' + out.join(' || ');
});
</script>
"""


def _check_calendar_click() -> None:
    """回归：日历面板内点击（翻月/选日）不得被外层开/关处理器误关闭。

    用无头浏览器加载页面副本、注入模拟点击脚本，结果写进 document.title 再解析。
    面板内点击若冒泡到外层会立刻 closeCalendar，本检查直接复现用户操作路径。
    """
    from mail_archiver.screenshot import _find_browser

    browser = _find_browser()
    if not browser:
        LOG.warning("找不到 Edge/Chrome，跳过日历点击检查")
        return
    page_src = Path(__file__).resolve().parent / "web" / "index.html"
    tmp = tempfile.mkdtemp(prefix="cal-check-")
    try:
        page = Path(tmp) / "index.html"
        page.write_text(
            page_src.read_text(encoding="utf-8").replace("</body>", CAL_TEST_SCRIPT + "</body>"),
            encoding="utf-8",
        )
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--virtual-time-budget=5000",
            "--dump-dom",
            page.as_uri(),
        ]
        dom = ""
        for headless in ("--headless=new", "--headless"):
            cmd[1] = headless
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            dom = (proc.stdout or b"").decode("utf-8", errors="replace")
            if "TEST:" in dom:
                break
        match = re.search(r"TEST:(.*?)</title>", dom, re.S)
        assert match, "日历点击测试没有返回结果（浏览器未渲染页面）"
        result = match.group(1)
        assert "ERR" not in result, f"日历点击测试脚本报错: {result}"
        assert "prev: hidden=false" in result and "next: hidden=false" in result, (
            f"翻月后面板被关闭: {result}"
        )
        assert "2026 年 8 月" in result, f"点击 ‹ 后月份没有切换: {result}"
        # day15 点在"回到 9 月"之后：若 next 没把月份翻回来，这里会是 2026-08-15
        assert "day15: hidden=false date=2026-09-15" in result and (
            "day18: hidden=false date=2026-09-15 ~ 2026-09-18" in result
        ), f"选日期后面板被关闭或区间值不对（区间两连点被中断）: {result}"
        LOG.info("日历点击检查通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_self_test(verbose: bool = False) -> int:
    setup_logging(verbose)
    _check_infer()
    _check_summarize_note()
    _check_empty_day_filter()
    _check_range_filter()
    _check_internaldate_tz()
    _check_calendar_click()
    with tempfile.TemporaryDirectory(prefix="mail-archiver-") as tmp:
        tmp_path = Path(tmp)
        _check_save_config(tmp_path)
        _check_auth_memory(tmp_path)
        sample = build_sample_dir(tmp_path)
        archive_root = tmp_path / "archive"
        cfg = AppConfig(
            archive_root=archive_root,
            timezone="Asia/Shanghai",
            imap=ImapConfig(enabled=False),
            extract=ExtractConfig(),
        )
        archiver = MailArchiver(cfg)
        archiver.setup()

        saved = skipped = 0
        raws = list(iter_local_messages(sample))
        if len(raws) < 5:
            raise AssertionError(f"样例邮件数量不足: {len(raws)}")
        for raw in raws:
            status = archiver.archive_raw(raw)
            if status == "saved":
                saved += 1
            elif status == "skipped":
                skipped += 1
        if saved != len(raws):
            raise AssertionError(f"首次归档应为 {len(raws)}，实际 {saved}")

        for raw in raws:
            if archiver.archive_raw(raw) != "skipped":
                raise AssertionError("重复运行未按 Message-ID 跳过")

        day = archive_root / "2026-09-16"
        sender_a = day / "张三zhangsan@example.com"
        sender_b = day / "李四lisi@example.com"
        sender_c = day / "王五wangwu@example.com"
        a_dirs = [p for p in sender_a.iterdir() if p.is_dir()]
        if len(a_dirs) != 2:
            raise AssertionError(f"同一客户当天应有 2 个子文件夹，实际 {len(a_dirs)}: {a_dirs}")

        login_dir = next(p for p in a_dirs if "无法登录" in p.name)
        if not login_dir.name.endswith("_001"):
            raise AssertionError(f"第一封流水号应为 _001，实际: {login_dir.name}")
        if (login_dir / "邮件预览.png").is_file():
            LOG.info("邮件预览截图已生成")
        else:
            LOG.warning("邮件预览截图未生成（可能缺少 Edge/Chrome）")
        if (login_dir / "mail.eml").exists() or (login_dir / "body.txt").exists():
            raise AssertionError("不应再生成 mail.eml 或 body.txt")
        _must_exist(login_dir / "attachments" / "截图.png")
        _must_exist(login_dir / "attachments" / "日志.zip")
        _must_exist(login_dir / "attachments" / "日志" / "error.log")
        _must_exist(login_dir / "attachments" / "日志" / "subdir" / "info.txt")

        evil_dir = next(p for p in sender_b.iterdir() if p.is_dir())
        _must_exist(evil_dir / "attachments" / "payload.zip")
        _must_exist(evil_dir / "attachments" / "payload" / "ok.txt")
        escaped = list(archive_root.rglob("escape.txt"))
        if escaped:
            raise AssertionError(f"压缩包路径穿越未被拦截: {escaped}")
        if (tmp_path / "escape.txt").exists():
            raise AssertionError("危险文件被解压到临时目录外")

        if not sender_c.exists():
            raise AssertionError("mbox 邮件未导入")

        sender_e = day / "赵六zhaoliu@example.com"
        if not sender_e.exists():
            raise AssertionError("内嵌图片测试邮件未归档")
        e_dir = next(p for p in sender_e.iterdir() if p.is_dir())
        if (e_dir / "邮件预览.png").is_file():
            LOG.info("内嵌图片邮件截图已生成")
        else:
            LOG.warning("内嵌图片邮件截图未生成")
        _must_exist(e_dir / "attachments" / "logo.png")

        _must_exist(day / "_日报.csv")
        daily = (day / "_日报.csv").read_text(encoding="utf-8-sig")
        if "zhangsan@example.com" not in daily or "wangwu@example.com" not in daily:
            raise AssertionError("日报缺少发件人")
        if "张三" not in daily or "李四" not in daily:
            raise AssertionError("日报缺少发件人姓名")

        # 区间聚合：样例全部落在 2026-09-16，区间应命中、空区间应返回 0
        from mail_archiver.results import list_range

        hit = list_range(archive_root, date(2026, 9, 15), date(2026, 9, 17))
        if len(hit) != saved:
            raise AssertionError(f"区间 9-15~9-17 应聚合 {saved} 封，实际 {len(hit)}")
        empty = list_range(archive_root, date(2026, 9, 18), date(2026, 9, 20))
        if empty:
            raise AssertionError(f"空区间应返回 0 封，实际 {len(empty)}")
        # start>end 时内部自动交换，结果应等价
        swapped = list_range(archive_root, date(2026, 9, 17), date(2026, 9, 15))
        if len(swapped) != len(hit):
            raise AssertionError("start>end 交换后结果应与正向一致")
        LOG.info("区间聚合检查通过")

        LOG.info("self-test 通过：导入 %s 封，去重跳过 %s 封，目录结构与解压安全检查均正常", saved, len(raws))
        print(f"self-test 通过。样例归档位置（即将随临时目录删除）: {archive_root}")
        print("本机 Python 可以直接使用本工具。配置邮箱后运行: python -m mail_archiver")
        return 0
