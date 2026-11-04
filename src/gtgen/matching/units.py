"""Small unit table. Conversion is only defined inside one physical dimension.

Unknown unit, or units from different dimensions -> ``convert`` returns None (caller emits UNKNOWN,
never FAIL).
"""
from __future__ import annotations

from typing import Optional

# unit (lower-case, normalised) -> (dimension, factor to the dimension's base unit)
_TABLE: dict[str, tuple[str, float]] = {}


def _add(dim: str, base_factor: dict[str, float]) -> None:
    for u, f in base_factor.items():
        _TABLE[u] = (dim, f)


_add("length", {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254, "inch": 0.0254,
                "ft": 0.3048, "feet": 0.3048})
_add("mass", {"mg": 1e-6, "g": 1e-3, "kg": 1.0, "t": 1000.0, "tonne": 1000.0, "lb": 0.45359237,
              "lbs": 0.45359237})
_add("power", {"w": 1.0, "kw": 1000.0, "mw": 1e6, "hp": 745.699872})
_add("current", {"ma": 0.001, "a": 1.0, "amp": 1.0, "amps": 1.0, "ka": 1000.0})
_add("voltage", {"mv": 0.001, "v": 1.0, "kv": 1000.0})
_add("volume", {"ml": 0.001, "l": 1.0, "ltr": 1.0, "litre": 1.0, "liter": 1.0})
_add("time", {"s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0, "hr": 3600.0, "day": 86400.0,
              "days": 86400.0, "week": 604800.0, "month": 2629800.0, "year": 31557600.0,
              "years": 31557600.0, "yr": 31557600.0})
_add("frequency", {"hz": 1.0, "khz": 1e3, "mhz": 1e6})
_add("pressure", {"pa": 1.0, "kpa": 1e3, "bar": 1e5, "psi": 6894.757})
_add("currency", {"inr": 1.0, "rs": 1.0, "rupee": 1.0, "rupees": 1.0})
# D088 live 209245: catalogue `size` uses "Sq mm" while the extractor emits "Sqmm"; treat the common
# spellings of square millimetres as one area unit so a valid range belief does not become UNKNOWN.
_add("area", {"sqmm": 1.0, "sq mm": 1.0, "sq. mm": 1.0, "sq.mm": 1.0,
              "mm2": 1.0, "mm²": 1.0, "sqcm": 100.0, "sq cm": 100.0, "sq. cm": 100.0,
              "cm2": 100.0, "cm²": 100.0, "sqm": 1e6, "sq m": 1e6, "sq. m": 1e6,
              "m2": 1e6, "m²": 1e6})
_add("count", {"pcs": 1.0, "pc": 1.0, "nos": 1.0, "no": 1.0, "unit": 1.0, "units": 1.0,
               "core": 1.0, "cores": 1.0})


def norm_unit(u: Optional[str]) -> Optional[str]:
    if u is None:
        return None
    s = str(u).strip().lower().rstrip(".")
    return s or None


def convert(value: float, from_unit: Optional[str], to_unit: Optional[str]) -> Optional[float]:
    """Convert value between units. Missing unit on either side -> value unchanged (assumed same).
    Identical strings -> unchanged. Unknown/incompatible pair -> None."""
    a, b = norm_unit(from_unit), norm_unit(to_unit)
    if a is None or b is None or a == b:
        return value
    ta, tb = _TABLE.get(a), _TABLE.get(b)
    if ta is None or tb is None or ta[0] != tb[0]:
        return None
    return value * ta[1] / tb[1]
