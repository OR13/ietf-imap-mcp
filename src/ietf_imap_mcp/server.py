"""FastMCP server exposing read-only IETF mailing-list archive navigation.

Tools:
* ``list_mailing_lists`` — discover list mailboxes on imap.ietf.org.
* ``search_list``        — search a list by subject/from/text/date, newest-first.
* ``page_list``          — page through a list's messages, newest-first.
* ``get_message``        — fetch one message (headers + plain-text body) by UID.

All access is read-only (``EXAMINE``), rate-limited, and result-capped. See
``client.py`` for the resource-respect guarantees.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .client import IetfImapClient, IetfImapConfig, ImapError

mcp = FastMCP("ietf-imap")

_client: IetfImapClient | None = None


def get_client() -> IetfImapClient:
    global _client
    if _client is None:
        _client = IetfImapClient(IetfImapConfig())
    return _client


@mcp.tool()
def list_mailing_lists(pattern: str = "*", limit: int = 100) -> dict:
    """List IETF mailing-list mailboxes available over IMAP.

    Args:
        pattern: IMAP LIST pattern, e.g. ``"agent*"`` or ``"*"``.
        limit: Maximum mailbox names to return (hard-capped at 100).
    """
    try:
        names = get_client().list_mailboxes(pattern=pattern, limit=limit)
        return {"pattern": pattern, "count": len(names), "mailboxes": names}
    except (ImapError, OSError) as e:
        return {"error": str(e)}


@mcp.tool()
def search_list(
    list_name: str,
    subject: str | None = None,
    from_addr: str | None = None,
    text: str | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: int = 25,
) -> dict:
    """Search a mailing list, returning matching message summaries (newest first).

    Args:
        list_name: Mailbox / list name, e.g. ``"agent2agent"``.
        subject: Substring to match in the Subject header.
        from_addr: Substring to match in the From header.
        text: Free-text search across the whole message.
        since: Only messages on/after this ISO date (``YYYY-MM-DD``).
        before: Only messages before this ISO date (``YYYY-MM-DD``).
        limit: Max summaries to return (default 25, hard-capped at 100).
    """
    try:
        return get_client().search(
            list_name, subject=subject, from_addr=from_addr, text=text,
            since=since, before=before, limit=limit,
        )
    except (ImapError, OSError, ValueError) as e:
        return {"error": str(e)}


@mcp.tool()
def page_list(list_name: str, offset: int = 0, limit: int = 25) -> dict:
    """Page through a list's messages, newest first.

    Args:
        list_name: Mailbox / list name, e.g. ``"agent2agent"``.
        offset: How many messages back from the newest to start (0 = newest).
        limit: Page size (default 25, hard-capped at 100).
    """
    try:
        return get_client().page(list_name, offset=offset, limit=limit)
    except (ImapError, OSError) as e:
        return {"error": str(e)}


@mcp.tool()
def get_message(
    list_name: str,
    uid: str,
    include_body: bool = True,
    max_body_chars: int = 20000,
) -> dict:
    """Fetch a single message by UID (from a prior search/page result).

    Args:
        list_name: Mailbox / list name, e.g. ``"agent2agent"``.
        uid: The message UID returned by ``search_list`` / ``page_list``.
        include_body: Include the plain-text body (default True).
        max_body_chars: Truncate the body to this many characters.
    """
    try:
        return get_client().get_message(
            list_name, uid, include_body=include_body, max_body_chars=max_body_chars
        )
    except (ImapError, OSError) as e:
        return {"error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
