# Convenience wrappers. The README spells out the raw commands if you would
# rather not use make (it is not installed by default on Windows).
.PHONY: help up down logs ps sim sim-stop verify reconcile test test-unit \
        test-e2e outage-on outage-off clean config

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## start the broker, database, collector and bridge
	docker compose up -d --build

down: ## stop everything (keeps the volumes)
	docker compose down

logs: ## follow the collector and bridge logs
	docker compose logs -f collector bridge

ps: ## show service status
	docker compose ps

sim: ## start the PLC simulator
	docker compose --profile sim up -d simulator

sim-stop: ## stop the PLC simulator
	docker compose --profile sim stop simulator

config: ## validate and print the resolved compose configuration
	docker compose config

verify: ## list the tables VTScada's REST interface exposes
	python tools/verify_history.py tables

reconcile: ## check every delivered queue row is really in the historian
	python tools/verify_history.py reconcile

test-unit: ## run the unit tests (no network needed)
	python tests/test_decoder.py
	python tests/test_sparkplug_payload.py

test-e2e: ## run the store-and-forward test against a running stack
	python tests/test_store_and_forward.py --outage-secs 60

outage-on: ## make VTScada unavailable (Windows)
	python tests/vt_suspend.py suspend

outage-off: ## bring VTScada back
	python tests/vt_suspend.py resume

clean: ## remove local run artefacts
	rm -rf tests/results state
