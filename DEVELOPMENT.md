# Development Guide

This guide covers local setup, the developer toolchain, and the checks that run
in GitHub CI. The goal is that you can reproduce CI locally before pushing.

> The bot product logic is **not** described here — this document only covers
> developer tooling, testing, and deployment notes.

---

## 1. Prerequisites

- **Python 3.11** (CI pins 3.11; the code targets `py311` in `pyproject.toml`).
- `git` and `make`.
- A POSIX shell (Linux/macOS, or WSL on Windows).

---

## 2. Initial setup

```bash
# Clone
git clone https://github.com/hjun3959-blip/telegram-ai-bot.git
cd telegram-ai-bot

# (recommended) create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install runtime deps + the dev tooling used by CI and pre-commit
make install

# Install the git pre-commit hook
make pre-commit-install
```

`make install` installs `requirements-dev.txt`, which pulls in `requirements.txt`
plus the full dev toolchain (`ruff`, `bandit`, `pip-audit`, `pre-commit`, and the
formatting/type-checking/testing tools).

---

## 3. Environment variables

The bot is configured entirely through environment variables. Copy the example
and fill in your own values:

```bash
cp .env.example .env
```

`.env` is git-ignored. The most important variables (see `.env.example` for the
full annotated list):

| Variable             | Purpose                                                        |
| -------------------- | ------------------------------------------------------------- |
| `TELEGRAM_TOKEN`     | Telegram bot token (required to actually run the bot).         |
| `OPENAI_API_KEY`     | API key for the OpenAI-compatible upstream (required).         |
| `OPENAI_BASE_URL`    | Base URL of the OpenAI-compatible endpoint.                    |
| `BOT_DB_PATH`        | Path to the SQLite database file (default `bot_data.sqlite3`). |
| `OWNER_USERNAMES` / `OWNER_CHAT_IDS` / `OWNER_USER_IDS` | Owner identity matching. |
| `CORE_MODEL`, `LIGHT_MODEL`, `VISION_MODEL`, ... | Model routing (don't change casually). |

> **Never commit secrets.** Keep all keys and tokens in `.env` only.

For local checks and the smoke tests you do **not** need real credentials —
dummy values are sufficient (the smoke tests are fully offline).

---

## 4. Local checks (mirror CI)

Each target maps 1:1 to a step in `.github/workflows/ci.yml`.

```bash
make compile   # python -m compileall -q .   (syntax check)
make lint      # ruff check .                 (config in pyproject.toml)
make bandit    # bandit -r . -c pyproject.toml --severity-level high
make audit     # pip-audit -r requirements.txt
make smoke     # run all offline smoke tests with dummy credentials
```

Run the whole CI-equivalent suite at once:

```bash
make ci
```

`make help` lists every available target.

---

## 5. Smoke tests

The `scripts/smoke_test_*.py` files are self-contained, **offline** tests. They:

- use a temporary SQLite file,
- never call the network or read real secrets,
- accept dummy `OPENAI_API_KEY` / `TELEGRAM_TOKEN` values.

Run them all:

```bash
make smoke
```

Run a single one (handy while iterating):

```bash
OPENAI_API_KEY=local-dummy \
TELEGRAM_TOKEN=local-dummy \
python scripts/smoke_test_copywriting.py
```

CI runs every `scripts/smoke_test*.py` with a per-file timeout, so keep new
smoke tests fast and network-free.

---

## 6. Pre-commit hooks

Pre-commit runs fast, offline checks on every commit so problems are caught
before they reach CI. Configured in `.pre-commit-config.yaml`:

- whitespace / end-of-file / line-ending fixers,
- `check-yaml`, `check-toml`, `check-json`,
- large-file, merge-conflict, debug-statement, and private-key guards,
- **ruff** (same config as CI),
- **compileall** (Python syntax check).

It intentionally does **not** run the smoke tests or any network/API checks —
those stay in CI and in `make smoke`.

```bash
make pre-commit-install   # one-time: install the hook
make pre-commit-run       # run all hooks against every file
```

Bypass only when you really must (e.g. WIP commit on a private branch):

```bash
git commit --no-verify
```

---

## 7. Linting & security config

All tool configuration lives in `pyproject.toml`:

- **`[tool.ruff]`** — target `py311`, line length 140, excludes generated files.
- **`[tool.ruff.lint]`** — currently a focused rule set (pyflakes/undefined-name
  and syntax-error families). Add rules here as the codebase tightens.
- **`[tool.bandit]`** — excluded dirs and skipped checks (`B101`).

To silence a ruff rule for one file:

```toml
[tool.ruff.lint.per-file-ignores]
"path/to/module.py" = ["E501"]
```

---

## 8. Continuous Integration (GitHub Actions)

`.github/workflows/ci.yml` runs on pushes and PRs to `master`/`main` (and via
manual dispatch). Steps, in order:

1. **Compile** — `python -m compileall -q .`
2. **Ruff lint** — `ruff check .`
3. **Bandit** — high-severity security scan.
4. **pip-audit** — dependency vulnerability scan.
5. **Smoke tests** — every `scripts/smoke_test*.py`, with per-file timeout and
   dummy credentials.

Other workflows in `.github/workflows/`:

- `secret-scan.yml` — scans for committed secrets.
- `dependabot-automerge.yml` — auto-merges Dependabot PRs that pass checks.

View runs at:
<https://github.com/hjun3959-blip/telegram-ai-bot/actions>

A PR is green when every required check passes. Run `make ci` locally first to
avoid round-trips.

---

## 9. Running the bot locally

```bash
# After filling in a real .env (TELEGRAM_TOKEN + OPENAI_API_KEY):
python app.py
```

The bot uses long polling (aiogram). It creates/uses the SQLite DB at
`BOT_DB_PATH`. Runtime artifacts (logs, the SQLite DB, `tmp/`, `plog_cache/`,
`temp_*`, `frames_*/`, generated media) are all git-ignored.

---

## 10. Deployment notes

Helper scripts live in `scripts/`:

- `scripts/preflight_check.py` — sanity-check configuration/environment before
  starting.
- `scripts/deploy_to_project_phase1_1_test.sh` — example deploy script.
- `scripts/install_logrotate.sh` — set up log rotation for the bot's logs.
- `scripts/diagnose_chatbot_server.sh` — on-server diagnostics.

General guidance:

1. Provision Python 3.11 and install `requirements.txt` in a virtualenv.
2. Provide a production `.env` (never commit it).
3. Run `python scripts/preflight_check.py` before first start.
4. Configure log rotation (`scripts/install_logrotate.sh`) so `logs/` doesn't
   grow unbounded.
5. Run `app.py` under a process manager (systemd / supervisor / pm2) so it
   restarts on failure.

---

## 11. Code graph artifacts

A static code graph under `docs/codegraph/` can be regenerated:

```bash
make codegraph        # rebuild artifacts
make codegraph-test   # no-network self-test of the builder
```

---

## 12. Troubleshooting

**Pre-commit modified files on commit.**
Some hooks (whitespace, end-of-file) auto-fix. Re-stage and commit again:

```bash
git add -A && git commit
```

**Ruff flags something in legacy code.**
Add a `per-file-ignores` entry in `pyproject.toml` (see §7) rather than
disabling the rule globally.

**Smoke test is slow or hangs.**
It should be offline and fast. Check it isn't making real network calls; CI
enforces a per-file timeout.

**`make` not available (Windows).**
Use WSL, or run the underlying commands shown in each target directly.
