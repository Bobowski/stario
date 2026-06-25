"""Tests for stario.cli.imports.load_symbol."""

import sys
from pathlib import Path

import pytest

from stario.cli.errors import CliError
from stario.cli.imports import load_symbol


def test_load_symbol_inserts_cwd_on_syspath(monkeypatch, tmp_path: Path) -> None:
    module_path = tmp_path / "demo.py"
    module_path.write_text("VALUE = 1\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path.parent)])

    assert load_symbol("demo:VALUE", label="test") == 1
    assert str(tmp_path) in sys.path


def test_load_symbol_rejects_empty_dotted_segment() -> None:
    with pytest.raises(CliError, match="empty segments"):
        load_symbol("demo:pkg..value", label="test")


def test_load_symbol_reports_missing_attribute(monkeypatch, tmp_path: Path) -> None:
    module_path = tmp_path / "attr_demo.py"
    module_path.write_text("pkg = object()\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    sys.modules.pop("attr_demo", None)

    with pytest.raises(CliError, match="has no attribute 'missing'"):
        load_symbol("attr_demo:pkg.missing", label="test")


def test_load_symbol_stario_export_hint(monkeypatch, tmp_path: Path) -> None:
    module_path = tmp_path / "main.py"
    module_path.write_text("from stario import NotARealExport\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "path",
        [
            str(tmp_path),
            str(Path(__file__).resolve().parents[1] / "src"),
        ],
    )
    sys.modules.pop("main", None)

    try:
        with pytest.raises(CliError, match="not exported from the stario package root"):
            load_symbol("main:bootstrap", label="app")
    finally:
        sys.modules.pop("main", None)
