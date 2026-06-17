"""Unit tests for the pure helpers (no network)."""

import pytest

from ietf_imap_mcp.client import (
    RateLimiter,
    build_search_criteria,
    clamp_results,
    compute_page_range,
    parse_email,
    parse_header_only,
)


# --- build_search_criteria ------------------------------------------------- #

def test_no_criteria_defaults_to_all():
    charset, criteria = build_search_criteria()
    assert charset is None
    assert criteria == ["ALL"]


def test_subject_from_text_criteria():
    charset, criteria = build_search_criteria(subject="charter", from_addr="rosenberg")
    assert charset is None
    assert criteria == ["SUBJECT", '"charter"', "FROM", '"rosenberg"']


def test_text_unicode_sets_utf8_charset():
    charset, criteria = build_search_criteria(text="café")
    assert charset == "UTF-8"
    assert criteria == ["TEXT", '"café"']


def test_date_conversion():
    _, criteria = build_search_criteria(since="2026-01-15", before="2026-06-17")
    assert "SINCE" in criteria and "15-Jan-2026" in criteria
    assert "BEFORE" in criteria and "17-Jun-2026" in criteria


def test_quoting_escapes_quotes():
    _, criteria = build_search_criteria(subject='say "hi"')
    assert criteria == ["SUBJECT", '"say \\"hi\\""']


# --- compute_page_range ---------------------------------------------------- #

@pytest.mark.parametrize(
    "total,offset,limit,expected",
    [
        (10, 0, 3, (8, 10)),    # newest 3
        (10, 3, 3, (5, 7)),     # next page back
        (10, 9, 3, (1, 1)),     # tail, clamped low
        (10, 8, 5, (1, 2)),     # limit overshoots start → clamp to 1
        (10, 10, 3, None),      # offset past the start
        (0, 0, 5, None),        # empty mailbox
        (10, 0, 0, None),       # zero limit
        (10, -1, 3, None),      # negative offset
    ],
)
def test_compute_page_range(total, offset, limit, expected):
    assert compute_page_range(total, offset, limit) == expected


# --- clamp_results --------------------------------------------------------- #

def test_clamp_results():
    assert clamp_results(1000) == 100      # ceiling
    assert clamp_results(10) == 10         # passthrough
    assert clamp_results(0) == 25          # default for non-positive
    assert clamp_results(-5) == 25
    assert clamp_results(None) == 25


# --- parse_email ----------------------------------------------------------- #

RAW = (
    b"Subject: Re: Proposed charter text\r\n"
    b"From: Jane Doe <jane@example.org>\r\n"
    b"To: agent2agent@ietf.org\r\n"
    b"Date: Tue, 17 Jun 2026 10:00:00 +0000\r\n"
    b"Message-ID: <abc123@example.org>\r\n"
    b"\r\n"
    b"I support the charter as written.\r\n"
)


def test_parse_email_headers_and_body():
    msg = parse_email(RAW)
    assert msg["subject"] == "Re: Proposed charter text"
    assert msg["from"] == "Jane Doe <jane@example.org>"
    assert msg["message_id"] == "<abc123@example.org>"
    assert "support the charter" in msg["body"]


def test_parse_email_body_truncation():
    big = b"Subject: x\r\nFrom: a@b.c\r\n\r\n" + (b"A" * 500)
    msg = parse_email(big, max_body_chars=100)
    assert "truncated at 100 characters" in msg["body"]
    assert msg["body"].count("A") == 100


def test_parse_email_can_skip_body():
    msg = parse_email(RAW, include_body=False)
    assert "body" not in msg


def test_parse_encoded_header():
    raw = (
        b"Subject: =?utf-8?B?Q2Fmw6kgY2hhcnRlcg==?=\r\n"
        b"From: a@b.c\r\n\r\nbody\r\n"
    )
    assert parse_email(raw)["subject"] == "Café charter"


def test_parse_header_only():
    h = parse_header_only(RAW)
    assert set(h) == {"subject", "from", "date", "message_id"}
    assert h["subject"] == "Re: Proposed charter text"


# --- RateLimiter ----------------------------------------------------------- #

def test_rate_limiter_first_call_no_sleep():
    slept: list[float] = []
    clock = [100.0]
    rl = RateLimiter(2.0, _clock=lambda: clock[0], _sleep=slept.append)
    rl.wait()
    assert slept == []


def test_rate_limiter_enforces_interval():
    slept: list[float] = []
    clock = [100.0]
    rl = RateLimiter(2.0, _clock=lambda: clock[0], _sleep=slept.append)
    rl.wait()             # establishes last=100
    clock[0] = 101.0      # only 1s elapsed
    rl.wait()             # must sleep the remaining 1s
    assert slept == [pytest.approx(1.0)]


def test_rate_limiter_no_sleep_when_enough_elapsed():
    slept: list[float] = []
    clock = [100.0]
    rl = RateLimiter(2.0, _clock=lambda: clock[0], _sleep=slept.append)
    rl.wait()
    clock[0] = 105.0      # plenty elapsed
    rl.wait()
    assert slept == []
