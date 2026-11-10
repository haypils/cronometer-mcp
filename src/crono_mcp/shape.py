"""Turn Cronometer CSV rows into compact, Claude-friendly structures."""

from __future__ import annotations

from collections import OrderedDict

ID_COLUMNS = {"Day", "Date", "Time", "Group", "Food Name", "Amount", "Category", "Completed"}

# Shown by default; everything else needs nutrients="all".
KEY_NUTRIENTS = [
    "Energy (kcal)",
    "Protein (g)",
    "Carbs (g)",
    "Net Carbs (g)",
    "Fiber (g)",
    "Sugars (g)",
    "Added Sugars (g)",
    "Fat (g)",
    "Saturated (g)",
    "Sodium (mg)",
    "Potassium (mg)",
    "Water (g)",
    "Caffeine (mg)",
    "Alcohol (g)",
]


def _num(v: str):
    if v is None or v == "":
        return None
    try:
        f = float(v.replace(",", ""))
    except ValueError:
        return v
    return int(f) if f.is_integer() else round(f, 2)


def _nutrients(row: dict[str, str], mode: str) -> dict:
    keys = KEY_NUTRIENTS if mode == "key" else [k for k in row if k not in ID_COLUMNS]
    out = {}
    for k in keys:
        if k in row:
            n = _num(row[k])
            if n not in (None, 0):
                out[k] = n
    return out


def _sum_into(total: dict, add: dict) -> None:
    for k, v in add.items():
        if isinstance(v, (int, float)):
            total[k] = round(total.get(k, 0) + v, 2)


def servings_by_day(rows: list[dict[str, str]], nutrients: str = "key") -> list[dict]:
    """Group servings into day -> meal group -> foods, with meal and day totals."""
    days: OrderedDict[str, OrderedDict[str, list]] = OrderedDict()
    for row in rows:
        day = row.get("Day") or row.get("Date") or "unknown"
        group = row.get("Group") or "Uncategorized"
        days.setdefault(day, OrderedDict()).setdefault(group, []).append(row)

    result = []
    for day, groups in days.items():
        day_total: dict = {}
        meals = []
        for group, items in groups.items():
            meal_total: dict = {}
            foods = []
            for r in items:
                n = _nutrients(r, nutrients)
                _sum_into(meal_total, n)
                food = {"food": r.get("Food Name", ""), "amount": r.get("Amount", "")}
                if r.get("Time"):
                    food["time"] = r["Time"]
                if r.get("Category"):
                    food["category"] = r["Category"]
                if n:
                    food["nutrients"] = n
                foods.append(food)
            _sum_into(day_total, meal_total)
            meal = {"meal": group, "foods": foods}
            if meal_total:
                meal["totals"] = meal_total
            meals.append(meal)
        entry = {"date": day, "meals": meals}
        if day_total:
            entry["day_totals_from_foods"] = day_total
        result.append(entry)
    return result


def food_log(serving_rows: list[dict[str, str]], summary_rows: list[dict[str, str]], nutrients: str = "key") -> list:
    """Servings grouped by meal, with Cronometer's official daily totals attached.

    Cronometer's servings export only includes per-food nutrients for some
    accounts; when absent, say so explicitly so nothing gets estimated.
    """
    days = {d["date"]: d for d in servings_by_day(serving_rows, nutrients)}
    has_per_food = any(_nutrients(r, "key") for r in serving_rows)
    for s in daily_summaries(summary_rows, nutrients):
        d = days.setdefault(s["date"], {"date": s["date"], "meals": []})
        d["day_totals"] = s["nutrients"]
        if "completed" in s:
            d["day_marked_complete"] = s["completed"]
    out = []
    for date_key in sorted(days):
        d = days[date_key]
        if "day_totals" in d:
            d.pop("day_totals_from_foods", None)
        if serving_rows and not has_per_food:
            d["note"] = ("Cronometer's export does not include per-food or per-meal nutrients for this "
                         "account; only day_totals are authoritative. Do not estimate per-meal numbers.")
        out.append(d)
    return out


def daily_summaries(rows: list[dict[str, str]], nutrients: str = "key") -> list[dict]:
    out = []
    for r in rows:
        entry = {"date": r.get("Date") or r.get("Day", "")}
        if r.get("Completed"):
            entry["completed"] = r["Completed"].lower() == "true"
        entry["nutrients"] = _nutrients(r, nutrients)
        out.append(entry)
    return out


def compact_rows(rows: list[dict[str, str]]) -> list[dict]:
    """Generic: drop empty fields and convert numbers."""
    return [{k: _num(v) for k, v in r.items() if v not in ("", None)} for r in rows]
