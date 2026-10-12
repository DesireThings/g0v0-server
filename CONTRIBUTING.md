# Contributing to Torii Server

Thank you for your interest in contributing to Torii Server! This guide outlines our setup instructions, development standards, and architecture.

## Repository Setup

Clone the server and its auxiliary services:

```bash
git clone https://github.com/ShikkesoraSIM/torii-server.git
```

Additionally, if running full-stack local testing:

- Clone the spectator server:
  ```bash
  git clone https://github.com/ShikkesoraSIM/torii-server-spectator.git spectator-server
  ```
- Place any custom ruleset DLLs into the `rulesets/` directory if required.

## Development Environment

### 1. Requirements

- Python 3.12+ (Python 3.13 recommended)
- `uv` for fast Python dependency management
- Docker & Docker Compose (or OrbStack) for local MySQL 8.0 and Redis services

### 2. Configuration

Copy the example configuration:

```bash
cp .env.example .env
```

Ensure secrets (`JWT_SECRET_KEY`, `SHARED_INTEROP_SECRET`) match what your local spectator server uses.

### 3. Dependency Management

Install dependencies with `uv`:

```bash
uv sync
```

### 4. Running Locally

Using Docker Compose:

```bash
docker compose up -d
```

Or running the services directly:

```bash
# torii-server
uv run uvicorn main:app --host 0.0.0.0 --port 9000 --reload

# spectator-server
dotnet run --project spectator-server/osu.Server.Spectator --urls "http://0.0.0.0:8086"
```

## Architectural Guidelines & Standards

### Project Structure

- `main.py`: FastAPI entry point initializing routing and lifespan events.
- `pyproject.toml`: Configuration for `uv`, `ruff`, and `pyright`.
- `alembic.ini` & `migrations/`: Alembic database migration scripts.
- `app/`: Core application source code.
  - `router/`: API route definitions (v1, v2, notification, private).
  - `service/`: Core business logic classes (ranking calculations, daily challenges, matchmaking).
  - `database/`: SQLModel database models and session management.
  - `models/`: Non-database models, schemas, and DTOs.
  - `tasks/`: Scheduled jobs (APScheduler) and startup/shutdown lifecycle tasks.
  - `dependencies/`: FastAPI dependency injection providers.
  - `helpers/` & `utils/`: Utility functions and helper classes.
  - `calculator.py`: Difficulty and performance point calculation logic.

### Database Models

Database models live in `app.database` and follow the `Dict` - `Model` - `Table` pattern with on-demand fields:

1. **Dict**: TypedDict representing converted model data (for static typing).
2. **Model**: Inherits from `DatabaseModel[Dict]`, declaring fields and computed attributes (`@included` or `@ondemand`).
3. **Table**: Inherits from `Model` with `table=True`, representing the actual SQL table.

Database access should use the `with_db` context manager from `app.dependencies.database`:

```python
from app.dependencies.database import with_db

async with with_db() as session:
    ...
```

### API Routers

- `app/router/v2`: osu! v2 API implementation. Do not add custom non-standard endpoints here.
- `app/router/notification`: osu! v2 chat, notification, and presence endpoints.
- `app/router/v1`: Legacy osu! v1 API implementation.
- `app/router/auth.py`: Authentication and token generation.
- `app/router/private`: Torii internal / server-to-server APIs.

### Code Quality & Validation

Before submitting a pull request, ensure all local checks pass:

```bash
uv run ruff check .
uv run pyright
```

Maintain the maintainer preference for small, testable, single-responsibility pull requests. Never break database schemas or public wire APIs.

## Community & Discussions

Join the [Torii Community Discord](https://discord.gg/zeuKQquCSE) for development discussions and server architecture questions.
