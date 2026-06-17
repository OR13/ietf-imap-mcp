# ietf-imap-mcp

An [MCP](https://modelcontextprotocol.io) server for navigating **IETF
mailing-list archives** over the IETF IMAP service
(`imap.ietf.org:993`). It lets an MCP-capable assistant search, page through,
and read list traffic — handy when chairing a working group or BoF and you need
to track what the list is actually saying.

> Built to be a **good citizen** of shared IETF infrastructure. See
> [Respecting IETF resources](#respecting-ietf-resources).

## Tools

| Tool | What it does |
|------|--------------|
| `list_mailing_lists(pattern, limit)` | Discover list mailboxes (IMAP `LIST`). |
| `search_list(list_name, subject, from_addr, text, since, before, limit)` | Search a list; returns message summaries, newest first. |
| `page_list(list_name, offset, limit)` | Page through a list's messages, newest first. |
| `get_message(list_name, uid, include_body, max_body_chars)` | Fetch one message (headers + plain-text body) by UID. |

## Access & configuration

Per the [IETF lists page](https://www.ietf.org/participate/lists/), the IMAP
service supports two modes. Configure via environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `IETF_IMAP_USER` | `anonymous`, or your Datatracker username for authenticated access | `anonymous` |
| `IETF_IMAP_PASSWORD` | For authenticated access, your Datatracker password. **For anonymous access, your email address** (IMAP uses the password field for it). | — |
| `IETF_IMAP_EMAIL` | Email used as the anonymous password if `IETF_IMAP_PASSWORD` is unset | — |
| `IETF_IMAP_MIN_INTERVAL` | Minimum seconds between IMAP commands | `1.0` |

**Anonymous access is enough for public list archives** — use
`IETF_IMAP_USER=anonymous` and put your email in `IETF_IMAP_EMAIL`. Only use
authenticated (Datatracker) access if you need non-public lists; store that
password in a secret manager, never in source or shell history.

## Run

```bash
uv run ietf-imap-mcp          # stdio MCP server
# or
uv run python -m ietf_imap_mcp
```

### Example MCP client config

```json
{
  "mcpServers": {
    "ietf-imap": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ietf-imap-mcp", "python", "-m", "ietf_imap_mcp"],
      "env": { "IETF_IMAP_USER": "anonymous", "IETF_IMAP_EMAIL": "you@example.org" }
    }
  }
}
```

## Respecting IETF resources

The IMAP server is shared infrastructure. This server:

- **Read-only** — mailboxes are opened with `EXAMINE`, never `SELECT`; it cannot
  flag, move, or delete anything.
- **Rate-limited** — a minimum interval (default 1s) is enforced between IMAP
  commands.
- **Bounded** — result counts are hard-capped (≤ 100 per call) so one call can't
  pull thousands of messages.
- **Connection-reusing** — one TLS connection is kept open rather than
  reconnecting per call.

Please don't lower the interval or remove the caps to hammer the service. For
bulk needs, use [rsync archive downloads](https://www.ietf.org/participate/lists/)
instead.

## Development

```bash
uv run pytest        # tests are fully mocked — no network, no live IMAP
```

## License

[MIT](LICENSE) © 2026 Orie Steele
