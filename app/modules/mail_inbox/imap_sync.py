from __future__ import annotations

import html
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.header import decode_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

NETEASE_IMAP_HOST_SUFFIXES = ("163.com", "126.com", "yeah.net", "188.com")


@dataclass(frozen=True, slots=True)
class ImapConnectionSettings:
    host: str
    port: int
    username: str
    password: str
    mailbox: str = "INBOX"
    limit: int = 50


@dataclass(frozen=True, slots=True)
class ImapFetchedMessage:
    provider_message_id: str
    sender_email: str
    sender_name: str | None
    recipients: list[str]
    subject: str
    snippet: str
    received_at: datetime
    unread: bool
    starred: bool


def fetch_imap_messages(settings: ImapConnectionSettings) -> list[ImapFetchedMessage]:
    with imaplib.IMAP4_SSL(settings.host, settings.port) as client:
        client.login(settings.username, settings.password)
        if _requires_client_id(settings.host):
            _send_client_id(client)
        status, select_data = client.select(settings.mailbox, readonly=False)
        if status != "OK":
            raise RuntimeError(f"IMAP mailbox select failed: {_format_imap_response(select_data)}")
        status, search_data = client.search(None, "ALL")
        if status != "OK" or not search_data:
            return []
        message_ids = search_data[0].split()[-settings.limit :]
        messages: list[ImapFetchedMessage] = []
        for message_id in reversed(message_ids):
            status, fetch_data = client.fetch(message_id, "(FLAGS BODY.PEEK[])")
            if status != "OK":
                continue
            fetched = _parse_fetch_data(message_id.decode("ascii", errors="ignore"), fetch_data)
            if fetched is not None:
                messages.append(fetched)
        return messages


def _parse_fetch_data(sequence_id: str, fetch_data: list[bytes | tuple[bytes, bytes]]) -> ImapFetchedMessage | None:
    message_bytes = b""
    flags = ""
    for item in fetch_data:
        if not isinstance(item, tuple):
            continue
        meta, payload = item
        meta_text = meta.decode("utf-8", errors="ignore")
        flags_match = re.search(r"FLAGS \(([^)]*)\)", meta_text)
        if flags_match:
            flags = flags_match.group(1)
        if b"BODY" in meta.upper():
            message_bytes = payload

    if not message_bytes:
        return None

    message = message_from_bytes(message_bytes, policy=policy.default)
    sender_name, sender_email = parseaddr(message.get("From", ""))
    sender_email = sender_email.strip().lower()
    if not sender_email:
        return None

    provider_message_id = (message.get("Message-ID") or f"imap:{sequence_id}").strip()
    received_at = _parse_received_at(message.get("Date"))
    recipients = [
        address.strip().lower()
        for _, address in getaddresses([message.get("To", ""), message.get("Cc", "")])
        if address.strip()
    ]

    return ImapFetchedMessage(
        provider_message_id=provider_message_id,
        sender_email=sender_email,
        sender_name=_decode_header_value(sender_name) or None,
        recipients=recipients,
        subject=_decode_header_value(message.get("Subject", "")),
        snippet=_snippet(message),
        received_at=received_at,
        unread="\\Seen" not in flags,
        starred="\\Flagged" in flags,
    )


def _decode_header_value(value: str) -> str:
    parts: list[str] = []
    for raw, encoding in decode_header(value):
        if isinstance(raw, bytes):
            parts.append(raw.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(raw)
    return "".join(parts).strip()


def _parse_received_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=UTC).replace(tzinfo=None)
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _snippet(message: Message) -> str:
    content = _message_text(message)
    compact = " ".join(content.split())
    return compact[:240]


def _message_text(message: Message) -> str:
    if isinstance(message, EmailMessage):
        body = message.get_body(preferencelist=("plain", "html"))
        if body is not None:
            content = body.get_content()
            if body.get_content_type() == "text/html":
                return _html_to_text(content)
            return str(content)
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            return _html_to_text(decoded) if content_type == "text/html" else decoded
        return ""
    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        decoded = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
        return _html_to_text(decoded) if message.get_content_type() == "text/html" else decoded
    payload_text = message.get_payload()
    return str(payload_text) if isinstance(payload_text, str) else ""


def _html_to_text(value: str) -> str:
    without_style = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    with_breaks = re.sub(r"(?i)<\s*(br|/p|/div|/tr|/li)\s*/?>", "\n", without_style)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", with_breaks)
    return html.unescape(without_tags)


def _requires_client_id(host: str) -> bool:
    normalized = host.strip().lower()
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in NETEASE_IMAP_HOST_SUFFIXES)


def _send_client_id(client: imaplib.IMAP4_SSL) -> None:
    imaplib.Commands.setdefault("ID", ("AUTH",))
    status, data = client._simple_command(  # noqa: SLF001
        "ID",
        '("name" "codex-lb" "version" "1.14.1" "vendor" "codex-lb" "support-email" "local")',
    )
    if status != "OK":
        raise RuntimeError(f"IMAP client ID failed: {_format_imap_response(data)}")


def _format_imap_response(values: list[bytes] | None) -> str:
    if not values:
        return "empty server response"
    return "; ".join(value.decode("utf-8", errors="replace") for value in values)
