# Repository Guidelines

Context Quilt is a FastAPI-based LLM gateway with shared memory, routing, and enrichment logic. Use this guide to keep contributions consistent and low-latency friendly.

## Project Structure & Module Organization
Runtime code lives in `src/contextquilt`: `gateway/` handles routing, authentication, and enrichment pipelines; `memory/` contains persistence adapters; `plugins/` exposes model connectors; shared contracts sit in `types.py` and helpers in `utils/`. `main.py` wires the FastAPI app and background jobs. Keep tests in `tests/` mirroring the source tree (e.g., `tests/gateway/test_router.py`). Operational assets (`docker-compose.yml`, `Dockerfile`, `.env.example`) remain at the repo root.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: create an isolated interpreter.
- `pip install -e .[dev]`: install the gateway plus tooling identical to CI.
- `uvicorn main:app --reload`: run the API with hot reload when iterating on `src/contextquilt/*`.
- `docker-compose up -d`: boot the full stack (API, Postgres, Redis) for realistic verification.
- `python main.py`: run the minimal MVP without Docker for smoke checks.
- `pytest tests/test_specific.py::test_function`: run a single test.
- `pytest -k "test_name"`: run tests matching pattern.
- `ruff check .`: lint code.
- `ruff format .`: format code.
- `mypy src/`: type check code.

## Coding Style & Naming Conventions
Use 4-space indentation, Black's 100-character line length, and Ruff's lint set (E,W,F,I,B,C4,UP). Keep modules snake_case, classes PascalCase, functions snake_case, and constants UPPER_SNAKE. Type hints are mandatory—`py.typed` is exported and mypy runs in strict mode (except for `tests`). Prefer dataclasses or Pydantic models for payloads; add short comments only when the control flow is non-obvious. Import order: stdlib, third-party, local. Use structlog for logging.

## Testing Guidelines
Write async-aware tests with `pytest` + `pytest-asyncio`; name files `test_*.py` and functions `test_*`. Run `pytest --cov=contextquilt --cov-report=term-missing` before opening a PR and keep new modules at parity with existing coverage. Use fixture-based fakes for Redis/Postgres interactions; integration tests that hit real services should live under `tests/integration/` and be guarded with `@pytest.mark.integration`.

## Commit & Pull Request Guidelines
No upstream Git metadata is available, so seed a readable history with Conventional Commits (`feat: memory patching`, `fix: redis timeout`). Each PR should include a short purpose summary, testing transcript (commands + outcomes), linked issue or roadmap item, and screenshots or cURL output when touching HTTP routes. Keep changes scoped—separate refactors from feature work to simplify review.

## Security & Configuration Tips
Never commit `.env`; derive new keys from `env.example` and load them via `docker compose --env-file` or `python-dotenv`. When running locally, scope API keys to least privilege, scrub Redis/Postgres snapshots before sharing logs, and confirm rate limiting is enabled before demo traffic.