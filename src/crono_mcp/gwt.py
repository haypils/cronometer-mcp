"""Decoders for the few GWT-RPC responses this client reads.

GWT-RPC responses look like ``//OK[<data...>,[<string table>],0,7]``. The data
is read from the END backwards. Objects are written as a class reference
(1-based index into the string table) followed by their fields; a negative
number is a back-reference to an object already read, and 0 is null.

Only the classes needed for ``getServings``, ``getTotalNutrients`` are decoded
here. ``getFood`` is a very large object graph, so it is read with targeted
patterns instead (see ``parse_food``). Anything unexpected raises ``GwtError``
so callers can fall back to the CSV export.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from datetime import date


class GwtError(ValueError):
    """Response did not have the expected shape (Cronometer probably changed something)."""


def split_response(text: str) -> tuple[list, list[str]]:
    """Return (tokens in read order, string table)."""
    if not text.startswith("//OK[") or not text.endswith(",0,7]"):
        raise GwtError(f"not a GWT OK response: {text[:120]!r}")
    i = text.rfind(',["')
    if i < 0:
        i = text.rfind(",[]")
    if i < 0:
        raise GwtError("string table not found")
    table = json.loads(text[i + 1: len(text) - len(",0,7]")])
    data = text[5:i].rstrip(",")
    tokens: list = []
    for p in data.split(",") if data else []:
        if p.startswith('"'):
            tokens.append(json.loads(p))  # GWT longs are sent as quoted base64 strings
        elif "." in p or "E" in p or "e" in p:
            tokens.append(float(p))
        else:
            tokens.append(int(p))
    tokens.reverse()
    return tokens, table


class _Reader:
    def __init__(self, text: str):
        self.tokens, self.table = split_response(text)
        self.pos = 0
        self.objs: list = []

    def next(self):
        if self.pos >= len(self.tokens):
            raise GwtError("ran out of data")
        v = self.tokens[self.pos]
        self.pos += 1
        return v

    def int(self) -> int:
        v = self.next()
        if not isinstance(v, int):
            raise GwtError(f"expected int at {self.pos}, got {v!r}")
        return v

    def num(self) -> float:
        v = self.next()
        if isinstance(v, str):
            raise GwtError(f"expected number at {self.pos}, got {v!r}")
        return float(v)

    def class_name(self, ref: int) -> str:
        if not 1 <= ref <= len(self.table):
            raise GwtError(f"bad class ref {ref}")
        return self.table[ref - 1].split("/")[0].rsplit(".", 1)[-1]

    def obj(self):
        t = self.int()
        if t == 0:
            return None
        if t < 0:
            idx = -t - 1
            if idx >= len(self.objs):
                raise GwtError(f"bad back-reference {t}")
            return self.objs[idx]
        name = self.class_name(t)
        slot = len(self.objs)
        self.objs.append(None)
        reader = _CLASSES.get(name)
        if reader is None:
            raise GwtError(f"unexpected class {name}")
        v = reader(self, slot)
        self.objs[slot] = v
        return v


def _list(r: _Reader, slot: int):
    v: list = []
    r.objs[slot] = v
    for _ in range(r.int()):
        v.append(r.obj())
    return v


def _map(r: _Reader, slot: int):
    v: dict = {}
    r.objs[slot] = v
    for _ in range(r.int()):
        k = r.obj()
        v[k] = r.obj()
    return v


def _day(r: _Reader, slot: int):
    d, m, y = r.int(), r.int(), r.int()
    return date(y, m, d)


def _time(r: _Reader, slot: int):
    hour, minute = r.int(), r.int()
    r.int()  # seconds
    return (hour, minute)


@dataclass
class Serving:
    day: date
    group: int  # meal group: (value >> 16); low 16 bits = order within the meal
    order: int
    time: tuple[int, int] | None
    amount: float  # grams for normal foods; see diary.py for recipes
    food_id: int
    measure_id: int


def _serving(r: _Reader, slot: int):
    day = r.obj()
    r.int(); r.int()           # flags
    r.obj()                    # Short (usually null)
    grp = r.int()
    time = r.obj()             # Time or null
    r.int()                    # user id
    amount = r.num()
    food_id = r.int()
    r.next()                   # serving id (long)
    measure_id = r.int()
    r.int()                    # secondary id, unused
    if not isinstance(day, date):
        raise GwtError("serving without a day")
    return Serving(day=day, group=grp >> 16, order=grp & 0xFFFF, time=time,
                   amount=amount, food_id=food_id, measure_id=measure_id)


@dataclass
class Contribution:
    amount: float
    food_id: int


def _contribution(r: _Reader, slot: int):
    amount = r.num()
    food_id = r.int()
    r.int()
    return Contribution(amount, food_id)


_CLASSES = {
    "ArrayList": _list,
    "HashSet": _list,
    "HashMap": _map,
    "FoodContributionMap": _map,
    "ConfidenceScores": lambda r, s: r.obj(),
    "Integer": lambda r, s: r.int(),
    "Short": lambda r, s: r.int(),
    "Double": lambda r, s: r.num(),
    "String": lambda r, s: r.table[r.int() - 1],
    "Day": _day,
    "Time": _time,
    "Serving": _serving,
    "FoodContribution": _contribution,
}


def _decode(fn):
    """Any failure while decoding means the response shape changed: report it as GwtError."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GwtError:
            raise
        except (ValueError, IndexError, KeyError, TypeError, AttributeError) as e:
            raise GwtError(f"{fn.__name__}: unexpected response ({type(e).__name__}: {e})") from e
    return wrapper


@_decode
def parse_servings(text: str) -> list[Serving]:
    r = _Reader(text)
    out = r.obj()
    if not isinstance(out, list) or not all(isinstance(s, Serving) for s in out):
        raise GwtError("getServings: expected a list of servings")
    return out


@dataclass
class Totals:
    serving_count: int
    totals: dict[int, float]                      # nutrient id -> amount
    top_contributors: dict[int, list[Contribution]] = field(default_factory=dict)


@_decode
def parse_total_nutrients(text: str) -> Totals:
    r = _Reader(text)
    if r.class_name(r.int()) != "NutritionReportContext":
        raise GwtError("getTotalNutrients: unexpected root object")
    r.objs.append("root")
    r.obj()                      # HashSet (flags)
    count = r.int()              # number of servings in range
    r.obj()                      # confidence scores
    contrib = r.obj()            # FoodContributionMap
    r.obj(); r.int()             # Day, int
    totals = r.obj()             # HashMap<Integer, Double>
    if not isinstance(totals, dict):
        raise GwtError("getTotalNutrients: totals missing")
    return Totals(count, {int(k): float(v) for k, v in totals.items()},
                  {int(k): v for k, v in (contrib or {}).items()})


@dataclass
class Food:
    food_id: int
    name: str
    nutrients: dict[int, float]   # per 100 base units (per 1 unit when per_unit)
    is_recipe: bool
    custom: bool                  # the user's own recipe or custom food: editable, so never cached
    per_unit: bool = False        # Serving Based recipe: values per serving, amount counts servings
    serving_weight: float | None = None  # custom food: base units in one "Serving" (for display)

    def scale(self, amount: float) -> float:
        """Factor to multiply nutrient values by for a serving of `amount` base units."""
        return amount if self.per_unit else amount / 100

    def to_json(self) -> dict:
        return {"food_id": self.food_id, "name": self.name, "nutrients": {str(k): v for k, v in self.nutrients.items()},
                "is_recipe": self.is_recipe, "custom": self.custom, "per_unit": self.per_unit,
                "serving_weight": self.serving_weight}

    @classmethod
    def from_json(cls, d: dict) -> "Food":
        return cls(int(d["food_id"]), d["name"], {int(k): float(v) for k, v in d["nutrients"].items()},
                   bool(d["is_recipe"]), bool(d["custom"]), bool(d.get("per_unit", False)),
                   d.get("serving_weight"))


_ENGLISH_FLAG = "https://cdn1.cronometer.com/media/flags/us.png"
_NOT_TEXT = re.compile(r"^(com|java)\.|^https?:|^\[")


def _class_index(table: list[str], prefix: str) -> int | None:
    for i, s in enumerate(table):
        if s.startswith(prefix):
            return i + 1
    return None


@_decode
def parse_food(text: str, food_id: int) -> Food:
    if text.startswith("//OK[0,[],0,7]"):
        raise GwtError(f"getFood: food {food_id} not found")
    tokens, table = split_response(text)
    data = text[: text.rfind(',["')]

    nut_cls = _class_index(table, "com.cronometer.shared.foods.models.Nutrient/")
    if nut_cls is None:
        raise GwtError(f"getFood {food_id}: no nutrients")
    # Each nutrient: ..., <id>, <value>, <Nutrient class>, <id>, <Integer class>  (id repeated as the map key)
    nutrients = {int(a): float(b) for a, b in
                 re.findall(r"(?<=[,\[])(-?\d+),(-?[\d.]+(?:E-?\d+)?),%d,\1,(?=\d)" % nut_cls, data)}
    if not nutrients:
        raise GwtError(f"getFood {food_id}: nutrient list empty")

    is_recipe = _class_index(table, "com.cronometer.shared.foods.models.Ingredient/") is not None
    per_unit = False
    if is_recipe:
        # Recipe "Serving Sizes" setting: Serving Based -> nutrients per serving and amounts
        # count servings; Weight Based -> per 100 g like any food.
        kind, weights = _recipe_measures(tokens, table, food_id)
        per_unit = kind == SERVING_BASED if kind is not None else weights.get("full recipe") == 1.0

    # Display name: the English translation when there is one (custom foods have none), else
    # the food's own name, which is the first plain string in the table.
    name = None
    if _ENGLISH_FLAG in table:
        k = table.index(_ENGLISH_FLAG)
        if k + 1 < len(table) and table[k + 1] and not _NOT_TEXT.match(table[k + 1]):
            name = table[k + 1]
    if not name:
        name = next((s for s in table if s and not _NOT_TEXT.match(s)), str(food_id))

    # Database foods (NCCDB, CRDB, CFCD, FDC, ...) can't be edited by the user; recipes and
    # custom foods are tagged "Custom" in their source field.
    custom = is_recipe or "Custom" in table
    serving_weight = None
    if custom and not is_recipe:  # label-style custom food: show amounts in its "Serving" unit
        serving_weight = _recipe_measures(tokens, table, food_id)[1].get("Serving") or None
    return Food(food_id, name, nutrients, is_recipe, custom, per_unit, serving_weight)


WEIGHT_BASED, SERVING_BASED = 0, 3  # Measure$Type ordinals seen on recipes


def _recipe_measures(tokens: list, table: list[str], food_id: int) -> tuple[int | None, dict[str, float]]:
    """(Measure$Type ordinal, {measure description: weight in base units}) for a recipe or custom
    food (their measures have no translations; database foods' measures are not decoded).

    Measure fields in read order: class, quantity, 0, food id, 0, measure id, 0,
    description ref, HashMap (translations) + size, Measure$Type enum (first use:
    class + ordinal; later uses: back-reference to the same value), weight.
    All of a recipe's measures share one type, so the first ordinal seen is the setting.
    """
    cls = _class_index(table, "com.cronometer.shared.foods.models.Measure/")
    kind: int | None = None
    weights: dict[str, float] = {}
    for p, v in enumerate(tokens):
        if v != cls or p + 12 >= len(tokens) or tokens[p + 3] != food_id:
            continue
        desc, size, enum = tokens[p + 7], tokens[p + 9], tokens[p + 10]
        if not (isinstance(desc, int) and 1 <= desc <= len(table)) or size != 0 or not isinstance(enum, int):
            continue
        if enum > 0:
            if kind is None and isinstance(tokens[p + 11], int):
                kind = tokens[p + 11]
            weight = tokens[p + 12]
        else:
            weight = tokens[p + 11]
        if isinstance(weight, (int, float)):
            weights.setdefault(table[desc - 1], float(weight))
    return kind, weights
