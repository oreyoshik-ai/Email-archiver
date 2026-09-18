from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from mail_archiver.util import sanitize_filename


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str
    content_id: str = ""


@dataclass
class ParsedMail:
    message_id: str
    subject: str
    from_raw: str
    from_name: str
    from_email: str
    to_raw: str
    date: datetime
    body_text: str
    body_html: str
    attachments: list[Attachment] = field(default_factory=list)
    raw: bytes = b""


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def decode_bytes(data: bytes, charset: str | None) -> str:
    candidates = [charset, "utf-8", "gb18030", "gbk", "big5", "latin-1"]
    seen: set[str] = set()
    for enc in candidates:
        if not enc:
            continue
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _part_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return b""
    return payload


def _part_text(part: Message) -> str:
    charset = part.get_content_charset()
    return decode_bytes(_part_bytes(part), charset)


def _attachment_name(part: Message, index: int) -> str:
    filename = part.get_filename()
    if filename:
        name = decode_header_value(filename)
    else:
        cid = (part.get("Content-ID") or "").strip("<>")
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(part.get_content_type(), "")
        name = (cid or f"inline_{index}") + ext
    return sanitize_filename(name, max_len=120)


def parse_raw_email(raw: bytes, fallback_date: datetime | None = None) -> ParsedMail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    return parse_message(msg, raw, fallback_date)


def parse_message(msg: Message | EmailMessage, raw: bytes, fallback_date: datetime | None = None) -> ParsedMail:
    subject = decode_header_value(msg.get("Subject"))
    from_raw = decode_header_value(msg.get("From"))
    to_raw = decode_header_value(msg.get("To"))
    from_name, from_email = parseaddr(from_raw)
    from_name = decode_header_value(from_name)
    from_email = (from_email or "").strip().lower()
    if not from_email and from_raw:
        addrs = getaddresses([from_raw])
        if addrs:
            from_name = decode_header_value(addrs[0][0]) or from_name
            from_email = (addrs[0][1] or "").strip().lower()
    if not from_email:
        from_email = "unknown"

    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    if not message_id:
        digest = hashlib.sha256(raw).hexdigest()
        message_id = f"<{digest}@local.generated>"

    mail_date = fallback_date or datetime.now(timezone.utc)
    date_raw = msg.get("Date")
    if date_raw:
        try:
            parsed = parsedate_to_datetime(date_raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            mail_date = parsed
        except Exception:
            pass

    body_text = ""
    body_html = ""
    attachments: list[Attachment] = []
    part_index = 0

    if msg.is_multipart():
        parts = list(msg.walk())
    else:
        parts = [msg]

    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        part_index += 1
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        is_attachment = bool(filename) or disposition == "attachment"
        is_inline_image = disposition == "inline" and ctype.startswith("image/")
        if is_attachment or is_inline_image:
            cid = (part.get("Content-ID") or "").strip("<>")
            attachments.append(
                Attachment(
                    filename=_attachment_name(part, part_index),
                    content=_part_bytes(part),
                    content_type=ctype,
                    content_id=cid,
                )
            )
            continue
        if ctype == "text/plain" and not body_text:
            body_text = _part_text(part)
        elif ctype == "text/html" and not body_html:
            body_html = _part_text(part)

    if not body_text and body_html:
        body_text = html_to_text(body_html)

    return ParsedMail(
        message_id=message_id,
        subject=subject or "(无主题)",
        from_raw=from_raw,
        from_name=from_name,
        from_email=from_email,
        to_raw=to_raw,
        date=mail_date,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        raw=raw,
    )
