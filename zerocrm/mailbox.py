"""Digest transport: SMTP send + IMAP read over one Gmail app password.

The message parsing (DKIM extraction, plain-body, thread match) is a pure
function so it is unit-tested against synthetic emails; SMTP/IMAP just do the IO
around it. Credentials come from ZEROCRM_MAILBOX_* with a fallback to the existing
DMARC_IMAP_* secret (same operator@example.com mailbox, same app password).
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
from email import policy
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

_DKIM_D = re.compile(r"header\.d=([^\s;]+)", re.I)
_DKIM_I = re.compile(r"header\.i=@?([^\s;]+)", re.I)


def _creds() -> tuple[str, str]:
    user = os.environ.get("ZEROCRM_MAILBOX_USER") or os.environ["DMARC_IMAP_USER"]
    pw = os.environ.get("ZEROCRM_MAILBOX_PASSWORD") or os.environ["DMARC_IMAP_PASSWORD"]
    return user, pw


def send_email(subject: str, body: str, to: str | None = None, sender: str | None = None,
               html: str | None = None, in_reply_to: str | None = None) -> str:
    """Send one message from the apex mailbox (sid@syndai.ai), return its
    Message-ID. `in_reply_to` threads a warm reply onto the prospect's message
    (In-Reply-To + References), so continuing from the apex still lands in-thread.
    HTML, if given, rides as a multipart/alternative."""
    user, pw = _creds()
    sender = sender or user
    to = to or user
    msg = EmailMessage()
    mid = make_msgid(domain=sender.split("@")[-1])
    msg["Message-ID"] = mid
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Reply-To"] = sender
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return mid


def send_digest(subject: str, body: str, to: str | None = None, sender: str | None = None,
                html: str | None = None) -> str:
    """Send the digest (record its Message-ID as a known thread so replies match)."""
    return send_email(subject, body, to=to, sender=sender, html=html)


class ReplySender:
    """Sender-protocol adapter so warm replies to prospects run through the same
    claim-first executor as cold enrollments. Sends from the apex, threaded onto
    the prospect's reply. Idempotency is the executor's claim (email has no
    provider lookup), so already_sent() is always False and the claim guards it."""

    channel = "email"

    def __init__(self, sender=None, send=None):
        self._from = sender or "sid@syndai.ai"
        self._send = send or send_email  # injectable for tests

    def send(self, item: dict) -> str:
        payload = item["payload"]
        prev = payload.get("preview") or {}
        in_reply_to = payload.get("in_reply_to")
        subject = prev.get("subject") or ""
        # a warm reply threads via In-Reply-To (empty subject reuses the thread).
        # Without a message-id to thread on, a blank subject would ship a
        # subjectless email, so fall back to a real one.
        if not subject and not in_reply_to:
            subject = "re: your note"
        return self._send(
            subject, prev.get("body") or "",
            to=payload["lead"]["email"], sender=self._from, in_reply_to=in_reply_to,
        )

    def already_sent(self, item: dict) -> bool:
        return False  # claim-first is the exactly-once guard for outbound email


def fetch_replies(known_thread_ids: set[str], mailbox: str = "INBOX", limit: int = 50) -> list[dict]:
    """Fetch messages that reply to a known digest thread, parse each into the
    shape authenticate_reply + parse_reply expect. Searches by In-Reply-To /
    References headers (robust to self-replies landing already-read). Read-only."""
    user, pw = _creds()
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    m.login(user, pw)
    m.select(mailbox, readonly=True)
    seen: set[bytes] = set()
    out: list[dict] = []
    for mid in known_thread_ids:
        for header in ("IN-REPLY-TO", "REFERENCES"):
            typ, data = m.search(None, "HEADER", header, mid)
            if typ != "OK" or not data or not data[0]:
                continue
            for i in data[0].split():
                if i in seen:
                    continue
                seen.add(i)
                typ, raw = m.fetch(i, "(RFC822)")
                if raw and raw[0]:
                    msg = email.message_from_bytes(raw[0][1], policy=policy.default)
                    out.append(parse_reply_message(msg, known_thread_ids))
    m.logout()
    return out[:limit]


def parse_reply_message(msg, known_thread_ids: set[str]) -> dict:
    """Pure: an email.message.Message -> the reply-metadata + body dict."""
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    refs = msg.get("References") or ""
    known = in_reply_to in known_thread_ids or any(t in refs for t in known_thread_ids)
    authres = msg.get("Authentication-Results", "") or ""
    has_auth_results = bool(authres.strip())
    dkim_pass = "dkim=pass" in authres.lower()
    md = _DKIM_D.search(authres) or _DKIM_I.search(authres)
    dkim_domain = md.group(1).lower() if md else ""
    return {
        "from": parseaddr(msg.get("From", ""))[1].lower(),
        "dkim_pass": dkim_pass,
        "dkim_domain": dkim_domain,
        "has_auth_results": has_auth_results,
        "in_reply_to": in_reply_to,
        "known_thread": known,
        "body": _plain_body(msg),
    }


def _plain_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_content()
        return ""
    return msg.get_content() if msg.get_content_type() == "text/plain" else ""
