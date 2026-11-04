"""Data loaders for canonical CSV files. OWNER: data-steward.

Selects exactly the retained headers from the canonical data analysis guide,
parses typed fields, and builds pydantic contract objects for downstream use.
"""
from __future__ import annotations

import ast
import json
import logging
import math
import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from gtgen.contracts import (
    BuyerConstraint,
    CandidateConstraint,
    CandidateView,
    CatalogueView,
    FramingDecision,
    Hardness,
    ScenarioBrief,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- canonical headers
# Exactly the retained columns per §4 of the canonical data analysis guide.

_FINAL_CONSTRAINTS_COLS = [
    "bid_id", "constraint_id", "golden_param", "attribute", "spec_name",
    "buyer_value", "operator", "unit", "allowed_values", "allows_higher",
    "constraint_category", "constraint_type", "scope", "status", "condition",
    "administrative_only", "matchability", "matching_relevance",
    "indiamart_matchable", "requirement_description", "requirement_group_id",
    "requirement_group_type", "line_item_id", "raw_text", "confidence",
    "evidence_status", "evidence_anchor", "source_document",
]

_GEM_BIDS_METADATA_COLS = [
    "bid_id", "bid_number", "bid_year", "buyer_org", "department",
    "product_category", "category_family", "quantity", "evaluation_type",
    "bid_start_date", "bid_end_date", "b_status", "b_buyer_status",
    "bid_document_url", "documents", "other_document_urls",
]

_PRODUCTS_COLS = [
    "product_id", "product_url", "product_name", "supplier_id",
    "supplier_name", "supplier_url", "supplier_website_url", "category_name",
    "product_target", "gem_bid_id", "search_keyword", "relevance_score",
    "price", "price_formatted", "moq", "moq_type", "description",
    "page_supplier", "specifications", "supplier_facts", "jsonld_raw",
    "brochure_candidate_pdfimage_urls", "image_urls", "brochure_status",
    "brochure_source_type", "brochure_page_urls", "brochure_pages",
    "brochure_ocr_required", "brochure_text", "brochure_page_text",
    "scraped_at",
]

_SUPPLIERS_COLS = [
    "supplier_id", "supplier_native_id", "supplier_name", "supplier_url",
    "supplier_website_url", "city", "state", "gst_number", "rating",
    "rating_count", "member_since", "storefront_status",
    "first_seen_category", "first_seen_at",
]

_PRODUCT_CONSTRAINTS_COLS = [
    "product_id", "supplier_id", "gem_bid_id", "constraint_id", "category",
    "scope", "constraint_type", "attribute", "spec_name", "operator", "value",
    "allowed_values", "min_value", "max_value", "unit", "raw_text",
    "raw_text_verified", "confidence", "source_field",
]

_PRODUCTS_WITH_CONSTRAINTS_COLS = [
    "product_id", "product_url", "product_name", "supplier_id",
    "supplier_name", "supplier_url", "supplier_website_url", "category_name",
    "product_target", "gem_bid_id", "search_keyword", "relevance_score",
    "price", "price_formatted", "moq", "moq_type", "description",
    "page_supplier", "specifications", "supplier_facts", "jsonld_raw",
    "brochure_candidate_pdfimage_urls", "image_urls", "brochure_status",
    "brochure_source_type", "brochure_page_urls", "brochure_pages",
    "brochure_ocr_required", "brochure_text", "brochure_page_text",
    "constraint_extraction", "scraped_at",
]


# ---------------------------------------------------------------- helpers

def _is_empty_cell(val: object) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    return isinstance(val, str) and val.strip() in ("", "nan")


def _parse_json_cell(val: object, kind: type) -> tuple[object, Optional[str], Optional[str]]:
    """Parse a JSON cell expected to be a *kind* (list or dict).

    Returns ``(parsed, raw, error)``. Empty cells and empty containers give
    ``(None, None, None)``. On failure (bad JSON or wrong JSON type) ``parsed`` is
    None, ``raw`` is the verbatim cell and ``error`` describes the problem (D019).
    """
    if _is_empty_cell(val):
        return None, None, None
    if isinstance(val, kind):
        return (val or None), None, None
    raw = str(val)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return None, raw, f"{type(exc).__name__}: {exc}"
    if not isinstance(parsed, kind):
        return None, raw, f"expected JSON {kind.__name__}, got {type(parsed).__name__}"
    if not parsed:
        return None, None, None
    return parsed, None, None


def _json_list(val: object) -> tuple[Optional[list[str]], Optional[str]]:
    """Return ``(allowed_values, allowed_values_raw)``."""
    parsed, raw, _ = _parse_json_cell(val, list)
    return ([str(v) for v in parsed] if parsed else None), raw


def _json_dict(val: object) -> tuple[Optional[dict], Optional[str]]:
    """Return ``(specifications, specifications_raw)``."""
    parsed, raw, _ = _parse_json_cell(val, dict)
    return parsed, raw


def _nan_to_none(val: object) -> object:
    """Convert NaN/NaT to None; pass through everything else."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    return val


def _nan_to_none_str(val: object) -> Optional[str]:
    """Return str or None."""
    v = _nan_to_none(val)
    return str(v) if v is not None else None


def _nan_to_none_float(val: object) -> Optional[float]:
    """Return float or None."""
    v = _nan_to_none(val)
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _nan_to_none_int(val: object) -> Optional[int]:
    """Return int or None."""
    v = _nan_to_none(val)
    if v is None:
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _nan_to_none_bool(val: object) -> Optional[bool]:
    """Return bool or None."""
    v = _nan_to_none(val)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


def _true_means(settings: dict) -> str:
    """Return validated ``golden_param_true_means`` (D020: mandatory, no default)."""
    if not isinstance(settings, dict) or "golden_param_true_means" not in settings:
        raise ValueError("settings.golden_param_true_means is required (HARD or SOFT)")
    mapping = settings["golden_param_true_means"]
    if mapping not in ("HARD", "SOFT"):
        raise ValueError(
            f"settings.golden_param_true_means must be 'HARD' or 'SOFT', got {mapping!r}"
        )
    return mapping


def _hardness_from_golden_param(golden: bool, settings: dict) -> Hardness:
    """Map golden_param bool to Hardness enum using settings only (D001/D020)."""
    if _true_means(settings) == "HARD":
        return Hardness.HARD if golden else Hardness.SOFT
    return Hardness.SOFT if golden else Hardness.HARD


# Columns that the newer exports store as exploded ``<column>.<key>`` headers instead of
# one JSON object per row.  They are reconstructed here so the canonical schema (and every
# downstream contract) stays unchanged.
_WIDE_PREFIX_COLUMNS = frozenset({"specifications", "supplier_facts", "constraint_extraction"})

# ``products_with_constraints.csv`` now carries the extracted constraint list under
# ``constraints`` (a Python-literal list) while ``constraint_extraction.*`` only holds
# extraction metadata.  Fold the list into the canonical ``constraint_extraction`` object.
_CONSTRAINT_LIST_COLUMN = "constraints"


def _json_or_python_literal(val: object) -> tuple[object, Optional[str]]:
    """Parse *val* as JSON, falling back to a Python literal (``None`` values etc.).

    The newer constraint export writes Python ``repr`` lists (single quotes) rather than
    JSON, so ``json.loads`` alone is not sufficient.  Returns ``(parsed, error)``.
    """
    if _is_empty_cell(val):
        return None, None
    raw = str(val)
    try:
        return json.loads(raw), None
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        return ast.literal_eval(raw), None
    except (ValueError, SyntaxError, TypeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _collapse_wide_column(
    df: pd.DataFrame, prefix: str, cols: Optional[list[str]] = None,
) -> Optional[list[Optional[dict]]]:
    """Collapse ``prefix.<key>`` columns into one dict per row.

    Sparse cells (NaN / empty strings) are omitted.  Returns ``None`` when the file has
    no matching columns, so the caller can report the canonical column as missing.
    """
    if cols is None:
        cols = [c for c in df.columns if c.startswith(prefix)]
    if not cols:
        return None
    names = [c[len(prefix):] for c in cols]
    out: list[Optional[dict]] = [None] * len(df)
    for name, col in zip(names, cols):
        series = df[col]
        mask = series.notna()
        if not mask.any():
            continue
        for i in np.flatnonzero(mask.to_numpy()):
            value = series.iat[int(i)]
            if isinstance(value, str) and not value.strip():
                continue
            row = out[int(i)]
            if row is None:
                row = {}
                out[int(i)] = row
            row[name] = value.item() if hasattr(value, "item") else value
    return out


def _synthesize_constraint_extraction(df: pd.DataFrame) -> Optional[list[Optional[str]]]:
    """Build canonical ``constraint_extraction`` JSON from wide columns + ``constraints``."""
    has_wide = any(c.startswith("constraint_extraction.") for c in df.columns)
    has_list = _CONSTRAINT_LIST_COLUMN in df.columns
    if not has_wide and not has_list:
        return None
    wide = _collapse_wide_column(df, "constraint_extraction.")
    out: list[Optional[str]] = []
    for i in range(len(df)):
        payload: dict = dict(wide[i] or {}) if wide is not None else {}
        if has_list:
            parsed, err = _json_or_python_literal(df[_CONSTRAINT_LIST_COLUMN].iat[i])
            if err is None:
                payload["constraints"] = parsed
            else:
                # Keep the raw value so the parse-failure report can surface it.
                payload["constraints_raw"] = str(df[_CONSTRAINT_LIST_COLUMN].iat[i])
                payload["constraints_error"] = err
        out.append(json.dumps(payload, ensure_ascii=False, default=str) if payload else None)
    return out


def _synthesize_wide_column(df: pd.DataFrame, column: str) -> Optional[list[Optional[dict]]]:
    """Reconstruct a canonical JSON-object column from ``<column>.<key>`` headers."""
    if column == "constraint_extraction":
        return _synthesize_constraint_extraction(df)  # type: ignore[return-value]
    if column in _WIDE_PREFIX_COLUMNS:
        return _collapse_wide_column(df, f"{column}.")
    return None


def _read_csv(raw_dir: str, filename: str, canonical_cols: list[str]) -> pd.DataFrame:
    """Read a CSV with BOM handling, select canonical columns only.

    Older exports contain JSON-object columns (``specifications``, ``supplier_facts``,
    ``constraint_extraction``); newer exports explode them into ``<column>.<key>`` headers
    and (for constraints) a ``constraints`` Python-literal list.  Both layouts are
    accepted and produce the same canonical DataFrame.
    """
    path = Path(raw_dir) / filename
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    # Strip any whitespace from column names (defensive)
    df.columns = df.columns.str.strip()
    missing = []
    synthesized_cols: dict[str, list] = {}
    for col in canonical_cols:
        if col in df.columns:
            continue
        synthesized = _synthesize_wide_column(df, col)
        if synthesized is not None:
            synthesized_cols[col] = synthesized
            continue
        missing.append(col)
    if missing:
        raise ValueError(f"{filename}: missing canonical columns: {missing}")
    if synthesized_cols:
        df = pd.concat(
            [df, pd.DataFrame(synthesized_cols, index=df.index)], axis=1,
        )
    extra = [c for c in df.columns if c not in canonical_cols]
    if extra:
        preview = ", ".join(extra[:10]) + (" ..." if len(extra) > 10 else "")
        logger.warning("%s: dropping %d non-canonical column(s): %s",
                       filename, len(extra), preview)
    return df[list(canonical_cols)]


# ---------------------------------------------------------------- settings

def load_settings(path: str = "config/settings.yaml") -> dict:
    """Load project settings from YAML. Requires PyYAML (``pip install pyyaml``)."""
    import yaml  # lazy import; PyYAML is needed only when reading the YAML file
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- CSV loaders

def load_final_constraints(raw_dir: str, settings: dict) -> pd.DataFrame:
    """Load final_constraints.csv with canonical columns only."""
    _true_means(settings)  # D020: fail at load time if the setting is missing/invalid
    df = _read_csv(raw_dir, "final_constraints.csv", _FINAL_CONSTRAINTS_COLS)
    # Parse golden_param to bool (CSV may have string True/False)
    df = df.copy()
    raw = df["golden_param"]
    parsed = raw.map(
        lambda v: v if isinstance(v, bool)
        else {"true": True, "false": False}.get(str(v).strip().lower())
    )
    bad = parsed.isna()
    if bad.any():
        details = {int(i): raw.loc[i] for i in raw.index[bad][:20]}
        raise ValueError(
            f"final_constraints.golden_param has {int(bad.sum())} unexpected "
            f"value(s) (row index: value): {details}"
        )
    df["golden_param"] = parsed.astype(bool)
    return df


def load_gem_bids_metadata(raw_dir: str) -> pd.DataFrame:
    """Load gem_bids_metadata.csv with canonical columns only."""
    return _read_csv(raw_dir, "gem_bids_metadata.csv", _GEM_BIDS_METADATA_COLS)


def load_products(raw_dir: str) -> pd.DataFrame:
    """Load products.csv with canonical columns only."""
    return _read_csv(raw_dir, "products.csv", _PRODUCTS_COLS)


def load_suppliers(raw_dir: str) -> pd.DataFrame:
    """Load suppliers.csv with canonical columns only."""
    return _read_csv(raw_dir, "suppliers.csv", _SUPPLIERS_COLS)


def load_product_constraints(raw_dir: str) -> pd.DataFrame:
    """Load product_constraints.csv with canonical columns only."""
    return _read_csv(raw_dir, "product_constraints.csv", _PRODUCT_CONSTRAINTS_COLS)


def load_products_with_constraints(raw_dir: str) -> pd.DataFrame:
    """Load products_with_constraints.csv with canonical columns only."""
    return _read_csv(raw_dir, "products_with_constraints.csv",
                     _PRODUCTS_WITH_CONSTRAINTS_COLS)


# ---------------------------------------------------------------- contract builders

def build_buyer_constraints(
    bid_id: str, raw_dir: str, settings: dict,
) -> list[BuyerConstraint]:
    """Build BuyerConstraint objects for all constraints belonging to *bid_id*."""
    df = load_final_constraints(raw_dir, settings)
    df = df[df["bid_id"] == bid_id]

    results: list[BuyerConstraint] = []
    for _, row in df.iterrows():
        golden = bool(row["golden_param"])
        hardness = _hardness_from_golden_param(golden, settings)
        av, av_raw = _json_list(row.get("allowed_values"))
        bc = BuyerConstraint(
            constraint_id=str(row["constraint_id"]),
            bid_id=str(row["bid_id"]),
            golden_param=golden,
            hardness=hardness,
            attribute=str(row["attribute"]),
            spec_name=_nan_to_none_str(row.get("spec_name")),
            buyer_value=_nan_to_none_str(row.get("buyer_value")),
            operator=_nan_to_none_str(row.get("operator")),
            unit=_nan_to_none_str(row.get("unit")),
            allowed_values=av,
            allowed_values_raw=av_raw,
            allows_higher=_nan_to_none_bool(row.get("allows_higher")),
            constraint_category=_nan_to_none_str(row.get("constraint_category")),
            constraint_type=_nan_to_none_str(row.get("constraint_type")),
            scope=_nan_to_none_str(row.get("scope")),
            condition=_nan_to_none_str(row.get("condition")),
            # LLM-filled fields left at defaults
            is_doc_proof=False,
            doc_name=None,
            framing_decision=FramingDecision.KEEP,
            commercial_text=None,
            raw_text=_nan_to_none_str(row.get("raw_text")),
            evidence_anchor=_nan_to_none_str(row.get("evidence_anchor")),
            llm_call_ids=[],
        )
        results.append(bc)
    return results


def build_candidate_constraints(
    bid_id: str, raw_dir: str,
) -> list[CandidateConstraint]:
    """Build CandidateConstraint objects for products associated with *bid_id*."""
    df = load_product_constraints(raw_dir)
    df = df[df["gem_bid_id"] == bid_id]

    results: list[CandidateConstraint] = []
    for _, row in df.iterrows():
        av, av_raw = _json_list(row.get("allowed_values"))
        cc = CandidateConstraint(
            product_id=str(row["product_id"]),
            supplier_id=str(row["supplier_id"]),
            constraint_id=str(row["constraint_id"]),
            category=_nan_to_none_str(row.get("category")),
            attribute=str(row["attribute"]),
            spec_name=_nan_to_none_str(row.get("spec_name")),
            operator=_nan_to_none_str(row.get("operator")),
            value=_nan_to_none_str(row.get("value")),
            allowed_values=av,
            allowed_values_raw=av_raw,
            min_value=_nan_to_none_float(row.get("min_value")),
            max_value=_nan_to_none_float(row.get("max_value")),
            unit=_nan_to_none_str(row.get("unit")),
            raw_text=_nan_to_none_str(row.get("raw_text")),
            raw_text_verified=_nan_to_none_bool(row.get("raw_text_verified")),
            confidence=_nan_to_none_float(row.get("confidence")),
            source_field=_nan_to_none_str(row.get("source_field")),
        )
        results.append(cc)
    return results


def build_catalogue_view(
    bid_id: str, raw_dir: str, scenario_id: str,
) -> CatalogueView:
    """Build a CatalogueView for *bid_id*. Excludes bid-derived fields and gst_number."""
    products_df = load_products(raw_dir)
    products_df = products_df[products_df["gem_bid_id"] == bid_id]

    suppliers_df = load_suppliers(raw_dir)
    # Drop gst_number before any join — it must never leave the data layer
    suppliers_df = suppliers_df.drop(columns=["gst_number"], errors="ignore")

    pc_df = load_product_constraints(raw_dir)
    pc_df = pc_df[pc_df["gem_bid_id"] == bid_id]

    # Join products with suppliers on supplier_id
    merged = products_df.merge(
        suppliers_df[["supplier_id", "city", "state", "rating", "rating_count",
                       "member_since"]],
        on="supplier_id",
        how="left",
        suffixes=("", "_sup"),
    )

    # Build constraint lookup: product_id -> list[CandidateConstraint]
    constraints_by_product: dict[str, list[CandidateConstraint]] = {}
    for _, row in pc_df.iterrows():
        pid = str(row["product_id"])
        av, av_raw = _json_list(row.get("allowed_values"))
        cc = CandidateConstraint(
            product_id=pid,
            supplier_id=str(row["supplier_id"]),
            constraint_id=str(row["constraint_id"]),
            category=_nan_to_none_str(row.get("category")),
            attribute=str(row["attribute"]),
            spec_name=_nan_to_none_str(row.get("spec_name")),
            operator=_nan_to_none_str(row.get("operator")),
            value=_nan_to_none_str(row.get("value")),
            allowed_values=av,
            allowed_values_raw=av_raw,
            min_value=_nan_to_none_float(row.get("min_value")),
            max_value=_nan_to_none_float(row.get("max_value")),
            unit=_nan_to_none_str(row.get("unit")),
            raw_text=_nan_to_none_str(row.get("raw_text")),
            raw_text_verified=_nan_to_none_bool(row.get("raw_text_verified")),
            confidence=_nan_to_none_float(row.get("confidence")),
            source_field=_nan_to_none_str(row.get("source_field")),
        )
        constraints_by_product.setdefault(pid, []).append(cc)

    candidates: list[CandidateView] = []
    for _, row in merged.iterrows():
        pid = str(row["product_id"])
        specs, specs_raw = _json_dict(row.get("specifications"))
        cv = CandidateView(
            product_id=pid,
            supplier_id=str(row["supplier_id"]),
            product_name=str(row["product_name"]),
            category_name=_nan_to_none_str(row.get("category_name")),
            price=_nan_to_none_float(row.get("price")),
            price_formatted=_nan_to_none_str(row.get("price_formatted")),
            moq=_nan_to_none_float(row.get("moq")),
            moq_type=_nan_to_none_str(row.get("moq_type")),
            description=_nan_to_none_str(row.get("description")),
            specifications=specs,
            specifications_raw=specs_raw,
            brochure_text=_nan_to_none_str(row.get("brochure_text")),
            supplier_name=_nan_to_none_str(row.get("supplier_name")),
            city=_nan_to_none_str(row.get("city")),
            state=_nan_to_none_str(row.get("state")),
            rating=_nan_to_none_float(row.get("rating")),
            rating_count=_nan_to_none_int(row.get("rating_count")),
            member_since=_nan_to_none_str(row.get("member_since")),
            constraints=constraints_by_product.get(pid, []),
        )
        candidates.append(cv)

    return CatalogueView(scenario_id=scenario_id, candidates=candidates)


def build_scenario_brief(
    bid_id: str, raw_dir: str, settings: dict, scenario_id: str,
) -> ScenarioBrief:
    """Build a ScenarioBrief for *bid_id*."""
    constraints = build_buyer_constraints(bid_id, raw_dir, settings)

    # Get quantity from gem_bids_metadata
    meta = load_gem_bids_metadata(raw_dir)
    meta_row = meta[meta["bid_number"] == bid_id]

    quantity = 1.0
    quantity_unit = "units"
    if len(meta_row) > 0:
        raw_qty = meta_row.iloc[0]["quantity"]
        if raw_qty is not None and not (isinstance(raw_qty, float) and math.isnan(raw_qty)):
            quantity = float(raw_qty)

    # Look for budget/price constraint
    budget_inr: Optional[float] = None
    for c in constraints:
        cat_lower = (c.constraint_category or "").lower()
        attr_lower = (c.attribute or "").lower()
        if "price" in cat_lower or "budget" in cat_lower or \
           "price" in attr_lower or "budget" in attr_lower:
            if c.buyer_value is not None:
                try:
                    budget_inr = float(c.buyer_value)
                except (ValueError, TypeError):
                    pass
            break

    return ScenarioBrief(
        scenario_id=scenario_id,
        bid_ref=bid_id,
        quantity=quantity,
        quantity_unit=quantity_unit,
        budget_inr=budget_inr,
        constraints=constraints,
    )


# ---------------------------------------------------------------- parse-failure report

# table -> (loader, row-key columns, JSON columns). Every JSON-serialised column of every
# loaded table is checked; expected container type is a list or dict, otherwise any JSON.
_JSON_COLUMNS: dict[str, dict[str, Optional[type]]] = {
    "final_constraints": {"allowed_values": list},
    "gem_bids_metadata": {"documents": None, "other_document_urls": None},
    "products": {"specifications": dict, "supplier_facts": dict, "jsonld_raw": None},
    "product_constraints": {"allowed_values": list},
    "products_with_constraints": {
        "specifications": dict, "supplier_facts": dict, "jsonld_raw": None,
        "constraint_extraction": None,
    },
}
_ROW_KEYS: dict[str, list[str]] = {
    "final_constraints": ["bid_id", "constraint_id"],
    "gem_bids_metadata": ["bid_number"],
    "products": ["product_id", "gem_bid_id"],
    "product_constraints": ["product_id", "constraint_id"],
    "products_with_constraints": ["product_id", "gem_bid_id"],
}
PARSE_FAILURE_COLUMNS = ["table", "row_key", "column", "raw_value", "error"]


def _load_all(raw_dir: str, settings: dict) -> dict[str, pd.DataFrame]:
    return {
        "final_constraints": load_final_constraints(raw_dir, settings),
        "gem_bids_metadata": load_gem_bids_metadata(raw_dir),
        "products": load_products(raw_dir),
        "suppliers": load_suppliers(raw_dir),
        "product_constraints": load_product_constraints(raw_dir),
        "products_with_constraints": load_products_with_constraints(raw_dir),
    }


def _parse_failures_for(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    recs: list[dict] = []
    for table, cols in _JSON_COLUMNS.items():
        df = tables[table]
        keys = _ROW_KEYS[table]
        for col, kind in cols.items():
            for idx, val in df[col].items():
                if _is_empty_cell(val):
                    continue
                if kind is not None:
                    _, raw, err = _parse_json_cell(val, kind)
                    if err is None:
                        continue
                else:
                    # Free-form JSON columns: the newer export writes Python literals
                    # (single-quoted lists/dicts), so accept either representation.
                    _, err = _json_or_python_literal(val)
                    if err is None:
                        continue
                    raw = str(val)
                key = "|".join(str(df.at[idx, k]) for k in keys)
                recs.append({"table": table, "row_key": key, "column": col,
                             "raw_value": raw, "error": err})
    return pd.DataFrame(recs, columns=PARSE_FAILURE_COLUMNS)


def parse_failure_report(raw_dir: str, settings: dict) -> pd.DataFrame:
    """Rows whose JSON-serialised cells failed to parse (D019).

    Columns: table, row_key, column, raw_value, error. Empty cells are not failures.
    """
    return _parse_failures_for(_load_all(raw_dir, settings))


# ---------------------------------------------------------------- canonical parquet

CANONICAL_TABLES: dict[str, list[str]] = {
    "final_constraints": _FINAL_CONSTRAINTS_COLS,
    "gem_bids_metadata": _GEM_BIDS_METADATA_COLS,
    "products": _PRODUCTS_COLS,
    # gst_number is retained by the guide but must not leave src/gtgen/data/ (CLAUDE.md §7),
    # so it is excluded from the on-disk parquet.
    "suppliers": [c for c in _SUPPLIERS_COLS if c != "gst_number"],
    "product_constraints": _PRODUCT_CONSTRAINTS_COLS,
    "products_with_constraints": _PRODUCTS_WITH_CONSTRAINTS_COLS,
}


def _parquet_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Make mixed-type object columns writable: non-null values become str."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].map(lambda v: v if v is None or (isinstance(v, float) and math.isnan(v))
                                or isinstance(v, str) else str(v))
            out[c] = out[c].astype("string")
    return out


def write_canonical(raw_dir: str, out_dir: str, settings: dict) -> dict[str, Path]:
    """Write one parquet per table (canonical columns only, no gst_number) to *out_dir*.

    Also writes ``parse_failures.csv`` (D019). JSON columns keep their original raw string.
    Returns a mapping table name -> parquet path (parse_failures under key
    ``"parse_failures"``).
    """
    tables = _load_all(raw_dir, settings)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, cols in CANONICAL_TABLES.items():
        path = out / f"{name}.parquet"
        _parquet_safe(tables[name][cols]).to_parquet(path, index=False)
        written[name] = path
    pf_path = out / "parse_failures.csv"
    _parse_failures_for(tables).to_csv(pf_path, index=False)
    written["parse_failures"] = pf_path
    return written


# ---------------------------------------------------------------- profile

_NUMERIC_OPS = {"<", "<=", ">", ">=", "=", "==", "between"}


def _is_number(v: object) -> bool:
    if _is_empty_cell(v):
        return False
    try:
        float(re.sub(r"[,\s]", "", str(v)))
        return True
    except ValueError:
        return False


def _orphans(a: pd.Series, b: pd.Series) -> tuple[int, int, int, int]:
    """(distinct in a, a not in b, distinct in b, b not in a); NaN keys ignored."""
    sa, sb = set(a.dropna().astype(str)), set(b.dropna().astype(str))
    return len(sa), len(sa - sb), len(sb), len(sb - sa)


def _numeric_soft_product(fc_soft: pd.DataFrame) -> pd.DataFrame:
    """golden_param=False constraints that are numeric (operator, numeric value, unit) and
    product-matchable (matchability == 'product', not administrative_only). Transaction or
    supplier scope numerics (delivery days, EMD, turnover) cannot be widened against
    product catalogue values."""
    m = (fc_soft["operator"].notna() & fc_soft["buyer_value"].map(_is_number)
         & fc_soft["unit"].notna() & (fc_soft["matchability"] == "product")
         & (fc_soft["administrative_only"] != True))  # noqa: E712 (NaN-safe)
    return fc_soft[m]


def _bid_stats(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    fc, products = tables["final_constraints"], tables["products"]
    pwc = tables["products_with_constraints"]
    pc = tables["product_constraints"]
    rows = []
    for bid in sorted(fc["bid_id"].unique()):
        b = fc[fc["bid_id"] == bid]
        soft = b[~b["golden_param"]]
        numeric_soft = soft[soft["operator"].notna() & soft["buyer_value"].map(_is_number)
                            & soft["unit"].notna()]
        numeric_soft_prod = _numeric_soft_product(soft)
        bp = products[products["gem_bid_id"] == bid]
        rows.append({
            "bid_id": bid,
            "constraints": len(b),
            "golden_true": int(b["golden_param"].sum()),
            "golden_false": int((~b["golden_param"]).sum()),
            "numeric_soft_any": len(numeric_soft),
            "numeric_soft": len(numeric_soft_prod),
            "cand_products": bp["product_id"].nunique(),
            "cand_suppliers": bp["supplier_id"].nunique(),
            "cand_with_constraints": pwc[pwc["gem_bid_id"] == bid]["product_id"].nunique(),
            "cand_constraint_rows": int((pc["gem_bid_id"] == bid).sum()),
        })
    return pd.DataFrame(rows)


def profile(raw_dir: str = "data/raw", settings: Optional[dict] = None) -> str:
    """Markdown data profile of the canonical (retained-column) tables."""
    if settings is None:
        settings = load_settings()
    min_shortlist = int(settings.get("negotiation", {}).get("min_shortlist", 3))
    true_means = _true_means(settings)
    tables = _load_all(raw_dir, settings)
    fc, meta = tables["final_constraints"], tables["gem_bids_metadata"]
    products, suppliers = tables["products"], tables["suppliers"]
    pc, pwc = tables["product_constraints"], tables["products_with_constraints"]
    L: list[str] = ["# Data Profile", "",
                    f"golden_param True means: **{true_means}** (settings.yaml).", ""]

    L += ["## Row counts", "", "| Table | Rows | Retained columns |", "|---|---|---|"]
    for name, df in tables.items():
        L.append(f"| {name} | {len(df)} | {len(df.columns)} |")
    L.append("")

    # per-bid
    stats = _bid_stats(tables)
    L += ["## Per-bid candidates and golden_param", "",
          "Candidates = distinct product_id / supplier_id in products.csv via "
          "products.gem_bid_id = final_constraints.bid_id. Retrieval is not compliance.", "",
          "| bid_id | constraints | golden True | golden False | numeric SOFT (any scope) | numeric SOFT (product-matchable) | "
          "cand. products | cand. suppliers | cand. with extracted constraints | "
          "cand. constraint rows |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in stats.itertuples():
        L.append(f"| {r.bid_id} | {r.constraints} | {r.golden_true} | {r.golden_false} | "
                 f"{r.numeric_soft_any} | {r.numeric_soft} | {r.cand_products} | {r.cand_suppliers} | "
                 f"{r.cand_with_constraints} | {r.cand_constraint_rows} |")
    L.append("")

    # orphans
    L += ["## Join orphan counts", "",
          "Each row: distinct keys on each side and how many have no partner on the other side. "
          "Names are never join keys.", "",
          "| Join (left <-> right) | distinct left | left orphans | distinct right | "
          "right orphans |", "|---|---|---|---|---|"]
    joins = [
        ("gem_bids_metadata.bid_number <-> final_constraints.bid_id",
         meta["bid_number"], fc["bid_id"]),
        ("final_constraints.bid_id <-> products.gem_bid_id", fc["bid_id"], products["gem_bid_id"]),
        ("gem_bids_metadata.bid_number <-> products.gem_bid_id",
         meta["bid_number"], products["gem_bid_id"]),
        ("products.product_id <-> product_constraints.product_id",
         products["product_id"], pc["product_id"]),
        ("products.product_id <-> products_with_constraints.product_id",
         products["product_id"], pwc["product_id"]),
        ("products_with_constraints.product_id <-> product_constraints.product_id",
         pwc["product_id"], pc["product_id"]),
        ("products.supplier_id <-> suppliers.supplier_id",
         products["supplier_id"], suppliers["supplier_id"]),
        ("products_with_constraints.supplier_id <-> suppliers.supplier_id",
         pwc["supplier_id"], suppliers["supplier_id"]),
        ("product_constraints.supplier_id <-> suppliers.supplier_id",
         pc["supplier_id"], suppliers["supplier_id"]),
        ("product_constraints.gem_bid_id <-> final_constraints.bid_id",
         pc["gem_bid_id"], fc["bid_id"]),
        ("products_with_constraints.gem_bid_id <-> final_constraints.bid_id",
         pwc["gem_bid_id"], fc["bid_id"]),
    ]
    for label, a, b in joins:
        na, oa, nb, ob = _orphans(a, b)
        L.append(f"| {label} | {na} | {oa} | {nb} | {ob} |")
    pairs_p = set(zip(products["product_id"].astype(str), products["gem_bid_id"].astype(str)))
    pairs_c = set(zip(pwc["product_id"].astype(str), pwc["gem_bid_id"].astype(str)))
    L += ["", f"products rows: {len(products)}; distinct product_id: "
          f"{products['product_id'].nunique()}; distinct (product_id, gem_bid_id): {len(pairs_p)}; "
          f"(product_id, gem_bid_id) in products_with_constraints but not products: "
          f"{len(pairs_c - pairs_p)}; in products but not products_with_constraints: "
          f"{len(pairs_p - pairs_c)}.", ""]

    # null rates
    L += ["## Null rates of retained columns", "",
          "Empty/NaN cells; missingness is preserved, never filled.", ""]
    for name, df in tables.items():
        L += [f"### {name}", "", "| column | null count | null % |", "|---|---|---|"]
        n = len(df)
        for c in df.columns:
            k = int(df[c].isna().sum())
            L.append(f"| {c} | {k} | {(k / n * 100 if n else 0):.1f}% |")
        L.append("")

    # parse failures
    pf = _parse_failures_for(tables)
    L += ["## JSON parse failures (D019)", "",
          "| table | column | failures | non-empty cells |", "|---|---|---|---|"]
    for table, cols in _JSON_COLUMNS.items():
        for col in cols:
            nf = int(((pf["table"] == table) & (pf["column"] == col)).sum())
            ne = int((~tables[table][col].map(_is_empty_cell)).sum())
            L.append(f"| {table} | {col} | {nf} | {ne} |")
    L += ["", f"Total parse failures: {len(pf)}. Full list: data/canonical/parse_failures.csv.", ""]

    # pilot recommendation
    elig = stats[(stats["cand_products"] >= min_shortlist) & (stats["golden_true"] > 0)
                 & (stats["golden_false"] > 0) & (stats["numeric_soft"] > 0)]
    L += ["## Pilot bid recommendation", "",
          f"Criteria: >= {min_shortlist} candidate products (ideally >= 10), at least one HARD "
          "and one SOFT constraint, at least one numeric SOFT (golden_param=False) constraint "
          "that is product-matchable (operator, numeric buyer_value, unit present, "
          "matchability=product); transaction/supplier numerics such as delivery days or turnover "
          "cannot be widened against catalogue values and are not counted. Ranked by candidates having extracted constraints "
          "(so compliance is not all UNKNOWN), capped at 30, then numeric SOFT count, then "
          "constraint count.", ""]
    if elig.empty:
        L.append("No bid meets all criteria.")
    else:
        el = elig.assign(_cap=elig["cand_with_constraints"].clip(upper=30)).sort_values(
            ["_cap", "numeric_soft", "constraints"], ascending=False)
        top = el.iloc[0]
        hard_label = "HARD" if true_means == "HARD" else "SOFT"
        L += [f"**Recommended: `{top.bid_id}`** - {int(top.cand_products)} candidate products from "
              f"{int(top.cand_suppliers)} suppliers ({int(top.cand_with_constraints)} with extracted "
              f"constraints, {int(top.cand_constraint_rows)} candidate constraint rows); "
              f"{int(top.constraints)} buyer constraints: {int(top.golden_true)} golden_param=True "
              f"({hard_label}) and {int(top.golden_false)} golden_param=False; "
              f"{int(top.numeric_soft)} numeric constraints with golden_param=False for "
              "relaxation search.", ""]
        sel = _numeric_soft_product(fc[(fc["bid_id"] == top.bid_id) & (~fc["golden_param"])])
        L += ["Numeric product-matchable golden_param=False constraints of the pilot bid "
              "(caveat: a `quantity` row is an order size, not a catalogue spec):", "",
              "| constraint_id | attribute | operator | buyer_value | unit |", "|---|---|---|---|---|"]
        for r in sel.itertuples():
            L.append(f"| {r.constraint_id} | {r.attribute} | {r.operator} | {r.buyer_value} | "
                     f"{r.unit} |")
        L += ["", f"Top alternatives (only {max(len(el) - 1, 0)} other bid(s) meet all criteria):", "",
              "| bid_id | cand. products | with constraints | golden True/False | numeric SOFT (product-matchable) |",
              "|---|---|---|---|---|"]
        for r in el.iloc[1:4].itertuples():
            L.append(f"| {r.bid_id} | {r.cand_products} | {r.cand_with_constraints} | "
                     f"{r.golden_true}/{r.golden_false} | {r.numeric_soft} |")
    L.append("")
    return "\n".join(L)
