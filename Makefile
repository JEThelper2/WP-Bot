# WP-Bot pilot shortcuts (Docker). Requires Docker Compose v2.20+.
#
#   make up      build + start the whole stack (Track A, Track B, Redis,
#                Postgres, WordPress sandbox)
#   make test    run the FULL test suite inside the stack, including the
#                Docker-gated WordPress/Postgres integration suites
#   make logs    follow all service logs
#   make down    stop the stack

COMPOSE := docker compose -f deploy/docker-compose.yml

.PHONY: up down logs test rebuild

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

test:
	$(COMPOSE) --profile test run --rm test

rebuild:
	$(COMPOSE) build --no-cache
