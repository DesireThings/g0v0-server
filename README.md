# torii-server

Backend API for [Torii](https://lazer.shikkesora.com), an osu!lazer private server. Built on FastAPI, MySQL, and Redis.

Originally forked from [g0v0-server](https://github.com/GooGuTeam/g0v0-server) (GooGuTeam), heavily reworked for Torii with custom difficulty/PP calculations, matchmaking, cosmetics, and server-authoritative logic.

## Stack

- **API:** Python 3.13 / FastAPI (uv)
- **Database:** MySQL 8.0 + Alembic migrations
- **Cache & Pub/Sub:** Redis
- **Calculators:** Performance servers (.NET 10 / .NET 8) for real-time PP calculation
- **Spectator / MP:** Interop with `torii-server-spectator` over internal network

## Quick Start (Local Dev)

### 1. Requirements

- Docker & Docker Compose (or [OrbStack](https://orbstack.dev) on macOS)

### 2. Setup

```bash
cp .env.example .env
```

Make sure the secrets (`JWT_SECRET_KEY`, `SHARED_INTEROP_SECRET`) match what your local spectator uses.

### 3. Run

```bash
docker compose up -d
```

On first run, Alembic applies database migrations automatically on startup.

- API health check: `http://localhost:9000/health`
- Adminer (DB web UI): `http://localhost:8081`

### Useful commands

```bash
# View app logs
docker compose logs -f app

# Run migrations manually
docker compose exec app uv run --no-sync g0v0-migrate upgrade-all

# Stop containers
docker compose down
```

## Contributing

See the [Contributing Guide](./CONTRIBUTING.md) for local development setup, coding guidelines, and PR workflow.

## License

Licensed under GNU AGPLv3. See [LICENSE](./LICENSE) for details.  
Original upstream: [GooGuTeam/g0v0-server](https://github.com/GooGuTeam/g0v0-server).
