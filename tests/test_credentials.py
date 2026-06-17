"""Tests for password/credential resolution (no secrets in code or tests)."""

import pytest

from ietf_imap_mcp.client import IetfImapConfig, ImapError


def cfg(**kw):
    # Pass every field explicitly so ambient env vars never leak into tests.
    base = dict(
        user="anonymous", password="", password_file="", password_cmd="",
        email="", min_interval=0.0, mailbox_prefix="",
    )
    base.update(kw)
    return IetfImapConfig(**base)


def test_anonymous_uses_email():
    assert cfg(user="anonymous", email="me@example.org").resolved_password() == "me@example.org"


def test_anonymous_password_field_also_accepted_as_email():
    assert cfg(user="anonymous", password="me@example.org").resolved_password() == "me@example.org"


def test_anonymous_default_when_nothing_set():
    assert cfg(user="anonymous").resolved_password() == "anonymous@example.org"


def test_authenticated_uses_password():
    assert cfg(user="alice", password="s3cret").resolved_password() == "s3cret"


def test_authenticated_password_cmd():
    c = cfg(user="alice", password_cmd="echo from-cmd")
    assert c.resolved_password() == "from-cmd"  # trailing newline stripped


def test_authenticated_password_file(tmp_path):
    p = tmp_path / "secret.txt"
    p.write_text("from-file\n")
    assert cfg(user="alice", password_file=str(p)).resolved_password() == "from-file"


def test_authenticated_precedence_password_over_cmd_over_file(tmp_path):
    p = tmp_path / "secret.txt"
    p.write_text("file-val")
    # password wins
    assert cfg(user="a", password="pw", password_cmd="echo cmd", password_file=str(p)).resolved_password() == "pw"
    # cmd beats file
    assert cfg(user="a", password_cmd="echo cmd", password_file=str(p)).resolved_password() == "cmd"


def test_authenticated_requires_a_secret():
    with pytest.raises(ImapError):
        cfg(user="alice").resolved_password()
