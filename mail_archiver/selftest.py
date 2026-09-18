from __future__ import annotations

import logging
import tempfile
import zipfile
from datetime import datetime, timezone, timedelta
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


def run_self_test(verbose: bool = False) -> int:
    setup_logging(verbose)
    _check_infer()
    _check_summarize_note()
    _check_empty_day_filter()
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

        LOG.info("self-test 通过：导入 %s 封，去重跳过 %s 封，目录结构与解压安全检查均正常", saved, len(raws))
        print(f"self-test 通过。样例归档位置（即将随临时目录删除）: {archive_root}")
        print("本机 Python 可以直接使用本工具。配置邮箱后运行: python -m mail_archiver")
        return 0
