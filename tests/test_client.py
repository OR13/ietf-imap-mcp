"""Client behavior tests against a fake in-memory IMAP server (no network)."""

import imaplib

import pytest

from ietf_imap_mcp.client import ImapError, IetfImapClient, IetfImapConfig


def make_raw(subject, frm, body, msgid, date="Tue, 17 Jun 2026 10:00:00 +0000"):
    return (
        f"Subject: {subject}\r\n"
        f"From: {frm}\r\n"
        f"Date: {date}\r\n"
        f"Message-ID: {msgid}\r\n"
        f"\r\n{body}\r\n"
    ).encode()


class FakeIMAP:
    """Mimics the subset of imaplib.IMAP4_SSL used by IetfImapClient.

    mailboxes: dict[name] -> list[(uid:int, raw:bytes)] in sequence order
    (index 0 == sequence number 1 == oldest).
    """

    def __init__(self, mailboxes):
        self.mailboxes = mailboxes
        self.selected = None
        self.logged_in = False
        self.shutdown_called = False
        self.commands: list[str] = []   # audit log for assertions

    def login(self, user, password):
        self.logged_in = True
        return ("OK", [b"Logged in"])

    def list(self, ref, pattern):
        self.commands.append(f"LIST {pattern}")
        lines = [f'(\\HasNoChildren) "." "{name}"'.encode() for name in self.mailboxes]
        return ("OK", lines)

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        self.selected = name
        # imaplib issues EXAMINE on the wire when readonly=True.
        self.commands.append(f"{'EXAMINE' if readonly else 'SELECT'} {name}")
        return ("OK", [str(len(self.mailboxes.get(name, []))).encode()])

    def uid(self, command, *args):
        self.commands.append(f"UID {command}")
        if command == "SEARCH":
            msgs = self.mailboxes.get(self.selected, [])
            ids = b" ".join(str(uid).encode() for uid, _ in msgs)
            return ("OK", [ids])
        if command == "FETCH":
            uid = int(args[0])
            spec = args[1]
            raw = self._raw(uid)
            if raw is None:
                return ("OK", [None])
            return ("OK", [(f"1 (UID {uid})".encode(), raw), b")"])
        raise AssertionError(f"unexpected uid command {command}")

    def fetch(self, seqset, spec):
        self.commands.append(f"FETCH {seqset} {spec}")
        low, high = (int(x) for x in seqset.split(":"))
        msgs = self.mailboxes.get(self.selected, [])
        lines = []
        for seq in range(low, high + 1):
            if 1 <= seq <= len(msgs):
                uid = msgs[seq - 1][0]
                lines.append(f"{seq} (UID {uid})".encode())
        return ("OK", lines)

    def logout(self):
        self.logged_in = False
        return ("BYE", [b"bye"])

    def shutdown(self):
        self.shutdown_called = True

    def _raw(self, uid):
        for msgs in self.mailboxes.values():
            for u, raw in msgs:
                if u == uid:
                    return raw
        return None


class FlakyIMAP(FakeIMAP):
    """A FakeIMAP whose data commands raise a connection-death error.

    Simulates the real failure: login + EXAMINE succeed, then the kept-alive
    socket is dead by the time a data command (SEARCH / FETCH) runs.
    """

    def __init__(self, mailboxes, *, fail_commands=(), exc=None):
        super().__init__(mailboxes)
        self.fail_commands = set(fail_commands)
        self.exc = exc if exc is not None else imaplib.IMAP4.abort("connection lost")
        self.shutdown_called = False

    def uid(self, command, *args):
        if command in self.fail_commands:
            raise self.exc
        return super().uid(command, *args)

    def fetch(self, seqset, spec):
        if "FETCH" in self.fail_commands:
            raise self.exc
        return super().fetch(seqset, spec)


@pytest.fixture
def fake_mailboxes():
    a2a = [
        (101 + i, make_raw(f"msg {i}", f"user{i}@example.org", f"body number {i}", f"<m{i}@x>"))
        for i in range(10)  # seq 1..10, uids 101..110, oldest→newest
    ]
    return {"agent2agent": a2a, "webbotauth": [(1, make_raw("hi", "a@b.c", "x", "<a@b>"))]}


@pytest.fixture
def client(fake_mailboxes):
    fake = FakeIMAP(fake_mailboxes)
    # No real sleeping in tests; no prefix so the fake's bare names match.
    cfg = IetfImapConfig(user="anonymous", email="test@example.org", min_interval=0.0, mailbox_prefix="")
    c = IetfImapClient(cfg, imap_factory=lambda: fake)
    c._fake = fake  # expose for assertions
    return c


def test_list_mailboxes(client):
    names = client.list_mailboxes()
    assert names == ["agent2agent", "webbotauth"]


def test_list_mailboxes_login_happened(client):
    client.list_mailboxes()
    assert client._fake.logged_in is True


def test_page_newest_first(client):
    res = client.page("agent2agent", offset=0, limit=3)
    assert res["total"] == 10
    assert res["returned"] == 3
    assert res["has_more"] is True
    # newest first → uids 110, 109, 108
    assert [m["uid"] for m in res["messages"]] == ["110", "109", "108"]


def test_page_second_page(client):
    res = client.page("agent2agent", offset=3, limit=3)
    assert [m["uid"] for m in res["messages"]] == ["107", "106", "105"]
    assert res["has_more"] is True


def test_page_last_page_no_more(client):
    res = client.page("agent2agent", offset=9, limit=3)
    assert [m["uid"] for m in res["messages"]] == ["101"]
    assert res["has_more"] is False


def test_page_beyond_end_is_empty(client):
    res = client.page("agent2agent", offset=99, limit=3)
    assert res["returned"] == 0
    assert res["messages"] == []
    assert res["has_more"] is False


def test_page_summaries_have_subject(client):
    res = client.page("agent2agent", offset=0, limit=2)
    assert res["messages"][0]["subject"] == "msg 9"  # uid 110 == seq 10 == "msg 9"


def test_search_returns_newest_first_and_caps(client):
    res = client.search("agent2agent", subject="msg", limit=4)
    assert res["total_matches"] == 10
    assert res["returned"] == 4
    assert res["truncated"] is True
    assert [m["uid"] for m in res["messages"]] == ["110", "109", "108", "107"]


def test_search_not_truncated_when_under_limit(client):
    res = client.search("webbotauth", limit=25)
    assert res["total_matches"] == 1
    assert res["truncated"] is False


def test_get_message_full_body(client):
    res = client.get_message("agent2agent", 105)
    assert res["uid"] == "105"
    assert res["mailbox"] == "agent2agent"
    assert res["subject"] == "msg 4"
    assert "body number 4" in res["body"]


def test_get_message_without_body(client):
    res = client.get_message("agent2agent", 105, include_body=False)
    assert "body" not in res


def test_examine_is_read_only(client):
    """The client must never issue SELECT — only EXAMINE."""
    client.page("agent2agent", 0, 2)
    assert any(c.startswith("EXAMINE") for c in client._fake.commands)
    assert not any(c.startswith("SELECT") for c in client._fake.commands)


def test_bare_name_gets_shared_folders_prefix(fake_mailboxes):
    """With the default prefix, a bare list name resolves under Shared Folders/."""
    msgs = fake_mailboxes["agent2agent"]
    fake = FakeIMAP({"Shared Folders/agent2agent": msgs})
    cfg = IetfImapConfig(user="anonymous", email="t@e.org", min_interval=0.0)  # default prefix
    c = IetfImapClient(cfg, imap_factory=lambda: fake)
    res = c.page("agent2agent", offset=0, limit=1)
    assert res["total"] == len(msgs)
    assert any(cmd == "EXAMINE Shared Folders/agent2agent" for cmd in fake.commands)


def test_full_path_passes_through(fake_mailboxes):
    """A name already containing '/' is not re-prefixed."""
    msgs = fake_mailboxes["agent2agent"]
    fake = FakeIMAP({"Shared Folders/agent2agent": msgs})
    cfg = IetfImapConfig(user="anonymous", email="t@e.org", min_interval=0.0)
    c = IetfImapClient(cfg, imap_factory=lambda: fake)
    res = c.page("Shared Folders/agent2agent", offset=0, limit=1)
    assert res["total"] == len(msgs)


def test_mailbox_selection_is_cached(client):
    """Re-paging the same mailbox should not re-EXAMINE it every time."""
    client.page("agent2agent", 0, 2)
    client.page("agent2agent", 2, 2)
    examines = [c for c in client._fake.commands if c.startswith("EXAMINE agent2agent")]
    assert len(examines) == 1


# --------------------------------------------------------------------------- #
# Connection self-healing: a dropped/corrupted socket must reconnect, not wedge.
# --------------------------------------------------------------------------- #

def _reconnecting_client(conns):
    """A client whose factory hands out the given connections in order."""
    it = iter(conns)
    cfg = IetfImapConfig(user="anonymous", email="t@e.org", min_interval=0.0, mailbox_prefix="")
    return IetfImapClient(cfg, imap_factory=lambda: next(it))


@pytest.mark.parametrize(
    "exc",
    [
        imaplib.IMAP4.abort("connection lost"),   # imaplib's mid-command drop
        BrokenPipeError("broken pipe"),           # OSError family
        OSError("[SSL: BAD_LENGTH] bad length"),  # the exact symptom we saw live
        EOFError("server hung up"),
    ],
)
def test_reconnects_after_dropped_connection(fake_mailboxes, exc):
    poisoned = FlakyIMAP(fake_mailboxes, fail_commands={"SEARCH"}, exc=exc)
    healthy = FakeIMAP(fake_mailboxes)
    client = _reconnecting_client([poisoned, healthy])

    res = client.search("agent2agent", subject="msg", limit=3)

    assert res["returned"] == 3                 # the call succeeded after reconnect
    assert poisoned.shutdown_called is True     # the dead connection was torn down
    assert healthy.logged_in is True            # a fresh connection logged in


def test_page_reconnects_after_dropped_connection(fake_mailboxes):
    poisoned = FlakyIMAP(fake_mailboxes, fail_commands={"FETCH"})
    healthy = FakeIMAP(fake_mailboxes)
    client = _reconnecting_client([poisoned, healthy])

    res = client.page("agent2agent", offset=0, limit=2)

    assert [m["uid"] for m in res["messages"]] == ["110", "109"]
    assert poisoned.shutdown_called is True


def test_persistent_connection_failure_raises_imaperror(fake_mailboxes):
    """If reconnect also fails, surface a clean ImapError (not a raw socket error)."""
    conns = [
        FlakyIMAP(fake_mailboxes, fail_commands={"SEARCH"}),
        FlakyIMAP(fake_mailboxes, fail_commands={"SEARCH"}),
    ]
    client = _reconnecting_client(conns)

    with pytest.raises(ImapError, match="reconnect failed"):
        client.search("agent2agent", subject="msg")


def test_command_error_is_not_retried(fake_mailboxes):
    """A command-level failure (missing uid) must not throw away the connection."""
    made = []

    def factory():
        f = FakeIMAP(fake_mailboxes)
        made.append(f)
        return f

    cfg = IetfImapConfig(user="anonymous", email="t@e.org", min_interval=0.0, mailbox_prefix="")
    client = IetfImapClient(cfg, imap_factory=factory)

    with pytest.raises(ImapError):
        client.get_message("agent2agent", 99999)  # no such uid

    assert len(made) == 1  # only one connection — no spurious reconnect


def test_recovers_on_next_call_after_reset(fake_mailboxes):
    """After a connection is reset, a subsequent independent call still works."""
    poisoned = FlakyIMAP(fake_mailboxes, fail_commands={"SEARCH"})
    healthy = FakeIMAP(fake_mailboxes)
    client = _reconnecting_client([poisoned, healthy])

    client.search("agent2agent", subject="msg", limit=1)  # triggers reconnect
    res = client.get_message("agent2agent", 105)           # reuses healthy conn
    assert res["uid"] == "105"
