# AI Stores — developer convenience wrapper around docker compose.
#
#   make init             # one-time: create .env with real generated secrets
#   make up               # build + run in the foreground (Ctrl-C to stop)
#   make provision-store SLUG=acme NAME="Acme Co"   # add a store at runtime
#
# One instance serves many stores at /{store}; adding a store never redeploys.
# Everything here is a thin, self-documenting shim; the source of truth is
# still docker-compose.yml / Dockerfile.

# Prefer `docker compose` (v2); fall back to legacy `docker-compose`.
COMPOSE ?= docker compose
PY      ?= python3
# Model for the conversational editor. Override: make ai-pull OLLAMA_MODEL=...
OLLAMA_MODEL ?= qwen2.5:7b-instruct

.DEFAULT_GOAL := help

.PHONY: help init secrets up up-d down restart logs ps clean build ai-pull provision-store reconcile archive-store restore-store delete-store test test-deps

# Log in as the .env admin into a temp cookie jar ($$JAR), setting $$BASE.
# Used by the store-management targets below; keeps each recipe a one-liner.
define login_admin
	BASE=$${BASE_URL:-http://localhost:$${APP_PORT:-8000}}; \
	EMAIL=$$(grep -E '^ADMIN_EMAIL=' .env | cut -d= -f2-); \
	PASS=$$(grep -E '^ADMIN_PASSWORD=' .env | cut -d= -f2-); \
	JAR=$$(mktemp); \
	curl -sf -c $$JAR -H 'Content-Type: application/json' \
	  -d "{\"email\":\"$$EMAIL\",\"password\":\"$$PASS\"}" $$BASE/auth/login >/dev/null \
	  || { echo "Admin login failed — check ADMIN_EMAIL/ADMIN_PASSWORD in .env and that the app is up."; rm -f $$JAR; exit 1; }
endef

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

init: ## Create .env from .env.example with freshly generated secrets
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it untouched. Delete it first to regenerate."; \
	else \
		cp .env.example .env; \
		MASTER_KEY=$$($(PY) -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"); \
		JWT_SECRET=$$($(PY) -c "import secrets; print(secrets.token_urlsafe(48))"); \
		sed -e "s|^MDB_ENGINE_MASTER_KEY=.*|MDB_ENGINE_MASTER_KEY=$$MASTER_KEY|" \
		    -e "s|^MDB_JWT_SECRET=.*|MDB_JWT_SECRET=$$JWT_SECRET|" \
		    .env > .env.tmp && mv .env.tmp .env; \
		echo "Created .env with generated MDB_ENGINE_MASTER_KEY and MDB_JWT_SECRET."; \
		echo "Review ADMIN_EMAIL / ADMIN_PASSWORD before exposing this anywhere."; \
	fi

secrets: ## Print freshly generated secrets (does not write .env)
	@echo "MDB_ENGINE_MASTER_KEY=$$($(PY) -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())')"
	@echo "MDB_JWT_SECRET=$$($(PY) -c 'import secrets; print(secrets.token_urlsafe(48))')"

build: ## Build the app image
	$(COMPOSE) build

up: ## Build + start in the foreground (Ctrl-C to stop)
	$(COMPOSE) up --build

up-d: ## Build + start detached (in the background)
	$(COMPOSE) up --build -d

down: ## Stop and remove containers (keeps the mongo volume)
	$(COMPOSE) down

restart: ## Restart the app container only
	$(COMPOSE) restart app

logs: ## Follow logs from all services
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

clean: ## Stop and remove containers AND the mongo data volume
	$(COMPOSE) down -v

ai-pull: ## Pull the Ollama model for the chat editor (run once after `up`)
	$(COMPOSE) exec ollama ollama pull $(OLLAMA_MODEL)

test-deps: ## Install the test/dev dependencies (pytest, pytest-asyncio, asgi-lifespan)
	$(PY) -m pip install -r requirements-dev.txt

test: ## Run the pytest suite in-process (needs a local Mongo on :27017; see test-deps)
	$(PY) -m pytest

provision-store: ## Create a store on a running instance: make provision-store SLUG=acme NAME="Acme Co"
	@test -n "$(SLUG)" || { echo 'Usage: make provision-store SLUG=acme NAME="Acme Co"'; exit 1; }
	@$(login_admin); \
	 echo "Provisioning '$(SLUG)' at $$BASE ..."; \
	 curl -s -b $$JAR -H 'Content-Type: application/json' \
	   -d "{\"slug\":\"$(SLUG)\",\"name\":\"$(NAME)\"}" $$BASE/manage/stores; \
	 echo; rm -f $$JAR

archive-store: ## Suspend a store (stops routing, keeps data): make archive-store SLUG=acme
	@test -n "$(SLUG)" || { echo 'Usage: make archive-store SLUG=acme'; exit 1; }
	@$(login_admin); \
	 curl -s -b $$JAR -X POST $$BASE/manage/stores/$(SLUG)/archive; \
	 echo; rm -f $$JAR

restore-store: ## Re-enable an archived/failed store: make restore-store SLUG=acme
	@test -n "$(SLUG)" || { echo 'Usage: make restore-store SLUG=acme'; exit 1; }
	@$(login_admin); \
	 curl -s -b $$JAR -X POST $$BASE/manage/stores/$(SLUG)/restore; \
	 echo; rm -f $$JAR

delete-store: ## Permanently deprovision a store (drops all data): make delete-store SLUG=acme
	@test -n "$(SLUG)" || { echo 'Usage: make delete-store SLUG=acme'; exit 1; }
	@$(login_admin); \
	 echo "Deleting '$(SLUG)' at $$BASE (irreversible) ..."; \
	 curl -s -b $$JAR -X DELETE -H 'Content-Type: application/json' \
	   -d "{\"confirm\":\"$(SLUG)\"}" $$BASE/manage/stores/$(SLUG); \
	 echo; rm -f $$JAR

reconcile: ## Finish stuck provisions/deletes and report orphans (DROP_ORPHANS=1 to drop them)
	@$(login_admin); \
	 BODY=$$( [ "$(DROP_ORPHANS)" = "1" ] && echo '{"drop_orphans":true}' || echo '{}' ); \
	 curl -s -b $$JAR -X POST -H 'Content-Type: application/json' -d "$$BODY" $$BASE/manage/reconcile; \
	 echo; rm -f $$JAR
