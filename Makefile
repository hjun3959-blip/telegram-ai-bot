# Developer tooling targets. These do NOT run or deploy the bot.

PYTHON ?= python

.PHONY: codegraph codegraph-test

# Regenerate the static code graph artifacts under docs/codegraph/.
codegraph:
	$(PYTHON) scripts/build_codegraph.py

# No-network self-test for the code graph builder.
codegraph-test:
	$(PYTHON) scripts/build_codegraph.py --self-test
