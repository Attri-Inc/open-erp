# Security Policy

## Supported versions

OpenERP is pre-1.0. Security fixes land on `main` and in the latest released
version only.

## Reporting a vulnerability

Please report security issues privately to **engineering@attri.ai** rather than
opening a public issue. Include the version or commit, what you did, and what
happened. We aim to acknowledge within three business days.

## Security posture

OpenERP is designed to run locally or on infrastructure you control.

- **No authentication.** v0.1 has no auth layer. Anyone who can reach the MCP
  server can read and write the books. Bind to localhost — the shipped
  `docker-compose.yml` publishes to `127.0.0.1` deliberately — and do not expose
  the port to a network you do not trust.
- **Ad-hoc SQL is read-only, twice over.** `run_query` rejects any statement that
  is not a single `SELECT`/`WITH` or that contains a mutating keyword, *and* runs
  it on a connection opened with `PRAGMA query_only=ON`, which the engine itself
  refuses to write through.
- **Parameterized everywhere else.** Every other query binds its values; no user
  input is interpolated into SQL.
- **Financial data.** The database holds a business's books. Treat the file as
  sensitive: encrypt the volume, back it up, and restrict filesystem access.
