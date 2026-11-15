"""Per-food and per-meal nutrition from Cronometer's diary RPCs.

    getServings(range)     -> what was logged: day, meal, time, food id, amount
    getFood(id)            -> nutrient profile per 100 base units (cached, see foodcache.py)
    getTotalNutrients(day) -> Cronometer's official day totals

Per-food values = profile x amount / 100 ("Serving Based" recipes store values
per serving and amounts in servings: profile x amount). Each day's summed foods are checked against the
official totals; on a mismatch the day's foods are refetched bypassing the
cache, and if it still does not match the day is marked unverified.
"""

from __future__ import annotations

from datetime import date, timedelta

from crono_mcp import nutrients as N
from crono_mcp.client import CronometerClient
from crono_mcp.foodcache import FoodCache
from crono_mcp.gwt import Food, Serving
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


def _matches(summed: dict[int, float], official: dict[int, float]) -> bool:
    for k in N.CHECK_IDS:
        a, b = summed.get(k, 0.0), official.get(k, 0.0)
        if abs(a - b) > 0.05 + 0.002 * abs(b):
            return False
    return True


class _Foods:
    """Food profiles for one request: cache first (public foods only), else fetch."""

    def __init__(self, client: CronometerClient, cache: FoodCache):
        self.client, self.cache = client, cache
        self.loaded: dict[int, Food] = {}
        self.fresh: set[int] = set()

    def get(self, food_id: int, *, fresh: bool = False) -> Food:
        if food_id in self.loaded and (not fresh or food_id in self.fresh):
            return self.loaded[food_id]
        food = None if fresh else self.cache.get(food_id)
        if food is None:
            food = self.client.food(food_id)
            self.cache.put(food)
            self.fresh.add(food_id)
        self.loaded[food_id] = food
        return food


def _amount(s: Serving, f: Food) -> str:
    servings = s.amount if f.per_unit else (s.amount / f.serving_weight if f.serving_weight else None)
    if servings is not None:
        return f"{servings:g} serving" + ("" if servings == 1 else "s")
    return f"{s.amount:.1f} g".replace(".0 g", " g")


def _build_day(day: date, servings: list[Serving], foods: _Foods, mode: str, fresh: bool):
    meals: dict[int, dict] = {}
    day_sum: dict[int, float] = {}
    for s in sorted(servings, key=lambda s: (s.group, s.order)):
        f = foods.get(s.food_id, fresh=fresh)
        scale = f.scale(s.amount)
        values = {k: v * scale for k, v in f.nutrients.items()}
        m = meals.setdefault(s.group, {"sum": {}, "foods": []})
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
                ok = _matches(summed, official.totals)
                if not ok:  # maybe a stale cached food: refetch everything for this day
                    meals, summed = _build_day(d, servings, foods, mode, fresh=True)
                    ok = _matches(summed, official.totals)
                entry.update(meals=meals, day_totals=select_nutrients(official.totals, mode))
                entry["check"] = "per-food values add up to Cronometer's day totals" if ok else "MISMATCH"
                if not ok:
                    entry["note"] = ("Per-food values do not add up to Cronometer's official day totals; "
                                     "treat per-food/per-meal numbers as approximate. day_totals are authoritative.")
            out.append(entry)
            d += timedelta(days=1)
    finally:
        cache.save()
    return out
