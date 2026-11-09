"""D106: a bid with no products.csv rows takes its candidates from the supplier_products.csv links. No network."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from gtgen.data import loaders


def _write(raw: Path, name: str, cols: list[str], rows: list[dict]) -> None:
    pd.DataFrame([{c: r.get(c, "") for c in cols} for r in rows], columns=cols).to_csv(raw / name, index=False)


def _raw_dir(tmp_path: Path) -> Path:
    # P1 is stored under its first bid B1; B2 found P1 too, so only supplier_products.csv links B2 to P1.
    _write(tmp_path, "products.csv", loaders._PRODUCTS_COLS, [
        {"product_id": 101, "product_name": "Wool blanket", "supplier_id": "s1", "gem_bid_id": "B1",
         "search_keyword": "home-bid keyword", "price": 500},
        {"product_id": 102, "product_name": "Cotton blanket", "supplier_id": "s1", "gem_bid_id": "B3", "price": 400},
    ])
    _write(tmp_path, "suppliers.csv", loaders._SUPPLIERS_COLS, [{"supplier_id": "s1", "state": "Punjab"}])
    _write(tmp_path, "product_constraints.csv", loaders._PRODUCT_CONSTRAINTS_COLS, [
        {"product_id": 101, "supplier_id": "s1", "gem_bid_id": "B1", "constraint_id": "c1", "attribute": "weight"},
        {"product_id": 101, "supplier_id": "s1", "gem_bid_id": "B9", "constraint_id": "c9", "attribute": "weight"},
    ])
    pd.DataFrame([
        {"supplier_id": "s1", "product_id": 101, "gem_bid_id": "B2", "search_keyword": "B2 secret keyword",
         "product_target": "B2 target", "relevance_score": 0.9},
        {"supplier_id": "s1", "product_id": 102, "gem_bid_id": "B1", "search_keyword": "x"},
    ]).to_csv(tmp_path / "supplier_products.csv", index=False)
    return tmp_path


def test_bid_without_products_rows_uses_supplier_product_links(tmp_path: Path) -> None:
    cat = loaders.build_catalogue_view("B2", str(_raw_dir(tmp_path)), scenario_id="B2_s0")
    assert [c.product_id for c in cat.candidates] == ["101"]
    # Evidence comes from the product's own row (home bid B1), one copy only.
    assert [cc.constraint_id for cc in cat.candidates[0].constraints] == ["c1"]


def test_supplier_product_link_fields_do_not_reach_the_catalogue(tmp_path: Path) -> None:
    dump = loaders.build_catalogue_view("B2", str(_raw_dir(tmp_path)), scenario_id="B2_s0").model_dump_json()
    for leaked in ("B2 secret keyword", "B2 target", "home-bid keyword", "B1", "gem_bid_id"):
        assert leaked not in dump


def test_bid_with_products_rows_ignores_supplier_product_links(tmp_path: Path) -> None:
    cat = loaders.build_catalogue_view("B1", str(_raw_dir(tmp_path)), scenario_id="B1_s0")
    assert [c.product_id for c in cat.candidates] == ["101"]   # the B1 -> 102 link is not added (D106 is a fallback)


def test_missing_supplier_products_file_keeps_an_empty_catalogue(tmp_path: Path) -> None:
    raw = _raw_dir(tmp_path)
    (raw / "supplier_products.csv").unlink()
    assert loaders.build_catalogue_view("B2", str(raw), scenario_id="B2_s0").candidates == []
