"""Admin write API for the changelog editor.

Mounted at ``/api/private/changelog`` (see app/router/private/router.py).
The public read endpoints live in ``app/router/v2/changelog.py`` and now
prefer DB rows over the hardcoded historical builds when any DB rows
exist. So this router is purely admin-side: streams, builds, entries,
plus a GitHub-commits helper that lets the editor build a new entry
from a recent commit message in one click.

All admin endpoints gate on ``is_admin``. The GitHub helper is also
admin-only because we don't want unauth'd callers fan-outing requests
through our server to GitHub's rate-limited API.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Security, status
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlmodel import col, select

from app.database import (
    ChangelogBuild,
    ChangelogBuildCreate,
    ChangelogBuildResponse,
    ChangelogEntry,
    ChangelogEntryCreate,
    ChangelogEntryResponse,
    ChangelogStream,
    ChangelogStreamCreate,
    ChangelogStreamResponse,
)
from app.database.user import User
from app.dependencies.database import Database
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.log import log
from app.utils import utcnow

# Use a dedicated APIRouter and let app/router/private/router.py mount it
# under the /changelog prefix. Same pattern as audio_proxy_router.
router = APIRouter(tags=["Admin", "g0v0 API", "Changelog"])

logger = log("AdminChangelog")


# ─── Auth helper ─────────────────────────────────────────────────────────


async def _require_admin(session: Database, user_and_token: UserAndToken) -> User:
    """Light wrapper so the router doesn't import admin.py just for one
    helper. Mirrors the require_admin used elsewhere: 403 when not admin."""
    user = user_and_token[0]
    # Re-fetch from session in case the token-loaded user is stale.
    db_user = await session.get(User, user.id)
    if db_user is None or not getattr(db_user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return db_user


# ─── Streams ─────────────────────────────────────────────────────────────


@router.get("/streams", name="List changelog streams")
async def list_streams(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await _require_admin(session, user_and_token)
    streams = (await session.exec(select(ChangelogStream).order_by(col(ChangelogStream.id)))).all()
    return [ChangelogStreamResponse.model_validate(s, from_attributes=True) for s in streams]


@router.post("/streams", name="Create changelog stream")
async def create_stream(
    session: Database,
    payload: ChangelogStreamCreate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await _require_admin(session, user_and_token)
    # Reject duplicate stream names early — the column has a UNIQUE
    # index but raising a clean 409 reads better than letting MySQL's
    # IntegrityError surface as a 500.
    existing = (await session.exec(select(ChangelogStream).where(ChangelogStream.name == payload.name))).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Stream {payload.name!r} already exists")

    stream = ChangelogStream.model_validate(payload, from_attributes=True)
    session.add(stream)
    await session.commit()
    await session.refresh(stream)
    return ChangelogStreamResponse.model_validate(stream, from_attributes=True)


# ─── Builds ──────────────────────────────────────────────────────────────


@router.get("/admin/builds", name="List builds for the editor table")
async def list_builds_for_admin(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Lightweight build list with entry counts joined in. Powers the
    "Builds" sidebar in the editor — admin clicks a build and we fetch
    its entries via /admin/entries/{build_id}."""
    await _require_admin(session, user_and_token)

    # Subquery for per-build entry counts. LEFT JOIN so builds with zero
    # entries still show up (newly-created drafts).
    entry_count_sq = (
        select(
            ChangelogEntry.build_id,
            sa_func.count(ChangelogEntry.id).label("entry_count"),
        )
        .group_by(ChangelogEntry.build_id)
        .subquery()
    )

    rows = (
        await session.exec(
            select(
                ChangelogBuild,
                ChangelogStream.name,
                entry_count_sq.c.entry_count,
            )
            .join(ChangelogStream, ChangelogBuild.stream_id == ChangelogStream.id)
            .join(entry_count_sq, entry_count_sq.c.build_id == ChangelogBuild.id, isouter=True)
            .order_by(col(ChangelogBuild.created_at).desc())
        )
    ).all()

    out: list[dict[str, Any]] = []
    for build, stream_name, entry_count in rows:
        out.append(
            {
                "id": build.id,
                "version": build.version,
                "display_version": build.display_version,
                "stream_name": stream_name,
                "stream_id": build.stream_id,
                "users": build.users,
                "created_at": build.created_at.isoformat() if build.created_at else None,
                "github_url": build.github_url,
                "entry_count": int(entry_count or 0),
            }
        )
    return out


@router.post("/builds", name="Create build")
async def create_build(
    session: Database,
    payload: ChangelogBuildCreate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await _require_admin(session, user_and_token)
    stream = await session.get(ChangelogStream, payload.stream_id)
    if stream is None:
        raise HTTPException(status_code=404, detail=f"Stream id {payload.stream_id} not found")

    data = payload.model_dump()
    if not data.get("created_at"):
        data["created_at"] = utcnow()
    build = ChangelogBuild(**data)
    session.add(build)
    await session.commit()
    await session.refresh(build)
    return ChangelogBuildResponse.model_validate(build, from_attributes=True)


@router.delete("/builds/{build_id}", name="Delete build (cascades entries)")
async def delete_build(
    session: Database,
    build_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await _require_admin(session, user_and_token)
    build = await session.get(ChangelogBuild, build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="Build not found")

    # Manual cascade delete — keeps the migration simple (no ON DELETE
    # CASCADE FK) while still cleaning up children. One transaction.
    entries = (await session.exec(select(ChangelogEntry).where(ChangelogEntry.build_id == build_id))).all()
    for entry in entries:
        await session.delete(entry)
    await session.delete(build)
    await session.commit()
    return {"message": "Build deleted", "deleted_entry_count": len(entries)}


# ─── Entries ─────────────────────────────────────────────────────────────


@router.get("/admin/entries/{build_id}", name="List entries for a build")
async def list_entries_for_build(
    session: Database,
    build_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await _require_admin(session, user_and_token)
    entries = (
        await session.exec(
            select(ChangelogEntry)
            .where(ChangelogEntry.build_id == build_id)
            .order_by(col(ChangelogEntry.id))
        )
    ).all()

    out: list[dict[str, Any]] = []
    for e in entries:
        out.append(
            {
                "id": e.id,
                "type": e.type,
                "category": e.category,
                "title": e.title,
                "major": e.major,
                "url": e.url,
                "github_pull_request_id": e.github_pull_request_id,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
        )
    return out


@router.post("/entries", name="Create entry")
async def create_entry(
    session: Database,
    payload: ChangelogEntryCreate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    actor = await _require_admin(session, user_and_token)

    build = await session.get(ChangelogBuild, payload.build_id)
    if build is None:
        raise HTTPException(status_code=404, detail=f"Build id {payload.build_id} not found")

    data = payload.model_dump()

    # Auto-wrap message_html when the admin form left it blank — keeps
    # the read endpoint's contract (always rendered HTML) intact without
    # forcing the editor to roundtrip through markdown.
    if not data.get("message_html"):
        data["message_html"] = f"<p>{escape(data.get('title', ''))}</p>"

    # Auto-fill github_user from the actor when the editor didn't provide
    # one. Lets the rendered changelog credit the admin who added the
    # entry without any extra UI.
    if not data.get("github_user"):
        data["github_user"] = {
            "id": actor.id,
            "display_name": actor.username,
            "github_url": "https://github.com/shikkesora",
            "osu_username": actor.username,
            "user_id": actor.id,
            "user_url": f"https://lazer.shikkesora.com/users/{actor.id}",
        }

    entry = ChangelogEntry(**data)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return ChangelogEntryResponse.model_validate(entry, from_attributes=True)


@router.delete("/entries/{entry_id}", name="Delete entry")
async def delete_entry(
    session: Database,
    entry_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await _require_admin(session, user_and_token)
    entry = await session.get(ChangelogEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    await session.delete(entry)
    await session.commit()
    return {"message": "Entry deleted"}


# ─── GitHub commits helper ───────────────────────────────────────────────
#
# Lets the editor populate "create entry" forms from recent commits so the
# admin doesn't have to retype messages by hand. We fan out to GitHub
# server-side so the browser can't be tricked into spamming GitHub's
# rate-limited API from many tabs (and our auth token, when set, never
# leaves the server).


def _extract_repo_from_url(repo_or_url: str) -> str:
    """Accept both ``owner/repo`` and full GitHub URLs. Returns the
    canonical ``owner/repo`` form. Raises HTTPException(400) on garbage."""
    raw = (repo_or_url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="repo is required")
    # Strip protocol + host + trailing /. Accepts:
    #   shikkesora/torii-osu
    #   https://github.com/shikkesora/torii-osu
    #   github.com/shikkesora/torii-osu
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip("/")
    parts = raw.split("/")
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="repo must be in 'owner/repo' form")
    return f"{parts[0]}/{parts[1]}"


@router.get("/github/test", name="Smoke-test the GitHub helper")
async def github_test(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    repo: str = "shikkesora/torii-osu",
):
    await _require_admin(session, user_and_token)
    return {"message": "GitHub helper online", "repo": _extract_repo_from_url(repo)}


@router.get("/github/commits", name="Recent commits for a repo")
async def github_commits(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    repo: str = Query("shikkesora/torii-osu", description="owner/repo or full GitHub URL"),
    per_page: int = Query(20, ge=1, le=100),
):
    """Fetch up to ``per_page`` recent commits from a public GitHub repo
    and return them in a flat shape the editor can iterate. We call
    GitHub server-side so the response is normalised and our token (if
    we ever wire one up) never reaches the browser."""
    await _require_admin(session, user_and_token)

    repo_canonical = _extract_repo_from_url(repo)
    url = f"https://api.github.com/repos/{repo_canonical}/commits"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Torii-Server",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"per_page": per_page}, headers=headers)
    except Exception as exc:
        logger.warning("GitHub commits fetch crashed for {}: {}", repo_canonical, exc)
        return {"error": f"network error: {exc}", "repo": repo_canonical}

    if resp.status_code == 403:
        return {"error": "GitHub rate-limited or forbidden", "repo": repo_canonical}
    if resp.status_code == 404:
        return {"error": "Repository not found", "repo": repo_canonical}
    if resp.status_code != 200:
        return {"error": f"GitHub returned {resp.status_code}", "repo": repo_canonical}

    out: list[dict[str, Any]] = []
    for raw in resp.json():
        commit = raw.get("commit", {}) or {}
        message = (commit.get("message") or "").splitlines()[0]
        author = (commit.get("author") or {}).get("name") or (raw.get("author") or {}).get("login") or "unknown"
        date = (commit.get("author") or {}).get("date") or ""
        sha = raw.get("sha", "")
        out.append({
            "sha": sha[:7],
            "full_sha": sha,
            "message": message,
            "author": author,
            "date": date,
            "html_url": raw.get("html_url"),
        })
    return out


class CreateEntryFromCommitRequest(BaseModel):
    build_id: int
    commit_sha: str
    commit_message: str
    repo: str = "shikkesora/torii-osu"


@router.post("/entries/from-commit", name="Create entry from a GitHub commit")
async def create_entry_from_commit(
    session: Database,
    payload: CreateEntryFromCommitRequest,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Convenience: take a commit reference, build a sensible entry,
    insert it. Category is inferred from the repo name so the editor
    doesn't have to ask. Type defaults to ``misc`` because the commit
    message verb is too varied to map reliably."""
    actor = await _require_admin(session, user_and_token)

    build = await session.get(ChangelogBuild, payload.build_id)
    if build is None:
        raise HTTPException(status_code=404, detail=f"Build id {payload.build_id} not found")

    repo_canonical = _extract_repo_from_url(payload.repo)
    repo_lower = repo_canonical.lower()
    if "torii-lazer-web" in repo_lower or "vipsu-frontend" in repo_lower:
        category = "web"
    elif "g0v0-server" in repo_lower:
        category = "server"
    else:
        category = "client"

    title = (payload.commit_message or "").strip() or "(no message)"
    entry = ChangelogEntry(
        build_id=payload.build_id,
        type="misc",
        category=category,
        title=title,
        message_html=f"<p>{escape(title)}</p>",
        major=False,
        url=f"https://github.com/{repo_canonical}/commit/{payload.commit_sha}",
        github_user={
            "id": actor.id,
            "display_name": actor.username,
            "github_url": "https://github.com/shikkesora",
            "osu_username": actor.username,
            "user_id": actor.id,
            "user_url": f"https://lazer.shikkesora.com/users/{actor.id}",
        },
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return ChangelogEntryResponse.model_validate(entry, from_attributes=True)
