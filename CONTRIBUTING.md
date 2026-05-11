# Contributing to Claude Developer Hub

Thanks for considering a contribution. CDH is small and pre-MVP, so the
bar for "what fits" is high — open an issue first if you want to land
anything more than a small bug fix or doc tweak.

## Development setup

```sh
# Required tooling on PATH:
#   - python 3.13+
#   - node 20+
#   - uv, pnpm, mise (mise auto-resolves the python/node versions
#     from mise.toml when you cd into the repo)
#   - gh, jq

git clone <your-fork-url>
cd claude-developer-hub
mise trust          # one-time, accepts mise.toml in this repo
make install        # uv sync + pnpm install
make run            # backend on :47823, vite on :5173
```

## Running tests

```sh
make test           # backend: pytest
cd frontend && pnpm test   # frontend: vitest
```

The `make iterm-smoke` target runs against a real local iTerm2 instance
and is excluded from CI; only run it locally with iTerm2's Python API
enabled.

## Pull requests

- Branch from `main`. Open a draft PR early.
- Keep changes scoped — one PR per logical unit.
- Make sure `make test` and `pnpm test` pass.
- The PR template asks for a short summary and a test plan; please fill
  both in.

## Reporting issues

Use the issue templates under `.github/ISSUE_TEMPLATE/`. For security
issues, follow [SECURITY.md](SECURITY.md) instead.
