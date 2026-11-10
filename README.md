# crono-mcp (read-only Cronometer MCP)

Lets Claude read your Cronometer diary using the session from your Chrome login.
**Read-only by construction**: the client can send only two fixed RPCs
(`authenticate`, `generateAuthorizationToken`) plus `GET /export`. See `src/crono_mcp/client.py`.

Protocol adapted from [cphoskins/cronometer-mcp](https://github.com/cphoskins/cronometer-mcp) (MIT; see `THIRD_PARTY_LICENSE_cronometer-mcp`).

## Tools
| Tool | What it returns |
|---|---|
| `cronometer_get_food_log` | Foods by day and meal, plus Cronometer's official day totals |
| `cronometer_get_daily_summary` | Daily nutrient totals (`nutrients="all"` for micros) |
| `cronometer_get_notes` / `_exercises` / `_biometrics` | Those exports |
| `cronometer_auth_status` / `cronometer_refresh_auth` | Check session / re-import from Chrome |

Max 31 days per call. Dates: `YYYY-MM-DD`, `today`, `yesterday`.

**Limitation:** for this account, Cronometer's servings export has no per-food nutrients,
so the food log lists foods and amounts per meal, but calories/macros are day totals only.

## Auth
```
uv run crono-mcp auth      # reads Chrome once (macOS may ask for "Chrome Safe Storage": click Always Allow)
uv run crono-mcp status    # uses keychain only, never Chrome
uv run crono-mcp logout
```
Session is saved in the macOS keychain (`crono-mcp-readonly`). Chrome is only ever read on
`auth` / `cronometer_refresh_auth`, never automatically. When the session expires: log into
cronometer.com in Chrome, then run `auth` again.

## If it breaks
Cronometer deploys change GWT hashes; these are auto-discovered from
`cronometer.nocache.js` / `<perm>.cache.js` each run. If RPCs start failing with
`IncompatibleRemoteServiceException`, the RPC body format in `_RPC_BODIES` changed;
compare against an upstream reference or the browser's network tab.

## Test
```
uv run pytest
uv run crono-mcp fetch servings 2026-11-09 --raw
```
