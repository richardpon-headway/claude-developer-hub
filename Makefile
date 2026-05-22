.PHONY: install run test build-ui iterm-smoke clean

# Python version for `uv sync`. uv resolves this via PATH (mise/homebrew/etc.)
# or downloads a managed standalone build if none is found.
PYTHON ?= 3.13

install:
	mise trust >/dev/null 2>&1 || true
	mise install
	cd backend && mise exec -- uv sync --python $(PYTHON)
	cd frontend && mise exec -- pnpm install

run:
	@echo "starting backend (:47823) + vite dev (:5174 with /api proxy)"
	@(cd backend && uv run python -m app.main 2>&1 | sed 's/^/[backend] /') & \
	 (cd frontend && pnpm dev 2>&1 | sed 's/^/[vite]    /') & \
	 wait

test:
	cd backend && uv run pytest tests/ -v

build-ui:
	cd frontend && pnpm build && rm -rf ../backend/app/static && cp -r dist ../backend/app/static

iterm-smoke:
	cd backend && uv run pytest tests/iterm_smoke -v

clean:
	rm -rf backend/.venv frontend/node_modules frontend/dist backend/app/static
