from datetime import date

import pytest

from crono_mcp import client
from crono_mcp.exportlimit import DAILY_LIMIT, WINDOW_SECONDS, ExportLimit


def clock(start=1_000_000.0):
    t = [start]
    return t, (lambda: t[0])


def test_429_blocks_for_24_hours_and_persists(tmp_path):
    t, now = clock()
    path = tmp_path / "export-limit.json"
    lim = ExportLimit(path, now=now)
    assert lim.blocked_reason() is None
    lim.record_rate_limited()
    assert "paused until" in ExportLimit(path, now=now).blocked_reason()  # survives restart
    t[0] += WINDOW_SECONDS - 60
    assert ExportLimit(path, now=now).blocked_reason() is not None
    t[0] += 120
    assert ExportLimit(path, now=now).blocked_reason() is None


def test_local_count_stops_at_daily_limit():
    t, now = clock()
    lim = ExportLimit(persist=False, now=now)
    for _ in range(DAILY_LIMIT):
        assert lim.blocked_reason() is None
        lim.record_success()
        t[0] += 60
    assert f"Used {DAILY_LIMIT} exports" in lim.blocked_reason()
    t[0] += WINDOW_SECONDS  # oldest ones age out of the rolling window
    assert lim.blocked_reason() is None


def test_blocked_export_never_hits_network():
    lim = ExportLimit(persist=False)
    lim.record_rate_limited()
    c = client.CronometerClient(cookies={"sesnonce": "x"}, persist=False, export_limit=lim)
    c._http = None  # any network use would raise AttributeError
    with pytest.raises(client.ExportLimitError, match="paused"):
        c.export_csv("daily_summary", date(2026, 1, 1), date(2026, 1, 1))


def test_limit_is_shared_between_processes(tmp_path):
    t, now = clock()
    path = tmp_path / "export-limit.json"
    a, b = ExportLimit(path, now=now), ExportLimit(path, now=now)
    a.status()  # A loads early, as a long-running server would
    for _ in range(5):
        b.record_success()
    a.record_success()
    assert a.status()["exports_used_last_24h"] == 6 == b.status()["exports_used_last_24h"]
    b.record_rate_limited()
    assert a.blocked_reason() is not None  # A sees B's 429 pause
