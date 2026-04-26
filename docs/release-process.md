# Release Process

`dbread` ships to PyPI via **fully automated GitHub Actions** on every `v*` tag push. No manual `twine upload`, no manual build, no remembering which token file to use.

This doc describes how the pipeline works, how to release, how to auto-approve the deployment gate, and how to recover when something goes wrong.

## TL;DR for shipping a release

```bash
# 1. Bump BOTH version fields (workflow rejects mismatch)
sed -i 's/version = "0.7.4"/version = "0.7.5"/' pyproject.toml
sed -i 's/SERVER_VERSION = "0.7.4"/SERVER_VERSION = "0.7.5"/' src/dbread/server.py

# 2. Commit + tag + push
git commit -am "fix: v0.7.5 - <one-line change>"
git tag -a v0.7.5 -m "v0.7.5"
git push --tags

# 3. Approve via https://github.com/tvtdev94/dbread/actions
#    Click "Review deployments -> pypi -> Approve and deploy"
#    OR ask Claude to auto-approve via `gh api` (see "Auto-approve" below)
```

That's it. PyPI gets the new version ~2 minutes after the approval click.

## Pipeline architecture

`.github/workflows/publish.yml` runs on push of any `v*` tag (and via `workflow_dispatch` for testing).

```
┌────────────────────────────────────────────────────────────────┐
│ on: push (tag v*)  OR  workflow_dispatch(tag)                  │
└────────────────────┬───────────────────────────────────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│ 1. verify   • tag matches pyproject.toml version (FAIL if not)   │
│             • tag matches src/dbread/server.py SERVER_VERSION    │
│             • pip install -e ".[dev,mongo]"  + pytest -m "not    │
│               integration"                                       │
│             • ruff check src/                                    │
└────────────────────┬─────────────────────────────────────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│ 2. build    • python -m build  →  sdist + wheel                  │
│             • upload artifact "dist"                             │
└────────────────────┬─────────────────────────────────────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│ 3. publish  • environment: pypi  (REQUIRES REVIEWER APPROVAL)    │
│             • download artifact                                  │
│             • pypa/gh-action-pypi-publish using PYPI_API_TOKEN   │
│               from repo secrets                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Safety gates

| Gate | What it catches |
|------|-----------------|
| Tag vs `pyproject.toml` version | Forgetting to bump pyproject (the v0.7.1 footgun) |
| Tag vs `server.py` SERVER_VERSION | Forgetting to bump the Python constant |
| `pytest -m "not integration"` | Code regressions before they hit users |
| `ruff check src/` | Lint regressions |
| `environment: pypi` approval gate | Tag pushed by mistake — human must click before PyPI upload |
| PyPI duplicate-version reject | Re-running on already-uploaded version |

## Auto-approve (Claude / scripted)

The approval gate is human-in-the-loop by default, but `tvtdev94` (the configured reviewer) can also approve via API. Useful for:
- AI agents finishing a release autonomously
- AFK / urgent hotfixes when the reviewer is the script-runner

```bash
RUN_ID=<workflow run id>
ENV_ID=$(gh api "repos/tvtdev94/dbread/actions/runs/$RUN_ID/pending_deployments" --jq '.[0].environment.id')

cat > /tmp/approve.json <<EOF
{"environment_ids": [$ENV_ID], "state": "approved", "comment": "auto-approved"}
EOF

gh api -X POST "repos/tvtdev94/dbread/actions/runs/$RUN_ID/pending_deployments" --input /tmp/approve.json
```

**Important:** must use `--input <json-file>` (not `--raw-field`). The latter sends `environment_ids` as a string `"[123]"` instead of integer array `[123]` and silently fails.

## One-time setup (already done)

These steps were performed once on 2026-04-26 — do NOT redo unless the PyPI token is rotated or the GitHub repo is forked:

1. **GitHub Environment `pypi`** — created via `gh api -X PUT /repos/tvtdev94/dbread/environments/pypi` with required reviewer `tvtdev94`.
2. **Repo secret `PYPI_API_TOKEN`** — pushed via `gh secret set PYPI_API_TOKEN < <token>`. Token sourced from `~/.pypirc` (PyPI account scoped to `dbread` project).
3. **Workflow file** — `.github/workflows/publish.yml`.

### Rotating the PyPI token

```bash
# On pypi.org/manage/account/token/ → create a new project-scoped token
NEW_TOKEN="pypi-AgEN..."

# Push to GitHub secret (overwrites existing)
printf '%s' "$NEW_TOKEN" | gh secret set PYPI_API_TOKEN

# Revoke old token on PyPI
```

No workflow change needed. No `~/.pypirc` change needed (workflow only reads from GitHub secret).

### Migrating to OIDC Trusted Publisher (optional, future)

Token-based publishing is fine but PyPI recommends OIDC for production projects. To migrate:

1. Configure a Trusted Publisher at https://pypi.org/manage/project/dbread/settings/publishing/?provider=github&owner=tvtdev94&repository=dbread&workflow_filename=publish.yml (PyPI auto-fills the form via that URL).
2. Update workflow `publish` job — remove `with: password:`, add `permissions: id-token: write`.
3. Delete the GitHub secret `PYPI_API_TOKEN` (no longer needed).
4. Revoke the long-lived token on PyPI.

Trade-off: token = simpler, OIDC = no long-lived credential to leak.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `verify` job fails on "version mismatch" | Forgot to bump `pyproject.toml` or `server.py` | Bump the missing one, recommit, retag (delete tag locally + remote first), re-push |
| `verify` job fails on tests with `ModuleNotFoundError: pymongo` | Workflow lost `[dev,mongo]` install line | Restore the line in `publish.yml:install deps` step |
| `publish` job fails with `400 File already exists` | Tried to re-upload an existing PyPI version | Bump to a new version. PyPI never allows overwrites. |
| `publish` job hangs at "Waiting" | Approval gate not yet acted on | Click approve in the Actions UI, or run the auto-approve command above |
| `publish` job fails with `Invalid or non-existent authentication information` | `PYPI_API_TOKEN` secret revoked or deleted | Rotate the token (see above) |
| Tag pushed but no workflow ran | Workflow file error or `on: push: tags` filter mismatch | Check Actions tab for "Failed to start" messages; verify tag matches pattern `v*` |

## Yanking a bad release

PyPI doesn't allow file overwrites, but it allows **yanking** (hiding from new installs while keeping the file accessible to anyone who has it pinned).

```bash
# Yank a specific version (requires the same PyPI token)
python -m twine yank --skip-existing dbread==0.7.4 --reason "broken installer"
```

Or via UI: https://pypi.org/manage/project/dbread/release/0.7.4/ → "Options → Yank release".

After yanking, `pip install dbread` will pick the next-newest non-yanked version. Always cut a `0.7.5` patch with the actual fix — yank alone doesn't help users on `pip install dbread@0.7.4`.

## File invariants the workflow depends on

- `pyproject.toml` line `version = "X.Y.Z"` — single quoted, matches regex `^version\s*=\s*"([^"]+)"`
- `src/dbread/server.py` line `SERVER_VERSION = "X.Y.Z"` — same shape
- `.github/workflows/publish.yml` — exists with current job names (`verify`, `build`, `publish`)
- GitHub Environment `pypi` — exists in repo settings with the workflow_filename allow-list including `publish.yml`
- Repo secret `PYPI_API_TOKEN` — present and valid

If any of these change shape (e.g. moving version into a `_version.py` file), update the workflow's grep/sed lines too.
