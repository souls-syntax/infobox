.PHONY: dev dev-backend dev-frontend check-dev-ports stop
.DEFAULT_GOAL := dev

BACKEND_PORT ?= 40000
FRONTEND_PORT ?= 40001
LLAMA_PORT ?= 40002

check-dev-ports:
	@python scripts/check_ports.py $(BACKEND_PORT) $(FRONTEND_PORT) $(LLAMA_PORT)

dev: check-dev-ports
	@test -x frontend/node_modules/.bin/vite || { echo 'Frontend dependencies are missing. Run: cd frontend && npm install' >&2; exit 1; }
	@test -x .venv/bin/uvicorn || { echo 'Backend dependencies are missing. Run: .venv/bin/pip install -r requirements.txt' >&2; exit 1; }
	LOCAL_MAIL_PROJECT_ROOT=$(CURDIR) python scripts/dev.py $(BACKEND_PORT) $(FRONTEND_PORT)

dev-backend:
	@python scripts/check_ports.py $(BACKEND_PORT) $(LLAMA_PORT)
	LOCAL_MAIL_PROJECT_ROOT=$(CURDIR) FRONTEND_PORT=$(FRONTEND_PORT) .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port $(BACKEND_PORT) --reload

dev-frontend:
	@python scripts/check_ports.py $(FRONTEND_PORT)
	@test -x frontend/node_modules/.bin/vite || { echo 'Frontend dependencies are missing. Run: cd frontend && npm install' >&2; exit 1; }
	LOCAL_MAIL_PROJECT_ROOT=$(CURDIR) npm --prefix frontend run dev -- --host 127.0.0.1 --port $(FRONTEND_PORT)

stop:
	@bash scripts/stop-dev.sh
