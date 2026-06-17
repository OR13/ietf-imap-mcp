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

## Configuration

All configuration is via environment variables — **no credentials are ever
stored in the code or the repo**.

| Variable | Purpose | Default |
|----------|---------|---------|
| `IETF_IMAP_USER` | `anonymous`, or your Datatracker username | `anonymous` |
| `IETF_IMAP_EMAIL` | Email used as the password for **anonymous** access | — |
| `IETF_IMAP_PASSWORD` | Datatracker password (authenticated access) — plaintext | — |
| `IETF_IMAP_PASSWORD_CMD` | Shell command whose stdout is the password (e.g. `op read …`) | — |
| `IETF_IMAP_PASSWORD_FILE` | Path to a file containing the password | — |
| `IETF_IMAP_MIN_INTERVAL` | Minimum seconds between IMAP commands | `1.0` |
| `IETF_IMAP_MAILBOX_PREFIX` | Prefix for bare list names | `Shared Folders/` |

See [`.env.example`](.env.example) for a copy-paste starting point.

## Authentication

Per the [IETF lists page](https://www.ietf.org/participate/lists/), the IMAP
service supports two modes.

### Anonymous (recommended for public lists)

IMAP anonymous login uses your **email address in the password field** — this is
not a secret. This is all you need for public list archives:

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

### Authenticated (Datatracker — only when anonymous is not possible)

Set `IETF_IMAP_USER` to your Datatracker username and supply the password.
The password is resolved in this order of preference:
`IETF_IMAP_PASSWORD` → `IETF_IMAP_PASSWORD_CMD` → `IETF_IMAP_PASSWORD_FILE`.

> ⚠️ **Keep the secret out of the config file.** Values in an MCP client's
> `env` block sit in **plaintext on disk** (and often sync to the cloud). Prefer
> `IETF_IMAP_PASSWORD_CMD` (resolve from a secret manager at launch) or
> `IETF_IMAP_PASSWORD_FILE` (a `chmod 600` file). Avoid `IETF_IMAP_PASSWORD`
> except for throwaway local testing, and never put a real password in a file
> you commit.

**Recommended — resolve from a secret manager (no secret on disk):**

```json
{
  "mcpServers": {
    "ietf-imap": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ietf-imap-mcp", "python", "-m", "ietf_imap_mcp"],
      "env": {
        "IETF_IMAP_USER": "your-datatracker-username",
        "IETF_IMAP_PASSWORD_CMD": "op read \"op://Private/IETF Datatracker/password\""
      }
    }
  }
}
```

(`op` is the 1Password CLI; substitute `pass`, `gopass`, `security find-generic-password`,
`vault kv get`, etc.)

**Alternative — a permission-restricted file:**

```bash
install -m 600 /dev/null ~/.config/ietf-imap/datatracker.pw
printf '%s' 'your-password' > ~/.config/ietf-imap/datatracker.pw
```

```json
"env": {
  "IETF_IMAP_USER": "your-datatracker-username",
  "IETF_IMAP_PASSWORD_FILE": "~/.config/ietf-imap/datatracker.pw"
}
```

## Run

```bash
uv run ietf-imap-mcp          # stdio MCP server
# or
uv run python -m ietf_imap_mcp
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
