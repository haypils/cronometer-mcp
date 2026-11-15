"""Diary RPC decoding, per-food scaling, the day-total check, and the food cache.

All payloads are synthetic GWT responses built in read order (the real wire
format is the reverse), shaped like what Cronometer returned in Nov 2026.
"""

import json
from datetime import date

import pytest

from crono_mcp import client, diary, gwt
from crono_mcp.foodcache import FoodCache

UID = 1000001


def gwt_text(read_order: list, table: list[str]) -> str:
    def fmt(v):
        if isinstance(v, str):
            return json.dumps(v)
        if isinstance(v, float):
            return repr(v)
        return str(v)
    return "//OK[" + ",".join(fmt(v) for v in reversed(read_order)) + "," + json.dumps(table) + ",0,7]"


SERVINGS = gwt_text(
    [1, 2,
     # Breakfast (group 1), 8:15, Short object present, 400.0 g of food 100001
     2, 3, 5, 1, 2026, 1, 1, 4, 0, (1 << 16) | 1, 5, 8, 15, 0, UID, 400.0, 100001, "AAAAAA", 200001, 200002,
     # Dinner (group 3), no Short, no time, 1 unit of recipe 300001
     2, 3, 5, 1, 2026, 1, 1, 0, (3 << 16) | 2, 0, UID, 1.0, 300001, "AAAAAB", 400001, 400002],
    ["java.util.ArrayList/4159755760", "com.cronometer.shared.entries.models.Serving/2895043750",
     "com.cronometer.shared.entries.models.Day/782579793", "java.lang.Short/551743396",
     "com.cronometer.shared.entries.models.Time/1552252503"])


def food_text(food_id: int, nutrients: dict[int, float], *, recipe: bool = False, full_recipe_weight: float = 1.0,
              kind: int = gwt.WEIGHT_BASED, name: str = "Thing", source: str = "NCCDB:1234") -> str:
    table = ["com.cronometer.shared.foods.models.Food/1", "com.cronometer.shared.foods.models.Nutrient/2",
             "java.lang.Integer/3", "com.cronometer.shared.foods.models.Measure/4", "java.util.HashMap/5",
             "com.cronometer.shared.foods.models.Measure$Type/6", "full recipe", "g",
             "com.cronometer.shared.foods.models.Ingredient/9" if recipe else "com.example.Unused/9",
             "Custom" if recipe else source,
             "https://cdn1.cronometer.com/media/flags/us.png", name]
    read = [1]
    for nid, v in nutrients.items():
        read += [3, nid, 2, float(v), nid]
    read += [4, 1.0, 0, food_id, 0, 900, 0, 7, 5, 0, 6, kind, float(full_recipe_weight)]  # "full recipe"
    read += [4, 1.0, 0, food_id, 0, 901, 0, 8, 5, 0, -2, 1.0]                          # "g"
    return gwt_text(read, table)


TOTALS = gwt_text(
    [1, 2, 0, 2, 3, 4, 0,
     7, 1, 5, 208, 8, 1, 9, 500.0, 300001, 400002,
     10, 5, 1, 2026, 0,
     4, 2, 5, 208, 6, 505.0, 5, 203, 6, 41.0],
    ["com.cronometer.shared.foods.models.NutritionReportContext/1", "java.util.HashSet/2",
     "com.cronometer.shared.foods.models.ConfidenceScores/3", "java.util.HashMap/4", "java.lang.Integer/5",
     "java.lang.Double/6", "com.cronometer.shared.foods.models.FoodContributionMap/7", "java.util.ArrayList/8",
     "com.cronometer.shared.foods.models.FoodContribution/9", "com.cronometer.shared.entries.models.Day/10"])


def test_parse_servings_handles_missing_time_and_short():
    a, b = gwt.parse_servings(SERVINGS)
    assert (a.day, a.group, a.time, a.amount, a.food_id) == (date(2026, 1, 5), 1, (8, 15), 400.0, 100001)
    assert (b.group, b.order, b.time, b.food_id, b.measure_id) == (3, 2, None, 300001, 400001)


def test_parse_total_nutrients():
    t = gwt.parse_total_nutrients(TOTALS)
    assert t.serving_count == 2
    assert t.totals == {208: 505.0, 203: 41.0}
    assert t.top_contributors[208][0].food_id == 300001


def test_parse_food_public_vs_recipe_base_units():
    plain = gwt.parse_food(food_text(100001, {208: 1.0, 262: 20.0}, recipe=False, name="Coffee"), 100001)
    assert plain.name == "Coffee" and not plain.custom and not plain.is_recipe
    assert plain.scale(400.0) == pytest.approx(4.0)

    # "Serving Based" recipe: values per serving, amount counts servings
    whole = gwt.parse_food(food_text(300001, {208: 500.0}, recipe=True, kind=gwt.SERVING_BASED), 300001)
    assert whole.is_recipe and whole.per_unit and whole.custom
    assert whole.scale(1.0) == 1.0

    # Serving Based recipe that makes 4 servings ("full recipe" = 4): still per serving
    four = gwt.parse_food(food_text(1, {208: 300.0}, recipe=True, kind=gwt.SERVING_BASED, full_recipe_weight=4.0), 1)
    assert four.per_unit and four.scale(2.0) == 2.0

    # "Weight Based" recipe: per 100 g like any food
    by_gram = gwt.parse_food(food_text(300002, {208: 150.0}, recipe=True, full_recipe_weight=450.0), 300002)
    assert by_gram.is_recipe and not by_gram.per_unit
    assert by_gram.scale(300.0) == pytest.approx(3.0)


def test_any_database_source_is_cacheable_custom_is_not():
    for src in ("NCCDB:1", "CRDB", "CFCD", "FDC:Branded:1234567", "SomeFutureDB"):
        assert not gwt.parse_food(food_text(5, {208: 1.0}, source=src), 5).custom
    assert gwt.parse_food(food_text(5, {208: 1.0}, source="Custom"), 5).custom


# Shaped like the real custom food: own name near the top, no English translation after the flag.
CUSTOM_FOOD = gwt_text(
    [1, 3, 208, 4, 1.0, 208],
    ["com.cronometer.shared.foods.models.Food/1", "Label Food", "java.lang.Integer/3",
     "com.cronometer.shared.foods.models.Nutrient/4", "Custom", "en", "English",
     "https://cdn1.cronometer.com/media/flags/us.png", "com.cronometer.shared.foods.FoodType/2323555378"])


def test_custom_food_name_and_scaling():
    f = gwt.parse_food(CUSTOM_FOOD, 300003)
    assert f.name == "Label Food" and f.custom and not f.is_recipe
    assert f.nutrients[208] * f.scale(100.0) == 1.0  # 1 serving (100 base units) of a 1 kcal label


def test_decode_failures_are_gwt_errors():
    bad = SERVINGS.replace(",2026,", ",NaN,", 1)  # malformed number -> ValueError inside the reader
    with pytest.raises(gwt.GwtError):
        gwt.parse_servings(bad)
    with pytest.raises(gwt.GwtError):
        gwt.parse_total_nutrients('//OK[1,["only/1"],0,7]')


def test_parse_food_not_found():
    with pytest.raises(gwt.GwtError):
        gwt.parse_food("//OK[0,[],0,7]", 1)


def test_bad_response_raises():
    with pytest.raises(gwt.GwtError):
        gwt.parse_servings('//EX[2,1,["com.google.gwt.user.client.rpc.IncompatibleRemoteServiceException/1"],0,7]')


class FakeClient:
    def __init__(self, foods, totals):
        self.foods, self.totals, self.food_calls = foods, totals, []

    def servings(self, start, end):
        return gwt.parse_servings(SERVINGS)

    def food(self, fid):
        self.food_calls.append(fid)
        return self.foods[fid]

    def total_nutrients(self, day):
        return gwt.Totals(2, self.totals)


COFFEE = gwt.Food(100001, "Coffee", {208: 1.0, 203: 0.02}, False, False)
OATS = gwt.Food(300001, "Test Recipe", {208: 500.0, 203: 40.0}, True, True, per_unit=True)
OFFICIAL = {208: 1.0 * 4.0 + 500.0, 203: 0.02 * 4.0 + 40.0}


def test_food_log_per_food_meals_and_check(tmp_path):
    c = FakeClient({100001: COFFEE, 300001: OATS}, OFFICIAL)
    cache = FoodCache(tmp_path / "foods.json")
    [day] = diary.food_log(c, cache, date(2026, 1, 5), date(2026, 1, 5))
    assert [m["meal"] for m in day["meals"]] == ["Breakfast", "Dinner"]
    coffee = day["meals"][0]["foods"][0]
    assert coffee == {"food": "Coffee", "amount": "400 g", "time": "8:15 AM",
                      "nutrients": {"Energy (kcal)": 4.0, "Protein (g)": 0.08}}
    oats = day["meals"][1]["foods"][0]
    assert oats["amount"] == "1 serving" and "time" not in oats
    assert oats["nutrients"]["Energy (kcal)"] == 500.0
    assert day["check"].startswith("per-food values add up")
    assert day["day_totals"]["Energy (kcal)"] == round(OFFICIAL[208], 2)

    # Public food cached, recipe not.
    stored = json.loads((tmp_path / "foods.json").read_text())["foods"]
    assert list(stored) == ["100001"]


def test_stale_cache_is_refetched_on_mismatch(tmp_path):
    cache = FoodCache(tmp_path / "foods.json")
    cache.put(gwt.Food(100001, "Coffee", {208: 50.0, 203: 0.02}, False, False))  # stale values
    c = FakeClient({100001: COFFEE, 300001: OATS}, OFFICIAL)
    [day] = diary.food_log(c, cache, date(2026, 1, 5), date(2026, 1, 5))
    assert day["check"].startswith("per-food values add up")
    assert day["meals"][0]["foods"][0]["nutrients"]["Energy (kcal)"] == 4.0
    assert c.food_calls.count(100001) == 1  # refetched once, fresh


def test_unresolvable_mismatch_is_flagged(tmp_path):
    c = FakeClient({100001: COFFEE, 300001: OATS}, {208: 9999.0, 203: 1.0})
    [day] = diary.food_log(c, FoodCache(tmp_path / "f.json"), date(2026, 1, 5), date(2026, 1, 5))
    assert day["check"] == "MISMATCH" and "approximate" in day["note"]


def test_empty_day(tmp_path):
    class Empty(FakeClient):
        def servings(self, s, e):
            return []
    [day] = diary.food_log(Empty({}, {}), FoodCache(tmp_path / "f.json"), date(2026, 1, 1), date(2026, 1, 1))
    assert day["meals"] == [] and day["note"] == "Nothing logged."


def test_cache_ttl_and_roundtrip(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "foods.json"
    cache = FoodCache(path, now=lambda: now[0])
    cache.put(COFFEE)
    cache.put(OATS)
    cache.save()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = FoodCache(path, now=lambda: now[0])
    assert again.get(100001) == COFFEE and again.get(300001) is None
    now[0] += 31 * 24 * 3600
    assert again.get(100001) is None


def test_cache_writes_only_when_changed_and_logout_sticks(tmp_path):
    path = tmp_path / "foods-v2.json"
    (tmp_path / "foods-v1.json").write_text("{}")  # older version left behind
    FoodCache(path).get(1)
    FoodCache(path).save()
    assert not path.exists()  # nothing added: no write
    c = FoodCache(path); c.put(COFFEE); c.save()
    assert path.exists() and not (tmp_path / "foods-v1.json").exists()
    FoodCache(path).clear()  # logout
    assert FoodCache(path).get(100001) is None


def test_rpc_params_must_be_ints():
    c = client.CronometerClient(cookies={"sesnonce": "x"}, persist=False)
    with pytest.raises(client.CronometerError, match="integers"):
        c._rpc("getFood", food_id="1|2|3")


def test_custom_food_amount_shown_in_servings():
    label = gwt.Food(1, "Label Food", {208: 1.0}, False, True, serving_weight=100.0)
    s = gwt.Serving(date(2026, 1, 6), 1, 1, None, 150.0, 1, 0)
    assert diary._amount(s, label) == "1.5 servings"
    assert diary._amount(s, COFFEE) == "150 g"


def test_undecodable_food_is_listed_without_nutrients(tmp_path):
    class OneBad(FakeClient):
        def food(self, fid):
            if fid == 300001:
                raise gwt.GwtError("odd food")
            return super().food(fid)
    c = OneBad({100001: COFFEE}, OFFICIAL)
    [day] = diary.food_log(c, FoodCache(tmp_path / "f.json"), date(2026, 1, 5), date(2026, 1, 5))
    coffee, = day["meals"][0]["foods"]
    bad, = day["meals"][1]["foods"]
    assert coffee["nutrients"]["Energy (kcal)"] == 4.0          # the rest still works
    assert bad["nutrients"] == {} and "unavailable" in bad["error"]
    assert day["check"] == "MISMATCH" and "1 food(s) could not be read" in day["note"]


def test_micronutrient_mismatch_is_caught():
    summed = {208: 100.0, 303: 2.0}
    assert diary.mismatched(summed, {208: 100.0, 303: 2.0}) == []
    assert diary.mismatched(summed, {208: 100.0, 303: 5.0}) == ["Iron (mg)"]
