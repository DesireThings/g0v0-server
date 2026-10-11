# copilot-instruction

> This file is a replica of AGENTS.md. AGENTS.md takes precedence in all cases.

> Guidelines for using automation and AI agents (GitHub Copilot, dependency/CI bots, and runtime schedulers/workers in the repository) for the g0v0-server repository.

---

## API Reference

This project must maintain compatibility with the public osu! API. When adding or mapping endpoints, please refer to:

- **v1 (Legacy):** [https://github.com/ppy/osu-api/wiki](https://github.com/ppy/osu-api/wiki)
- **v2 (OpenAPI):** [https://osu.ppy.sh/docs/openapi.yaml](https://osu.ppy.sh/docs/openapi.yaml)

Any implementation in `app/router/v1/`, `app/router/v2/`, or `app/router/notification/` must remain consistent with the official specifications. Custom or experimental endpoints must be placed in `app/router/private/`.

---

## Agent Categories

Permitted agents are divided into three categories:

- **Code generation / completion agents** (e.g., GitHub Copilot or other LLMs) — Permitted **only when** a maintainer reviews and approves the output.
- **Automated maintenance agents** (e.g., Dependabot, Renovate, pre-commit.ci) — Permitted, but must adhere to strict PR and CI policies.
- **Runtime / background agents** (schedulers, workers) — Part of production code; must adhere to lifecycle, concurrency, and idempotency specifications.

All changes generated or suggested by agents must comply with the following rules.

---

## Rules for All Agents

1. **Single-responsibility PRs.** An agent's PR must solve only one issue (one feature, one bug fix, or one dependency update). Commit messages should follow the Angular style (e.g., `feat(api): add ...`).
2. **Pass Lint and CI checks.** Every PR (including those created by agents) must pass `pyright`, `ruff`, `pre-commit` hooks, and repository CI before merging. PRs should include links to CI run results.
3. **Never commit secrets.** Agents must not commit secrets, passwords, tokens, or real `.env` values. If potential secrets are detected, the agent must abort and notify designated maintainers.
4. **API location restrictions.** Do not add new public endpoints under `app/router/v1` or `app/router/v2` unless the endpoint exists in the official v1/v2 specification. Custom or experimental endpoints must be placed in `app/router/private/`.
5. **Keep public contracts stable.** Do not modify response schemas, route prefixes, or other public contracts without an approved migration plan. If changes occur, the PR must include clear compatibility explanations.

---

## Copilot / LLM Usage

> Unified guidance on using GitHub Copilot and other LLM-based assistive tools in this repository.

### Key Project Structure (Things to Know)

- **Application Entry Point:** `main.py` — FastAPI application containing startup/shutdown lifecycle management (fetchers, GeoIP, scheduler, caching & health checks, Redis messaging, statistics, achievement system).

- **Routers:** `app/router/` contains all route groups. Major routes include:
  - `v1/` (v1 endpoints)
  - `v2/` (v2 endpoints)
  - `notification/` routes (chat/notification subsystem)
  - `auth.py` (authentication/token flows)
  - `private/` (custom or experimental endpoints)

  **Rule:** `v1/` and `v2/` must correspond to the official API. Only internal or experimental endpoints should be placed in `app/router/private/`.

- **Models and Database Tools:**
  - SQLModel/ORM models are in `app/database/`.
  - Non-database models are in `app/models/`.
  - When modifying models/schemas, Alembic migrations must be generated, and the generated SQL and indexes must be inspected manually.

- **Service Layer:** `app/service/` holds domain logic (e.g., caching utilities, notification/email logic). Complex logic should be placed in services, not in router handlers.

- **Tasks:** `app/tasks/` holds tasks (scheduled tasks, startup tasks, shutdown tasks).
  - All are exported in `__init__.py`.
  - Startup/shutdown tasks are invoked in `main.py`'s `lifespan`.
  - Scheduled tasks use APScheduler.

- **Caching and Dependencies:** Use Redis dependencies and caching services provided in `app/dependencies/` (following existing key naming conventions, e.g., `user:{id}:...`).

- **Logging:** Use the logging utility provided in `app/log`.

### Practical Workflows (Prompt Patterns)

- **Adding a v2 endpoint (the right way):** Add a file under `app/router/v2/`, export the router, and implement an async handler based on database and cache dependencies. **Do not** add non-official endpoints to v1/v2.
- **Adding custom endpoints:** Place in `app/router/private/`, keep handlers concise, and put business logic into `app/service/`.
- **Authentication:** Use dependency injection provided by [`app.dependencies.user`](../app/dependencies/user.py), such as `ClientUser` and `get_current_user`, as shown below:
  
  ```python
  from typing import Annotated
  from fastapi import Security
  from app.dependencies.user import ClientUser, get_current_user


  @router.get("/some-api")
  async def _(current_user: Annotated[User, Security(get_current_user, scopes=["public"])]):
      ...


  @router.get("/some-client-api")
  async def _(current_user: ClientUser):
      ...
  ```

- **Adding background tasks:** Write task logic in `app/service/_job.py` (idempotent, retryable). Place scheduler entry points in `app/scheduler/_scheduler.py`, and register in the application lifecycle.
- **Database schema changes:** Modify SQLModel models in `app/models/`, run `alembic revision --autogenerate`, check the migration, and test `alembic upgrade head` locally before committing.
- **Cache writes and responses:** Use existing `UserResp` models and `UserCacheService`; async cache writes should use background tasks.

### Prompting Guidance (Input for LLMs / Copilot)

- Specify exact file locations and constraints (e.g., `Add an async endpoint under app/router/private/... DO NOT add to app/router/v1 or v2`).
- Request async handler functions, DB/Redis dependency injection, reuse of existing services/utilities, proper type annotations, and minimal pytest test cases.

### Conventions & Quality Requirements

- **Use Annotated-style dependency injection** in route handlers.
- **Commit message style:** `type(scope): subject` (Angular style).
- **Async first:** Route handlers must be async functions; avoid blocking the event loop.
- **Separation of concerns:** Business logic should reside in services, not in routes.
- **Error handling:** Use `HTTPException` for client errors; use structured logging for server errors.
- **Types and linting:** Code must pass `pyright` and `ruff` checks before requesting review.
- **Comments:** Avoid excessive comments; only add concise explanations for non-obvious logic.
- **Logging:** Use the `log` function provided in `app.log` to get a logger instance (except for services and tasks).

### Tooling Reference

```
uv sync
pre-commit install
pre-commit run --all-files
pyright
ruff .
alembic revision --autogenerate -m "feat(db): ..."
alembic upgrade head
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### PR Scope Guidance

- Keep PRs focused: do one thing at a time (e.g., endpoints or refactoring, do not mix).
- When unsure, consult existing services and add brief explanatory comments.

### PR Review Rules

> Reference for GitHub Copilot PR reviews.

1. If the PR modifies endpoints, briefly explain the endpoint's purpose and expected behavior. Also verify whether it meets the API location restrictions above.
2. If the PR modifies database models, it must include an Alembic migration. Verify that the migration's SQL statements and indexes are appropriate.
3. Other modified functionality must include a brief explanation.
4. Provide performance optimization suggestions (see below).

---

## Performance Optimization Tips

The following performance optimization suggestions are tailored to this repository's architecture (FastAPI + SQLModel/SQLAlchemy, Redis caching, background scheduler):

### Database

- **Select only required columns.** Use `select(Model.col1, Model.col2)` instead of `select(Model)`.

```py
stmt = select(User.id, User.username).where(User.active == True)
rows = await session.execute(stmt)
```

- **Use `select(exists())` to check existence.** Avoid loading full rows:

```py
from sqlalchemy import select, exists
exists_stmt = select(exists().where(User.id == some_id))
found = await session.scalar(exists_stmt)
```

- **Avoid N+1 queries.** Use `selectinload` or `joinedload` when related objects are needed.

- **Batch operations.** Insert/update operations should be executed in batches within a single transaction rather than multiple small transactions.


- If the task originates from an API router, use FastAPI's [`BackgroundTasks`](https://fastapi.tiangolo.com/tutorial/background-tasks).
- Otherwise, use `bg_tasks` from `app.utils`, which provides functionality similar to FastAPI's `BackgroundTasks`.

---

## Additional Requirements for Specific LLMs

### Claude Code

- Do not create extra test scripts.
