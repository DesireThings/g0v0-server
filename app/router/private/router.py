from app.dependencies.rate_limit import LIMITERS

from fastapi import APIRouter

router = APIRouter(prefix="/api/private", dependencies=LIMITERS)

# Import and include sub-routers
from .audio_proxy import router as audio_proxy_router
from . import client_version_webhook  # noqa: F401 — registers routes on import
from .changelog import router as changelog_router

router.include_router(audio_proxy_router)
# Changelog admin endpoints live under /api/private/changelog/...
# (streams, builds, entries, plus the github helper). The public read
# endpoints stay at /api/v2/changelog.
router.include_router(changelog_router, prefix="/changelog")
