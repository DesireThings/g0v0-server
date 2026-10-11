import asyncio
import hashlib
import re
import zipfile
from io import BytesIO
from typing import Any

from app.dependencies.database import get_redis
from app.log import fetcher_logger

from ._base import BaseFetcher
from .beatconnect import beatconnect_base_url, beatconnect_enabled, beatconnect_headers

from httpx import AsyncClient, HTTPError, HTTPStatusError, Limits
import redis.asyncio as redis

urls = [
    "https://osu.direct/api/osu/{beatmap_id}",        # osu.direct
    "https://b.ppy.sh/osu/{beatmap_id}",              # old bancho endpoint
    "https://osu.ppy.sh/osu/{beatmap_id}",            # official (rate limited — last resort)
    "https://old.ppy.sh/osu/{beatmap_id}",            # legacy official fallback
]
FAST_RAW_URLS = urls[:2]
OFFICIAL_RAW_URLS = urls[2:]

logger = fetcher_logger("BeatmapRawFetcher")

BEATCONNECT_NEGATIVE_CACHE_SECONDS = 60 * 60 * 6
RAW_NEGATIVE_CACHE_SECONDS = 60 * 60
# "no lo pudimos bajar recien" no es "no existe". Se cachea igual, pero cortito y
# solo para no martillar la fuente mientras esta caida. Ver _ausencia_real().
RAW_TRANSIENT_CACHE_SECONDS = 60


class NoBeatmapError(Exception):
    """Beatmap not found exception."""

    pass


def _ausencia_real(e: BaseException) -> bool:
    """
    Si podemos afirmar que el mapa NO EXISTE, o si solo fallo el intento.

    Importa porque el resultado se cachea: marcar "no existe" le da 0pp a TODO el que
    juegue ese mapa hasta que venza el TTL, sin un solo error a la vista.

    El 8-ago-2026 habia 54 mapas marcados asi que se bajaban perfecto de ppy y de
    osu.direct. Se habian marcado durante una tanda de timeouts, porque el except de
    abajo agarraba httpx.HTTPError, que es la CLASE BASE de TimeoutException,
    ConnectError y compania. Un hipo de red = una hora de 0pp para ese mapa.
    """
    if isinstance(e, NoBeatmapError):
        return True

    # HTTPStatusError es lo unico que trae una respuesta de la que fiarse.
    if isinstance(e, HTTPStatusError):
        return e.response.status_code in (404, 410)

    # _fetch_beatmap_raw envuelve el ultimo error en un HTTPError generico, asi que
    # la verdad esta en la causa.
    causa = e.__cause__
    return _ausencia_real(causa) if causa is not None else False


class BeatmapRawFetcher(BaseFetcher):
    def __init__(self, client_id: str = "", client_secret: str = "", **kwargs):
        # BeatmapRawFetcher does not require OAuth, pass empty values to base class.
        super().__init__(client_id, client_secret, **kwargs)
        # Shared HTTP client and connection pool.
        self._client: AsyncClient | None = None
        # De-duplicate concurrent requests.
        self._pending_requests: dict[int, asyncio.Future[str]] = {}
        self._request_lock = asyncio.Lock()
        self._request_headers = {
            "User-Agent": "ToriiBeatmapRawFetcher/1.0 (+https://lazer.shikkesora.com)",
            "Accept": "text/plain,application/octet-stream;q=0.9,*/*;q=0.8",
        }

    async def _get_client(self) -> AsyncClient:
        """Get or create a shared HTTP client."""
        if self._client is None:
            limits = Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0,
            )
            self._client = AsyncClient(
                timeout=10.0,
                limits=limits,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _extract_beatmap_id_from_osu(content: str) -> int | None:
        match = re.search(r"(?mi)^BeatmapID:\s*(\d+)\s*$", content)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _is_valid_osu_payload(content: str) -> bool:
        if not content or not content.strip():
            return False

        lowered = content.lstrip().lower()
        if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
            return False

        has_header = "osu file format v" in content.lower()
        has_hitobjects = "[HitObjects]" in content
        return has_header and has_hitobjects

    def _extract_osu_from_osz_bytes(
        self,
        osz_bytes: bytes,
        beatmap_id: int,
        checksum: str | None,
    ) -> str | None:
        try:
            with zipfile.ZipFile(BytesIO(osz_bytes)) as zf:
                osu_files = [name for name in zf.namelist() if name.lower().endswith(".osu")]
                if not osu_files:
                    return None

                # First pass: exact BeatmapID match.
                for osu_name in osu_files:
                    raw_bytes = zf.read(osu_name)
                    text = raw_bytes.decode("utf-8-sig", errors="ignore")
                    parsed_id = self._extract_beatmap_id_from_osu(text)
                    if parsed_id == beatmap_id and self._is_valid_osu_payload(text):
                        return text

                # Second pass: checksum match (for malformed/missing BeatmapID).
                if checksum:
                    expected = checksum.lower()
                    for osu_name in osu_files:
                        raw_bytes = zf.read(osu_name)
                        md5 = hashlib.md5(raw_bytes, usedforsecurity=False).hexdigest().lower()
                        if md5 == expected:
                            text = raw_bytes.decode("utf-8-sig", errors="ignore")
                            if self._is_valid_osu_payload(text):
                                return text

                # Last resort: one-difficulty set.
                if len(osu_files) == 1:
                    text = zf.read(osu_files[0]).decode("utf-8-sig", errors="ignore")
                    if self._is_valid_osu_payload(text):
                        return text
        except Exception:
            return None
        return None

    async def _resolve_from_osu_direct(self, beatmap_id: int) -> tuple[int | None, str | None]:
        client = await self._get_client()
        try:
            resp = await client.get(
                "https://osu.direct/api/get_beatmaps",
                params={"b": beatmap_id},
                headers=self._request_headers,
            )
            if resp.status_code >= 400:
                return None, None

            payload: Any = resp.json()
            if not isinstance(payload, list):
                return None, None

            exact = None
            for row in payload:
                if not isinstance(row, dict):
                    continue
                try:
                    if int(row.get("beatmap_id", 0)) == beatmap_id:
                        exact = row
                        break
                except Exception:
                    continue

            if not exact:
                return None, None

            beatmapset_id = exact.get("beatmapset_id")
            file_md5 = exact.get("file_md5")
            return int(beatmapset_id), (str(file_md5) if file_md5 else None)
        except Exception:
            return None, None

    async def _resolve_beatmap_context(self, beatmap_id: int) -> tuple[int | None, str | None, bool]:
        beatmapset_id: int | None = None
        checksum: str | None = None
        is_local = False

        # Try local DB first.
        try:
            from app.database import Beatmap
            from app.dependencies.database import with_db

            async with with_db() as session:
                beatmap = await session.get(Beatmap, beatmap_id)
                if beatmap is not None:
                    beatmapset_id = beatmap.beatmapset_id
                    checksum = beatmap.checksum
                    is_local = bool(beatmap.is_local)
                    return beatmapset_id, checksum, is_local
        except Exception as e:
            logger.debug(f"Failed to resolve local beatmap context for {beatmap_id}: {e}")

        # Fall back to official API lookup (available on Fetcher combined class).
        if hasattr(self, "get_beatmap"):
            try:
                data = await self.get_beatmap(beatmap_id=beatmap_id)  # type: ignore[attr-defined]
                beatmapset_id = data.get("beatmapset_id")
                checksum = data.get("checksum")
                return int(beatmapset_id) if beatmapset_id is not None else None, checksum, False
            except Exception as e:
                logger.debug(f"Official beatmap lookup failed for {beatmap_id}: {e}")

        # Last fallback: osu.direct lookup endpoint.
        sid, md5 = await self._resolve_from_osu_direct(beatmap_id)
        return sid, md5, False

    async def _fetch_local_osz_beatmap_raw(
        self,
        beatmap_id: int,
        beatmapset_id: int | None,
        checksum: str | None,
    ) -> str | None:
        if beatmapset_id is None:
            return None

        try:
            from app.dependencies.storage import get_storage_service

            storage = get_storage_service()
            file_path = f"beatmapsets/{beatmapset_id}.osz"
            if not await storage.is_exists(file_path):
                return None

            osz_bytes = await storage.read_file(file_path)
            return self._extract_osu_from_osz_bytes(osz_bytes, beatmap_id, checksum)
        except Exception as e:
            logger.debug(f"Local .osz fallback failed for beatmap {beatmap_id}: {e}")
            return None

    def _build_archive_urls(self, beatmapset_id: int) -> list[str]:
        # Prefer mirrors first. Official downloads are kept as a last-resort fallback.
        candidates: list[str] = []

        try:
            from app.service.beatmap_download_service import download_service

            candidates.extend(download_service.get_download_urls(beatmapset_id, no_video=True, is_china=False))
            candidates.extend(download_service.get_download_urls(beatmapset_id, no_video=True, is_china=True))
        except Exception as e:
            logger.debug(f"Could not load beatmap download service URLs: {e}")

        candidates.append(f"https://osu.ppy.sh/beatmapsets/{beatmapset_id}/download?noVideo=1")

        # Keep order and remove duplicates.
        deduped: list[str] = []
        seen: set[str] = set()
        for url in candidates:
            if url and url not in seen:
                deduped.append(url)
                seen.add(url)
        return deduped

    @staticmethod
    def _beatconnect_osu_miss_cache_key(beatmap_id: int) -> str:
        return f"beatconnect:osu:missing:beatmap:{beatmap_id}"

    @staticmethod
    def _beatconnect_archive_set_miss_cache_key(beatmapset_id: int) -> str:
        return f"beatconnect:archive:missing:set:{beatmapset_id}"

    @staticmethod
    def _beatconnect_archive_diff_miss_cache_key(beatmap_id: int) -> str:
        return f"beatconnect:archive:missing:beatmap:{beatmap_id}"

    async def _is_negative_cached(self, cache_key: str) -> bool:
        try:
            redis_client = get_redis()
            return bool(await redis_client.exists(cache_key))
        except Exception as e:
            logger.debug(f"Negative cache read failed for {cache_key}: {e}")
            return False

    async def _set_negative_cache(self, cache_key: str, value: str) -> None:
        try:
            redis_client = get_redis()
            await redis_client.set(cache_key, value, ex=BEATCONNECT_NEGATIVE_CACHE_SECONDS)
        except Exception as e:
            logger.debug(f"Negative cache write failed for {cache_key}: {e}")

    async def _fetch_from_archive_fallback(
        self,
        beatmap_id: int,
        beatmapset_id: int | None,
        checksum: str | None,
    ) -> str | None:
        if beatmapset_id is None:
            return None

        client = await self._get_client()
        archive_urls = self._build_archive_urls(beatmapset_id)
        for archive_url in archive_urls:
            try:
                logger.debug(f"Archive fallback fetch for beatmap {beatmap_id}: {archive_url}")
                resp = await client.get(archive_url, headers=self._request_headers, timeout=12.0)
                if resp.status_code >= 400:
                    continue

                body = resp.content
                if not body:
                    continue

                content_type = (resp.headers.get("Content-Type") or "").lower()
                if not body.startswith(b"PK") and "zip" not in content_type and "octet-stream" not in content_type:
                    continue

                raw = self._extract_osu_from_osz_bytes(body, beatmap_id, checksum)
                if raw is not None:
                    logger.debug(f"Archive fallback succeeded for beatmap {beatmap_id} via {archive_url}")
                    return raw
            except Exception as e:
                logger.debug(f"Archive fallback source failed for beatmap {beatmap_id}: {archive_url} ({e})")

        return None

    async def _fetch_from_raw_urls(
        self,
        beatmap_id: int,
        *,
        checksum: str | None,
        is_local: bool,
        url_templates: list[str],
    ) -> str | None:
        client = await self._get_client()
        expected_checksum = checksum.lower() if checksum else None
        last_error = None

        for url_template in url_templates:
            req_url = url_template.format(beatmap_id=beatmap_id)
            try:
                logger.opt(colors=True).debug(f"get_beatmap_raw: <y>{req_url}</y>")
                resp = await client.get(req_url, headers=self._request_headers)

                if resp.status_code >= 400:
                    logger.warning(f"Beatmap {beatmap_id} from {req_url}: HTTP {resp.status_code}")
                    last_error = NoBeatmapError(f"HTTP {resp.status_code}")
                    continue

                raw_bytes = resp.content
                body = raw_bytes.decode("utf-8-sig", errors="ignore")
                if not body or not body.strip():
                    logger.warning(f"Beatmap {beatmap_id} from {req_url}: empty response")
                    last_error = NoBeatmapError("Empty response")
                    continue

                if not self._is_valid_osu_payload(body):
                    logger.warning(f"Beatmap {beatmap_id} from {req_url}: invalid/non-osu payload")
                    last_error = NoBeatmapError("Invalid payload")
                    continue

                parsed_id = self._extract_beatmap_id_from_osu(body)
                if parsed_id is not None and parsed_id != beatmap_id:
                    logger.warning(
                        f"Beatmap {beatmap_id} from {req_url}: returned mismatched BeatmapID={parsed_id}"
                    )
                    last_error = NoBeatmapError("Mismatched BeatmapID")
                    continue

                if expected_checksum:
                    payload_checksum = hashlib.md5(raw_bytes, usedforsecurity=False).hexdigest().lower()
                    if payload_checksum != expected_checksum:
                        # Always reject mismatched payloads — pp would be computed
                        # against the wrong .osu version, which silently produces
                        # incorrect rank / leaderboard entries. Try the next source
                        # in the chain instead. The final fallback in the calling
                        # function still allows official osu.ppy.sh to be reached
                        # if every mirror is out of sync.
                        logger.warning(
                            "Beatmap {} from {}: checksum mismatch (expected {}, got {}), trying next source",
                            beatmap_id,
                            req_url,
                            expected_checksum,
                            payload_checksum,
                        )
                        last_error = NoBeatmapError("Checksum mismatch")
                        continue

                logger.debug(f"Successfully fetched beatmap {beatmap_id} from {req_url}")
                return body

            except Exception as e:
                logger.warning(f"Error fetching beatmap {beatmap_id} from {req_url}: {e}")
                last_error = e
                continue

        if last_error and isinstance(last_error, NoBeatmapError):
            raise last_error
        return None

    async def _fetch_from_beatconnect_osu(
        self,
        beatmap_id: int,
        beatmapset_id: int | None,
        checksum: str | None,
    ) -> str | None:
        if beatmapset_id is None or not beatconnect_enabled():
            return None

        negative_cache_key = self._beatconnect_osu_miss_cache_key(beatmap_id)
        if await self._is_negative_cached(negative_cache_key):
            logger.debug(f"Skipping BeatConnect .osu fetch for beatmap {beatmap_id}: cached miss")
            return None

        client = await self._get_client()
        url = f"{beatconnect_base_url()}/osu/{beatmapset_id}/{beatmap_id}/"
        try:
            logger.debug(f"BeatConnect .osu fetch for beatmap {beatmap_id}: {url}")
            resp = await client.get(
                url,
                headers={**self._request_headers, **beatconnect_headers()},
                timeout=15.0,
            )
            if resp.status_code >= 400:
                logger.warning(f"BeatConnect .osu fetch for beatmap {beatmap_id}: HTTP {resp.status_code}")
                if resp.status_code == 404:
                    await self._set_negative_cache(negative_cache_key, "404")
                return None

            raw_bytes = resp.content
            body = raw_bytes.decode("utf-8-sig", errors="ignore")
            if not self._is_valid_osu_payload(body):
                logger.warning(f"BeatConnect .osu fetch for beatmap {beatmap_id}: invalid payload")
                await self._set_negative_cache(negative_cache_key, "invalid")
                return None

            if checksum:
                payload_checksum = hashlib.md5(raw_bytes, usedforsecurity=False).hexdigest().lower()
                expected_checksum = checksum.lower()
                if payload_checksum != expected_checksum:
                    # Reject mismatched payloads. Returning the wrong .osu version
                    # would cause pp to be calculated against a beatmap that does
                    # not match what the player actually played — the most common
                    # cause of "my pp is wrong" reports. Return None so the caller
                    # falls through to the next fetcher in the chain (osu.direct,
                    # b.ppy.sh, archive download, then official). Cache the miss
                    # so we don't keep re-asking BeatConnect for this beatmap on
                    # every score.
                    logger.warning(
                        "BeatConnect .osu fetch for beatmap {}: checksum mismatch (expected {}, got {}), rejecting and trying next source",
                        beatmap_id,
                        expected_checksum,
                        payload_checksum,
                    )
                    await self._set_negative_cache(negative_cache_key, "checksum_mismatch")
                    return None

            return body
        except Exception as e:
            logger.debug(f"BeatConnect .osu fetch failed for beatmap {beatmap_id}: {e}")
            return None

    async def _fetch_from_beatconnect_archive(
        self,
        beatmap_id: int,
        beatmapset_id: int | None,
        checksum: str | None,
    ) -> str | None:
        if beatmapset_id is None or not beatconnect_enabled():
            return None

        set_negative_cache_key = self._beatconnect_archive_set_miss_cache_key(beatmapset_id)
        diff_negative_cache_key = self._beatconnect_archive_diff_miss_cache_key(beatmap_id)
        if await self._is_negative_cached(set_negative_cache_key):
            logger.debug(f"Skipping BeatConnect archive fetch for set {beatmapset_id}: cached set miss")
            return None
        if await self._is_negative_cached(diff_negative_cache_key):
            logger.debug(f"Skipping BeatConnect archive fetch for beatmap {beatmap_id}: cached diff miss")
            return None

        client = await self._get_client()
        url = f"{beatconnect_base_url()}/b/{beatmapset_id}/"
        try:
            logger.debug(f"BeatConnect archive fetch for beatmap {beatmap_id}: {url}")
            resp = await client.get(
                url,
                headers={**self._request_headers, **beatconnect_headers()},
                timeout=30.0,
            )
            if resp.status_code >= 400:
                logger.warning(f"BeatConnect archive fetch for beatmap {beatmap_id}: HTTP {resp.status_code}")
                if resp.status_code == 404:
                    body = resp.text.lower()
                    if "difficulty not found" in body:
                        await self._set_negative_cache(diff_negative_cache_key, "404")
                    else:
                        await self._set_negative_cache(set_negative_cache_key, "404")
                return None

            body = resp.content
            if not body.startswith(b"PK"):
                logger.warning(f"BeatConnect archive fetch for beatmap {beatmap_id}: non-zip payload")
                await self._set_negative_cache(set_negative_cache_key, "non-zip")
                return None

            extracted = self._extract_osu_from_osz_bytes(body, beatmap_id, checksum)
            if extracted is None:
                await self._set_negative_cache(diff_negative_cache_key, "not-in-archive")
            return extracted
        except Exception as e:
            logger.debug(f"BeatConnect archive fetch failed for beatmap {beatmap_id}: {e}")
            return None

    async def get_beatmap_raw(self, beatmap_id: int) -> str:
        """Un solo fetch en vuelo por beatmap.

        REGLA: adentro de self._request_lock NO se espera NADA.

        Ese lock es UNO SOLO para todo el proceso (self._request_lock del __init__,
        y el Fetcher es un singleton de modulo: app/dependencies/fetcher.py). La
        version vieja tenia un `return await future` ADENTRO del
        `async with self._request_lock`: una corrutina se quedaba con el lock global
        colgada de una bajada de red ajena, y el `finally` del dueño necesita ESE
        MISMO lock para limpiar. Se trababan mutuamente y no salian nunca, y de
        paso congelaban todos los get_beatmap_raw del server, incluido el que
        cuelga del calculo de pp del submit de scores.

        A ese bloque solo se llegaba si primero fallaba la espera de mas arriba. O
        sea que hacia falta que un fetch fallara para entrar. El 3 de agosto los
        tres mirrors se cayeron a la vez (old.ppy.sh, osu.direct y b.ppy.sh en el
        mismo minuto) y eso fue el gatillo.

        Los otros dos arreglos, mas chicos:
        - el `finally` hacia `pop(beatmap_id, None)` a secas, o sea que si dos
          corrutinas se cruzaban se llevaba el future de la otra y la dejaba sin
          registrar. Ahora borra SOLO el propio.
        - si el fetch en vuelo falla, ahora falla para todos los que lo esperaban
          en vez de que cada uno reintente. _fetch_beatmap_raw ya prueba todos los
          mirrors por dentro: si fallo, fallaron todos, y reintentar es pegarle mas
          fuerte a un mirror que ya se esta cayendo. Ese reintento era justo la
          puerta de entrada al bloque roto.
        """
        propio: asyncio.Future[str] | None = None

        # Dos vueltas como mucho: la segunda es solo para el caso de engancharse a
        # un dueño que se cancela justo. Mas que eso es girar al pedo.
        for _ in range(2):
            async with self._request_lock:
                en_vuelo = self._pending_requests.get(beatmap_id)
                if en_vuelo is None:
                    propio = asyncio.get_running_loop().create_future()
                    # Si nadie lo espera y termina en excepcion, asyncio loguea
                    # "Future exception was never retrieved". Este callback la
                    # consume para que no ensucie el log.
                    propio.add_done_callback(lambda f: f.cancelled() or f.exception())
                    self._pending_requests[beatmap_id] = propio
                    break

            # Esperar SIEMPRE afuera del lock. El shield es para distinguir quien se
            # cancelo: sin el, si nos cancelan a NOSOTROS tambien matariamos el
            # fetch del dueño, que puede tener otros esperandolo.
            logger.debug(f"Beatmap {beatmap_id} request already in progress, waiting...")
            try:
                return await asyncio.shield(en_vuelo)
            except asyncio.CancelledError:
                if not en_vuelo.cancelled():
                    raise  # la cancelacion es nuestra, no del dueño
                logger.debug(f"Beatmap {beatmap_id}: el dueño se cancelo, doy otra vuelta")

        if propio is None:
            # Dos veces seguidas nos enganchamos a dueños que se cancelaron. Lo
            # hacemos nosotros sin registrarnos, que es peor para el dedup pero
            # termina.
            return await self._fetch_beatmap_raw(beatmap_id)

        try:
            resultado = await self._fetch_beatmap_raw(beatmap_id)
            if not propio.done():
                propio.set_result(resultado)
            return resultado
        except BaseException as e:
            if not propio.done():
                if isinstance(e, asyncio.CancelledError):
                    propio.cancel()
                else:
                    propio.set_exception(e)
            raise
        finally:
            async with self._request_lock:
                # Solo el propio: ver el docstring.
                if self._pending_requests.get(beatmap_id) is propio:
                    del self._pending_requests[beatmap_id]

    async def _fetch_beatmap_raw(self, beatmap_id: int) -> str:
        last_error = None
        beatmapset_id, checksum, is_local = await self._resolve_beatmap_context(beatmap_id)

        # Local maps: use local .osz first. This avoids relying on external mirrors for local uploads.
        local_raw = await self._fetch_local_osz_beatmap_raw(beatmap_id, beatmapset_id, checksum)
        if local_raw is not None:
            logger.debug(f"Successfully fetched local beatmap raw for {beatmap_id} (set {beatmapset_id})")
            return local_raw

        beatconnect_raw = await self._fetch_from_beatconnect_osu(beatmap_id, beatmapset_id, checksum)
        if beatconnect_raw is not None:
            logger.debug(f"Successfully fetched beatmap {beatmap_id} from BeatConnect direct .osu")
            return beatconnect_raw

        try:
            fast_raw = await self._fetch_from_raw_urls(
                beatmap_id,
                checksum=checksum,
                is_local=is_local,
                url_templates=FAST_RAW_URLS,
            )
            if fast_raw is not None:
                return fast_raw
        except Exception as e:
            last_error = e

        beatconnect_archive_raw = await self._fetch_from_beatconnect_archive(beatmap_id, beatmapset_id, checksum)
        if beatconnect_archive_raw is not None:
            logger.debug(f"Successfully fetched beatmap {beatmap_id} from BeatConnect archive fallback")
            return beatconnect_archive_raw

        archive_raw = await self._fetch_from_archive_fallback(beatmap_id, beatmapset_id, checksum)
        if archive_raw is not None:
            logger.debug(
                "Successfully fetched beatmap {} from archive fallback (set {}, local={})",
                beatmap_id,
                beatmapset_id,
                is_local,
            )
            return archive_raw

        try:
            official_raw = await self._fetch_from_raw_urls(
                beatmap_id,
                checksum=checksum,
                is_local=is_local,
                url_templates=OFFICIAL_RAW_URLS,
            )
            if official_raw is not None:
                return official_raw
        except Exception as e:
            last_error = e

        error_msg = f"Failed to fetch beatmap {beatmap_id} from all sources"
        if last_error and isinstance(last_error, NoBeatmapError):
            raise last_error
        raise HTTPError(error_msg) from last_error

    async def get_or_fetch_beatmap_raw(self, redis: redis.Redis, beatmap_id: int) -> str:
        from app.config import settings

        cache_key = f"beatmap:{beatmap_id}:raw"
        miss_cache_key = f"beatmap:{beatmap_id}:raw:missing"
        cache_expire = settings.beatmap_cache_expire_hours * 60 * 60

        if await redis.exists(cache_key):
            content = await redis.get(cache_key)
            if content:
                await redis.expire(cache_key, cache_expire)
                return content

        if await redis.exists(miss_cache_key):
            raise NoBeatmapError(f"Beatmap {beatmap_id} is cached as missing from all sources")

        try:
            raw = await self.get_beatmap_raw(beatmap_id)
        except (NoBeatmapError, HTTPError) as e:
            # Una hora de "no existe" SOLO si de verdad no existe. Si fue un timeout o
            # la fuente estaba caida, se marca 60s nomas: alcanza para no martillarla y
            # no le regala 0pp a todo el que juegue ese mapa durante una hora.
            if _ausencia_real(e):
                await redis.set(miss_cache_key, str(e), ex=RAW_NEGATIVE_CACHE_SECONDS)
            else:
                logger.warning(
                    "No se pudo bajar el beatmap {} y NO parece que no exista ({}: {}). "
                    "Se reintenta en {}s en vez de darlo por perdido una hora.",
                    beatmap_id,
                    type(e).__name__,
                    e,
                    RAW_TRANSIENT_CACHE_SECONDS,
                )
                await redis.set(miss_cache_key, str(e), ex=RAW_TRANSIENT_CACHE_SECONDS)
            raise

        await redis.set(cache_key, raw, ex=cache_expire)
        await redis.delete(miss_cache_key)
        return raw
