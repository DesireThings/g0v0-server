from contextlib import asynccontextmanager
import json
from pathlib import Path

from app.calculator import init_calculator
from app.config import settings

print("CHECK_CLIENT_VERSION env raw =", repr(getattr(settings, "check_client_version", None)))
print("CLIENT_VERSION_URLS =", getattr(settings, "client_version_urls", None))

from app.database import User
from app.dependencies.database import (
    Database,
    engine,
    redis_binary_client,
    redis_client,
    redis_message_client,
    redis_rate_limit_client,
)
from app.dependencies.fetcher import get_fetcher
from app.dependencies.scheduler import start_scheduler, stop_scheduler
from app.log import system_logger
from app.middleware.hwid_tracking import HwidTrackingMiddleware
from app.middleware.verify_session import VerifySessionMiddleware
from app.models.mods import init_mods, init_ranked_mods
from app.models.score import init_ruleset_version_hash
from app.router.notification.server import chat_router
from app.router import (
    api_v1_router,
    api_v2_router,
    auth_router,
    beatmap_submission_router,
    chat_router,
    file_router,
    lio_router,
    lio_spectator_router,
    private_router,
    redirect_api_router,
)
from app.router.hiccups_dashboard import router as hiccups_dashboard_router
from app.router.redirect import redirect_router
from app.router.status import router as status_router
from app.router.v1 import api_v1_public_router
from app.service.beatmap_download_service import download_service
from app.service.beatmapset_update_service import init_beatmapset_update_service
from app.service.client_verification_service import init_client_verification_service
from app.service.email_queue import start_email_processor, stop_email_processor
from app.service.replay_render_poll_service import (
    start_replay_render_poller,
    stop_replay_render_poller,
)
from app.service.redis_message_system import redis_message_system
from app.service.subscribers.user_cache import user_online_subscriber
from app.tasks import (
    calculate_user_rank,
    create_banchobot,
    create_custom_ruleset_statistics,
    create_rx_statistics,
    daily_challenge_job,
    init_geoip,
    load_achievements,
    process_daily_challenge_top,
    start_cache_tasks,
    stop_cache_tasks,
)
from app.utils import bg_tasks, utcnow

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi_limiter import FastAPILimiter
import sentry_sdk


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    # === on startup ===
    # init mods, achievements and performance calculator
    init_mods()
    init_ranked_mods()
    init_ruleset_version_hash()
    load_achievements()
    await init_calculator()


    # Always initialize client-version metadata so we can display client details
    # even when strict validation is disabled.
    try:
        await init_client_verification_service()
    except Exception as e:
        system_logger("ClientVerificationService").warning(
            f"Initialization failed, continuing without version metadata: {e}"
        )

    # init rate limiter
    await FastAPILimiter.init(redis_rate_limit_client)

    # init fetcher
    fetcher = await get_fetcher()
    # init GeoIP
    await init_geoip()

    # init game server
    await create_rx_statistics()
    await create_custom_ruleset_statistics()
    await calculate_user_rank(True)
    await daily_challenge_job()
    await process_daily_challenge_top()
    await create_banchobot()

    # services
    await start_email_processor()
    start_replay_render_poller()
    await download_service.start_health_check()
    from app.router.status import start_collector as _start_status_collector
    await _start_status_collector()
    await start_cache_tasks()
    init_beatmapset_update_service(fetcher)  # Initialize beatmapset update service
    redis_message_system.start()
    start_scheduler()
    await user_online_subscriber.start_subscribe()

    # show the status of AssetProxy
    if settings.enable_asset_proxy:
        system_logger("AssetProxy").info(f"Asset Proxy enabled - Domain: {settings.custom_asset_domain}")

    yield

    # === on shutdown ===
    # stop services
    bg_tasks.stop()
    await stop_cache_tasks()
    stop_scheduler()
    await download_service.stop_health_check()
    await stop_email_processor()
    stop_replay_render_poller()

    # close database & redis
    await engine.dispose()
    await redis_client.aclose()
    await redis_binary_client.aclose()
    await redis_message_client.aclose()
    await redis_rate_limit_client.aclose()


desc = f"""osu! API emulation server, supporting the vast majority of osu! API v1, v2, and osu!lazer features.

## Endpoints

All v2 APIs start with `/api/v2/`, all v1 APIs start with `/api/v1/` (accessing v1 APIs directly via `/api` will be redirected).

All extra APIs provided by g0v0-server (g0v0-api) start with `/api/private/`.

## Authentication

v2 API uses OAuth 2.0 authentication, supporting the following grant types:

- `password`: Password authentication, only suitable for osu!lazer clients, frontend services, etc.; requires user username and password to log in.
- `authorization_code`: Authorization code authentication, suitable for third-party applications; requires user authorization code to log in.
- `client_credentials`: Client credentials authentication, suitable for server-side applications; requires client ID and client secret to log in.

Logins using `password` have full permissions. `authorization_code` has permissions for specified scopes. `client_credentials` only has `public` permissions. For permissions required by each interface, see the Authorization section of each endpoint.

v1 API uses API Key authentication; provide the API Key in the `k` query parameter.

{
    '''
## Rate Limiting

All API requests are subject to rate limiting according to the following rules:

- A maximum of 1200 requests per minute
- Burst limit is capped at 200 requests per second

In addition, replay download APIs (`/api/v1/get_replay`, `/api/v2/scores/{score_id}/download`) are rate limited to a maximum of 10 requests per minute.
'''
    if settings.enable_rate_limit
    else ""
}

## References

- v2 API documentation: [osu-web documentation](https://osu.ppy.sh/docs/index.html)
- v1 API documentation: [osu-api](https://github.com/ppy/osu-api/wiki)
"""  # noqa: E501

# Check if New Relic config file exists, initialize New Relic if present
newrelic_config_path = Path("newrelic.ini")
if newrelic_config_path.is_file():
    try:
        import newrelic.agent

        environment = settings.new_relic_environment or ("production" if not settings.debug else "development")

        newrelic.agent.initialize(newrelic_config_path, environment)
        system_logger("NewRelic").info(f"Enabled, environment: {environment}")
    except Exception as e:
        system_logger("NewRelic").error(f"Initialization failed: {e}")

if settings.sentry_dsn is not None:
    sentry_sdk.init(
        dsn=str(settings.sentry_dsn),
        send_default_pii=False,
        environment="production" if not settings.debug else "development",
    )

app = FastAPI(
    title="g0v0-server",
    version="0.1.0",
    lifespan=lifespan,
    description=desc,
)


app.include_router(api_v2_router)
app.include_router(api_v1_router)
app.include_router(api_v1_public_router)
app.include_router(chat_router)
app.include_router(status_router)
# Public Torii hiccup-report dashboard at /hiccups/* — read-only,
# no auth, rate-limited via the shared LIMITERS dependency.
app.include_router(hiccups_dashboard_router)
app.include_router(redirect_api_router)
# app.include_router(fetcher_router)
app.include_router(file_router)
app.include_router(auth_router)
app.include_router(private_router)
# lio_spectator_router MUST be registered BEFORE lio_router. lio.py has a
# catch-all `@router.api_route("/{path:path}", ...)` at line 996 that
# matches every `/_lio/*` request and returns `{"error":"not_found"}`,
# including paths intended for the Phase 1B spectator surface. Registering
# the more-specific `/_lio/spectator/*` routes FIRST makes FastAPI match
# them ahead of the catch-all. A proper fix moves the catch-all to the end
# of lio.py (after every concrete endpoint including
# `/_lio/ruleset-hashes` at line 1005 which is currently also shadowed)
# but that's a separate cleanup; this ordering is sufficient for Phase 1.
app.include_router(lio_spectator_router)
app.include_router(lio_router)
app.include_router(beatmap_submission_router)

# Session verification middleware
if settings.enable_session_verification:
    app.add_middleware(VerifySessionMiddleware)

# Capture X-Torii-HWID on write requests for multi-account correlation.
# Fire-and-forget — never blocks the response.
app.add_middleware(HwidTrackingMiddleware)

# CORS configuration
origins = []
for url in [*settings.cors_urls, settings.server_url]:
    origins.append(str(url))
    origins.append(str(url).removesuffix("/"))
if settings.frontend_url:
    origins.append(str(settings.frontend_url))
    origins.append(str(settings.frontend_url).removesuffix("/"))
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if settings.frontend_url is not None:
    app.include_router(redirect_router)


@app.get("/users/{user_id}/avatar", include_in_schema=False)
async def get_user_avatar_root(
    user_id: int,
    session: Database,
):
    """User avatar redirect endpoint (root path)."""
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    avatar_url = user.avatar_url

    # torii: espejo del tratamiento de UserModel.transform — sin avatar subido va el default
    # torii per-user (o el logo si el user opto por eso), y los avatares nsfw se enmascaran
    # (este endpoint no tiene viewer, asi que nsfw nunca se sirve por aca). antes redirigia
    # al default.jpg generico de g0v0 y el cliente mostraba cualquier cosa para ids sin avatar.
    if not avatar_url or avatar_url == User.DEFAULT_AVATAR_URL or user.avatar_nsfw:
        use_logo = False
        if not user.avatar_nsfw:
            await user.awaitable_attrs.user_preference
            pref = user.user_preference
            use_logo = bool(pref and (pref.extra or {}).get("default_avatar_use_logo"))
        avatar_url = User.torii_logo_avatar_url() if use_logo else User.torii_default_avatar_url(user.id)

    separator = "&" if "?" in avatar_url else "?"
    avatar_url_with_timestamp = f"{avatar_url}{separator}"

    return RedirectResponse(url=avatar_url_with_timestamp, status_code=301)


@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint."""
    return {"message": "osu! API emulation server is running"}


@app.get("/health", include_in_schema=False)
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": utcnow().isoformat()}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Log the validation detail so we can debug why a request is being
    # rejected without having to either ask the user to share devtools
    # or add a one-off diagnostic deploy each time. Path + brief error
    # summary is enough -- the full error list is also returned in the
    # response body so the client can show it to the user.
    try:
        errors = exc.errors()
        summary = "; ".join(
            f"{'.'.join(str(p) for p in (e.get('loc') or []))}: {e.get('msg')}"
            for e in errors[:5]
        )
        system_logger("Validation").warning(
            f"422 on {request.method} {request.url.path} -- {summary}"
        )
    except Exception:
        # Logging must never break the error response itself.
        pass
    # ctx trae el ValueError original, que json.dumps no serializa: el handler
    # reventaba y devolvia 500 en vez del 422.
    return JSONResponse(
        status_code=422,
        content={
            "error": json.dumps(jsonable_encoder(exc.errors())),
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):  # noqa: ARG001
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


if settings.secret_key == "your_jwt_secret_here":  # noqa: S105
    raise RuntimeError(
        "jwt_secret_key is unset. Your server is unsafe. Use this command to generate: openssl rand -hex 32"
    )
if settings.osu_web_client_secret == "your_osu_web_client_secret_here":  # noqa: S105
    system_logger("Security").opt(colors=True).warning(
        "<y>osu_web_client_secret</y> is unset. Your server is unsafe. "
        "Use this command to generate: <blue>openssl rand -hex 40</blue>."
    )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_config=None,  # Disable uvicorn default log config
        access_log=True,  # Enable access log
    )
