"""Controllo bloccante: ogni modo di guasto del registro FMEA deve avere test esistenti."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_every_fmea_row_maps_to_existing_tests():
    text = (ROOT / "docs" / "FMEA.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if re.match(r"^\| F\d+ ", ln)]
    assert len(rows) >= 30
    for row in rows:
        refs = re.findall(r"`(tests/[\w/]+\.py)::(\w+)`", row)
        assert refs, f"riga FMEA senza test: {row[:40]}"
        for path, name in refs:
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert name in names, f"{path}::{name} citato in FMEA ma inesistente"
