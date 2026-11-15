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
class Measure:
    description: str   # e.g. "cup", "medium or grande - each 16 fl oz", "capsules"
    quantity: float    # how many of `description` the measure stands for (e.g. 5 capsules)
    weight: float      # base units in one measure


@dataclass
class Food:
    food_id: int
    name: str
    nutrients: dict[int, float]   # per 100 base units (per 1 unit when per_unit)
    is_recipe: bool
    custom: bool                  # the user's own recipe or custom food: editable, so never cached
    per_unit: bool = False        # Serving Based recipe: values per serving, amount counts servings
    measures: dict[int, Measure] = field(default_factory=dict)  # measure id -> measure

    def scale(self, amount: float) -> float:
        """Factor to multiply nutrient values by for a serving of `amount` base units."""
        return amount if self.per_unit else amount / 100

    def to_json(self) -> dict:
        return {"food_id": self.food_id, "name": self.name, "nutrients": {str(k): v for k, v in self.nutrients.items()},
                "is_recipe": self.is_recipe, "custom": self.custom, "per_unit": self.per_unit,
                "measures": {str(k): [m.description, m.quantity, m.weight] for k, m in self.measures.items()}}

    @classmethod
    def from_json(cls, d: dict) -> "Food":
        return cls(int(d["food_id"]), d["name"], {int(k): float(v) for k, v in d["nutrients"].items()},
                   bool(d["is_recipe"]), bool(d["custom"]), bool(d.get("per_unit", False)),
                   {int(k): Measure(str(m[0]), float(m[1]), float(m[2])) for k, m in d.get("measures", {}).items()})


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
    kind, measures = _measures(tokens, table, food_id)
    per_unit = False
    if is_recipe:
        # Recipe "Serving Sizes" setting: Serving Based -> nutrients per serving and amounts
        # count servings; Weight Based -> per 100 g like any food.
        full = next((m for m in measures.values() if m.description == "full recipe"), None)
        per_unit = kind == SERVING_BASED if kind is not None else bool(full and full.weight == 1.0)

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
    return Food(food_id, name, nutrients, is_recipe, custom, per_unit, measures)


WEIGHT_BASED, SERVING_BASED = 0, 3  # Measure$Type ordinals seen on recipes


_MEASURE_CLASSES = ("Measure", "DerivedMeasure")


def _measures(tokens: list, table: list[str], food_id: int) -> tuple[int | None, dict[int, Measure]]:
    """(first Measure$Type ordinal seen, {measure id: Measure}) for a food.

    Measure / DerivedMeasure fields in read order:
        quantity, flag, food id, 0, measure id, gram weight (Double or null), description ref,
        translations (HashMap of Localization -> MeasureTranslation, or null),
        Measure$Type enum (class + ordinal the first time, back-reference after), weight.
    The food's own measures are found by their food id; any measure that doesn't decode
    cleanly is skipped (it only affects how an amount is displayed).
    """
    def cls(v) -> str | None:
        return table[v - 1].split("/")[0].rsplit(".", 1)[-1] if isinstance(v, int) and 0 < v <= len(table) else None

    def skip(i: int) -> int:
        """Skip one serialized object (null, back-reference, or one of the translation classes)."""
        t = tokens[i]
        if not isinstance(t, int):
            raise GwtError("expected object")
        if t <= 0:
            return i + 1
        c = cls(t)
        if c == "Localization":
            return i + 2                      # class, language string
        if c == "MeasureTranslation":
            i = skip(i + 1)                   # Localization
            return i + 3                      # measure id, translation id (long), description
        if c == "Double":
            return i + 2
        raise GwtError(f"unexpected {c} in measure")

    kind: int | None = None
    out: dict[int, Measure] = {}
    for p, v in enumerate(tokens):
        if cls(v) not in _MEASURE_CLASSES or p + 3 >= len(tokens) or tokens[p + 3] != food_id:
            continue
        try:
            quantity, mid, i = tokens[p + 1], tokens[p + 5], skip(p + 6)
            desc = tokens[i]
            i += 1
            t = tokens[i]
            if isinstance(t, int) and t > 0 and cls(t) == "HashMap":
                n, i = tokens[i + 1], i + 2
                for _ in range(n):
                    i = skip(skip(i))
            else:
                i = skip(i)
            t = tokens[i]
            if isinstance(t, int) and t > 0:  # enum class + ordinal
                ordinal, i = tokens[i + 1], i + 2
                if kind is None and isinstance(ordinal, int):
                    kind = ordinal
            else:
                i += 1
            weight = tokens[i]
            if (isinstance(quantity, (int, float)) and isinstance(mid, int) and isinstance(desc, int)
                    and 1 <= desc <= len(table) and isinstance(weight, (int, float))):
                out.setdefault(mid, Measure(" ".join(table[desc - 1].split()), float(quantity), float(weight)))
        except (GwtError, IndexError):
            continue
    return kind, out
