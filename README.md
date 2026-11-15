# crono-mcp (read-only Cronometer MCP)

> **Unofficial.** Not affiliated with or endorsed by Cronometer. Uses undocumented web endpoints that may change; read-only, for accessing your own data.

Lets Claude read your Cronometer diary using the session from your Chrome login.
**Read-only by construction**: the client can send only fixed read RPCs
(`authenticate`, `generateAuthorizationToken`, `getServings`, `getFood`, `getTotalNutrients`),
with integer-only parameters, plus `GET /export`. See `src/crono_mcp/client.py`.

Protocol adapted from [cphoskins/cronometer-mcp](https://github.com/cphoskins/cronometer-mcp) (MIT; see `THIRD_PARTY_LICENSE_cronometer-mcp`).

## Tools
| Tool | What it returns |
|---|---|
| `cronometer_get_food_log` | Foods by day and meal with per-food and per-meal nutrients, plus Cronometer's official day totals |
| `cronometer_get_daily_summary` | Daily nutrient totals (`nutrients="all"` for micros); "completed" flag on ranges over 7 days |
| `cronometer_get_notes` / `_exercises` / `_biometrics` | Those exports |
| `cronometer_auth_status` / `cronometer_refresh_auth` | Check session / re-import from Chrome |

Max 31 days per call. Dates: `YYYY-MM-DD`, `today`, `yesterday`.

## How per-food nutrients work
Cronometer's servings CSV export has no nutrient columns, so the food log uses the web app's
own diary RPCs instead (`src/crono_mcp/diary.py`):

- `getServings(range)`: what was logged (day, meal, time, food id, amount in base units)
- `getFood(id)`: nutrient profile per 100 g. Recipes follow their "Serving Sizes" setting:
  Weight Based recipes work like any food; Serving Based recipes store values per serving and
  log amounts in servings (read from the recipe's measure type, not guessed from weights).
- `getTotalNutrients(day)`: Cronometer's official totals. Every day's summed foods are checked
  against these; a mismatch triggers a refetch, then the day is flagged `MISMATCH`.

If the RPCs break after a Cronometer deploy, the food log falls back to the servings export
(foods per meal, no per-food nutrients: 1 export), with day totals from `getTotalNutrients`, or
from a second export only if that RPC is broken too.

## Export limit
Cronometer allows 10 data exports per day for personal use. The client never sends an export
after 10 in a rolling 24 hours, and after any HTTP 429 it pauses all exports for 24 hours
(state in `~/Library/Caches/crono-mcp/export-limit.json`, survives restarts). To save that budget,
`cronometer_get_daily_summary` uses `getTotalNutrients` for ranges of 7 days or fewer (no
"completed" flag; 1 export only if that RPC is broken), and the export for longer ranges, falling
back to the RPC if the export is unavailable. Notes, exercises and biometrics still come from the export.

The model is told about the budget in three places: the server instructions, each tool's
description ("Export cost: ..."), and an `export_budget` block in every result that used an
export (also in `cronometer_auth_status`, which costs nothing). When exports are exhausted, tools
return an MCP tool error (`isError`) with `EXPORT_LIMIT`, the resume time, and what still works.

**Food cache:** `~/Library/Caches/crono-mcp/foods-v2.json` (0600, nutrition data only). Database
foods (anything you can't edit, from any database) are cached for 30 days; your own recipes and
custom foods (tagged `Custom` by Cronometer) are fetched fresh each request. The file is re-read
on every request and written only when something new was fetched. `crono-mcp logout` deletes it.

## Auth
```
uv run crono-mcp auth      # reads Chrome once (macOS may ask for "Chrome Safe Storage": click Always Allow)
uv run crono-mcp status    # uses keychain only, never Chrome
uv run crono-mcp logout     # also deletes the food cache
```
Session is saved in the macOS keychain (`crono-mcp-readonly`). Chrome is only ever read on
`auth` / `cronometer_refresh_auth`, never automatically. When the session expires: log into
cronometer.com in Chrome, then run `auth` again.

## If it breaks
Cronometer deploys change GWT build ids and, when a class changes, its type signature
(`<class>/<hash>`). Both are read from `cronometer.nocache.js` / `<perm>.cache.js` on startup,
and if Cronometer rejects a request as out of date (`IncompatibleRemoteServiceException`), they
are re-read once and the request retried, so a deploy mid-session needs no restart.

What can still break, and what you'll see:
- A method's parameters change: still rejected after the retry. The food log falls back to
  the export; fix the body in `_RPC_BODIES` (compare with the browser's network tab).
- A response layout changes (fields added/reordered): `GwtError`, same fallback; fix the
  decoder in `gwt.py`. One food that can't be decoded is listed without nutrients instead.
- Scaling semantics change: every tracked nutrient is checked against Cronometer's day totals,
  so the day is flagged `MISMATCH` with the nutrients that differ.

## Test
```
uv run pytest
uv run crono-mcp fetch servings 2026-11-09 --raw
uv run crono-mcp foodlog 2026-01-05 2026-01-11   # per-food log; each day shows its total check
```
