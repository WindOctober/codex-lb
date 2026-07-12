from __future__ import annotations

import imaplib
from email.message import EmailMessage

from app.modules.mail_inbox.imap_sync import _parse_fetch_data, _requires_client_id, _send_client_id


class FakeImapClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []

    def _simple_command(self, command: str, payload: str):
        self.commands.append((command, payload))
        return "OK", [b"ID completed"]


def test_netease_hosts_require_client_id():
    assert _requires_client_id("imap.163.com")
    assert _requires_client_id("imap.126.com")
    assert _requires_client_id("imap.yeah.net")
    assert not _requires_client_id("imap.gmail.com")


def test_send_client_id_registers_imap_id_command():
    imaplib.Commands.pop("ID", None)
    client = FakeImapClient()

    _send_client_id(client)  # type: ignore[arg-type]

    assert imaplib.Commands["ID"] == ("AUTH",)
    assert client.commands == [
        (
            "ID",
            '("name" "codex-lb" "version" "1.14.1" "vendor" "codex-lb" "support-email" "local")',
        )
    ]


def test_parse_fetch_data_decodes_html_message_snippet():
    message = EmailMessage()
    message["From"] = "ChatGPT <noreply@tm.openai.com>"
    message["To"] = "betsun2908@163.com"
    message["Subject"] = "你的临时 ChatGPT 登录代码"
    message["Date"] = "Fri, 19 Jun 2026 16:19:00 +0000"
    message["Message-ID"] = "<login-code@example.com>"
    message.set_content(
        "<html><body><p>你的临时 ChatGPT 登录代码是 123456</p></body></html>",
        subtype="html",
        charset="utf-8",
        cte="quoted-printable",
    )

    parsed = _parse_fetch_data(
        "1",
        [(b"1 (FLAGS (\\Seen) BODY[] {123}", message.as_bytes())],
    )

    assert parsed is not None
    assert parsed.sender_email == "noreply@tm.openai.com"
    assert parsed.subject == "你的临时 ChatGPT 登录代码"
    assert parsed.snippet == "你的临时 ChatGPT 登录代码是 123456"
