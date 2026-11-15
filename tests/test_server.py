import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from crono_mcp import client, diary, gwt, server
from crono_mcp.exportlimit import ExportLimit


@pytest.fixture
def blocked_client(monkeypatch):
    lim = ExportLimit(persist=False)
    lim.record_rate_limited()
    c = client.CronometerClient(cookies={"sesnonce": "x"}, persist=False, export_limit=lim)
    monkeypatch.setattr(server, "_client", c)
    return c


def call(name, args):
    return asyncio.run(server.mcp.call_tool(name, args))


def test_exhausted_export_is_a_tool_error_with_budget(blocked_client):
    # MCPServer.call_tool raises ToolError; the protocol handler turns it into is_error=True
    # with this message as the content the model sees.
    with pytest.raises(ToolError) as exc:
        call("cronometer_get_notes", {"start_date": "2026-01-01", "end_date": "2026-01-07"})
    body = json.loads(str(exc.value).split(": ", 1)[1])
    assert body["error"] == "EXPORT_LIMIT"
    assert body["export_budget"]["exports_remaining"] == 0 and "paused_until" in body["export_budget"]
    assert "Do not retry" in body["fix"]


def test_auth_status_reports_budget_without_network(blocked_client, monkeypatch):
    monkeypatch.setattr(blocked_client, "check", lambda: "123")
    body = json.loads(server.cronometer_auth_status())
    assert body["export_budget"]["exports_remaining"] == 0


def test_descriptions_state_export_cost():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("cronometer_get_notes", "cronometer_get_exercises", "cronometer_get_biometrics"):
        assert "1 export (of 10/day)" in tools[name].description
    assert "none for 7 days or fewer" in tools["cronometer_get_daily_summary"].description
    assert "10 per day" in server.mcp.instructions


class FallbackClient:
    """Diary RPCs broken; counts exports."""

    def __init__(self, totals_ok=True):
        self.exports, self.totals_ok = [], totals_ok

    def export_rows(self, kind, d0, d1):
        self.exports.append(kind)
        if kind == "servings":
            return [{"Day": "2026-01-05", "Group": "Breakfast", "Food Name": "Coffee", "Amount": "1 cup"}]
        return [{"Date": "2026-01-05", "Energy (kcal)": "100.0", "Completed": "false"}]

    def total_nutrients(self, day):
        if not self.totals_ok:
            raise gwt.GwtError("totals broken")
        return gwt.Totals(1, {208: 99.5})

    def export_budget(self):
        return {"exports_remaining": 10 - len(self.exports)}


@pytest.fixture
def broken_diary(monkeypatch):
    def fail(*a, **k):
        raise gwt.GwtError("servings broken")
    monkeypatch.setattr(diary, "food_log", fail)


def test_food_log_fallback_spends_one_export(broken_diary, monkeypatch):
    c = FallbackClient()
    monkeypatch.setattr(server, "_client", c)
    out = json.loads(server.cronometer_get_food_log("2026-01-05"))
    assert c.exports == ["servings"]
    day = out["data"][0]
    assert day["day_totals"] == {"Energy (kcal)": 99.5} and "day_marked_complete" not in day
    assert "Diary RPCs failed" in out["note"] and "export_budget" in out


def test_food_log_fallback_second_export_only_if_totals_broken(broken_diary, monkeypatch):
    c = FallbackClient(totals_ok=False)
    monkeypatch.setattr(server, "_client", c)
    out = json.loads(server.cronometer_get_food_log("2026-01-05"))
    assert c.exports == ["servings", "daily_summary"]
    assert out["data"][0]["day_totals"] == {"Energy (kcal)": 100}


def test_short_summary_falls_back_to_one_export(monkeypatch):
    c = FallbackClient(totals_ok=False)
    monkeypatch.setattr(server, "_client", c)
    out = json.loads(server.cronometer_get_daily_summary("2026-01-05"))
    assert c.exports == ["daily_summary"] and "used 1 export" in out["note"]


def test_short_summary_uses_no_export(monkeypatch):
    c = FallbackClient()
    monkeypatch.setattr(server, "_client", c)
    out = json.loads(server.cronometer_get_daily_summary("2026-01-05"))
    assert c.exports == [] and out["data"][0]["nutrients"] == {"Energy (kcal)": 99.5}
