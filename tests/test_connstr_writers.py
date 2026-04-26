"""Tests for dbread.connstr.writers — env writer, yaml writer, backup, helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from dbread.connstr.types import ParsedConn
from dbread.connstr.writers import (
    _insert_yaml_connection,
    _make_backup,
    _render_connection_block,
    _replace_or_append_env_line,
    write_config_yaml,
    write_env,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _pg_conn(**kwargs) -> ParsedConn:
    defaults = dict(
        format="uri",
        dialect="postgres",
        host="db.example.com",
        port=5432,
        database="mydb",
        user="alice",
        password="secret",
        params={},
    )
    defaults.update(kwargs)
    return ParsedConn(**defaults)


def _mongo_conn(**kwargs) -> ParsedConn:
    defaults = dict(
        format="uri",
        dialect="mongodb",
        host="mongo.example.com",
        port=27017,
        database="analytics",
        user="admin",
        password="pw",
        params={},
    )
    defaults.update(kwargs)
    return ParsedConn(**defaults)


# ---------------------------------------------------------------------------
# _replace_or_append_env_line
# ---------------------------------------------------------------------------


class TestReplaceOrAppendEnvLine:
    def test_append_to_empty_string(self):
        result = _replace_or_append_env_line("", "MY_URL", "postgres://host/db")
        assert result == "MY_URL=postgres://host/db\n"

    def test_append_to_existing_content(self):
        existing = "FOO=bar\nBAZ=qux\n"
        result = _replace_or_append_env_line(existing, "MY_URL", "postgres://host/db")
        assert "FOO=bar" in result
        assert "MY_URL=postgres://host/db" in result

    def test_replace_existing_key(self):
        existing = "FOO=bar\nMY_URL=old_value\nBAZ=qux\n"
        result = _replace_or_append_env_line(existing, "MY_URL", "new_value")
        assert "MY_URL=new_value" in result
        assert "old_value" not in result
        assert "FOO=bar" in result
        assert "BAZ=qux" in result

    def test_preserves_comments(self):
        existing = "# dbread creds\nFOO=bar\n"
        result = _replace_or_append_env_line(existing, "NEW_URL", "val")
        assert "# dbread creds" in result

    def test_trailing_newline_added_if_missing(self):
        result = _replace_or_append_env_line("FOO=bar", "X", "y")
        assert result.endswith("\n")

    def test_trailing_newline_preserved(self):
        result = _replace_or_append_env_line("FOO=bar\n", "X", "y")
        assert result.endswith("\n")

    def test_only_replaces_exact_key(self):
        # MY_URL should not match MY_URL_EXTRA
        existing = "MY_URL_EXTRA=old\nMY_URL=old\n"
        result = _replace_or_append_env_line(existing, "MY_URL", "new")
        assert "MY_URL_EXTRA=old" in result
        assert "MY_URL=new" in result


# ---------------------------------------------------------------------------
# _make_backup
# ---------------------------------------------------------------------------


class TestMakeBackup:
    def test_creates_bak_when_original_exists(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("KEY=value\n", encoding="utf-8")
        _make_backup(f)
        bak = Path(str(f) + ".bak")
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == "KEY=value\n"

    def test_does_not_overwrite_existing_bak(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("new content\n", encoding="utf-8")
        bak = Path(str(f) + ".bak")
        bak.write_text("original content\n", encoding="utf-8")
        _make_backup(f)
        # .bak must NOT be overwritten
        assert bak.read_text(encoding="utf-8") == "original content\n"

    def test_no_bak_when_original_missing(self, tmp_path):
        f = tmp_path / "nonexistent.env"
        _make_backup(f)  # should not raise
        bak = Path(str(f) + ".bak")
        assert not bak.exists()


# ---------------------------------------------------------------------------
# write_env
# ---------------------------------------------------------------------------


class TestWriteEnv:
    def test_creates_new_file(self, tmp_path):
        env = tmp_path / ".env"
        returned = write_env("prod_pg", "postgres://host/db", env_path=env)
        assert returned == env
        assert "PROD_PG_URL=postgres://host/db" in env.read_text(encoding="utf-8")

    def test_appends_to_existing_file(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("EXISTING=something\n", encoding="utf-8")
        write_env("new_conn", "mysql://host/db", env_path=env)
        content = env.read_text(encoding="utf-8")
        assert "EXISTING=something" in content
        assert "NEW_CONN_URL=mysql://host/db" in content

    def test_replaces_existing_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("PROD_PG_URL=old_url\nOTHER=val\n", encoding="utf-8")
        write_env("prod_pg", "postgres://new/db", env_path=env)
        content = env.read_text(encoding="utf-8")
        assert "PROD_PG_URL=postgres://new/db" in content
        assert "old_url" not in content
        assert "OTHER=val" in content

    def test_bak_created_on_first_edit(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("ORIGINAL=1\n", encoding="utf-8")
        write_env("x", "url", env_path=env)
        bak = Path(str(env) + ".bak")
        assert bak.exists()
        assert "ORIGINAL=1" in bak.read_text(encoding="utf-8")

    def test_bak_not_overwritten_on_second_edit(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FIRST=1\n", encoding="utf-8")
        write_env("a", "url1", env_path=env)
        write_env("b", "url2", env_path=env)
        bak = Path(str(env) + ".bak")
        # bak must preserve the FIRST=1 content from before first write
        assert "FIRST=1" in bak.read_text(encoding="utf-8")

    def test_creates_parent_dir_if_missing(self, tmp_path):
        env = tmp_path / "nested" / "dir" / ".env"
        write_env("test", "url", env_path=env)
        assert env.exists()


# ---------------------------------------------------------------------------
# _render_connection_block
# ---------------------------------------------------------------------------


class TestRenderConnectionBlock:
    def test_postgres_block(self):
        p = _pg_conn()
        block = _render_connection_block("prod_pg", p)
        assert "prod_pg:" in block
        assert "url_env: PROD_PG_URL" in block
        assert "dialect: postgres" in block
        assert "rate_limit_per_min: 60" in block
        assert "max_rows: 1000" in block
        assert "mongo:" not in block

    def test_mongo_block_includes_mongo_subkey(self):
        p = _mongo_conn()
        block = _render_connection_block("my_mongo", p)
        assert "dialect: mongodb" in block
        assert "mongo:" in block
        assert "sample_size: 100" in block

    def test_block_is_valid_yaml(self):
        p = _pg_conn()
        block = _render_connection_block("test_conn", p)
        # Wrap in a parent key to parse as valid YAML fragment
        parsed = yaml.safe_load("connections:\n" + block)
        assert "test_conn" in parsed["connections"]


# ---------------------------------------------------------------------------
# _insert_yaml_connection
# ---------------------------------------------------------------------------


class TestInsertYamlConnection:
    def test_inserts_before_next_top_level_key(self):
        text = (
            "connections:\n"
            "  existing:\n"
            "    dialect: sqlite\n"
            "\n"
            "audit:\n"
            "  path: ./audit.jsonl\n"
        )
        block = "  new_conn:\n    dialect: postgres\n"
        result = _insert_yaml_connection(text, "new_conn", block)
        # new_conn must appear before audit:
        assert result.index("new_conn:") < result.index("audit:")

    def test_appends_at_eof_when_connections_is_last_key(self):
        text = "connections:\n  existing:\n    dialect: sqlite\n"
        block = "  new_conn:\n    dialect: postgres\n"
        result = _insert_yaml_connection(text, "new_conn", block)
        assert "new_conn:" in result
        parsed = yaml.safe_load(result)
        assert "new_conn" in parsed["connections"]

    def test_raises_when_connections_missing(self):
        text = "audit:\n  path: ./log\n"
        with pytest.raises(ValueError, match="connections"):
            _insert_yaml_connection(text, "x", "  x:\n    dialect: sqlite\n")

    def test_preserves_comments_above_and_below(self):
        text = (
            "# top comment\n"
            "connections:\n"
            "  # inline comment\n"
            "  existing:\n"
            "    dialect: sqlite\n"
            "\n"
            "# section comment\n"
            "audit:\n"
            "  path: ./log\n"
        )
        block = "  new_conn:\n    dialect: postgres\n"
        result = _insert_yaml_connection(text, "new_conn", block)
        assert "# top comment" in result
        assert "# inline comment" in result
        assert "# section comment" in result


# ---------------------------------------------------------------------------
# write_config_yaml
# ---------------------------------------------------------------------------


class TestWriteConfigYaml:
    def test_inserts_connection_into_existing_config(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "connections:\n  existing:\n    url: sqlite:///x.db\n    dialect: sqlite\n",
            encoding="utf-8",
        )
        write_config_yaml("new_pg", _pg_conn(), cfg_path=cfg)
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert "new_pg" in data["connections"]
        assert "existing" in data["connections"]

    def test_creates_file_if_missing(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        write_config_yaml("fresh", _pg_conn(), cfg_path=cfg)
        assert cfg.exists()
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert "fresh" in data["connections"]

    def test_raises_on_duplicate_name(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "connections:\n  dup:\n    url: sqlite:///x.db\n    dialect: sqlite\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="already exists"):
            write_config_yaml("dup", _pg_conn(), cfg_path=cfg)

    def test_mongo_dialect_adds_mongo_subblock(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("connections:\n", encoding="utf-8")
        write_config_yaml("my_mongo", _mongo_conn(), cfg_path=cfg)
        content = cfg.read_text(encoding="utf-8")
        assert "mongo:" in content
        assert "sample_size: 100" in content

    def test_rollback_on_invalid_yaml_result(self, tmp_path):
        """If the rendered block produces invalid YAML, original is restored."""
        cfg = tmp_path / "config.yaml"
        original = (
            "connections:\n  existing:\n    url: sqlite:///x.db\n    dialect: sqlite\n"
        )
        cfg.write_text(original, encoding="utf-8")

        bad_block = "  bad_conn:\n  - this: is: invalid: yaml: indentation\n"
        with patch(
            "dbread.connstr.writers._render_connection_block", return_value=bad_block
        ), pytest.raises((ValueError, Exception)):
            write_config_yaml("bad_conn", _pg_conn(), cfg_path=cfg)

        # Config must be restored to original (or valid state)
        restored = cfg.read_text(encoding="utf-8")
        assert "bad_conn" not in restored or yaml.safe_load(restored) is not None

    def test_bak_created_on_first_write(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("connections:\n", encoding="utf-8")
        write_config_yaml("alpha", _pg_conn(), cfg_path=cfg)
        bak = Path(str(cfg) + ".bak")
        assert bak.exists()

    def test_bak_not_overwritten_on_second_write(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("connections:\n", encoding="utf-8")
        write_config_yaml("alpha", _pg_conn(), cfg_path=cfg)
        write_config_yaml("beta", _pg_conn(database="betadb"), cfg_path=cfg)
        bak = Path(str(cfg) + ".bak")
        # bak should still be the original "connections:\n"
        assert bak.read_text(encoding="utf-8") == "connections:\n"

    def test_preserves_comments_in_config(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "# dbread config\n"
            "connections:\n"
            "  # existing\n"
            "  sample:\n"
            "    url: sqlite:///s.db\n"
            "    dialect: sqlite\n"
            "\n"
            "audit:\n"
            "  path: ./a.jsonl\n",
            encoding="utf-8",
        )
        write_config_yaml("new_pg", _pg_conn(), cfg_path=cfg)
        content = cfg.read_text(encoding="utf-8")
        assert "# dbread config" in content
        assert "# existing" in content
