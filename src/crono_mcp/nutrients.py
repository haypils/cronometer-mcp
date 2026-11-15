"""Cronometer nutrient ids (mostly USDA SR numbers) -> the column names used by
Cronometer's CSV exports, so the RPC path and the export path produce the same keys.

Derived by matching getTotalNutrients output against the daily-summary export
for the same day; zero-valued columns were resolved with the USDA numbering.
"""

NAMES: dict[int, str] = {
    208: "Energy (kcal)",
    221: "Alcohol (g)",
    262: "Caffeine (mg)",
    10012: "Oxalate (mg)",
    246: "Phytate (mg)",
    255: "Water (g)",
    404: "B1 (Thiamine) (mg)",
    405: "B2 (Riboflavin) (mg)",
    406: "B3 (Niacin) (mg)",
    410: "B5 (Pantothenic Acid) (mg)",
    415: "B6 (Pyridoxine) (mg)",
    418: "B12 (Cobalamin) (µg)",
    417: "Folate (µg)",
    320: "Vitamin A (µg)",
    401: "Vitamin C (mg)",
    324: "Vitamin D (IU)",
    323: "Vitamin E (mg)",
    430: "Vitamin K (µg)",
    301: "Calcium (mg)",
    312: "Copper (mg)",
    303: "Iron (mg)",
    304: "Magnesium (mg)",
    315: "Manganese (mg)",
    305: "Phosphorus (mg)",
    306: "Potassium (mg)",
    317: "Selenium (µg)",
    307: "Sodium (mg)",
    309: "Zinc (mg)",
    -1205: "Net Carbs (g)",
    205: "Carbs (g)",
    291: "Fiber (g)",
    297: "Insoluble Fiber (g)",
    295: "Soluble Fiber (g)",
    209: "Starch (g)",
    269: "Sugars (g)",
    10009: "Added Sugars (g)",
    204: "Fat (g)",
    601: "Cholesterol (mg)",
    645: "Monounsaturated (g)",
    646: "Polyunsaturated (g)",
    606: "Saturated (g)",
    605: "Trans-Fats (g)",
    10001: "Omega-3 (g)",
    851: "ALA (g)",
    621: "DHA (g)",
    629: "EPA (g)",
    10002: "Omega-6 (g)",
    853: "AA (g)",
    675: "LA (g)",
    507: "Cystine (g)",
    512: "Histidine (g)",
    503: "Isoleucine (g)",
    504: "Leucine (g)",
    505: "Lysine (g)",
    506: "Methionine (g)",
    508: "Phenylalanine (g)",
    203: "Protein (g)",
    502: "Threonine (g)",
    501: "Tryptophan (g)",
    509: "Tyrosine (g)",
    510: "Valine (g)",
}

ENERGY, PROTEIN, CARBS, FAT = 208, 203, 205, 204


def named(values: dict[int, float]) -> dict[str, float]:
    """Map id-keyed values to export column names, dropping unmapped ids."""
    return {NAMES[k]: v for k, v in values.items() if k in NAMES}
