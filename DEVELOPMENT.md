# 🚀 Development Guide

This document provides a comprehensive guide for setting up and using the development toolchain.

## 📋 Quick Start

### 1. Initial Setup

```bash
# Clone repository
git clone https://github.com/hjun3959-blip/telegram-ai-bot.git
cd telegram-ai-bot

# Install development tools
make install

# Setup pre-commit hooks
make pre-commit-install
```

### 2. Make Your Changes

```bash
# Make code changes
# ...

# Format and lint before committing
make format
make lint-fix
```

### 3. Test Your Changes

```bash
# Run tests
make test

# Check coverage
make coverage-report
```

---

## 🛠️ Available Tools

### Code Formatting

**Black** - Automatic code formatter
```bash
make format           # Format all code
make format-check     # Check without modifying
make format-diff      # Show differences
```

**isort** - Import organizer
```bash
# Included in `make format`
python -m isort scripts/ services/ routers/ db/ utils/
```

### Code Quality

**Ruff** - Fast Python linter (replaces flake8 + isort + etc)
```bash
make lint             # Show issues
make lint-fix         # Auto-fix issues
```

**Pylint** - Detailed code analysis
```bash
python -m pylint services/ routers/ db/ utils/ --fail-under=8.0
```

### Type Checking

**MyPy** - Static type checker
```bash
make type-check       # Normal mode
make type-check-strict  # Strict mode
```

### Testing

**Pytest** - Test runner
```bash
make test             # Run all tests
make test-verbose     # Verbose output
make test-coverage    # With coverage
```

### Security

**Bandit** - Security issue scanner
```bash
python -m bandit -r services/ -c pyproject.toml
```

**pip-audit** - Dependency vulnerability scanner
```bash
python -m pip_audit -r requirements.txt
```

---

## 🔄 Complete Quality Pipeline

Run all checks at once:

```bash
# Via Make
make quality-check

# Via Shell Script
./scripts/quality-check.sh

# Via Pre-commit
make pre-commit-run
```

---

## 📊 File Structure

```
telegram-ai-bot/
├── .github/
│   └── workflows/
│       └── ci.yml                    # CI/CD pipeline
├── scripts/
│   ├── smoke_test_copywriting.py    # Smoke tests
│   └── quality-check.sh             # Quality check script
├── services/                         # Business logic
├── routers/                          # Telegram handlers
├── db/                              # Database
├── utils/                           # Utilities
├── requirements.txt                 # Production dependencies
├── requirements-dev.txt             # Development dependencies
├── pyproject.toml                   # Tool configurations
├── .pre-commit-config.yaml          # Pre-commit hooks
├── Makefile                         # Development commands
└── DEVELOPMENT.md                   # This file
```

---

## ⚙️ Configuration Files

### `pyproject.toml`

Central configuration for all tools:
- **[tool.ruff]** - Ruff linter & formatter
- **[tool.black]** - Black formatter
- **[tool.isort]** - Import sorter
- **[tool.mypy]** - Type checker
- **[tool.pytest.ini_options]** - Test runner
- **[tool.coverage]** - Coverage analysis
- **[tool.bandit]** - Security scanner

### `.pre-commit-config.yaml`

Automatically runs checks before each commit:
- Code formatting (Black, isort)
- Linting (Ruff)
- Type checking (MyPy)
- Security scanning (Bandit)
- General checks (trailing whitespace, etc.)

### `.github/workflows/ci.yml`

GitHub Actions CI/CD pipeline that runs:
1. Code quality checks
2. Syntax validation
3. Unit/smoke tests
4. Coverage reports

---

## 🚦 Pre-commit Hooks

### Installation

```bash
make pre-commit-install
```

### What it does

Every time you commit, it automatically:
1. ✅ Formats code with Black
2. ✅ Sorts imports with isort
3. ✅ Checks with MyPy
4. ✅ Lints with Ruff
5. ✅ Scans for security issues
6. ✅ Fixes end-of-file issues
7. ✅ Checks YAML/JSON syntax

### Bypass (use carefully!)

```bash
git commit --no-verify
```

### Manual run

```bash
make pre-commit-run
```

---

## 🐛 Troubleshooting

### Issue: Pre-commit fails on first run

**Solution:**
```bash
# Let it auto-fix issues
git add .
make pre-commit-run
git commit
```

### Issue: Type checking too strict

**Solution - Ignore specific errors:**
```python
# In code
value = unknown_type  # type: ignore[assignment]
```

### Issue: Linting issues in legacy code

**Solution - Add file-specific ignores:**

Update `pyproject.toml`:
```toml
[tool.ruff.lint.per-file-ignores]
"legacy_module.py" = ["E501", "F841"]
```

### Issue: Tests are slow

**Solution - Run specific tests:**
```bash
python -m pytest scripts/smoke_test_copywriting.py -v
```

---

## 📈 Metrics & Reports

### Coverage Report

```bash
make coverage-report
```

Generates HTML report at `htmlcov/index.html`

### Code Quality Metrics

After running `make quality-check`, you'll get:
- Line count by tool
- Issues by severity
- Type checking results
- Security vulnerabilities
- Dependency issues

---

## 🔐 Security Best Practices

### Before Committing

1. Run security scan:
   ```bash
   python -m bandit -r services/
   ```

2. Check dependencies:
   ```bash
   python -m pip_audit -r requirements.txt
   ```

3. Never commit secrets:
   - API keys
   - Passwords
   - Tokens

Use `.env` file (ignored by git):
```bash
# .env
OPENAI_API_KEY=your-key-here
TELEGRAM_TOKEN=your-token-here
```

---

## 🚀 Continuous Integration

### GitHub Actions

When you push or create a PR, GitHub Actions automatically:

1. **Syntax Check** - Compiles all Python files
2. **Quality Checks** - Black, isort, Ruff, MyPy
3. **Security** - Bandit, pip-audit
4. **Tests** - Smoke tests with coverage
5. **Report** - Coverage uploaded to Codecov

View results at: `https://github.com/hjun3959-blip/telegram-ai-bot/actions`

---

## 📚 Additional Resources

- [Black Documentation](https://black.readthedocs.io/)
- [Ruff Documentation](https://docs.astral.sh/ruff/)
- [MyPy Documentation](https://mypy.readthedocs.io/)
- [Pytest Documentation](https://docs.pytest.org/)
- [Pre-commit Documentation](https://pre-commit.com/)

---

## ✨ Tips & Tricks

### Format on Save (VS Code)

Add to `.vscode/settings.json`:
```json
{
  "[python]": {
    "editor.defaultFormatter": "ms-python.black-formatter",
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": {
      "source.fixAll.ruff": "explicit",
      "source.organizeImports.ruff": "explicit"
    }
  }
}
```

### Quick Quality Check Before Push

```bash
# One-liner
make format && make lint-fix && make test && git push
```

### Generate Type Stubs

```bash
python -m mypy services/ --emitted-type-stubs
```

---

## 🎯 Code Style Guidelines

### Naming Conventions

- **Functions/Variables**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`

### Type Hints

Always add type hints:
```python
def process_message(text: str, user_id: int) -> dict[str, Any]:
    """Process a user message.
    
    Args:
        text: The message text
        user_id: The user ID
        
    Returns:
        Processing result dictionary
    """
    ...
```

### Docstrings

Use Google-style docstrings:
```python
def optimize_copy(text: str, signals: ExpressiveSignals | None = None) -> str:
    """Optimize copywriting based on extracted signals.
    
    Args:
        text: The text to optimize
        signals: Optional extracted signals
        
    Returns:
        Optimized text
        
    Raises:
        ValueError: If text is invalid
    """
```

---

## 📞 Getting Help

For issues or questions:
1. Check the troubleshooting section above
2. Review tool documentation
3. Check CI/CD logs in GitHub Actions
4. Ask for help in issues/discussions

---

Last updated: 2024-06-04
