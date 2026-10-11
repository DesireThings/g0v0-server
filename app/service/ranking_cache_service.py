"""
User leaderboard cache service.
Used to cache user leaderboard data to reduce database load.
"""

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Literal

from app.config import settings
from app.const import NEW_SCORE_FORMAT_VER
from app.database.statistics import UserStatistics, UserStatisticsModel, active_cutoff
from app.helpers.asset_proxy_helper import replace_asset_urls
from app.log import logger
from app.models.score import GameMode
from app.utils import safe_json_dumps, utcnow
from sqlalchemy import and_, func, literal_column, or_
from app.database.user_account_history import (
    UserAccountHistory,
    UserAccountHistoryType,
)

from redis.asyncio import Redis
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    pass

def _user_is_currently_restricted_expr():
    # MySQL: TIMESTAMPADD(SECOND, length, timestamp) > NOW()
    second_unit = literal_column("SECOND")

    return select(UserAccountHistory.user_id).where(
        and_(
            or_(
                UserAccountHistory.type == UserAccountHistoryType.RESTRICTION,
                UserAccountHistory.type == "RESTRICTION",
            ),
            UserAccountHistory.timestamp <= func.now(),
            or_(
                UserAccountHistory.permanent.is_(True),
                func.timestampadd(second_unit, UserAccountHistory.length, UserAccountHistory.timestamp) > func.now(),
            ),
        )
    )



class RankingCacheService:
    """User leaderboard cache service."""

    def __init__(self, redis: Redis):
        self.redis = redis
        self._refreshing = False
        self._background_tasks: set = set()

    def _get_cache_key(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        country: str | None = None,
        page: int = 1,
    ) -> str:
        """Generate cache key."""
        country_part = f":{country.upper()}" if country else ""
        return f"ranking:{ruleset}:{type}{country_part}:page:{page}"

    def _get_stats_cache_key(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        country: str | None = None,
    ) -> str:
        """Generate statistics cache key."""
        country_part = f":{country.upper()}" if country else ""
        return f"ranking:stats:{ruleset}:{type}{country_part}"

    def _get_country_cache_key(self, ruleset: GameMode, page: int = 1) -> str:
        """Generate country leaderboard cache key."""
        return f"country_ranking:{ruleset}:page:{page}"

    def _get_country_stats_cache_key(self, ruleset: GameMode) -> str:
        """Generate country leaderboard statistics cache key."""
        return f"country_ranking:stats:{ruleset}"

    def _get_team_cache_key(self, ruleset: GameMode, type: Literal["performance", "score"], page: int = 1) -> str:
        """Generate team leaderboard cache key."""
        return f"team_ranking:{ruleset}:{type}:page:{page}"

    def _get_team_stats_cache_key(self, ruleset: GameMode, type: Literal["performance", "score"]) -> str:
        """Generate team leaderboard statistics cache key."""
        return f"team_ranking:stats:{ruleset}:{type}"

    async def get_cached_ranking(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        country: str | None = None,
        page: int = 1,
    ) -> list[dict] | None:
        """Get cached leaderboard data."""
        try:
            cache_key = self._get_cache_key(ruleset, type, country, page)
            cached_data = await self.redis.get(cache_key)

            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting cached ranking: {e}")
            return None

    async def cache_ranking(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        ranking_data: list[dict],
        country: str | None = None,
        page: int = 1,
        ttl: int | None = None,  # Allow None to use default value from config
    ) -> None:
        """Cache leaderboard data."""
        try:
            cache_key = self._get_cache_key(ruleset, type, country, page)
            # Use TTL setting from config
            if ttl is None:
                ttl = settings.ranking_cache_expire_minutes * 60
            await self.redis.set(cache_key, safe_json_dumps(ranking_data), ex=ttl)
            logger.debug(f"Cached ranking data for {cache_key}")
        except Exception as e:
            logger.error(f"Error caching ranking: {e}")

    async def get_cached_stats(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        country: str | None = None,
    ) -> dict | None:
        """Get cached statistics."""
        try:
            cache_key = self._get_stats_cache_key(ruleset, type, country)
            cached_data = await self.redis.get(cache_key)

            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting cached stats: {e}")
            return None

    async def cache_stats(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        stats: dict,
        country: str | None = None,
        ttl: int | None = None,  # Allow None to use default value from config
    ) -> None:
        """Cache statistics."""
        try:
            cache_key = self._get_stats_cache_key(ruleset, type, country)
            # Use TTL setting from config, stats are cached for longer
            if ttl is None:
                ttl = settings.ranking_cache_expire_minutes * 60 * 6  # 6x duration
            await self.redis.set(cache_key, safe_json_dumps(stats), ex=ttl)
            logger.debug(f"Cached stats for {cache_key}")
        except Exception as e:
            logger.error(f"Error caching stats: {e}")

    async def get_cached_country_ranking(
        self,
        ruleset: GameMode,
        page: int = 1,
    ) -> list[dict] | None:
        """Get cached country leaderboard data."""
        try:
            cache_key = self._get_country_cache_key(ruleset, page)
            cached_data = await self.redis.get(cache_key)

            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting cached country ranking: {e}")
            return None

    async def cache_country_ranking(
        self,
        ruleset: GameMode,
        ranking_data: list[dict],
        page: int = 1,
        ttl: int | None = None,
    ) -> None:
        """Cache country leaderboard data."""
        try:
            cache_key = self._get_country_cache_key(ruleset, page)
            if ttl is None:
                ttl = settings.ranking_cache_expire_minutes * 60
            await self.redis.set(cache_key, safe_json_dumps(ranking_data), ex=ttl)
            logger.debug(f"Cached country ranking data for {cache_key}")
        except Exception as e:
            logger.error(f"Error caching country ranking: {e}")

    async def get_cached_team_ranking(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        page: int = 1,
    ) -> list[dict] | None:
        """Get cached team leaderboard data."""
        try:
            cache_key = self._get_team_cache_key(ruleset, type, page)
            cached_data = await self.redis.get(cache_key)

            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting cached team ranking: {e}")
            return None

    async def cache_team_ranking(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        ranking_data: list[dict],
        page: int = 1,
        ttl: int | None = None,
    ) -> None:
        """Cache team leaderboard data."""
        try:
            cache_key = self._get_team_cache_key(ruleset, type, page)
            if ttl is None:
                ttl = settings.ranking_cache_expire_minutes * 60
            await self.redis.set(cache_key, safe_json_dumps(ranking_data), ex=ttl)
            logger.debug(f"Cached team ranking data for {cache_key}")
        except Exception as e:
            logger.error(f"Error caching team ranking: {e}")

    async def get_cached_team_stats(self, ruleset: GameMode, type: Literal["performance", "score"]) -> dict | None:
        """Get cached team leaderboard statistics."""
        try:
            cache_key = self._get_team_stats_cache_key(ruleset, type)
            cached_data = await self.redis.get(cache_key)

            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting cached team stats: {e}")
            return None

    async def cache_team_stats(
        self,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        stats: dict,
        ttl: int | None = None,
    ) -> None:
        """Cache team leaderboard statistics."""
        try:
            cache_key = self._get_team_stats_cache_key(ruleset, type)
            if ttl is None:
                ttl = settings.ranking_cache_expire_minutes * 60 * 6
            await self.redis.set(cache_key, safe_json_dumps(stats), ex=ttl)
            logger.debug(f"Cached team stats for {cache_key}")
        except Exception as e:
            logger.error(f"Error caching team stats: {e}")

    async def get_cached_country_stats(self, ruleset: GameMode) -> dict | None:
        """Get cached country leaderboard statistics."""
        try:
            cache_key = self._get_country_stats_cache_key(ruleset)
            cached_data = await self.redis.get(cache_key)

            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting cached country stats: {e}")
            return None

    async def cache_country_stats(
        self,
        ruleset: GameMode,
        stats: dict,
        ttl: int | None = None,
    ) -> None:
        """Cache country leaderboard statistics."""
        try:
            cache_key = self._get_country_stats_cache_key(ruleset)
            if ttl is None:
                ttl = settings.ranking_cache_expire_minutes * 60 * 6  # 6x duration
            await self.redis.set(cache_key, safe_json_dumps(stats), ex=ttl)
            logger.debug(f"Cached country stats for {cache_key}")
        except Exception as e:
            logger.error(f"Error caching country stats: {e}")

    async def cache_top_scores(self, data: list, ruleset: GameMode, page: int = 1) -> None:
        """Cache top scores for a specific game mode (private API top-scores endpoint)."""
        try:
            cache_key = f"top_scores:{ruleset}:page:{page}"
            ttl = settings.ranking_cache_expire_minutes * 60
            await self.redis.set(cache_key, safe_json_dumps(data), ex=ttl)
            logger.debug(f"Cached top scores for {cache_key}")
        except Exception as e:
            logger.error(f"Error caching top scores: {e}")

    async def get_cached_top_scores(self, ruleset: GameMode, page: int = 1) -> list[dict] | None:
        """Get cached top scores for a specific game mode."""
        try:
            cache_key = f"top_scores:{ruleset}:page:{page}"
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting cached top scores: {e}")
            return None

    async def refresh_top_scores_cache(
        self, session: AsyncSession, ruleset: GameMode, max_pages: int | None = None
    ) -> None:
        """Refresh top scores cache for a ruleset."""
        from app.database import BestScore, Score, ScoreModel

        wheres = [
            Score.gamemode == ruleset,
            col(Score.id).in_(select(BestScore.score_id).where(BestScore.gamemode == ruleset)),
        ]

        if max_pages is None:
            max_pages = settings.top_score_cache_max_pages

        try:
            logger.info(f"Starting top scores cache refresh for {ruleset}")
            for page in range(1, max_pages + 1):
                if page == 1:
                    cursor = sys.maxsize
                else:
                    cursor = (
                        await session.exec(
                            select(Score.pp)
                            .where(*wheres)
                            .order_by(col(Score.pp).desc())
                            .offset((page - 1) * 50 - 1)
                            .limit(1)
                        )
                    ).first()
                    if cursor is None:
                        break
                scores = (
                    await session.exec(
                        select(Score)
                        .where(*wheres, col(Score.pp) <= cursor)
                        .order_by(col(Score.pp).desc())
                        .limit(50)
                    )
                ).all()
                data = [
                    await score.to_resp(
                        session,
                        api_version=NEW_SCORE_FORMAT_VER + 1,
                        includes=ScoreModel.DEFAULT_SCORE_INCLUDES,
                    )
                    for score in scores
                ]
                await self.cache_top_scores(data, ruleset, page)
                await asyncio.sleep(0.1)
            logger.info(f"Completed top scores cache refresh for {ruleset}")
        except Exception as e:
            logger.error(f"Top scores cache refresh failed for {ruleset}: {e}")

    async def refresh_ranking_cache(
        self,
        session: AsyncSession,
        ruleset: GameMode,
        type: Literal["performance", "score"],
        country: str | None = None,
        max_pages: int | None = None,  # Allow None to use default value from config
    ) -> None:
        """Refresh leaderboard cache."""
        if self._refreshing:
            logger.debug(f"Ranking cache refresh already in progress for {ruleset}:{type}")
            return

        # Use settings from config
        if max_pages is None:
            max_pages = settings.ranking_cache_max_pages

        self._refreshing = True
        try:
            logger.info(f"Starting ranking cache refresh for {ruleset}:{type}")

            # Build query conditions
            restricted_users_subq = _user_is_currently_restricted_expr()
            wheres = [
                col(UserStatistics.mode) == ruleset,
                col(UserStatistics.pp) > 0,
		col(UserStatistics.user).has(is_active=True),
		~col(UserStatistics.user_id).in_(restricted_users_subq),
                col(UserStatistics.is_ranked).is_(True),
                # Active-only ranking: keep the cached pages in sync with the
                # live get_rank() population (30d+ inactive excluded).
                col(UserStatistics.last_played) >= active_cutoff(),
            ]
            include = UserStatistics.RANKING_INCLUDES.copy()

            # Desempate explicito por user_id: get_rank() desempata user_id ASC, y un
            # ORDER BY pelado deja a los pp empatados en cualquier orden, contradiciendo
            # los numeros de esas mismas filas.
            if type == "performance":
                order_by = (col(UserStatistics.pp).desc(), col(UserStatistics.user_id).asc())
                include.append("rank_change_since_30_days")
            else:
                order_by = (col(UserStatistics.ranked_score).desc(), col(UserStatistics.user_id).asc())

            if country:
                wheres.append(col(UserStatistics.user).has(country_code=country.upper()))
                include.append("country_rank")

            # Get total users count for statistics
            total_users_query = select(UserStatistics).where(*wheres)
            total_users = len((await session.exec(total_users_query)).all())

            # Calculate statistics
            stats = {
                "total": total_users,
                "total_users": total_users,
                "last_updated": utcnow().isoformat(),
                "type": type,
                "ruleset": ruleset,
                "country": country,
            }

            # Cache statistics
            await self.cache_stats(ruleset, type, stats, country)

            # Cache data by page
            for page in range(1, max_pages + 1):
                try:
                    statistics_list = await session.exec(
                        select(UserStatistics).where(*wheres).order_by(*order_by).limit(50).offset(50 * (page - 1))
                    )

                    statistics_data = statistics_list.all()
                    if not statistics_data:
                        break  # No more data

                    # Transform to response format and ensure proper serialization
                    ranking_data = []
                    for statistics in statistics_data:
                        # Cache canonical payload (unmasked NSFW media); viewer policy is applied at request time.
                        user_stats_resp = await UserStatisticsModel.transform(
                            statistics,
                            includes=include,
                            show_nsfw_media=True,
                        )

                        user_dict = user_stats_resp

                        # Apply asset proxy processing
                        if settings.enable_asset_proxy:
                            try:
                                user_dict = await replace_asset_urls(user_dict)
                            except Exception as e:
                                logger.warning(f"Asset proxy processing failed for ranking cache: {e}")

                        ranking_data.append(user_dict)

                    # El puesto que ve el usuario sale POSICIONAL del orden de esta misma
                    # query, no de get_rank() por fila. Los resolvers corren en momentos
                    # distintos con caches propios, y en prod quedo una pagina con el orden
                    # bien pero global_rank embebidos [2,2,3,5,5]: dos #2 y dos #5 en la
                    # misma pagina, con el #1 real del server marcado como 2. Stampear el
                    # indice hace la pagina auto-consistente por construccion y de paso
                    # ahorra el COUNT por fila. Solo para performance: en las paginas por
                    # score la posicion es por score y el global_rank sigue siendo de pp.
                    if type == "performance":
                        for idx, user_dict in enumerate(ranking_data):
                            pos = 50 * (page - 1) + idx + 1
                            if country:
                                # Pagina de pais: la posicion es el country rank. El
                                # transform de arriba ni siquiera recibia user_country,
                                # asi que su country_rank era el rank GLOBAL (el kwarg
                                # defaultea None y get_rank sin pais cuenta a todos).
                                user_dict["country_rank"] = pos
                            else:
                                user_dict["global_rank"] = pos

                    # Cache data for this page
                    await self.cache_ranking(ruleset, type, ranking_data, country, page)

                    # Add delay to avoid database overload
                    if page < max_pages:
                        await asyncio.sleep(0.1)

                except Exception as e:
                    # Log the full validation error so failing user_id and field
                    # path are visible (Pydantic ValidationError str() is
                    # truncated by default loguru output). Include the user_id
                    # of the row that triggered the failure when known so we can
                    # attribute the bad data.
                    failing_user_id: int | None = None
                    try:
                        failing_user_id = locals().get("statistics") and getattr(
                            locals()["statistics"], "user_id", None
                        )
                    except Exception:
                        pass
                    logger.opt(exception=e).error(
                        f"Error caching page {page} for {ruleset}:{type} "
                        f"(failing_user_id={failing_user_id}): {e}"
                    )

            logger.debug(f"Completed ranking cache refresh for {ruleset}:{type}")

        except Exception as e:
            logger.error(f"Ranking cache refresh failed for {ruleset}:{type}: {e}")
        finally:
            self._refreshing = False

    async def refresh_country_ranking_cache(
        self,
        session: AsyncSession,
        ruleset: GameMode,
        max_pages: int | None = None,
    ) -> None:
        """Refresh country leaderboard cache."""
        if self._refreshing:
            logger.debug(f"Country ranking cache refresh already in progress for {ruleset}")
            return

        if max_pages is None:
            max_pages = settings.ranking_cache_max_pages

        self._refreshing = True
        try:
            logger.info(f"Starting country ranking cache refresh for {ruleset}")

            # Get all countries
            from app.database import User

            countries = (await session.exec(select(User.country_code).distinct())).all()

            # Calculate statistics for each country
            country_stats_list = []
            for country in countries:
                if not country:  # Skip empty country code
                    continue

                statistics = (
                    await session.exec(
                        select(UserStatistics).where(
                            UserStatistics.mode == ruleset,
                            UserStatistics.pp > 0,
                            col(UserStatistics.user).has(country_code=country),
                            col(UserStatistics.user).has(is_active=True),
                        )
                    )
                ).all()

                if not statistics:  # Skip countries without data
                    continue

                pp = 0
                country_stats = {
                    "code": country,
                    "active_users": 0,
                    "play_count": 0,
                    "ranked_score": 0,
                    "performance": 0,
                }

                for stat in statistics:
                    country_stats["active_users"] += 1
                    country_stats["play_count"] += stat.play_count
                    country_stats["ranked_score"] += stat.ranked_score
                    pp += stat.pp

                country_stats["performance"] = round(pp)
                country_stats_list.append(country_stats)

            # Sort by performance points
            country_stats_list.sort(key=lambda x: x["performance"], reverse=True)

            # Calculate statistics
            stats = {
                "total_countries": len(country_stats_list),
                "last_updated": utcnow().isoformat(),
                "ruleset": ruleset,
            }

            # Cache statistics
            await self.cache_country_stats(ruleset, stats)

            # Cache data by page (50 countries per page)
            page_size = 50
            for page in range(1, max_pages + 1):
                start_idx = (page - 1) * page_size
                end_idx = start_idx + page_size

                page_data = country_stats_list[start_idx:end_idx]
                if not page_data:
                    break  # No more data

                # Cache data for this page
                await self.cache_country_ranking(ruleset, page_data, page)

                # Add delay to avoid Redis overload
                if page < max_pages and page_data:
                    await asyncio.sleep(0.1)

            logger.info(f"Completed country ranking cache refresh for {ruleset}")

        except Exception as e:
            logger.error(f"Country ranking cache refresh failed for {ruleset}: {e}")
        finally:
            self._refreshing = False

    async def refresh_all_rankings(self, session: AsyncSession) -> None:
        """Refresh all leaderboard caches."""
        game_modes = [GameMode.OSU, GameMode.TAIKO, GameMode.FRUITS, GameMode.MANIA]
        ranking_types: list[Literal["performance", "score"]] = ["performance", "score"]

        # Get list of countries to cache (top 20 countries by active user count)
        from app.database import User

        from sqlmodel import func

        countries_query = (
            await session.exec(
                select(User.country_code, func.count().label("user_count"))
                .where(col(User.is_active).is_(True))
                .group_by(User.country_code)
                .order_by(func.count().desc())
                .limit(settings.ranking_cache_top_countries)
            )
        ).all()

        top_countries = [country for country, _ in countries_query]

        refresh_tasks = []

        # Global leaderboard
        for mode in game_modes:
            for ranking_type in ranking_types:
                task = self.refresh_ranking_cache(session, mode, ranking_type)
                refresh_tasks.append(task)

        # Country leaderboard (top 20 countries only)
        for country in top_countries:
            for mode in game_modes:
                for ranking_type in ranking_types:
                    task = self.refresh_ranking_cache(session, mode, ranking_type, country)
                    refresh_tasks.append(task)

        # Country rankings
        for mode in game_modes:
            task = self.refresh_country_ranking_cache(session, mode)
            refresh_tasks.append(task)

            # Top scores cache (private API)
            task = self.refresh_top_scores_cache(session, mode)
            refresh_tasks.append(task)

        # Concurrently execute refresh tasks, limiting concurrency
        semaphore = asyncio.Semaphore(15)  # raised from 5 to absorb the extra top-score tasks

        async def bounded_refresh(task):
            async with semaphore:
                await task

        bounded_tasks = [bounded_refresh(task) for task in refresh_tasks]

        try:
            await asyncio.gather(*bounded_tasks, return_exceptions=True)
            logger.info("All ranking cache refresh completed")
        except Exception as e:
            logger.error(f"Error in batch ranking cache refresh: {e}")

    async def invalidate_cache(
        self,
        ruleset: GameMode | None = None,
        type: Literal["performance", "score"] | None = None,
        country: str | None = None,
        include_country_ranking: bool = True,
    ) -> None:
        """Invalidate cache."""
        try:
            deleted_keys = 0

            if ruleset and type:
                # Delete specific user leaderboard cache
                country_part = f":{country.upper()}" if country else ""
                pattern = f"ranking:{ruleset}:{type}{country_part}:page:*"
                keys = await self.redis.keys(pattern)
                if keys:
                    await self.redis.delete(*keys)
                    deleted_keys += len(keys)
                    logger.info(f"Invalidated {len(keys)} cache keys for {ruleset}:{type}")
            elif ruleset:
                # Delete all cache for specific game mode
                patterns = [
                    f"ranking:{ruleset}:*",
                    f"country_ranking:{ruleset}:*" if include_country_ranking else None,
                ]
                for pattern in patterns:
                    if pattern:
                        keys = await self.redis.keys(pattern)
                        if keys:
                            await self.redis.delete(*keys)
                            deleted_keys += len(keys)
            else:
                # Delete all leaderboard caches
                patterns = ["ranking:*"]
                if include_country_ranking:
                    patterns.append("country_ranking:*")

                for pattern in patterns:
                    keys = await self.redis.keys(pattern)
                    if keys:
                        await self.redis.delete(*keys)
                        deleted_keys += len(keys)

                logger.info(f"Invalidated all {deleted_keys} ranking cache keys")

        except Exception as e:
            logger.error(f"Error invalidating cache: {e}")

    async def invalidate_country_cache(self, ruleset: GameMode | None = None) -> None:
        """Invalidate country leaderboard cache."""
        try:
            pattern = f"country_ranking:{ruleset}:*" if ruleset else "country_ranking:*"

            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} country ranking cache keys")
        except Exception as e:
            logger.error(f"Error invalidating country cache: {e}")

    async def invalidate_team_cache(self, ruleset: GameMode | None = None) -> None:
        """Invalidate team leaderboard cache."""
        try:
            pattern = f"team_ranking:{ruleset}:*" if ruleset else "team_ranking:*"

            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} team ranking cache keys")
        except Exception as e:
            logger.error(f"Error invalidating team cache: {e}")

    async def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        try:
            # Get user leaderboard cache
            ranking_keys = await self.redis.keys("ranking:*")
            # Get country leaderboard cache
            country_keys = await self.redis.keys("country_ranking:*")

            total_keys = ranking_keys + country_keys
            total_size = 0

            for key in total_keys[:100]:  # Limit number of checked keys to avoid performance issues
                try:
                    size = await self.redis.memory_usage(key)
                    if size:
                        total_size += size
                except Exception:
                    logger.warning(f"Failed to get memory usage for key {key}")
                    continue

            return {
                "cached_user_rankings": len(ranking_keys),
                "cached_country_rankings": len(country_keys),
                "total_cached_rankings": len(total_keys),
                "estimated_total_size_mb": (round(total_size / 1024 / 1024, 2) if total_size > 0 else 0),
                "refreshing": self._refreshing,
            }
        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {"error": str(e)}


# Global cache service instance
_ranking_cache_service: RankingCacheService | None = None


def get_ranking_cache_service(redis: Redis) -> RankingCacheService:
    """Get leaderboard cache service instance."""
    global _ranking_cache_service
    if _ranking_cache_service is None:
        _ranking_cache_service = RankingCacheService(redis)
    return _ranking_cache_service


async def schedule_ranking_refresh_task(session: AsyncSession, redis: Redis):
    """Scheduled leaderboard refresh task."""
    # Leaderboard cache is enabled by default unless explicitly disabled
    if not settings.enable_ranking_cache:
        return

    cache_service = get_ranking_cache_service(redis)
    try:
        await cache_service.refresh_all_rankings(session)
    except Exception as e:
        logger.error(f"Scheduled ranking refresh task failed: {e}")

