# Krita MCP v4

This v4 implementation is built from
[nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp).

This canonical NixOS/Krita 6 bridge is a strict protocol-v4 implementation.
It contains:

- a PyQt6 Krita plugin with loopback HTTP endpoints;
- an asynchronous, typed FastMCP 4.0.3 server;
- contract and security tests that run without importing Krita.

There is no v3 or pixel-paint compatibility surface.

## Install

Copy `krita-plugin/kritamcp/` and `krita-plugin/kritamcp.desktop` into
Krita's Python plugin directory, enable “Krita MCP Bridge” in the Python Plugin
Manager, and restart Krita. Install the wrapper environment with:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Configure the MCP client to run this repository's `server.py`.

## Security and transport

The plugin binds only to `127.0.0.1:5678`. At startup it creates a random
256-bit bearer token at `$XDG_RUNTIME_DIR/krita-mcp/token` with mode `0600`.
The token is required for `/v4/capabilities` and `/v4/command` and is never
logged. `/health` is the only unauthenticated endpoint.

The bridge validates request size, action names, queue depth, revisions,
request-id retries, stroke batch limits, and allowlisted resolved paths.
One session owns the writer lease; disconnect or timeout releases it without
closing the document.

## Protocol

See [docs/API.md](docs/API.md). The command envelope contains protocol version,
request/session IDs, optional document ID, expected revision for every write,
an action, and parameters. Mutating replies are cached by request ID within the
session, making retries idempotent.

Documents created by v4 are RGBA/U16, 300 PPI, using
`sRGB-elle-V2-srgbtrc.icc`. KRA save and PNG export are separate operations.
Projection capture explicitly converts to 8-bit sRGB.

Painting accepts one native `paintLine` per line stroke or one native cubic
`paintPath` per curved stroke. Every stroke supplies complete brush state.
Line pressure is supported; cubic pressure, tilt, rotation, and speed are
rejected. Candidate transactions use ephemeral paint layers and never snapshot
a full U16 canvas.

## Verification

```bash
python -m unittest discover -s tests -v
```

Headless tests cover schemas, authentication, idempotency, revision conflicts,
path/symlink traversal, resource identity, unsupported sensors, limits,
transaction ownership, and source-level native painting invariants. Live Krita
verification requires the user-started application and plugin.

## License

MIT
