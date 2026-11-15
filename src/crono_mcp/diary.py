"""Per-food and per-meal nutrition from Cronometer's diary RPCs.

    getServings(range)     -> what was logged: day, meal, time, food id, amount
    getFood(id)            -> nutrient profile per 100 base units (cached, see foodcache.py)
    getTotalNutrients(day) -> Cronometer's official day totals

Per-food values = profile x amount / 100 ("Serving Based" recipes store values
per serving and amounts in servings: profile x amount). Each day's summed foods
are checked against the official totals for every tracked nutrient; on a mismatch the day's foods are
refetched bypassing the cache, and if it still does not match the day is
flagged MISMATCH with the nutrients that differ. A food whose profile can't be
decoded is listed without nutrients instead of failing the whole log.
"""

from __future__ import annotations

from datetime import date, timedelta

from crono_mcp import nutrients as N
from crono_mcp.client import CronometerClient
from crono_mcp.foodcache import FoodCache
from crono_mcp.gwt import Food, GwtError, Serving
from crono_mcp.shape import KEY_NUTRIENTS

MEALS = {0: "Uncategorized", 1: "Breakfast", 2: "Lunch", 3: "Dinner", 4: "Snacks"}


def _fmt_time(t: tuple[int, int] | None) -> str | None:
    if t is None:
        return None
    h, m = t
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def select_nutrients(values: dict[int, float], mode: str) -> dict[str, float]:
    named = N.named(values)
    keys = KEY_NUTRIENTS if mode == "key" else list(named)
    out = {}
    for k in keys:
        v = named.get(k)
        if v is not None and round(v, 2) != 0:
            out[k] = round(v, 2)
    return out


def _add(total: dict[int, float], add: dict[int, float]) -> None:
    for k, v in add.items():
        total[k] = total.get(k, 0.0) + v


def mismatched(summed: dict[int, float], official: dict[int, float]) -> list[str]:
    """Names of tracked nutrients whose summed per-food value differs from the official total."""
    out = []
    for k, name in N.NAMES.items():
        a, b = summed.get(k, 0.0), official.get(k, 0.0)
        if abs(a - b) > 0.05 + 0.002 * abs(b):
            out.append(name)
    return out


class _Foods:
    """Food profiles for one request: cache first (public foods only), else fetch."""

    def __init__(self, client: CronometerClient, cache: FoodCache):
        self.client, self.cache = client, cache
        self.loaded: dict[int, Food] = {}
        self.fresh: set[int] = set()
        self.failed: dict[int, str] = {}  # food id -> why its profile couldn't be decoded

    def get(self, food_id: int, *, fresh: bool = False) -> Food | None:
        """The food's profile, or None if Cronometer's response for it couldn't be decoded."""
        if food_id in self.failed:
            return None
        if food_id in self.loaded and (not fresh or food_id in self.fresh):
            return self.loaded[food_id]
        food = None if fresh else self.cache.get(food_id)
        if food is None:
            try:
                food = self.client.food(food_id)
            except GwtError as e:  # one odd food shouldn't sink the whole log
                self.failed[food_id] = str(e)
                return None
            self.cache.put(food)
            self.fresh.add(food_id)
        self.loaded[food_id] = food
        return food


def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _amount(s: Serving, f: Food) -> str:
    """Amount in the unit it was logged with, e.g. "1.25 cup", "3 each", "5 capsules"."""
    m = f.measures.get(s.measure_id)
    if m and m.weight > 0:
        return f"{_num(s.amount / m.weight * m.quantity)} {m.description}"
    if f.per_unit:
        return f"{_num(s.amount)} serving" + ("" if s.amount == 1 else "s")
    return f"{_num(s.amount)} g"


def _build_day(day: date, servings: list[Serving], foods: _Foods, mode: str, fresh: bool):
    meals: dict[int, dict] = {}
    day_sum: dict[int, float] = {}
    for s in sorted(servings, key=lambda s: (s.group, s.order)):
        f = foods.get(s.food_id, fresh=fresh)
        if f is not None and s.measure_id and s.measure_id not in f.measures and s.food_id not in foods.fresh:
            f = foods.get(s.food_id, fresh=True)  # cached copy predates this measure: refetch once
        m = meals.setdefault(s.group, {"sum": {}, "foods": []})
        if f is None:
            item = {"food": f"(food {s.food_id})", "amount": f"{s.amount:g} base units",
                    "nutrients": {}, "error": "nutrients unavailable (Cronometer's data for this food couldn't be read)"}
            if s.time:
                item["time"] = _fmt_time(s.time)
            m["foods"].append(item)
            continue
        values = {k: v * f.scale(s.amount) for k, v in f.nutrients.items()}
        _add(m["sum"], values)
        _add(day_sum, values)
        item = {"food": f.name, "amount": _amount(s, f)}
        if s.time:
            item["time"] = _fmt_time(s.time)
        item["nutrients"] = select_nutrients(values, mode)
        m["foods"].append(item)
    out_meals = [{"meal": MEALS.get(g, f"Meal {g}"), "foods": m["foods"], "totals": select_nutrients(m["sum"], mode)}
                 for g, m in sorted(meals.items())]
    return out_meals, day_sum


def food_log(client: CronometerClient, cache: FoodCache, start: date, end: date, mode: str = "key") -> list[dict]:
    by_day: dict[date, list[Serving]] = {}
    for s in client.servings(start, end):
        by_day.setdefault(s.day, []).append(s)
    foods = _Foods(client, cache)
    out = []
    try:
        d = start
        while d <= end:
            servings = by_day.get(d, [])
            entry: dict = {"date": d.isoformat()}
            if not servings:
                entry.update(meals=[], day_totals={}, note="Nothing logged.")
            else:
                official = client.total_nutrients(d)
                meals, summed = _build_day(d, servings, foods, mode, fresh=False)
                diff = mismatched(summed, official.totals)
                if diff:  # maybe a stale cached food: refetch everything for this day
                    meals, summed = _build_day(d, servings, foods, mode, fresh=True)
                    diff = mismatched(summed, official.totals)
                entry.update(meals=meals, day_totals=select_nutrients(official.totals, mode))
                entry["check"] = "MISMATCH" if diff else "per-food values add up to Cronometer's day totals"
                if diff:
                    missing = sorted({s.food_id for s in servings} & set(foods.failed))
                    why = f" {len(missing)} food(s) could not be read." if missing else ""
                    entry["note"] = (f"Per-food values do not add up to Cronometer's day totals for: "
                                     f"{', '.join(diff[:8])}{'...' if len(diff) > 8 else ''}.{why} "
                                     "Treat per-food/per-meal numbers as approximate; day_totals are authoritative.")
            out.append(entry)
            d += timedelta(days=1)
    finally:
        cache.save()
    return out
