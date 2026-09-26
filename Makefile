# Thin wrapper over scripts/bootstrap.sh.
#
# The script is the single source of truth; this only makes the common verbs
# shorter to type. Everything here delegates, so `make up` and
# `./scripts/bootstrap.sh up` can never drift apart.

SHELL := /bin/bash
BOOTSTRAP := ./scripts/bootstrap.sh

.DEFAULT_GOAL := help
.PHONY: help up down reset status disk test sim-start sim-stop logs

help: ## show this help
	@$(BOOTSTRAP) help

up: ## bring the whole system up (idempotent)
	@$(BOOTSTRAP) up

down: ## stop everything, keep the data
	@$(BOOTSTRAP) down

reset: ## destroy the database, secrets and TLS material
	@$(BOOTSTRAP) reset

status: ## what is running, and where
	@$(BOOTSTRAP) status

disk: ## storage used and the projected growth rate
	@$(BOOTSTRAP) disk

test: ## every test suite and verification script
	@$(BOOTSTRAP) test

sim-start: ## start the simulator on the host
	@$(BOOTSTRAP) sim:start

sim-stop: ## stop the simulator
	@$(BOOTSTRAP) sim:stop

logs: ## follow logs from the stack and the simulator
	@trap 'exit 0' INT TERM; \
	docker compose logs -f & \
	tail -f sim.log 2>/dev/null || true
