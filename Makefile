.PHONY: setup ollama-install ollama-serve ollama-pull test

setup:
	python3 -m venv .venv
	PIP_CACHE_DIR=.cache/pip .venv/bin/pip install -r requirements.txt
	test -f .env || cp .env.example .env

ollama-install:
	./scripts/ollama.sh install

ollama-serve:
	./scripts/ollama.sh serve

ollama-pull:
	./scripts/ollama.sh pull

test:
	.venv/bin/python -m pytest -v
