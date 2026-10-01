SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the whole platform
	$(COMPOSE) up -d --build
	@echo ""
	@echo "Gateway      : http://localhost:8000/api/v1"
	@echo "Jaeger UI    : http://localhost:16686"
	@echo "Kong Manager : http://localhost:8002"
	@echo "Metrics      : http://localhost:8100/metrics"

down: ## Stop everything (keeps database volumes)
	$(COMPOSE) down

reset: ## Stop everything and delete database volumes (fresh seed data)
	$(COMPOSE) down -v

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail logs of all services
	$(COMPOSE) logs -f --tail=100

demo: ## Run the scripted demo (all scenarios)
	./scripts/demo.sh

test: ## Unit/API tests for services and agents (no Docker needed)
	cd services/customer-service && python -m pytest -q
	cd services/order-service && python -m pytest -q
	cd agents/coordinator && python -m pytest -q

e2e: ## End-to-end tests through the gateway (requires `make up`)
	python -m pytest tests/e2e -v

openapi: ## Regenerate docs/openapi/*.yaml from the code
	python scripts/export_openapi.py

install-dev: ## Install Python deps for running tests locally
	pip install -e libs/comco_common -e libs/comco_agentkit -r services/customer-service/requirements.txt -r agents/coordinator/requirements.txt -r requirements-dev.txt

stop-order: ## Failure demo: stop the Order Service
	$(COMPOSE) stop order-service

start-order: ## Failure demo: start the Order Service again
	$(COMPOSE) start order-service

up-llm: ## Start with the optional local LLM planner (Ollama) and pull the model
	USE_LLM=true $(COMPOSE) --profile llm up -d --build
	$(COMPOSE) exec ollama ollama pull $${OLLAMA_MODEL:-llama3.2:3b}

.PHONY: help up down reset ps logs demo test e2e openapi install-dev stop-order start-order up-llm
