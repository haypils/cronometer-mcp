from crono_mcp import client, shape

SERVINGS = """﻿Day,Time,Group,Food Name,Amount,Energy (kcal),Protein (g),Carbs (g),Fat (g),Vitamin C (mg)
2026-11-08,7:30 AM,Breakfast,Oats,80.00 g,300,10,54,5,0
2026-11-08,7:30 AM,Breakfast,Banana,1 medium,105,1.3,27,0.4,10.3
2026-11-08,12:15 PM,Lunch,Chicken breast,150.00 g,248,46.5,0,5.4,
"""


def test_servings_grouping_and_totals():
    rows = client.parse_csv(SERVINGS)
    days = shape.servings_by_day(rows)
    assert len(days) == 1
    d = days[0]
    assert d["date"] == "2026-11-08"
    assert [m["meal"] for m in d["meals"]] == ["Breakfast", "Lunch"]
    assert d["meals"][0]["totals"]["Energy (kcal)"] == 405
    assert d["day_totals_from_foods"]["Protein (g)"] == 57.8
    assert "Vitamin C (mg)" not in d["meals"][0]["foods"][1]["nutrients"]  # key mode


def test_all_nutrients_mode():
    rows = client.parse_csv(SERVINGS)
    banana = shape.servings_by_day(rows, "all")[0]["meals"][0]["foods"][1]
    assert banana["nutrients"]["Vitamin C (mg)"] == 10.3
    assert banana["time"] == "7:30 AM"


NO_NUTRIENT_SERVINGS = """Day,Group,Food Name,Amount,Category
2026-11-09,"Breakfast","Oats","1.00 packet",""
"""
SUMMARY = """Date,Energy (kcal),Protein (g),Vitamin C (mg),Completed
2026-11-09,179.70,4.8,53.64,false
"""


def test_food_log_attaches_official_totals_and_flags_missing_per_food():
    log = shape.food_log(client.parse_csv(NO_NUTRIENT_SERVINGS), client.parse_csv(SUMMARY))
    d = log[0]
    assert d["day_totals"] == {"Energy (kcal)": 179.7, "Protein (g)": 4.8}
    assert d["day_marked_complete"] is False
    assert "nutrients" not in d["meals"][0]["foods"][0]
    assert "Do not estimate" in d["note"]


def test_only_read_rpcs_allowed():
    c = client.CronometerClient(cookies={"sesnonce": "x"}, persist=False)
    try:
        c._rpc("updateDiary")
    except client.CronometerError as e:
        assert "not permitted" in str(e)
    else:
        raise AssertionError("write RPC should be refused")
