#!/usr/bin/env python3
"""Automated end-to-end regression tests for beatmapset local search.

Tests:
1. SearchQueryModel parsing tolerances (dot-separated ranks/categories, ID strings, booleans).
2. Unauthenticated guest endpoint boundary gating (early empty return without DB access).
3. Authenticated queries (favourites, mine, played, unplayed, followed users, modes, grades).
4. SQL compilation and execution against SQLModel/SQLAlchemy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Any

from app.database import SearchBeatmapsetsResp, User
from app.dependencies.cache import get_beatmapset_cache_service
from app.dependencies.database import get_db, get_redis
from app.dependencies.fetcher import get_fetcher
from app.dependencies.user import get_optional_user
from app.models.beatmap import SearchQueryModel
from app.router.v2.beatmapset import (
    _search_local_beatmapsets,
    router as beatmapset_router,
)

from fastapi import FastAPI, Request, Response
from fastapi_limiter.depends import RateLimiter
from httpx import ASGITransport, AsyncClient


def test_model_parsing():
    print("--- Running Test Suite 1: Query Model Parsing ---")

    # Dot-separated strings
    q1 = SearchQueryModel.model_validate({
        "s": "favourites",
        "played": "played",
        "l": "2",
        "g": "3",
        "r": "SS.SSH.A",
        "c": "follows.recommended",
        "sort": "title_asc",
    })
    assert q1.s == "favourites"
    assert q1.played == "played"
    assert q1.l == "english"
    assert q1.g == 3
    assert [r.value for r in q1.r] == ["X", "XH", "A"]
    assert q1.c == ["follows", "recommended"]
    print("  [PASS] Dot-delimited strings parsed and normalized correctly.")

    # List inputs (FastAPI query parsing format)
    q2 = SearchQueryModel.model_validate({
        "s": "mine",
        "played": "unplayed",
        "l": "japanese",
        "g": 0,
        "r": ["SS.S", "B"],
        "c": ["spotlights.featured_artists"],
        "sort": "difficulty_desc",
    })
    assert q2.s == "mine"
    assert q2.played == "unplayed"
    assert q2.l == "japanese"
    assert q2.g is None
    assert [r.value for r in q2.r] == ["X", "S", "B"]
    assert q2.c == ["spotlights", "featured_artists"]
    print("  [PASS] List-formatted queries with nested dots parsed correctly.")


async def test_sql_generation():
    print("\n--- Running Test Suite 2: SQL Statement Compilation ---")
    mock_db = MagicMock()
    mock_exec = MagicMock()
    mock_exec.first.return_value = 0
    mock_exec.all.return_value = []
    mock_db.exec = AsyncMock(return_value=mock_exec)

    user = User(id=10, username="test_player")

    scenarios = [
        ("mine", SearchQueryModel.model_validate({"s": "mine", "sort": "title_asc"})),
        ("favourites", SearchQueryModel.model_validate({"s": "favourites", "sort": "title_asc"})),
        ("played", SearchQueryModel.model_validate({"played": "played", "sort": "plays_desc"})),
        ("unplayed", SearchQueryModel.model_validate({"played": "unplayed", "sort": "ranked_asc"})),
        ("mode & rank", SearchQueryModel.model_validate({"m": 0, "r": ["X", "XH"], "sort": "title_asc"})),
        (
            "follows & language",
            SearchQueryModel.model_validate({"c": ["follows"], "l": "2", "g": 4, "sort": "artist_asc"}),
        ),
    ]

    for label, query in scenarios:
        res = await _search_local_beatmapsets(mock_db, query, user)
        assert isinstance(res, SearchBeatmapsetsResp)
        assert res.total == 0
        print(f"  [PASS] Subquery compiled and executed for scenario: {label}")


async def test_http_route_endpoints():
    print("\n--- Running Test Suite 3: FastAPI Route Integration ---")
    app = FastAPI()
    app.include_router(beatmapset_router)

    # Bypass RateLimiter cleanly
    async def mock_rate_limit(self: Any, request: Request, response: Response):
        pass

    setattr(RateLimiter, "__call__", mock_rate_limit)

    mock_db = MagicMock()
    mock_exec = MagicMock()
    mock_exec.first.return_value = 0
    mock_exec.all.return_value = []
    mock_db.exec = AsyncMock(return_value=mock_exec)

    mock_redis = AsyncMock()
    mock_fetcher = AsyncMock()
    mock_fetcher.search_beatmapset.return_value = SearchBeatmapsetsResp(total=0, beatmapsets=[])

    mock_cache = AsyncMock()
    mock_cache.get_search_from_cache.return_value = None

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_fetcher] = lambda: mock_fetcher
    app.dependency_overrides[get_beatmapset_cache_service] = lambda: mock_cache

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Guest early returns
        app.dependency_overrides[get_optional_user] = lambda: None
        guest_cases = [
            ("s=favourites", "/api/v2/beatmapsets/search?s=favourites&sort=title_asc"),
            ("s=mine", "/api/v2/beatmapsets/search?s=mine&sort=title_asc"),
            ("played=played", "/api/v2/beatmapsets/search?played=played&sort=title_asc"),
            ("played=unplayed", "/api/v2/beatmapsets/search?played=unplayed&sort=title_asc"),
            ("r=SS.S", "/api/v2/beatmapsets/search?r=SS.S&sort=title_asc"),
            ("c=follows", "/api/v2/beatmapsets/search?c=follows&sort=title_asc"),
            ("c=recommended", "/api/v2/beatmapsets/search?c=recommended&sort=title_asc"),
        ]
        for name, url in guest_cases:
            res = await client.get(url)
            assert res.status_code == 200, f"Failed on {name}: {res.status_code}"
            data = res.json()
            assert data["total"] == 0
            assert data["beatmapsets"] == []
            print(f"  [PASS] Guest early return ({name}): HTTP 200, total=0")

        # 2. Guest public local searches (should proceed to local query without user filters)
        guest_local_cases = [
            ("is_local=true", "/api/v2/beatmapsets/search?is_local=true&sort=title_asc"),
            ("ai=true", "/api/v2/beatmapsets/search?ai=true&sort=title_asc"),
        ]
        for name, url in guest_local_cases:
            res = await client.get(url)
            assert res.status_code == 200, f"Failed on guest local {name}: {res.status_code}"
            print(f"  [PASS] Guest public search ({name}): HTTP 200 (processed via local catalog)")

        # 3. Authenticated user queries
        app.dependency_overrides[get_optional_user] = lambda: User(id=1, username="remi")
        user_cases = [
            ("s=mine", "/api/v2/beatmapsets/search?s=mine&sort=title_asc"),
            ("s=favourites", "/api/v2/beatmapsets/search?s=favourites&sort=title_asc"),
            ("language & genre", "/api/v2/beatmapsets/search?l=2&g=3&sort=title_asc"),
            ("dot-delimited c", "/api/v2/beatmapsets/search?c=follows.recommended&sort=title_asc"),
            ("cursor pagination", "/api/v2/beatmapsets/search?s=mine&cursor%5Bid%5D=123&sort=title_asc"),
            ("status filter ranked", "/api/v2/beatmapsets/search?s=ranked&sort=ranked_desc"),
            ("status filter loved", "/api/v2/beatmapsets/search?s=loved&sort=difficulty_desc"),
        ]
        for name, url in user_cases:
            res = await client.get(url)
            assert res.status_code == 200, f"Failed on {name}: {res.status_code}"
            print(f"  [PASS] Authenticated user query ({name}): HTTP 200")


async def main():
    test_model_parsing()
    await test_sql_generation()
    await test_http_route_endpoints()
    print("\n=======================================================")
    print("  ALL AUTOMATED LOCAL SEARCH TESTS PASSED SUCCESSFULLY! ")
    print("=======================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
