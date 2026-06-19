"""Read-only IMAP client for the IETF mailing-list archives (imap.ietf.org:993).

Designed to be a good citizen of shared IETF infrastructure:

* **Read-only.** Mailboxes are opened with ``EXAMINE`` (never ``SELECT``), so the
  server can never flag, move, or delete anything.
* **Rate limited.** A minimum interval is enforced between IMAP commands.
* **Bounded.** Result counts are hard-capped so a single tool call can never ask
  the server for thousands of messages.
* **Connection reuse.** One TLS connection is kept open and shared rather than
  reconnecting per call.

The pure helpers (:func:`build_search_criteria`, :func:`compute_page_range`,
:func:`parse_email`) contain the logic worth testing and need no network.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from email.header import decode_header, make_header
from typing import Iterable

HOST = "imap.ietf.org"
PORT = 993

# Hard ceiling on how many messages any single call may request, regardless of
# what the caller asks for. Protects the shared server from us.
MAX_RESULTS_CEILING = 100
DEFAULT_MIN_INTERVAL = 1.0  # seconds between IMAP commands


# --------------------------------------------------------------------------- #
# Pure helpers (no network — unit tested directly)
# --------------------------------------------------------------------------- #

_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _to_imap_date(value: str | date | datetime) -> str:
    """Convert an ISO date string / date / datetime to IMAP ``DD-Mon-YYYY``."""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        # Accept "YYYY-MM-DD" (and tolerate a trailing time component).
        text = str(value).strip()
        d = datetime.strptime(text[:10], "%Y-%m-%d").date()
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _quote(value: str) -> str:
    """Quote a string for an IMAP search argument."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_search_criteria(
    subject: str | None = None,
    from_addr: str | None = None,
    text: str | None = None,
    since: str | date | datetime | None = None,
    before: str | date | datetime | None = None,
) -> tuple[str | None, list[str]]:
    """Build IMAP SEARCH criteria.

    Returns ``(charset, criteria)``. ``charset`` is ``"UTF-8"`` when any free-text
    argument is present (so non-ASCII works), otherwise ``None``. When no criteria
    are supplied the search is ``["ALL"]``.
    """
    criteria: list[str] = []
    needs_charset = False

    if subject:
        criteria += ["SUBJECT", _quote(subject)]
        needs_charset = needs_charset or not subject.isascii()
    if from_addr:
        criteria += ["FROM", _quote(from_addr)]
        needs_charset = needs_charset or not from_addr.isascii()
    if text:
        criteria += ["TEXT", _quote(text)]
        needs_charset = needs_charset or not text.isascii()
    if since:
        criteria += ["SINCE", _to_imap_date(since)]
    if before:
        criteria += ["BEFORE", _to_imap_date(before)]

    if not criteria:
        criteria = ["ALL"]
    charset = "UTF-8" if needs_charset else None
    return charset, criteria


def compute_page_range(total: int, offset: int, limit: int) -> tuple[int, int] | None:
    """Sequence range for newest-first paging over ``total`` messages.

    IMAP sequence numbers run ``1..total`` oldest→newest. ``offset`` counts back
    from the newest message (0 = newest). Returns an inclusive ``(low, high)``
    sequence range to fetch, or ``None`` when the page is empty / out of range.
    Caller should reverse the fetched results to present newest-first.
    """
    if total <= 0 or limit <= 0 or offset < 0:
        return None
    high = total - offset
    if high < 1:
        return None
    low = max(1, high - limit + 1)
    return low, high


def _header(msg: email.message.Message, name: str) -> str:
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _extract_text_body(msg: email.message.Message, max_chars: int) -> str:
    """Return the best-effort plain-text body, truncated to ``max_chars``."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                parts.append(_decode_part(part))
        if not parts:  # fall back to any text/* part
            for part in msg.walk():
                if part.get_content_maintype() == "text" and not part.get_filename():
                    parts.append(_decode_part(part))
    else:
        parts.append(_decode_part(msg))

    body = "\n".join(p for p in parts if p).strip()
    if max_chars and len(body) > max_chars:
        body = body[:max_chars] + f"\n\n[... truncated at {max_chars} characters ...]"
    return body


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")


def parse_email(raw: bytes, include_body: bool = True, max_body_chars: int = 20000) -> dict:
    """Parse raw RFC822 bytes into a JSON-serializable dict."""
    msg = email.message_from_bytes(raw)
    out = {
        "subject": _header(msg, "Subject"),
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "cc": _header(msg, "Cc"),
        "date": _header(msg, "Date"),
        "message_id": _header(msg, "Message-ID"),
        "in_reply_to": _header(msg, "In-Reply-To"),
    }
    if include_body:
        out["body"] = _extract_text_body(msg, max_body_chars)
    return out


def parse_header_only(raw: bytes) -> dict:
    """Parse a small header-only summary (subject/from/date/message-id)."""
    msg = email.message_from_bytes(raw)
    return {
        "subject": _header(msg, "Subject"),
        "from": _header(msg, "From"),
        "date": _header(msg, "Date"),
        "message_id": _header(msg, "Message-ID"),
    }


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Enforce a minimum interval between successive calls (thread-safe)."""

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL, *, _clock=time.monotonic, _sleep=time.sleep):
        self.min_interval = max(0.0, min_interval)
        self._last = 0.0
        self._lock = threading.Lock()
        self._clock = _clock
        self._sleep = _sleep

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            elapsed = now - self._last
            if self._last and elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
            self._last = self._clock()


# --------------------------------------------------------------------------- #
# IMAP client
# --------------------------------------------------------------------------- #

def _read_password_file(path: str) -> str:
    with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _run_password_cmd(cmd: str) -> str:
    """Run a shell command and return its stdout (stripped) as the password.

    Lets the secret stay in a manager (e.g. ``op read op://vault/item/password``)
    instead of sitting in plaintext inside an MCP client config file.
    """
    import subprocess

    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@dataclass
class IetfImapConfig:
    user: str = field(default_factory=lambda: os.environ.get("IETF_IMAP_USER", "anonymous"))
    password: str = field(default_factory=lambda: os.environ.get("IETF_IMAP_PASSWORD", ""))
    password_file: str = field(default_factory=lambda: os.environ.get("IETF_IMAP_PASSWORD_FILE", ""))
    password_cmd: str = field(default_factory=lambda: os.environ.get("IETF_IMAP_PASSWORD_CMD", ""))
    email: str = field(default_factory=lambda: os.environ.get("IETF_IMAP_EMAIL", ""))
    min_interval: float = field(
        default_factory=lambda: float(os.environ.get("IETF_IMAP_MIN_INTERVAL", DEFAULT_MIN_INTERVAL))
    )
    # On imap.ietf.org the lists live under "Shared Folders/<list>". A bare list
    # name (no "/") gets this prefix; full paths are passed through unchanged.
    mailbox_prefix: str = field(
        default_factory=lambda: os.environ.get("IETF_IMAP_MAILBOX_PREFIX", "Shared Folders/")
    )

    def resolved_password(self) -> str:
        """Resolve the password used at login.

        * Anonymous access uses an **email address** as the password (not a
          secret) — taken from ``IETF_IMAP_PASSWORD`` or ``IETF_IMAP_EMAIL``.
        * Authenticated (Datatracker) access resolves the real secret in order
          of preference, favoring forms that keep it out of the MCP config file:
          ``IETF_IMAP_PASSWORD`` → ``IETF_IMAP_PASSWORD_CMD`` → ``IETF_IMAP_PASSWORD_FILE``.
        """
        if self.user == "anonymous":
            return self.password or self.email or "anonymous@example.org"
        if self.password:
            return self.password
        if self.password_cmd:
            return _run_password_cmd(self.password_cmd)
        if self.password_file:
            return _read_password_file(self.password_file)
        raise ImapError(
            "Authenticated access requires one of IETF_IMAP_PASSWORD, "
            "IETF_IMAP_PASSWORD_CMD, or IETF_IMAP_PASSWORD_FILE."
        )


def clamp_results(requested: int, default: int = 25) -> int:
    if requested is None or requested <= 0:
        requested = default
    return min(requested, MAX_RESULTS_CEILING)


# Errors that mean the kept-alive TLS connection is dead and must be rebuilt
# rather than reused. ``imaplib.IMAP4.abort`` is raised on a lost connection
# mid-command; ``OSError`` covers broken pipes, connection resets, and
# ``ssl.SSLError`` (e.g. ``BAD_LENGTH`` on a corrupted socket); ``EOFError`` is
# raised when the server hangs up. Plain ``imaplib.IMAP4.error`` is deliberately
# excluded — those are command-level failures (bad mailbox, auth) that a
# reconnect would not fix.
_CONNECTION_ERRORS = (imaplib.IMAP4.abort, OSError, EOFError)


class IetfImapClient:
    """Thin, read-only, rate-limited wrapper over ``imaplib.IMAP4_SSL``."""

    def __init__(self, config: IetfImapConfig | None = None, *, imap_factory=None):
        self.config = config or IetfImapConfig()
        self._rl = RateLimiter(self.config.min_interval)
        self._lock = threading.RLock()
        self._conn = None
        self._selected = None
        # Injectable for tests; defaults to a real TLS connection.
        self._imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL(HOST, PORT))

    # -- connection lifecycle ------------------------------------------------ #

    def _connect(self):
        if self._conn is not None:
            return self._conn
        conn = self._imap_factory()
        conn.login(self.config.user, self.config.resolved_password())
        self._conn = conn
        self._selected = None
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.logout()
                except Exception:
                    # The connection may already be dead; tearing it down
                    # cleanly is best-effort, not something to raise over.
                    pass
                finally:
                    self._conn = None
                    self._selected = None

    def _reset_connection(self) -> None:
        """Discard the current connection so the next call reconnects.

        Tears down the socket without a LOGOUT roundtrip (which would itself
        fail or hang on an already-dead connection).
        """
        conn, self._conn, self._selected = self._conn, None, None
        if conn is not None:
            try:
                conn.shutdown()
            except Exception:
                pass

    def _execute(self, op):
        """Run ``op()`` under the lock, reconnecting once if the link is dead.

        The kept-alive TLS connection can be dropped by the server or corrupted
        between calls. On a connection-level error we throw the stale connection
        away and retry exactly once on a fresh one; a second failure is surfaced
        as an :class:`ImapError`.
        """
        with self._lock:
            try:
                return op()
            except _CONNECTION_ERRORS:
                self._reset_connection()
                try:
                    return op()
                except _CONNECTION_ERRORS as exc:
                    self._reset_connection()
                    raise ImapError(
                        f"IMAP connection lost and reconnect failed: {exc}"
                    ) from exc

    def _normalize(self, name: str) -> str:
        """Map a bare list name to its full mailbox path (idempotent)."""
        name = name.strip().strip('"')
        prefix = self.config.mailbox_prefix
        if prefix and "/" not in name:
            return prefix + name
        return name

    def _examine(self, mailbox: str) -> int:
        """Open a mailbox read-only; return the message count."""
        conn = self._connect()
        if self._selected != mailbox:
            self._rl.wait()
            # readonly=True makes imaplib issue EXAMINE (never modifies the mailbox).
            typ, data = conn.select(_mailbox_arg(mailbox), readonly=True)
            if typ != "OK":
                raise ImapError(f"Could not open mailbox {mailbox!r}: {_first(data)}")
            self._selected = mailbox
            self._count = int(_first(data) or 0)
        return self._count

    # -- operations ---------------------------------------------------------- #

    def list_mailboxes(self, pattern: str = "*", limit: int = MAX_RESULTS_CEILING) -> list[str]:
        def _op():
            conn = self._connect()
            self._rl.wait()
            # imap.ietf.org requires quoted reference + pattern.
            typ, data = conn.list('""', _quote(pattern))
            if typ != "OK":
                raise ImapError(f"LIST failed: {_first(data)}")
            names = [_parse_list_line(line) for line in data if line]
            names = [n for n in names if n]
            return sorted(set(names))[:limit]

        return self._execute(_op)

    def search(
        self,
        mailbox: str,
        *,
        subject=None,
        from_addr=None,
        text=None,
        since=None,
        before=None,
        limit: int = 25,
    ) -> dict:
        limit = clamp_results(limit)
        mailbox = self._normalize(mailbox)
        charset, criteria = build_search_criteria(subject, from_addr, text, since, before)

        def _op():
            total = self._examine(mailbox)
            conn = self._connect()
            self._rl.wait()
            typ, data = conn.uid("SEARCH", charset, *criteria) if charset else conn.uid("SEARCH", *criteria)
            if typ != "OK":
                raise ImapError(f"SEARCH failed: {_first(data)}")
            uids = (_first(data) or b"").split()
            # newest first, then cap
            uids = list(reversed(uids))
            shown = uids[:limit]
            messages = [self._summary(mailbox, uid) for uid in shown]
            return {
                "mailbox": mailbox,
                "total_matches": len(uids),
                "returned": len(messages),
                "truncated": len(uids) > len(messages),
                "messages": messages,
            }

        return self._execute(_op)

    def page(self, mailbox: str, offset: int = 0, limit: int = 25) -> dict:
        limit = clamp_results(limit)
        mailbox = self._normalize(mailbox)

        def _op():
            total = self._examine(mailbox)
            rng = compute_page_range(total, offset, limit)
            if rng is None:
                return {
                    "mailbox": mailbox, "total": total, "offset": offset,
                    "limit": limit, "returned": 0, "has_more": False, "messages": [],
                }
            low, high = rng
            conn = self._connect()
            self._rl.wait()
            typ, data = conn.fetch(f"{low}:{high}", "(UID)")
            if typ != "OK":
                raise ImapError(f"FETCH (UID) failed: {_first(data)}")
            uids = _parse_uids(data)
            uids = list(reversed(uids))  # newest first
            messages = [self._summary(mailbox, uid) for uid in uids]
            return {
                "mailbox": mailbox,
                "total": total,
                "offset": offset,
                "limit": limit,
                "returned": len(messages),
                "has_more": low > 1,
                "messages": messages,
            }

        return self._execute(_op)

    def get_message(self, mailbox: str, uid: str | int, include_body=True, max_body_chars=20000) -> dict:
        mailbox = self._normalize(mailbox)

        def _op():
            self._examine(mailbox)
            conn = self._connect()
            self._rl.wait()
            typ, data = conn.uid("FETCH", str(uid), "(RFC822)")
            if typ != "OK":
                raise ImapError(f"FETCH failed for uid {uid}: {_first(data)}")
            raw = _first_rfc822(data)
            if raw is None:
                raise ImapError(f"No message found for uid {uid} in {mailbox!r}")
            parsed = parse_email(raw, include_body=include_body, max_body_chars=max_body_chars)
            parsed["uid"] = str(uid)
            parsed["mailbox"] = mailbox
            return parsed

        return self._execute(_op)

    def _summary(self, mailbox: str, uid: bytes | str) -> dict:
        conn = self._connect()
        self._rl.wait()
        typ, data = conn.uid("FETCH", _as_str(uid), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
        summary = {"uid": _as_str(uid)}
        if typ == "OK":
            raw = _first_rfc822(data)
            if raw is not None:
                summary.update(parse_header_only(raw))
        return summary


# --------------------------------------------------------------------------- #
# Small parsing utilities for imaplib's awkward response shapes
# --------------------------------------------------------------------------- #

class ImapError(RuntimeError):
    pass


def _as_str(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _first(data):
    return data[0] if data else None


def _mailbox_arg(mailbox: str) -> str:
    # Quote mailbox names so list names with odd characters are handled.
    return '"' + mailbox.replace('"', '\\"') + '"'


def _parse_list_line(line) -> str:
    """Extract the mailbox name from an IMAP LIST response line."""
    text = line.decode() if isinstance(line, bytes) else line
    # Format: (\flags) "delim" "name"   — name may be quoted or atom.
    if '"' in text:
        # last quoted token is the name
        parts = text.split('"')
        if len(parts) >= 2:
            return parts[-2]
    return text.rsplit(" ", 1)[-1].strip()


def _parse_uids(data) -> list[str]:
    """Extract UIDs from a ``FETCH ... (UID n)`` response."""
    import re
    uids: list[str] = []
    for item in data:
        text = item.decode() if isinstance(item, (bytes, bytearray)) else (item[0].decode() if isinstance(item, tuple) else str(item))
        m = re.search(r"UID (\d+)", text)
        if m:
            uids.append(m.group(1))
    return uids


def _first_rfc822(data) -> bytes | None:
    """imaplib returns FETCH bodies as tuples (envelope, payload)."""
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and item[1] is not None:
            return item[1]
    return None
