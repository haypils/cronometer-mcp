"""MCP server exposing read-only Cronometer tools."""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from crono_mcp import auth, diary, shape
from crono_mcp.client import AuthError, CronometerClient, CronometerError, ExportLimitError
from crono_mcp.foodcache import FoodCache
from crono_mcp.gwt import GwtError

MAX_DAYS = 31

mcp = MCPServer(
    "cronometer",
    instructions=(
        "Read-only access to the user's Cronometer food diary. Dates are YYYY-MM-DD, "
        "or 'today' / 'yesterday'. If a tool reports an auth error, ask the user to "
        "open cronometer.com in Chrome (logged in) and then call cronometer_refresh_auth.\n\n"
        "Export budget: some tools use Cronometer's CSV export, which is limited to 10 per day "
        "(each tool description says when). Food log and daily summaries of 7 days or fewer cost "
        "nothing. Notes, exercises, biometrics, and daily summaries over 7 days cost 1 export per call. "
        "Fetch each range once and reuse the result instead of re-calling; prefer one wide range over "
        "several narrow ones. Results that used an export include 'export_budget'; "
        "cronometer_auth_status shows it without spending anything. When exports are exhausted, "
        "tell the user when they resume rather than retrying."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

_client: CronometerClient | None = None
_lock = threading.Lock()


def _get_client() -> CronometerClient:
    global _client
    if _client is None:
        _client = CronometerClient()
    return _client


def _parse_date(s: str | None, default: date) -> date:
    if not s:
        return default
    s = s.strip().lower()
    if s == "today":
        return date.today()
    if s == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(s)


def _range(start: str | None, end: str | None) -> tuple[date, date]:
    d0 = _parse_date(start, date.today())
    d1 = _parse_date(end, d0)
    if d1 < d0:
        raise ValueError("end_date is before start_date")
    if (d1 - d0).days + 1 > MAX_DAYS:
        raise ValueError(f"Date range too large; max {MAX_DAYS} days per call")
    return d0, d1


def _dump(d0: date, d1: date, data, **extra) -> str:
    return json.dumps({"start_date": d0.isoformat(), "end_date": d1.isoformat(), **extra, "data": data},
                      indent=1, ensure_ascii=False)


def _error(e: Exception, **extra) -> ToolError:
    """A tool error (is_error=True) with a JSON body the model can act on."""
    if isinstance(e, AuthError):
        body = {"error": "AUTH", "message": str(e),
                "fix": "Log into cronometer.com in Chrome, then call cronometer_refresh_auth."}
    elif isinstance(e, ExportLimitError):
        body = {"error": "EXPORT_LIMIT", "message": str(e), "export_budget": _get_client().export_budget(),
                "fix": "Do not retry. Tell the user when exports resume. Food log and daily summaries "
                       "of 7 days or fewer still work (they don't use exports)."}
    else:
        body = {"error": type(e).__name__, "message": str(e)}
    return ToolError(json.dumps({**body, **extra}, ensure_ascii=False))


def _run(export_type: str | tuple[str, ...], start: str | None, end: str | None, fmt) -> str:
    """Export-backed tools: costs one export per type in `export_type`."""
    try:
        d0, d1 = _range(start, end)
        types = export_type if isinstance(export_type, tuple) else (export_type,)
        with _lock:
            results = [_get_client().export_rows(t, d0, d1) for t in types]
            budget = _get_client().export_budget()
        return _dump(d0, d1, fmt(*results), export_budget=budget)
    except (AuthError, CronometerError, ValueError) as e:
        raise _error(e) from e


Nutrients = Literal["key", "all"]


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_food_log(start_date: str = "today", end_date: str | None = None, nutrients: Nutrients = "key") -> str:
    """Get the daily meal report: every food logged, grouped by day and meal (Breakfast/Lunch/etc.),
    with each food's amount and nutrients, per-meal totals, and Cronometer's official day totals.
    Each day says whether the per-food values add up to the official totals.
    Export cost: none (only if Cronometer's diary API breaks does it fall back to 1 export).

    start_date/end_date: YYYY-MM-DD or 'today'/'yesterday' (end defaults to start; max 31 days).
    nutrients: 'key' = calories, protein, carbs, net carbs, fiber, sugars, fat, sat fat, sodium,
               potassium, water, caffeine, alcohol; 'all' = every nonzero nutrient (vitamins, minerals...).
    """
    try:
        d0, d1 = _range(start_date, end_date)
    except ValueError as e:
        raise _error(e) from e
    with _lock:
        try:
            return _dump(d0, d1, diary.food_log(_get_client(), FoodCache(), d0, d1, nutrients))
        except AuthError as e:
            raise _error(e) from e
        except (GwtError, CronometerError) as e:
            # Diary RPCs failed (Cronometer may have changed them): fall back to the CSV export.
            return _food_log_from_export(d0, d1, nutrients, f"Diary RPCs failed ({type(e).__name__}: {e})")


def _food_log_from_export(d0: date, d1: date, nutrients: str, reason: str) -> str:
    """Foods per meal from the servings export (1 export). Day totals come from the totals RPC,
    and only if that is broken too, from a second export."""
    c = _get_client()
    try:
        servings = c.export_rows("servings", d0, d1)
    except (AuthError, CronometerError) as e:
        raise _error(e, diary_error=reason) from e
    try:
        summary = [{"Date": d["date"], **{k: str(v) for k, v in d["nutrients"].items()}}
                   for d in _rpc_daily_totals(d0, d1, "all")]
        totals_from = "day totals from Cronometer's diary"
    except (GwtError, CronometerError):
        try:
            summary = c.export_rows("daily_summary", d0, d1)
            totals_from = "day totals from the export"
        except CronometerError as e:
            summary, totals_from = [], f"day totals unavailable ({e})"
    data = shape.food_log(servings, summary, nutrients)
    for day in data:
        day.pop("day_marked_complete", None)  # same shape as the normal path
    return _dump(d0, d1, data, export_budget=c.export_budget(),
                 note=f"{reason}; showing foods from the export without per-food nutrients ({totals_from}).")


RPC_SUMMARY_MAX_DAYS = 7  # up to this many days: diary RPCs only, saving the 10/day export budget


def _rpc_daily_totals(d0: date, d1: date, nutrients: str) -> list[dict]:
    data = []
    d = d0
    while d <= d1:
        totals = _get_client().total_nutrients(d).totals
        data.append({"date": d.isoformat(), "nutrients": diary.select_nutrients(totals, nutrients)})
        d += timedelta(days=1)
    return data


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_daily_summary(start_date: str = "today", end_date: str | None = None,
                                 nutrients: Nutrients = "key") -> str:
    """Get Cronometer's daily nutrition totals per day (one row per day). Ranges over 7 days also
    include whether each day was marked complete (when Cronometer's export is available).
    Export cost: none for 7 days or fewer (1 only if Cronometer's diary API breaks); 1 export
    (of 10/day) for longer ranges.

    start_date/end_date: YYYY-MM-DD or 'today'/'yesterday' (end defaults to start; max 31 days).
    nutrients: 'key' (macros etc.) or 'all' (every nonzero nutrient).
    """
    try:
        d0, d1 = _range(start_date, end_date)
    except ValueError as e:
        raise _error(e) from e
    c = _get_client()
    with _lock:
        try:
            long_range = (d1 - d0).days + 1 > RPC_SUMMARY_MAX_DAYS
            if long_range:
                try:  # one export call beats one RPC per day
                    rows = c.export_rows("daily_summary", d0, d1)
                    return _dump(d0, d1, shape.daily_summaries(rows, nutrients), export_budget=c.export_budget())
                except CronometerError as e:
                    note = f"Export unavailable ({e}). "
            else:
                note = ""
            try:
                data = _rpc_daily_totals(d0, d1, nutrients)
            except (GwtError, CronometerError) as e:
                if long_range:
                    raise
                # Short range but the totals RPC is broken: spend one export instead.
                rows = c.export_rows("daily_summary", d0, d1)
                return _dump(d0, d1, shape.daily_summaries(rows, nutrients), export_budget=c.export_budget(),
                             note=f"Diary totals failed ({type(e).__name__}: {e}); used 1 export instead.")
            extra = {"export_budget": c.export_budget()} if note else {}
            return _dump(d0, d1, data, note=note + "Totals from Cronometer's diary; 'completed' flag not available.",
                         **extra)
        except (AuthError, CronometerError, ValueError) as e:
            raise _error(e) from e


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_notes(start_date: str = "today", end_date: str | None = None) -> str:
    """Get diary notes written in Cronometer for a date range (max 31 days).
    Export cost: 1 export (of 10/day) per call; fetch the whole range you need at once."""
    return _run("notes", start_date, end_date, shape.compact_rows)


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_exercises(start_date: str = "today", end_date: str | None = None) -> str:
    """Get exercises logged in Cronometer (name, minutes, calories burned) for a date range (max 31 days).
    Export cost: 1 export (of 10/day) per call; fetch the whole range you need at once."""
    return _run("exercises", start_date, end_date, shape.compact_rows)


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_biometrics(start_date: str = "today", end_date: str | None = None) -> str:
    """Get biometrics logged in Cronometer (weight, body fat, blood pressure, etc.) for a date range (max 31 days).
    Export cost: 1 export (of 10/day) per call; fetch the whole range you need at once."""
    return _run("biometrics", start_date, end_date, shape.compact_rows)


@mcp.tool(annotations=READ_ONLY)
def cronometer_auth_status() -> str:
    """Check whether the stored Cronometer session works, and how many of today's 10 exports remain.
    Export cost: none."""
    with _lock:
        budget = _get_client().export_budget()
        try:
            uid = _get_client().check()
        except (AuthError, CronometerError) as e:
            return json.dumps({"authenticated": False, "message": str(e), "export_budget": budget})
    return json.dumps({"authenticated": True, "user_id": uid, "export_budget": budget})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def cronometer_refresh_auth(browser: str = "chrome") -> str:
    """Re-read the Cronometer login session from the browser (default Chrome) after the user logs in again.
    Only reads browser cookies and the local keychain; changes nothing in Cronometer."""
    try:
        with _lock:
            c = _get_client()
            msg = c.reload_from_browser(browser)
            uid = c.check()
        return json.dumps({"authenticated": True, "user_id": uid, "message": msg})
    except (AuthError, CronometerError) as e:
        return json.dumps({"authenticated": False, "message": str(e)})


def serve() -> None:
    mcp.run()


__all__ = ["auth", "mcp", "serve"]
