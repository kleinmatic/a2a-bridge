# a2a-bridge — development and demo tasks.
#
# `make` on its own lists every target. Nothing here needs a global install:
# the Python targets build a local .venv, the demo targets need only Docker.

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff

# The LibreChat example stack. Compose resolves the paths inside it relative to
# the file, so these targets work from the repo root.
DEMO       := examples/librechat
DEMO_FILE  := $(DEMO)/compose.yaml
COMPOSE    := docker compose -f $(DEMO_FILE)

.DEFAULT_GOAL := help

# ── Development ───────────────────────────────────────────────────────────────

setup: $(VENV) ## Create .venv and install the package with dev extras

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e '.[dev]'
	@echo "  .venv ready — run 'make test'"

test: setup ## Run the contract tests against the recorded fixtures
	$(PYTEST) -q

lint: setup ## Check lint rules
	$(RUFF) check src tests

# Not part of `lint`: the existing source predates ruff format, so requiring it
# would fail on untouched code and turn any small change into a whole-repo
# reformat. Run it deliberately if you want that, on its own commit.
format: setup ## Reformat and autofix
	$(RUFF) format src tests
	$(RUFF) check --fix src tests

run: setup agents.yml ## Run the bridge on localhost:8600 against ./agents.yml
	A2A_BRIDGE_CONFIG=agents.yml $(PY) -m a2a_bridge.server

agents.yml:
	cp examples/agents.example.yml agents.yml
	@echo "  wrote agents.yml from the example — set card_url before 'make run'"

clean: ## Remove the venv and caches
	rm -rf $(VENV) .pytest_cache .ruff_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# ── Image ─────────────────────────────────────────────────────────────────────

image: ## Build the container image
	docker build -t a2a-bridge .

# ── LibreChat demo stack ──────────────────────────────────────────────────────
# LibreChat + Mongo + this bridge, wired together. See examples/librechat/README.md.

demo-config: $(DEMO)/agents.yml $(DEMO)/.env ## Create the two ignored config files from their examples

$(DEMO)/agents.yml:
	cp $(DEMO)/agents.yml.example $@
	@echo "  wrote $@ — point card_url at your agent"

# LibreChat refuses to start without CREDS_KEY/CREDS_IV/JWT secrets, and they are
# per-deployment identity: regenerating them invalidates existing sessions and makes
# already-encrypted values unreadable. Generated once, here, and then left alone.
$(DEMO)/.env:
	@sed -e "s|^CREDS_KEY=.*|CREDS_KEY=$$(openssl rand -hex 32)|" \
	     -e "s|^CREDS_IV=.*|CREDS_IV=$$(openssl rand -hex 16)|" \
	     -e "s|^JWT_SECRET=.*|JWT_SECRET=$$(openssl rand -hex 32)|" \
	     -e "s|^JWT_REFRESH_SECRET=.*|JWT_REFRESH_SECRET=$$(openssl rand -hex 32)|" \
	     $(DEMO)/env.example > $@
	@echo "  wrote $@ with freshly generated secrets"

demo-up: demo-config ## Build and start LibreChat + the bridge (http://localhost:3080)
	$(COMPOSE) up --build -d
	@echo
	@echo "  LibreChat  http://localhost:3080   (register an account on first visit)"
	@echo "  bridge     http://localhost:8600/v1/models"

demo-down: ## Stop the demo stack, keeping conversations and the context map
	$(COMPOSE) down

demo-logs: ## Follow the bridge's logs — where wiring problems actually surface
	$(COMPOSE) logs -f a2a-bridge

demo-restart: ## Restart the bridge after editing agents.yml (it reads config once, at startup)
	$(COMPOSE) up -d --force-recreate a2a-bridge

# /healthz is open; everything under /v1 wants the shared key, so read it back
# out of the .env rather than making the caller remember it.
demo-check: ## Ask the bridge directly, with no chat client in the loop
	@curl -fsS localhost:8600/healthz && echo "  <- healthz"
	@key=$$(grep -E '^A2A_BRIDGE_API_KEY=' $(DEMO)/.env | cut -d= -f2-); \
	 curl -fsS -H "Authorization: Bearer $$key" localhost:8600/v1/models \
	   && echo "  <- models" \
	   || echo "  /v1/models failed — see 'make demo-logs'"

demo-reset: ## Delete the demo stack AND its data (conversations, accounts, context map)
	$(COMPOSE) down -v

# ── Meta ──────────────────────────────────────────────────────────────────────

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: setup test lint format run clean image demo-config demo-up demo-down \
        demo-logs demo-restart demo-check demo-reset help
