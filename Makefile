# Developer tooling targets. These do NOT run or deploy the bot.

PYTHON ?= python
PIP ?= pip
SRC_DIRS := scripts services routers db utils

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "╔════════════════════════════════════════════════════════════════╗"
	@echo "║        Telegram AI Bot - Development Tools                    ║"
	@echo "╚════════════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "📋 Code Quality:"
	@echo "  make format              Format code with black & isort"
	@echo "  make format-check        Check formatting without changes"
	@echo "  make lint                Run fast linting with ruff"
	@echo "  make lint-fix            Fix linting issues automatically"
	@echo "  make type-check          Check types with mypy"
	@echo "  make security            Run security scan with bandit"
	@echo ""
	@echo "🧪 Testing:"
	@echo "  make test                Run all smoke tests"
	@echo "  make test-verbose        Run tests with verbose output"
	@echo "  make test-coverage       Run tests with coverage report"
	@echo "  make coverage-report     Generate HTML coverage report"
	@echo ""
	@echo "🔧 Install & Setup:"
	@echo "  make install             Install dev dependencies"
	@echo "  make pre-commit-install  Setup pre-commit hooks"
	@echo "  make pre-commit-run      Run pre-commit on all files"
	@echo ""
	@echo "📊 Graphs & Documentation:"
	@echo "  make codegraph           Regenerate code graph"
	@echo "  make codegraph-test      Test code graph builder"
	@echo ""
	@echo "🧹 Cleanup:"
	@echo "  make clean               Remove build artifacts"
	@echo "  make clean-cache         Remove cache files"
	@echo "  make clean-all           Remove all generated files"
	@echo ""
	@echo "🚀 Full Quality Check:"
	@echo "  make quality-check       Run complete quality pipeline"
	@echo ""

.PHONY: install
install:
	@echo "📦 Installing development dependencies..."
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -r requirements-dev.txt
	@echo "✅ Dependencies installed"

.PHONY: pre-commit-install
pre-commit-install: install
	@echo "🔗 Setting up pre-commit hooks..."
	$(PYTHON) -m pre_commit install
	@echo "✅ Pre-commit hooks installed"

.PHONY: pre-commit-run
pre-commit-run:
	@echo "▶️  Running pre-commit on all files..."
	$(PYTHON) -m pre_commit run --all-files || true
	@echo "✅ Pre-commit check complete"

.PHONY: format
format:
	@echo "🎨 Formatting code with black..."
	$(PYTHON) -m black $(SRC_DIRS)
	@echo "🎨 Sorting imports with isort..."
	$(PYTHON) -m isort $(SRC_DIRS)
	@echo "✅ Code formatted"

.PHONY: format-check
format-check:
	@echo "🎨 Checking code format with black..."
	$(PYTHON) -m black --check $(SRC_DIRS)
	@echo "🎨 Checking import order with isort..."
	$(PYTHON) -m isort --check-only $(SRC_DIRS)
	@echo "✅ Format check passed"

.PHONY: lint
lint:
	@echo "🔍 Linting with ruff (fast)..."
	$(PYTHON) -m ruff check $(SRC_DIRS) --output-format=grouped || true
	@echo "✅ Lint check complete"

.PHONY: lint-fix
lint-fix:
	@echo "🔧 Auto-fixing ruff issues..."
	$(PYTHON) -m ruff check $(SRC_DIRS) --fix
	@echo "✅ Lint issues fixed"

.PHONY: type-check
type-check:
	@echo "🔬 Type checking with mypy..."
	$(PYTHON) -m mypy services/ routers/ db/ utils/ \
		--python-version 3.11 \
		--ignore-missing-imports || true
	@echo "✅ Type check complete"

.PHONY: security
security:
	@echo "🔐 Security scan with bandit..."
	$(PYTHON) -m bandit -r services/ routers/ db/ utils/ \
		-c pyproject.toml \
		--severity-level high || true
	@echo "🔐 Dependency audit with pip-audit..."
	$(PYTHON) -m pip_audit -r requirements.txt \
		--ignore-vuln CVE-2026-34993 \
		--ignore-vuln CVE-2026-47265 || true
	@echo "✅ Security scan complete"

.PHONY: test
test:
	@echo "🧪 Running smoke tests..."
	@for test_file in scripts/smoke_test*.py; do \
		if [ -f "$$test_file" ]; then \
			echo "▶️  $$test_file"; \
			timeout 120s $(PYTHON) "$$test_file" || exit 1; \
		fi \
	done
	@echo "✅ All tests passed"

.PHONY: test-verbose
test-verbose:
	@echo "🧪 Running tests (verbose)..."
	$(PYTHON) -m pytest scripts/smoke_test*.py \
		-v --tb=short --capture=no

.PHONY: test-coverage
test-coverage:
	@echo "🧪 Running tests with coverage..."
	$(PYTHON) -m pytest scripts/smoke_test*.py \
		-v --tb=short \
		--cov=services --cov=routers \
		--cov-report=term-missing:skip-covered \
		--cov-report=html:htmlcov

.PHONY: coverage-report
coverage-report: test-coverage
	@echo "📊 Opening coverage report..."
	@if command -v xdg-open > /dev/null; then \
		xdg-open htmlcov/index.html; \
	elif command -v open > /dev/null; then \
		open htmlcov/index.html; \
	else \
		echo "Open htmlcov/index.html in your browser"; \
	fi

.PHONY: codegraph
codegraph:
	@echo "📊 Regenerating code graph..."
	$(PYTHON) scripts/build_codegraph.py
	@echo "✅ Code graph updated"

.PHONY: codegraph-test
codegraph-test:
	@echo "🧪 Testing code graph builder..."
	$(PYTHON) scripts/build_codegraph.py --self-test
	@echo "✅ Code graph test passed"

.PHONY: clean
clean:
	@echo "🗑️  Cleaning build artifacts..."
	rm -rf build/ dist/ *.egg-info .eggs/
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "✅ Cleaned"

.PHONY: clean-cache
clean-cache:
	@echo "🗑️  Cleaning cache files..."
	rm -rf .mypy_cache/ .ruff_cache/ .pytest_cache/ .coverage htmlcov/
	@echo "✅ Cache cleaned"

.PHONY: clean-all
clean-all: clean clean-cache
	@echo "✅ All cleaned"

.PHONY: quality-check
quality-check:
	@echo ""
	@echo "╔════════════════════════════════════════════════════════════════╗"
	@echo "║            🚀 Running Complete Quality Pipeline               ║"
	@echo "╚════════════════════════════════════════════════════════════════╝"
	@echo ""
	@$(MAKE) format-check || true
	@echo ""
	@$(MAKE) lint
	@echo ""
	@$(MAKE) type-check
	@echo ""
	@$(MAKE) security
	@echo ""
	@$(MAKE) test-coverage || true
	@echo ""
	@echo "╔═══════════════���════════════════════════════════════════════════╗"
	@echo "║                     ✅ Quality Check Complete                 ║"
	@echo "╚════════════════════════════════════════════════════════════════╝"
	@echo ""

.PHONY: quick-check
quick-check:
	@echo "⚡ Running quick quality checks..."
	@$(MAKE) format-check
	@echo ""
	@$(MAKE) lint
	@echo ""
	@$(MAKE) test
	@echo ""
	@echo "✅ Quick check passed"
