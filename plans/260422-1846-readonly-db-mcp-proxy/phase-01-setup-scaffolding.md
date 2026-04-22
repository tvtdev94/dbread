---
phase: 01
title: "Setup & Scaffolding"
status: pending
priority: P1
effort: 1h
created: 2026-04-22
---

# Phase 01 — Setup & Scaffolding

## Context Links

- Brainstorm: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Plan overview: [plan.md](plan.md)

## Overview

- **Priority:** P1 (blocker cho mọi phase sau)
- **Status:** pending
- **Description:** Bootstrap Python project với `uv`, khởi tạo `pyproject.toml`, `.gitignore`, `.env.example`, `config.example.yaml`, package skeleton. Đảm bảo `uv sync` chạy thành công.

## Key Insights

- `uv` nhanh hơn pip/poetry, hỗ trợ lockfile — modern choice
- Deps chia thành: core (mcp, sqlglot, sqlalchemy, pydantic-settings, pyyaml) + dev (pytest, pytest-cov, ruff) + optional extras (postgres, mysql, mssql, oracle) để user chọn driver
- File nhạy cảm (config.yaml thật, .env, *.jsonl audit) PHẢI gitignore từ đầu
- Python modules dùng `snake_case` (PEP 8), markdown docs dùng `kebab-case`

## Requirements

**Functional:**
- `uv init` project + `uv sync` thành công
- Package `dbread` import được
- Entry point CLI `dbread` chạy được (placeholder)

**Non-functional:**
- `.gitignore` chặn mọi file chứa secret
- Python 3.11+ requires
- Config example rõ ràng, có comment

## Architecture

```
dbread/
├── pyproject.toml              # deps + extras + entry point
├── uv.lock                     # auto-generated
├── .gitignore
├── .env.example
├── config.example.yaml
├── README.md                   # quickstart (placeholder phase này)
└── src/dbread/
    └── __init__.py             # export version
```

**Flow:** user clone → copy `.env.example` → `.env`, copy `config.example.yaml` → `config.yaml` → `uv sync` → `uv run dbread`.

## Related Code Files

**Create:**
- `pyproject.toml`
- `.gitignore`
- `.env.example`
- `config.example.yaml`
- `README.md` (stub quickstart)
- `src/dbread/__init__.py`

**Modify:** none
**Delete:** none

## Implementation Steps

1. **Init uv project** — `uv init --package --python 3.11` tại repo root (Windows path: `C:\w\dbread`).

2. **Edit `pyproject.toml`** — set metadata + deps:
   ```toml
   [project]
   name = "dbread"
   version = "0.1.0"
   description = "Read-only DB MCP proxy for AI"
   requires-python = ">=3.11"
   dependencies = [
       "mcp>=1.0",
       "sqlglot>=23.0",
       "sqlalchemy>=2.0",
       "pydantic-settings>=2.0",
       "pyyaml>=6.0",
   ]

   [project.optional-dependencies]
   postgres = ["psycopg2-binary>=2.9"]
   mysql = ["pymysql>=1.1"]
   mssql = ["pyodbc>=5.0"]
   oracle = ["oracledb>=2.0"]
   dev = ["pytest>=8.0", "pytest-cov>=5.0", "ruff>=0.4"]

   [project.scripts]
   dbread = "dbread.server:main"

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"
   ```

3. **Create `.gitignore`** — chặn secrets/build artifacts:
   ```
   # Secrets / config
   .env
   config.yaml
   *.jsonl

   # Python build
   __pycache__/
   *.egg-info/
   *.pyc
   .venv/
   dist/
   build/

   # Test/coverage
   .pytest_cache/
   .coverage
   htmlcov/

   # IDE
   .vscode/
   .idea/
   ```

4. **Create `.env.example`** — template env vars:
   ```env
   # Example: export URLs via env, referenced by `url_env` in config.yaml
   ANALYTICS_PROD_URL=postgresql+psycopg2://ai_readonly:password@host:5432/analytics
   LOCAL_MYSQL_URL=mysql+pymysql://ai_readonly:password@localhost:3306/shop
   ```

5. **Create `config.example.yaml`** — skeleton config với comment:
   ```yaml
   # dbread config — copy to config.yaml and edit
   connections:
     analytics_prod:
       url_env: ANALYTICS_PROD_URL   # preferred: read from env var
       dialect: postgres
       rate_limit_per_min: 60
       statement_timeout_s: 30
       max_rows: 1000
     local_mysql:
       url: mysql+pymysql://readonly:pw@localhost/shop   # or inline (less secure)
       dialect: mysql
       rate_limit_per_min: 120
       statement_timeout_s: 15
       max_rows: 500

   audit:
     path: ./audit.jsonl
     rotate_mb: 50
   ```

6. **Create `src/dbread/__init__.py`** — expose version:
   ```python
   __version__ = "0.1.0"
   ```

7. **Write stub `README.md`** — chỉ quickstart ngắn (sẽ elaborate Phase 06):
   ```
   # dbread — Read-only DB MCP Proxy
   Setup:
       uv sync --extra postgres   # pick drivers you need
       cp config.example.yaml config.yaml
       cp .env.example .env
       uv run dbread
   ```

8. **Run `uv sync`** — verify lockfile generated, no errors.

9. **Commit** — `git init` (nếu chưa) + first commit `chore: scaffold dbread project`.

## Todo List

- [ ] Run `uv init --package --python 3.11`
- [ ] Fill `pyproject.toml` với deps + extras + scripts entry
- [ ] Create `.gitignore`
- [ ] Create `.env.example`
- [ ] Create `config.example.yaml`
- [ ] Create `src/dbread/__init__.py`
- [ ] Create stub `README.md`
- [ ] Run `uv sync` → verify OK
- [ ] Verify `uv run python -c "import dbread; print(dbread.__version__)"` prints `0.1.0`

## Success Criteria

- `uv sync` hoàn thành không lỗi
- `uv.lock` được commit
- `python -c "import dbread"` không lỗi
- `.gitignore` test: tạo file `.env` fake → `git status` không show
- Tất cả file < 200 LOC

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| `mcp` SDK version breaking changes | Low | Medium | Pin minor version `>=1.0,<2.0` nếu gặp |
| Driver build fail trên Windows (pyodbc, oracledb) | Medium | Low | Optional extras — user tự chọn cần cài |
| `uv` chưa có trên máy user | Low | Low | README hướng dẫn install `uv` |

## Security Considerations

- `.gitignore` phải commit NGAY trước khi ai đó lỡ commit `config.yaml` thật
- Không hardcode credentials trong `.env.example` — chỉ dùng format placeholder
- Không commit `uv.lock` chứa private index URL nếu có

## Next Steps

- **Blocks:** Phase 02-07
- **Dependencies:** None (first phase)
- **Follow-up:** Phase 02 bắt đầu ngay sau scaffolding OK
