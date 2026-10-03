.PHONY: test gate

# scripts/setup.sh installs into .venv; prefer it so `python` is the project's.
export PATH := $(CURDIR)/.venv/bin:$(PATH)

test:  ## Dispatcher/console unit suite
	python -m pytest -q

# The box's gate_cmd: sessions run it after every ticket and in review.
# Mirrors CI (api-types freshness, typecheck + build, both suites). The suites
# run explicitly because the CRAP gate skips an area with no changed source.
gate: test  ## Everything a change must pass before it is pushed
	cd frontend && pnpm gen:api && git diff --exit-code src/lib/api-types.ts
	cd frontend && pnpm build && pnpm test
	sh .my-skills/crap-gate/run.sh
