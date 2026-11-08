.PHONY: test fast gate

# Each command runs in .venv through uv: uv run first brings .venv to the exact
# versions of uv.lock (the set CI installs), and --locked fails when uv.lock
# does not match pyproject.toml. (An `export PATH` here does not reach simple
# recipe commands under macOS make 3.81, so `python` was the global one.)
RUN := uv run --locked
# Pass selections as literal environment values, never shell or make code.
ifneq ($(origin TESTS), undefined)
override TESTS := $(value TESTS)
export TESTS
endif
override IMPLEMENT_SPEC_LOCKED := $(value IMPLEMENT_SPEC_LOCKED)
export IMPLEMENT_SPEC_LOCKED

test:  ## Dispatcher/console unit suite
	$(RUN) python -m pytest -q

fast:  ## Selected acceptance tests plus frontend lint and types
	$(RUN) python scripts/fast_gate.py

# The box's gate_cmd: sessions run it after every ticket and in review.
# Mirrors CI (api-types freshness, typecheck + build, both suites). The suites
# run explicitly because the CRAP gate skips an area with no changed source.
gate: test  ## Everything a change must pass before it is pushed
	cd frontend && pnpm lint && pnpm typecheck && pnpm test
	cd frontend && $(RUN) pnpm gen:api && git diff --exit-code src/lib/api-types.ts
	cd frontend && pnpm build
	$(RUN) sh .my-skills/crap-gate/run.sh
