"""Tests for dbread.connstr.wizard — full flow with mocked I/O and side effects."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from dbread.connstr.wizard import _suggest_name, run_add_wizard

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PG_URI = "postgresql://alice:secret@db.example.com:5432/mydb"
_UNKNOWN = "not-a-connection-string-at-all!!!"
_SQLITE_URL = "sqlite:///path/to/test.db"


def _patch_writes(monkeypatch, env_path: Path | None = None, cfg_path: Path | None = None):
    """Patch write_env and write_config_yaml to no-ops that return dummy paths."""
    dummy_env = env_path or Path("/tmp/.env")
    dummy_cfg = cfg_path or Path("/tmp/config.yaml")

    monkeypatch.setattr(
        "dbread.connstr.wizard.write_env",
        lambda name, url, **kw: dummy_env,
    )
    monkeypatch.setattr(
        "dbread.connstr.wizard.write_config_yaml",
        lambda name, p, **kw: dummy_cfg,
    )
    return dummy_env, dummy_cfg


def _patch_extras_present(monkeypatch, extras=("postgres",)):
    monkeypatch.setattr(
        "dbread.extras.manager.scan_installed_extras",
        lambda: list(extras),
    )


def _patch_test_ok(monkeypatch):
    monkeypatch.setattr(
        "dbread.connstr.wizard._test_connection",
        lambda dialect, url: (True, ""),
    )


def _patch_test_fail(monkeypatch, msg="connection refused"):
    monkeypatch.setattr(
        "dbread.connstr.wizard._test_connection",
        lambda dialect, url: (False, msg),
    )


def _patch_name_not_existing(monkeypatch):
    monkeypatch.setattr(
        "dbread.connstr.wizard._name_exists_in_config",
        lambda name, path: False,
    )


# ---------------------------------------------------------------------------
# _suggest_name unit tests
# ---------------------------------------------------------------------------


class TestSuggestName:
    def test_plain_database_name(self):
        assert _suggest_name("mydb") == "mydb"

    def test_slugifies_special_chars(self):
        assert _suggest_name("My-DB_2024") == "my_db_2024"

    def test_none_returns_conn(self):
        assert _suggest_name(None) == "conn"

    def test_empty_string_returns_conn(self):
        assert _suggest_name("") == "conn"

    def test_strips_leading_trailing_underscores(self):
        assert _suggest_name("-leading-") == "leading"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestWizardHappyPath:
    def test_exit_0_on_success(self, monkeypatch):
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)
        _patch_writes(monkeypatch)

        # Accept default name at prompt
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        code = run_add_wizard(no_test=False)
        assert code == 0

    def test_write_env_called_on_success(self, monkeypatch):
        called = {}

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        def fake_write_env(name, url, **kw):
            called["env"] = (name, url)
            return Path("/tmp/.env")

        def fake_write_cfg(name, p, **kw):
            called["cfg"] = name
            return Path("/tmp/config.yaml")

        monkeypatch.setattr("dbread.connstr.wizard.write_env", fake_write_env)
        monkeypatch.setattr("dbread.connstr.wizard.write_config_yaml", fake_write_cfg)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        run_add_wizard()
        assert "env" in called
        assert "cfg" in called

    def test_name_auto_suggested_from_database(self, monkeypatch):
        written_names = []

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        def fake_write_env(name, url, **kw):
            written_names.append(name)
            return Path("/tmp/.env")

        monkeypatch.setattr("dbread.connstr.wizard.write_env", fake_write_env)
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda name, p, **kw: Path("/tmp/config.yaml"),
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "")  # accept default

        run_add_wizard()
        # database="mydb" from URI → suggested name is "mydb"
        assert written_names == ["mydb"]


# ---------------------------------------------------------------------------
# Error paths (existing, updated for new menu-based flows)
# ---------------------------------------------------------------------------


class TestWizardErrorPaths:
    def test_unknown_format_shows_menu_cancel_returns_1(self, monkeypatch):
        """UnknownFormat now shows fallback menu; option 3 = cancel → return 1."""
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _UNKNOWN,
        )
        _patch_writes(monkeypatch)
        # Menu option 3 = cancel
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")
        code = run_add_wizard()
        assert code == 1

    def test_unknown_format_does_not_call_write(self, monkeypatch):
        write_called = []

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _UNKNOWN,
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda *a, **kw: write_called.append("env") or Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda *a, **kw: write_called.append("cfg") or Path("/tmp/config.yaml"),
        )
        # Cancel via menu
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")

        run_add_wizard()
        assert write_called == []

    def test_test_fails_user_declines_save_returns_1_no_write(self, monkeypatch):
        """_handle_test_failure option 3 (cancel) → return 1, no write."""
        write_called = []

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_fail(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda *a, **kw: write_called.append("env") or Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda *a, **kw: write_called.append("cfg") or Path("/tmp/config.yaml"),
        )

        # First input = connection name (accept default), second = "3" for cancel
        inputs = iter(["", "3"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard()
        assert code == 1
        assert write_called == []

    def test_test_fails_user_accepts_save_returns_0_write_called(self, monkeypatch):
        """_handle_test_failure option 1 (save anyway) → return 0, writes called."""
        write_called = []

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_fail(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda *a, **kw: write_called.append("env") or Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda *a, **kw: write_called.append("cfg") or Path("/tmp/config.yaml"),
        )

        # name prompt = accept default, save-anyway menu = "1" (save anyway)
        inputs = iter(["", "1"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard()
        assert code == 0
        assert "env" in write_called
        assert "cfg" in write_called

    def test_name_exists_user_declines_overwrite_returns_1(self, monkeypatch):
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_ok(monkeypatch)
        _patch_writes(monkeypatch)

        # Simulate name already exists
        monkeypatch.setattr(
            "dbread.connstr.wizard._name_exists_in_config",
            lambda name, path: True,
        )

        # name prompt = accept default ("mydb"), overwrite prompt = "n"
        inputs = iter(["", "n"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard()
        assert code == 1

    def test_missing_extra_user_declines_install_continues(self, monkeypatch):
        """Declining install should still continue (test step may then fail)."""
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        # postgres extra not installed
        monkeypatch.setattr(
            "dbread.extras.manager.scan_installed_extras",
            lambda: [],
        )
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)
        _patch_writes(monkeypatch)

        # "Install now?" = "n", name prompt = accept default
        inputs = iter(["n", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        # Should not return 3; it continues past the extra check
        code = run_add_wizard()
        assert code in {0, 1}  # continues; test may succeed with mocked _test_connection

    def test_missing_extra_user_accepts_install_failure_returns_3(self, monkeypatch):
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        monkeypatch.setattr(
            "dbread.extras.manager.scan_installed_extras",
            lambda: [],
        )
        # install_or_print returns False (failure)
        monkeypatch.setattr(
            "dbread.extras.installer.install_or_print",
            lambda extras, method: False,
        )
        monkeypatch.setattr(
            "dbread.extras.manager.load_state",
            lambda: None,
        )
        monkeypatch.setattr(
            "dbread.extras.manager.bootstrap_state",
            lambda: MagicMock(extras=[], installed_via="uv-tool"),
        )
        monkeypatch.setattr(
            "dbread.extras.manager.save_state",
            lambda state: None,
        )

        # "Install now?" = "y"
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        code = run_add_wizard()
        assert code == 3

    def test_no_test_flag_skips_connection_test(self, monkeypatch):
        test_called = []

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_name_not_existing(monkeypatch)
        _patch_writes(monkeypatch)

        monkeypatch.setattr(
            "dbread.connstr.wizard._test_connection",
            lambda dialect, url: test_called.append(1) or (True, ""),
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        run_add_wizard(no_test=True)
        assert test_called == []


# ---------------------------------------------------------------------------
# Fallback menu tests (UnknownFormat → _offer_fallback_menu)
# ---------------------------------------------------------------------------


class TestFallbackMenu:
    def test_unknown_format_option_1_manual_entry_succeeds(self, monkeypatch):
        """UnknownFormat → menu option 1 → manual URL → writes config."""
        write_called = []

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _UNKNOWN,
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda *a, **kw: write_called.append("env") or Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda *a, **kw: write_called.append("cfg") or Path("/tmp/config.yaml"),
        )
        _patch_extras_present(monkeypatch, extras=[])  # sqlite — no extra needed
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        # inputs: menu choice "1", then URL, then connection name (accept default)
        inputs = iter(["1", _SQLITE_URL, ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard()
        assert code == 0
        assert "env" in write_called
        assert "cfg" in write_called

    def test_unknown_format_option_2_generates_template_returns_1(self, monkeypatch, capsys):
        """UnknownFormat → menu option 2 → template printed → returns 1."""
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _UNKNOWN,
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "2")

        code = run_add_wizard()
        assert code == 1
        out = capsys.readouterr().out
        assert "config.yaml" in out
        assert "url_env" in out

    def test_unknown_format_option_3_cancel_returns_1(self, monkeypatch):
        """UnknownFormat → menu option 3 → returns 1."""
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _UNKNOWN,
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")

        code = run_add_wizard()
        assert code == 1


# ---------------------------------------------------------------------------
# Manual flag tests (--manual skips detection)
# ---------------------------------------------------------------------------


class TestManualFlag:
    def test_manual_flag_skips_detection_and_prompts_url(self, monkeypatch):
        """--manual skips _read_connection_string and detect_and_parse."""
        detect_called = []

        # detect_and_parse should NOT be called
        monkeypatch.setattr(
            "dbread.connstr.wizard.detect_and_parse",
            lambda raw, **kw: detect_called.append(1) or (_ for _ in ()).throw(
                AssertionError("detect_and_parse should not be called in manual mode")
            ),
        )
        _patch_extras_present(monkeypatch, extras=[])
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)
        _patch_writes(monkeypatch)

        # URL prompt then name prompt (accept default)
        inputs = iter([_SQLITE_URL, ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard(manual=True)
        assert code == 0
        assert detect_called == []

    def test_manual_flag_produces_parsed_conn_with_format_manual(self, monkeypatch):
        """ParsedConn written to config has format='manual'."""
        written_parsed = []

        _patch_extras_present(monkeypatch, extras=[])
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda name, url, **kw: Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda name, p, **kw: written_parsed.append(p) or Path("/tmp/config.yaml"),
        )

        # URL prompt, then connection name (accept default)
        inputs = iter([_SQLITE_URL, ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard(manual=True)
        assert code == 0
        assert written_parsed[0].format == "manual"

    def test_manual_flag_with_dialect_hint_skips_dialect_prompt(self, monkeypatch):
        """With --dialect-hint sqlite, no dialect input prompt should appear."""
        prompt_texts = []

        def capturing_input(prompt=""):
            prompt_texts.append(prompt)
            # Return URL on first call, empty string (accept default name) on second
            if "URL" in prompt:
                return _SQLITE_URL
            return ""

        _patch_extras_present(monkeypatch, extras=[])
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)
        _patch_writes(monkeypatch)
        monkeypatch.setattr("builtins.input", capturing_input)

        code = run_add_wizard(manual=True, dialect_hint="sqlite")
        assert code == 0
        # No prompt should ask for dialect
        assert not any("Dialect" in p for p in prompt_texts)


# ---------------------------------------------------------------------------
# _handle_test_failure tests
# ---------------------------------------------------------------------------


class TestHandleTestFailure:
    def test_test_failure_option_1_save_anyway_writes(self, monkeypatch):
        """_handle_test_failure option 1 saves and returns 0."""
        write_called = []

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_fail(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda *a, **kw: write_called.append("env") or Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda *a, **kw: write_called.append("cfg") or Path("/tmp/config.yaml"),
        )

        # name (accept default), then menu choice "1" (save anyway)
        inputs = iter(["", "1"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard()
        assert code == 0
        assert "env" in write_called

    def test_test_failure_option_2_edit_then_retest_succeeds(self, monkeypatch):
        """Option 2: edit URL and re-test → new URL passes → write called."""
        write_called = []
        test_count = {"n": 0}

        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_name_not_existing(monkeypatch)

        def fake_test(dialect, url):
            test_count["n"] += 1
            # First test (original URL) fails; subsequent (edited URL) passes
            if test_count["n"] == 1:
                return False, "connection refused"
            return True, ""

        monkeypatch.setattr("dbread.connstr.wizard._test_connection", fake_test)

        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda *a, **kw: write_called.append("env") or Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda *a, **kw: write_called.append("cfg") or Path("/tmp/config.yaml"),
        )

        # Sequence: name (default) → menu "2" (edit) → new URL → name (default again)
        inputs = iter(["", "2", _SQLITE_URL])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard()
        assert code == 0
        assert "env" in write_called

    def test_test_failure_option_2_edit_fails_thrice_returns_none(self, monkeypatch):
        """Option 2 repeated 3 times → cap reached → cancel → return 1."""
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_name_not_existing(monkeypatch)
        # Every test fails
        _patch_test_fail(monkeypatch, msg="always fails")

        # Sequence:
        # - name (accept default)
        # - 1st failure menu: "2" (edit) → new URL for _manual_url_entry
        # - 2nd failure menu: "2" (edit) → new URL for _manual_url_entry
        # - cap reached → auto-cancel (no more input needed)
        inputs = iter(["", "2", _SQLITE_URL, "2", _SQLITE_URL])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        _patch_writes(monkeypatch)

        code = run_add_wizard()
        assert code == 1

    def test_test_failure_option_3_cancel(self, monkeypatch):
        """Option 3 from failure menu → return 1."""
        monkeypatch.setattr(
            "dbread.connstr.wizard._read_connection_string",
            lambda from_stdin: _PG_URI,
        )
        _patch_extras_present(monkeypatch, extras=["postgres"])
        _patch_test_fail(monkeypatch)
        _patch_name_not_existing(monkeypatch)
        _patch_writes(monkeypatch)

        # name (default), then failure menu "3" (cancel)
        inputs = iter(["", "3"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard()
        assert code == 1

    def test_manual_url_entry_validates_url_syntax_bad_then_good(self, monkeypatch):
        """Bad URL → error printed, re-prompt. Good URL → success."""
        write_called = []

        _patch_extras_present(monkeypatch, extras=[])
        _patch_test_ok(monkeypatch)
        _patch_name_not_existing(monkeypatch)

        monkeypatch.setattr(
            "dbread.connstr.wizard.write_env",
            lambda *a, **kw: write_called.append("env") or Path("/tmp/.env"),
        )
        monkeypatch.setattr(
            "dbread.connstr.wizard.write_config_yaml",
            lambda *a, **kw: write_called.append("cfg") or Path("/tmp/config.yaml"),
        )

        # In manual mode: first URL is garbage, second is valid; then accept default name
        inputs = iter(["not://a valid:url:here !!!", _SQLITE_URL, ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        code = run_add_wizard(manual=True, dialect_hint="sqlite")
        assert code == 0
        assert "env" in write_called
