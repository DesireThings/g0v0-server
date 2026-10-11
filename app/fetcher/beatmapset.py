import asyncio
import base64
import hashlib
import json

from app.database import BeatmapsetDict, BeatmapsetModel, SearchBeatmapsetsResp
from app.log import fetcher_logger
from app.models.beatmap import SearchQueryModel
from app.models.model import Cursor
from app.utils import bg_tasks

from ._base import BaseFetcher
from .beatconnect import beatconnect_base_url, beatconnect_enabled, beatconnect_headers, beatconnect_beatmapset_to_dict

from httpx import AsyncClient
from pydantic import TypeAdapter
import redis.asyncio as redis

logger = fetcher_logger("BeatmapsetFetcher")


adapter = TypeAdapter(
    BeatmapsetModel.generate_typeddict(
        (
            "availability",
            "bpm",
            "last_updated",
            "ranked",
            "ranked_date",
            "submitted_date",
            "tags",
            "storyboard",
            "description",
            "genre",
            "language",
            *[
                f"beatmaps.{inc}"
                for inc in (
                    "checksum",
                    "accuracy",
                    "ar",
                    "bpm",
                    "convert",
                    "count_circles",
                    "count_sliders",
                    "count_spinners",
                    "cs",
                    "deleted_at",
                    "drain",
                    "hit_length",
                    "is_scoreable",
                    "last_updated",
                    "mode_int",
                    "ranked",
                    "url",
                    "max_combo",
                )
            ],
        )
    )
)


class BeatmapsetFetcher(BaseFetcher):
    async def _get_beatmapset_from_beatconnect(self, beatmap_set_id: int) -> BeatmapsetDict | None:
        if not beatconnect_enabled():
            return None

        logger.opt(colors=True).debug(f"get_beatmapset (BeatConnect): <y>{beatmap_set_id}</y>")

        async with AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(
                f"{beatconnect_base_url()}/api/beatmap/{beatmap_set_id}/",
                headers=beatconnect_headers(),
            )

        if resp.status_code == 404:
            return None

        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            return None

        return beatconnect_beatmapset_to_dict(payload)

    @staticmethod
    def _ensure_local_flags(beatmapset: dict) -> dict:
        """Normalize remote payloads that do not contain local-only fields."""
        beatmapset.setdefault("is_local", False)
        # osu! no sabe nada de esto, pero el tag lo pone el autor del mapa: un mapa de
        # bancho generado con la tool se marca igual que uno de aca.
        beatmapset.setdefault("ai", "mapperatorinator" in (beatmapset.get("tags") or "").lower())
        for beatmap in beatmapset.get("beatmaps") or []:
            if isinstance(beatmap, dict):
                beatmap.setdefault("is_local", False)

        # Older / graveyarded beatmapsets from osu.ppy.sh sometimes return
        # genre/language as null, which the BeatmapsetModelDict adapter (with
        # genre/language in its includes tuple) rejects with a model_type
        # validation error. Substitute the "Unspecified" enum default so the
        # payload validates and the sync continues — every other field on the
        # set is still authoritative.
        if not isinstance(beatmapset.get("genre"), dict):
            beatmapset["genre"] = {"id": 1, "name": "Unspecified"}
        if not isinstance(beatmapset.get("language"), dict):
            beatmapset["language"] = {"id": 1, "name": "Unspecified"}
        return beatmapset

    @staticmethod
    def _get_homepage_queries() -> list[tuple[SearchQueryModel, Cursor]]:
        """Get list of homepage pre-cache queries."""
        # Common homepage query combinations
        homepage_queries = []

        # Primary sort options
        sorts = ["ranked_desc", "updated_desc", "favourites_desc", "plays_desc"]

        for sort in sorts:
            # Page 1 - use minimal parameter set to match user requests
            query = SearchQueryModel(
                q="",
                s="leaderboard",
                sort=sort,  # type: ignore
            )
            homepage_queries.append((query, {}))

        return homepage_queries

    @staticmethod
    def _generate_cache_key(query: SearchQueryModel, cursor: Cursor) -> str:
        """Generate search cache key."""
        # Include only core query parameters, ignore default values
        cache_data = {}

        # Add non-default/non-empty query parameters
        if query.q:
            cache_data["q"] = query.q
        if query.s != "leaderboard":  # Only add if non-default
            cache_data["s"] = query.s
        if hasattr(query, "sort") and query.sort:
            cache_data["sort"] = query.sort
        if query.nsfw is not False:  # Only add if non-default
            cache_data["nsfw"] = query.nsfw
        if query.m is not None:
            cache_data["m"] = query.m
        if query.c:
            cache_data["c"] = query.c
        if query.l != "any":  # Check language default value
            cache_data["l"] = query.l
        if query.e:
            cache_data["e"] = query.e
        if query.r:
            cache_data["r"] = query.r
        if query.played is not False:
            cache_data["played"] = query.played
        query_is_local = getattr(query, "is_local", False)
        if query_is_local:
            cache_data["is_local"] = query_is_local

        # Add cursor
        if cursor:
            cache_data["cursor"] = cursor

        # Serialize to JSON and generate MD5 hash
        cache_json = json.dumps(cache_data, sort_keys=True, separators=(",", ":"))
        cache_hash = hashlib.md5(cache_json.encode(), usedforsecurity=False).hexdigest()

        logger.opt(colors=True).debug(f"<blue>[CacheKey]</blue> Query: {cache_data}, Hash: {cache_hash}")

        return f"beatmapset:search:{cache_hash}"

    @staticmethod
    def _encode_cursor(cursor_dict: dict[str, int | float]) -> str:
        """Encode cursor dictionary to base64 string."""
        cursor_json = json.dumps(cursor_dict, separators=(",", ":"))
        return base64.b64encode(cursor_json.encode()).decode()

    @staticmethod
    def _decode_cursor(cursor_string: str) -> dict[str, int | float]:
        """Decode base64 string to cursor dictionary."""
        try:
            cursor_json = base64.b64decode(cursor_string).decode()
            return json.loads(cursor_json)
        except Exception:
            return {}

    async def get_beatmapset(self, beatmap_set_id: int) -> BeatmapsetDict:
        logger.opt(colors=True).debug(f"get_beatmapset: <y>{beatmap_set_id}</y>")

        # BeatConnect remains the primary metadata source — it's our paid
        # mirror and using it everywhere keeps us off osu!'s tightened API
        # rate limits during routine browsing.
        #
        # BeatConnect doesn't return per-difficulty MD5 checksums though,
        # which would otherwise leave us writing `checksum: null` into the
        # DB. The lazer client downstream uses the response's checksum field
        # to populate `OnlineMD5Hash`; with an empty string there, the
        # `MatchesOnlineVersion` gate inside RealmPopulatingOnlineLookupSource
        # fails (local md5 != "") and Torii's `effective_rank_status`
        # promotion never reaches the realm row. The user-visible result is
        # the "graveyard pill flips to approved but leaderboard remains
        # unavailable" symptom in song select.
        #
        # Fix: after BeatConnect succeeds, consult osu.direct (free, no
        # osu!-API quota cost) for the per-difficulty checksums and splice
        # them into the BeatConnect payload before we hand it back. Falling
        # all the way through to osu! API stays as a last resort so a
        # double-mirror outage doesn't take detail pages down completely.
        beatconnect_payload = await self._get_beatmapset_from_beatconnect(beatmap_set_id)
        if beatconnect_payload is not None:
            await self._enrich_set_with_osu_direct_checksums(beatmap_set_id, beatconnect_payload)
            # el espejo tampoco sabe nada de nuestros campos: sin esto la validacion
            # rebota y la pagina del set devuelve 500.
            beatconnect_payload = self._ensure_local_flags(beatconnect_payload)
            return adapter.validate_python(beatconnect_payload)  # pyright: ignore[reportReturnType]

        try:
            payload = await self.request_api(f"https://osu.ppy.sh/api/v2/beatmapsets/{beatmap_set_id}")
            payload = self._ensure_local_flags(payload)
            return adapter.validate_python(payload)  # pyright: ignore[reportReturnType]
        except Exception as e:
            logger.warning(
                "Both BeatConnect and osu! API failed for beatmapset {}: {}",
                beatmap_set_id,
                e,
            )
            raise

    async def _enrich_set_with_osu_direct_checksums(
        self,
        beatmap_set_id: int,
        beatmapset_payload: dict,
    ) -> None:
        """Splice per-difficulty `file_md5`s from osu.direct into a BeatConnect set.

        Mutates ``beatmapset_payload`` in place. Best-effort: failure is
        logged at debug level and the payload is returned as-is so callers
        downstream still get the BeatConnect response. Healing pathways
        (`Beatmap.from_resp_batch`, `BeatmapsetCacheService.get_beatmapset_from_cache`)
        will pick up the slack on the next request if needed.
        """
        beatmaps = beatmapset_payload.get("beatmaps") or []
        if not beatmaps:
            return
        # Skip the supplemental call entirely if every difficulty already has
        # a checksum (e.g. served from a previously-healed cache layer).
        if all(bm.get("checksum") for bm in beatmaps if isinstance(bm, dict)):
            return

        try:
            async with AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(
                    "https://osu.direct/api/get_beatmaps",
                    params={"s": beatmap_set_id, "limit": 100},
                    headers={
                        "User-Agent": "ToriiBeatmapsetFetcher/1.0 (+https://lazer.shikkesora.com)",
                        "Accept": "application/json,*/*;q=0.8",
                    },
                )
        except Exception as e:
            logger.debug("osu.direct set checksum supplement failed for {}: {}", beatmap_set_id, e)
            return

        if resp.status_code >= 400:
            logger.debug(
                "osu.direct set checksum supplement got HTTP {} for {}",
                resp.status_code,
                beatmap_set_id,
            )
            return

        try:
            rows = resp.json()
        except Exception as e:
            logger.debug("osu.direct set checksum supplement non-JSON for {}: {}", beatmap_set_id, e)
            return

        if not isinstance(rows, list):
            return

        md5_by_id: dict[int, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                row_id = int(row.get("beatmap_id") or 0)
            except (TypeError, ValueError):
                continue
            md5 = str(row.get("file_md5") or "")
            if row_id and md5:
                md5_by_id[row_id] = md5

        if not md5_by_id:
            return

        filled = 0
        for bm in beatmaps:
            if not isinstance(bm, dict) or bm.get("checksum"):
                continue
            try:
                bid = int(bm.get("id") or 0)
            except (TypeError, ValueError):
                continue
            md5 = md5_by_id.get(bid)
            if md5:
                bm["checksum"] = md5
                filled += 1

        if filled:
            logger.debug(
                "Filled {} checksums for set {} via osu.direct supplement",
                filled,
                beatmap_set_id,
            )

    async def search_beatmapset(
        self, query: SearchQueryModel, cursor: Cursor, redis_client: redis.Redis
    ) -> SearchBeatmapsetsResp:
        logger.opt(colors=True).debug(f"search_beatmapset: <y>{query}</y>")

        # Generate cache key
        cache_key = self._generate_cache_key(query, cursor)

        # Try to get result from cache
        cached_result = await redis_client.get(cache_key)
        if cached_result:
            logger.opt(colors=True).debug(f"Cache hit for key: <y>{cache_key}</y>")
            try:
                cached_data = json.loads(cached_result)
                for beatmapset in cached_data.get("beatmapsets") or []:
                    if isinstance(beatmapset, dict):
                        self._ensure_local_flags(beatmapset)

                return SearchBeatmapsetsResp.model_validate(cached_data)
            except Exception as e:
                logger.warning(f"Cache data invalid, fetching from API: {e}")

        # Cache miss, fetch data from API
        logger.debug("Cache miss, fetching from API")

        params = query.model_dump(exclude_none=True, exclude_unset=True, exclude_defaults=True)

        # filtros nuestros: osu! no los conoce y no tienen por que llegarle.
        for own in ("is_local", "ai", "hide_local", "hide_ai"):
            params.pop(own, None)

        if query.cursor_string:
            params["cursor_string"] = query.cursor_string
        else:
            for k, v in cursor.items():
                params[f"cursor[{k}]"] = v

        api_response = await self.request_api(
            "https://osu.ppy.sh/api/v2/beatmapsets/search",
            params=params,
        )
        for beatmapset in api_response.get("beatmapsets") or []:
            if isinstance(beatmapset, dict):
                self._ensure_local_flags(beatmapset)

        # Process cursor info in response
        if api_response.get("cursor"):
            cursor_dict = api_response["cursor"]
            api_response["cursor_string"] = self._encode_cursor(cursor_dict)

        # Cache result for 15 minutes
        cache_ttl = 15 * 60  # 15 minutes
        await redis_client.set(cache_key, json.dumps(api_response, separators=(",", ":")), ex=cache_ttl)

        logger.opt(colors=True).debug(f"Cached result for key: <y>{cache_key}</y> (TTL: {cache_ttl}s)")

        for beatmapset in api_response.get("beatmapsets") or []:
            if isinstance(beatmapset, dict):
                self._ensure_local_flags(beatmapset)

        resp = SearchBeatmapsetsResp.model_validate(api_response)

        # Smart prefetching: only prefetch when user explicitly searches to avoid excessive API requests,
        # and only when search keywords or specific criteria are present to avoid over-fetching on homepage browsing.
        if api_response.get("cursor") and (query.q or query.s != "leaderboard" or cursor):
            # Prefetch next 1 page in background (reduce prefetch volume)
            import asyncio

            # Do not create task immediately; delay a period before prefetching
            async def delayed_prefetch():
                await asyncio.sleep(3.0)  # Delay 3 seconds
                await self.prefetch_next_pages(query, api_response["cursor"], redis_client, pages=1)

            bg_tasks.add_task(delayed_prefetch)

        return resp

    async def prefetch_next_pages(
        self,
        query: SearchQueryModel,
        current_cursor: Cursor,
        redis_client: redis.Redis,
        pages: int = 3,
    ) -> None:
        """Prefetch next few pages of content."""
        if not current_cursor:
            return

        cursor = current_cursor.copy()

        for page in range(1, pages + 1):
            # Request next page using current cursor
            next_query = query.model_copy()

            logger.debug(f"Prefetching page {page + 1}")

            # Generate cache key for next page
            next_cache_key = self._generate_cache_key(next_query, cursor)

            # Check if already cached
            if await redis_client.exists(next_cache_key):
                logger.debug(f"Page {page + 1} already cached")
                # Try to get cursor from cache to continue prefetching
                cached_data = await redis_client.get(next_cache_key)
                if cached_data:
                    try:
                        data = json.loads(cached_data)
                        if data.get("cursor"):
                            cursor = data["cursor"]
                            continue
                    except Exception:
                        logger.warning("Failed to parse cached data for cursor")
                break

            # Add delay between prefetch pages to prevent burst requests
            if page > 1:
                await asyncio.sleep(1.5)  # 1.5 seconds delay

            # Request next page data
            params = next_query.model_dump(exclude_none=True, exclude_unset=True, exclude_defaults=True)

            # filtros nuestros: osu! no los conoce y no tienen por que llegarle.
            for own in ("is_local", "ai", "hide_local", "hide_ai"):
                params.pop(own, None)

            for k, v in cursor.items():
                params[f"cursor[{k}]"] = v

            api_response = await self.request_api(
                "https://osu.ppy.sh/api/v2/beatmapsets/search",
                params=params,
            )
            for beatmapset in api_response.get("beatmapsets") or []:
                if isinstance(beatmapset, dict):
                    self._ensure_local_flags(beatmapset)

            # Process cursor info in response
            if api_response.get("cursor"):
                cursor_dict = api_response["cursor"]
                api_response["cursor_string"] = self._encode_cursor(cursor_dict)
                cursor = cursor_dict  # Update cursor for next page
            else:
                # No more pages
                break

            # Cache result (shorter TTL for prefetched data)
            prefetch_ttl = 10 * 60  # 10 minutes
            await redis_client.set(
                next_cache_key,
                json.dumps(api_response, separators=(",", ":")),
                ex=prefetch_ttl,
            )

            logger.debug(f"Prefetched page {page + 1} (TTL: {prefetch_ttl}s)")

    async def warmup_homepage_cache(self, redis_client: redis.Redis) -> None:
        """Warm up homepage cache."""
        homepage_queries = self._get_homepage_queries()

        logger.info(f"Starting homepage cache warmup ({len(homepage_queries)} queries)")

        for i, (query, cursor) in enumerate(homepage_queries):
            try:
                # Add delay between requests to prevent burst requests
                if i > 0:
                    await asyncio.sleep(5.0)  # 5s delay — gentler on osu! API rate limit

                cache_key = self._generate_cache_key(query, cursor)

                # Check if already cached
                if await redis_client.exists(cache_key):
                    logger.debug(f"Query {query.sort} already cached")
                    continue

                # Request and cache
                params = query.model_dump(exclude_none=True, exclude_unset=True, exclude_defaults=True)

                # filtros nuestros: osu! no los conoce y no tienen por que llegarle.
                for own in ("is_local", "ai", "hide_local", "hide_ai"):
                    params.pop(own, None)

                api_response = await self.request_api(
                    "https://osu.ppy.sh/api/v2/beatmapsets/search",
                    params=params,
                )

                if api_response.get("cursor"):
                    cursor_dict = api_response["cursor"]
                    api_response["cursor_string"] = self._encode_cursor(cursor_dict)

                # Cache result — long TTL so we don't re-fetch too aggressively
                cache_ttl = 90 * 60  # 90 minutes
                await redis_client.set(
                    cache_key,
                    json.dumps(api_response, separators=(",", ":")),
                    ex=cache_ttl,
                )

                logger.info(f"Warmed up cache for {query.sort} (TTL: {cache_ttl}s)")

                # Skip prefetching extra pages to conserve API rate limit
                # if api_response.get("cursor"):
                #     await self.prefetch_next_pages(query, api_response["cursor"], redis_client, pages=2)

            except Exception as e:
                logger.error(f"Failed to warmup cache for {query.sort}: {e}")
