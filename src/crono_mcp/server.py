"""MCP server exposing read-only Cronometer tools."""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from crono_mcp import auth, shape
from crono_mcp.client import AuthError, CronometerClient, CronometerError

MAX_DAYS = 31

mcp = MCPServer(
    "cronometer",
    instructions=(
        "Read-only access to the user's Cronometer food diary. Dates are YYYY-MM-DD, "
        "or 'today' / 'yesterday'. If a tool reports an auth error, ask the user to "
        "open cronometer.com in Chrome (logged in) and then call cronometer_refresh_auth."
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


def _run(export_type: str | tuple[str, ...], start: str | None, end: str | None, fmt) -> str:
    try:
        d0, d1 = _range(start, end)
        types = export_type if isinstance(export_type, tuple) else (export_type,)
        with _lock:
            results = [_get_client().export_rows(t, d0, d1) for t in types]
        return json.dumps({"start_date": d0.isoformat(), "end_date": d1.isoformat(), "data": fmt(*results)},
                          indent=1, ensure_ascii=False)
    except AuthError as e:
        return json.dumps({"error": "AUTH", "message": str(e),
                           "fix": "Log into cronometer.com in Chrome, then call cronometer_refresh_auth."})
    except (CronometerError, ValueError) as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


Nutrients = Literal["key", "all"]


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_food_log(start_date: str = "today", end_date: str | None = None, nutrients: Nutrients = "key") -> str:
    """Get the daily meal report: every food logged, grouped by day and meal (Breakfast/Lunch/etc.)
    with amounts, plus Cronometer's official day totals (calories, macros, etc.) and whether the day
    was marked complete. Per-food/per-meal nutrients are included only if Cronometer's export provides them.

    start_date/end_date: YYYY-MM-DD or 'today'/'yesterday' (end defaults to start; max 31 days).
    nutrients: 'key' = calories, protein, carbs, net carbs, fiber, sugars, fat, sat fat, sodium,
               potassium, water, caffeine, alcohol; 'all' = every nonzero nutrient (vitamins, minerals...).
    """
    return _run(("servings", "daily_summary"), start_date, end_date,
                lambda s, d: shape.food_log(s, d, nutrients))


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_daily_summary(start_date: str = "today", end_date: str | None = None,
                                 nutrients: Nutrients = "key") -> str:
    """Get Cronometer's daily nutrition totals per day (one row per day), plus whether the day was marked complete.

    start_date/end_date: YYYY-MM-DD or 'today'/'yesterday' (end defaults to start; max 31 days).
    nutrients: 'key' (macros etc.) or 'all' (every nonzero nutrient).
    """
    return _run("daily_summary", start_date, end_date, lambda r: shape.daily_summaries(r, nutrients))


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_notes(start_date: str = "today", end_date: str | None = None) -> str:
    """Get diary notes written in Cronometer for a date range (max 31 days)."""
    return _run("notes", start_date, end_date, shape.compact_rows)


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_exercises(start_date: str = "today", end_date: str | None = None) -> str:
    """Get exercises logged in Cronometer (name, minutes, calories burned) for a date range (max 31 days)."""
    return _run("exercises", start_date, end_date, shape.compact_rows)


@mcp.tool(annotations=READ_ONLY)
def cronometer_get_biometrics(start_date: str = "today", end_date: str | None = None) -> str:
    """Get biometrics logged in Cronometer (weight, body fat, blood pressure, etc.) for a date range (max 31 days)."""
    return _run("biometrics", start_date, end_date, shape.compact_rows)


@mcp.tool(annotations=READ_ONLY)
def cronometer_auth_status() -> str:
    """Check whether the stored Cronometer session works."""
    try:
        with _lock:
            uid = _get_client().check()
        return json.dumps({"authenticated": True, "user_id": uid})
    except (AuthError, CronometerError) as e:
        return json.dumps({"authenticated": False, "message": str(e)})


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
