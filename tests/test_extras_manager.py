"""Unit tests for dbread.extras.manager."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from dbread.extras.manager import (
    DIALECT_TO_EXTRA,
    EXTRA_TO_MODULE,
    ExtrasState,
    bootstrap_state,
    detect_install_method,
    load_state,
    merge_extras,
    save_state,
    scan_installed_extras,
    state_path,
)

# ---------------------------------------------------------------------------
# state_path
# ---------------------------------------------------------------------------

def test_state_path_ends_with_expected_components():
    path = state_path()
    assert path.name == "installed_extras.json"
    assert path.parent.name == ".dbread"


# ---------------------------------------------------------------------------
# save_state / load_state roundtrip
# ---------------------------------------------------------------------------

def test_roundtrip_saves_and_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "dbread.extras.manager.state_path",
        lambda: tmp_path / "installed_extras.json",
    )
    state = ExtrasState(
        extras=["postgres", "mongo"],
        installed_via="uv-tool",
        updated_at="2024-01-01T00:00:00+00:00",
    )
    save_state(state)
    loaded = load_state()
    assert loaded is not None
    assert loaded.extras == ["postgres", "mongo"]
    assert loaded.installed_via == "uv-tool"
    assert loaded.updated_at == "2024-01-01T00:00:00+00:00"


def test_load_state_returns_none_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "dbread.extras.manager.state_path",
        lambda: tmp_path / "nonexistent.json",
    )
    assert load_state() is None


def test_load_state_returns_none_on_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "installed_extras.json"
    path.write_text("{ this is not valid json }", encoding="utf-8")
    monkeypatch.setattr("dbread.extras.manager.state_path", lambda: path)
    assert load_state() is None


def test_load_state_returns_none_on_invalid_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "installed_extras.json"
    # Valid JSON but wrong shape (missing required fields)
    path.write_text(json.dumps({"unexpected": "data"}), encoding="utf-8")
    monkeypatch.setattr("dbread.extras.manager.state_path", lambda: path)
    assert load_state() is None


# ---------------------------------------------------------------------------
# Atomic write: temp file replaced atomically, original survives mid-write failure
# ---------------------------------------------------------------------------

def test_atomic_write_does_not_corrupt_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "installed_extras.json"
    monkeypatch.setattr("dbread.extras.manager.state_path", lambda: path)

    # Write a good initial state
    original = ExtrasState(
        extras=["duckdb"],
        installed_via="pip",
        updated_at="2024-06-01T12:00:00+00:00",
    )
    save_state(original)
    assert path.exists()

    # Simulate a crash mid-write by patching os.replace to raise
    crash = OSError("simulated crash")
    with (
        patch("dbread.extras.manager.os.replace", side_effect=crash),
        pytest.raises(OSError, match="simulated crash"),
    ):
        save_state(
            ExtrasState(
                extras=["postgres"],
                installed_via="uv-tool",
                updated_at="2024-06-02T00:00:00+00:00",
            )
        )

    # Original state must still be intact
    loaded = load_state()
    assert loaded is not None
    assert loaded.extras == ["duckdb"]


def test_save_state_creates_parent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    nested = tmp_path / "new_dir" / "installed_extras.json"
    monkeypatch.setattr("dbread.extras.manager.state_path", lambda: nested)
    state = ExtrasState(extras=[], installed_via="unknown", updated_at="2024-01-01T00:00:00+00:00")
    save_state(state)
    assert nested.exists()


# ---------------------------------------------------------------------------
# merge_extras
# ---------------------------------------------------------------------------

def test_merge_extras_dedup_and_sort():
    result = merge_extras(["postgres", "mongo"], ["mongo", "duckdb"])
    assert result == ["duckdb", "mongo", "postgres"]


def test_merge_extras_empty_lists():
    assert merge_extras([], []) == []


def test_merge_extras_one_empty():
    assert merge_extras(["mysql"], []) == ["mysql"]
    assert merge_extras([], ["oracle"]) == ["oracle"]


# ---------------------------------------------------------------------------
# scan_installed_extras
# ---------------------------------------------------------------------------

def test_scan_installed_extras_returns_sorted_keys_for_found_specs():
    def fake_find_spec(name: str):
        # Simulate only "psycopg2" and "duckdb" being installed
        if name in ("psycopg2", "duckdb"):
            return object()  # truthy
        return None

    with patch("dbread.extras.manager.importlib.util.find_spec", side_effect=fake_find_spec):
        result = scan_installed_extras()
    assert result == ["duckdb", "postgres"]


def test_scan_installed_extras_handles_find_spec_exception():
    def raising_find_spec(name: str):
        raise ModuleNotFoundError(f"no module {name}")

    with patch("dbread.extras.manager.importlib.util.find_spec", side_effect=raising_find_spec):
        result = scan_installed_extras()
    assert result == []


def test_scan_installed_extras_none_installed():
    with patch("dbread.extras.manager.importlib.util.find_spec", return_value=None):
        assert scan_installed_extras() == []


# ---------------------------------------------------------------------------
# detect_install_method
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exe,expected", [
    # Windows uv-tool path
    (r"C:\Users\user\AppData\Local\uv\tools\dbread\Scripts\python.exe", "uv-tool"),
    # Linux uv-tool path
    ("/home/user/.local/share/uv/tools/dbread/bin/python", "uv-tool"),
    # pipx path
    ("/home/user/.local/share/pipx/venvs/dbread/bin/python", "pipx"),
    # unknown path
    ("/usr/local/bin/python3", "unknown"),
])
def test_detect_install_method(exe: str, expected: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "executable", exe)
    assert detect_install_method() == expected


# ---------------------------------------------------------------------------
# bootstrap_state
# ---------------------------------------------------------------------------

def test_bootstrap_state_composition(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "dbread.extras.manager.scan_installed_extras",
        lambda: ["mysql"],
    )
    monkeypatch.setattr(
        "dbread.extras.manager.detect_install_method",
        lambda: "pipx",
    )
    state = bootstrap_state()
    assert state.extras == ["mysql"]
    assert state.installed_via == "pipx"
    assert "T" in state.updated_at  # ISO8601 contains 'T'


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

def test_extra_to_module_keys_match_pyproject_extras():
    expected_keys = {"postgres", "mysql", "mssql", "oracle", "duckdb", "clickhouse", "mongo"}
    assert set(EXTRA_TO_MODULE.keys()) == expected_keys


def test_dialect_to_extra_covers_non_sqlite_dialects():
    # sqlite must NOT appear (no extra needed)
    assert "sqlite" not in DIALECT_TO_EXTRA
    # All values must be valid EXTRA_TO_MODULE keys
    for dialect, extra in DIALECT_TO_EXTRA.items():
        assert extra in EXTRA_TO_MODULE, f"{dialect} maps to unknown extra {extra!r}"
