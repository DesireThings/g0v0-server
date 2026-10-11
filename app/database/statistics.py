from datetime import datetime, timedelta
import math
from typing import TYPE_CHECKING, ClassVar, NotRequired, TypedDict
from weakref import WeakKeyDictionary

from app.models.score import GameMode
from app.utils import utcnow

from ._base import DatabaseModel, included, ondemand
from .rank_history import RankHistory

from pydantic import field_validator
from sqlalchemy import DateTime, Index, and_, or_
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlmodel import (
    BigInteger,
    Column,
    Field,
    ForeignKey,
    Relationship,
    col,
    func,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from .user import User, UserDict


class UserStatisticsDict(TypedDict):
    mode: GameMode
    count_100: int
    count_300: int
    count_50: int
    count_miss: int
    pp: float
    ranked_score: int
    hit_accuracy: float
    total_score: int
    total_hits: int
    maximum_combo: int
    play_count: int
    play_time: int
    replays_watched_by_others: int
    is_ranked: bool
    level: NotRequired[dict[str, int]]
    global_rank: NotRequired[int | None]
    global_rank_percent: NotRequired[float | None]
    grade_counts: NotRequired[dict[str, int]]
    rank_change_since_30_days: NotRequired[int]
    country_rank: NotRequired[int | None]
    is_inactive: NotRequired[bool]
    user: NotRequired["UserDict"]


class UserStatisticsModel(DatabaseModel[UserStatisticsDict]):
    RANKING_INCLUDES: ClassVar[list[str]] = [
        "user.country",
        "user.cover",
        "user.team",
    ]

    mode: GameMode = Field(index=True)
    count_100: int = Field(default=0, sa_column=Column(BigInteger))
    count_300: int = Field(default=0, sa_column=Column(BigInteger))
    count_50: int = Field(default=0, sa_column=Column(BigInteger))
    count_miss: int = Field(default=0, sa_column=Column(BigInteger))

    pp: float = Field(default=0.0, index=True)
    ranked_score: int = Field(default=0, sa_column=Column(BigInteger))
    hit_accuracy: float = Field(default=0.00)
    total_score: int = Field(default=0, sa_column=Column(BigInteger))
    total_hits: int = Field(default=0, sa_column=Column(BigInteger))
    maximum_combo: int = Field(default=0)

    play_count: int = Field(default=0)
    play_time: int = Field(default=0, sa_column=Column(BigInteger))
    replays_watched_by_others: int = Field(default=0)
    is_ranked: bool = Field(default=True)

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, v):
        """Convert string to GameMode enum"""
        if isinstance(v, str):
            try:
                return GameMode(v)
            except ValueError:
                # If conversion fails, return default value
                return GameMode.OSU
        return v

    @included
    @staticmethod
    async def level(_session: AsyncSession, statistics: "UserStatistics") -> dict[str, int]:
        return {
            "current": int(statistics.level_current),
            "progress": int(math.fmod(statistics.level_current, 1) * 100),
        }

    @included
    @staticmethod
    async def global_rank(session: AsyncSession, statistics: "UserStatistics") -> int | None:
        return await get_rank(session, statistics)

    @included
    @staticmethod
    async def is_inactive(_session: AsyncSession, statistics: "UserStatistics") -> bool:
        # True only for the 15-30d "greyed" window: still ranked, but clients
        # dim the row. 30d+ are already unranked (global_rank None) and dropped
        # from the board, so they never reach a client to grey. Reads the cached
        # last_played column only (no scores query).
        from datetime import timezone

        last = statistics.last_played
        if last is None:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return active_cutoff() <= last < grey_cutoff()

    # Minimum virtual population for global_rank_percent so that
    # rank-tier colours spread out on small servers instead of
    # everyone landing in the same tier.
    _VIRTUAL_MIN_POPULATION: ClassVar[int] = 10_000

    @included
    @staticmethod
    async def global_rank_percent(session: AsyncSession, statistics: "UserStatistics") -> float | None:
        from .user import User

        rank = await get_rank(session, statistics)
        if rank is None:
            return None
        total = (
            await session.exec(
                select(func.count()).where(
                    UserStatistics.mode == statistics.mode,
                    UserStatistics.pp > 0,
                    col(UserStatistics.is_ranked).is_(True),
                    col(UserStatistics.user).has(is_active=True),
                    ~User.is_restricted_query(col(UserStatistics.user_id)),
                    # Same active-only population as get_rank, so the percentile
                    # denominator matches the rank numerator.
                    col(UserStatistics.last_played) >= active_cutoff(),
                )
            )
        ).one()
        if total == 0:
            return None
        return rank / max(total, UserStatisticsModel._VIRTUAL_MIN_POPULATION)

    @included
    @staticmethod
    async def grade_counts(_session: AsyncSession, statistics: "UserStatistics") -> dict[str, int]:
        return {
            "ss": statistics.grade_ss,
            "ssh": statistics.grade_ssh,
            "s": statistics.grade_s,
            "sh": statistics.grade_sh,
            "a": statistics.grade_a,
        }

    @ondemand
    @staticmethod
    async def rank_change_since_30_days(session: AsyncSession, statistics: "UserStatistics") -> int:
        global_rank = await get_rank(session, statistics)
        rank_best = (
            await session.exec(
                select(func.max(RankHistory.rank)).where(
                    RankHistory.date > utcnow() - timedelta(days=30),
                    RankHistory.user_id == statistics.user_id,
                )
            )
        ).first()
        if rank_best is None or global_rank is None:
            return 0
        return rank_best - global_rank

    @ondemand
    @staticmethod
    async def country_rank(
        session: AsyncSession, statistics: "UserStatistics", user_country: str | None = None
    ) -> int | None:
        return await get_rank(session, statistics, user_country)

    @ondemand
    @staticmethod
    async def user(
        _session: AsyncSession,
        statistics: "UserStatistics",
        show_nsfw_media: bool = False,
    ) -> "UserDict":
        from .user import UserModel

        user_instance = await statistics.awaitable_attrs.user
        # torii: CARD_INCLUDES para que el user de los rankings lleve su aura/color/grupos (ademas de
        # country/cover/team). el resolver no forwardea sub-includes, asi que lo damos explicito.
        return await UserModel.transform(user_instance, includes=UserModel.CARD_INCLUDES, show_nsfw_media=show_nsfw_media)


class UserStatistics(AsyncAttrs, UserStatisticsModel, table=True):
    __tablename__: str = "lazer_user_statistics"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        default=None,
        sa_column=Column(
            BigInteger,
            ForeignKey("lazer_users.id"),
            index=True,
        ),
    )
    grade_ss: int = Field(default=0)
    grade_ssh: int = Field(default=0)
    grade_s: int = Field(default=0)
    grade_sh: int = Field(default=0)
    grade_a: int = Field(default=0)

    level_current: float = Field(default=1)

    # Cached MAX(scores.ended_at) of a passed play in this mode. Drives the
    # active-only leaderboard: within 30d = ranked, 15-30d = greyed, 30d+ =
    # unranked. Stamped on score submit; backfilled by the add-last-played
    # migration. NULL = never passed a score in this mode (treated as unranked).
    last_played: datetime | None = Field(
        default=None,
        sa_column=Column("last_played", DateTime(timezone=True), nullable=True),
    )

    # Serves the active-filter predicate (mode == X AND last_played >= cutoff)
    # as an index range, so get_rank() never has to touch the scores table.
    __table_args__ = (
        Index("idx_user_stats_mode_lastplayed", "mode", "last_played"),
        # El ranking pregunta "de este modo, cuantos tienen mas pp que X".
        # Con el indice de pp suelto hay que filtrar el modo despues; con el
        # compuesto es un rango directo. Ver get_rank.
        Index("idx_user_stats_mode_pp", "mode", "pp"),
    )

    user: "User" = Relationship(back_populates="statistics")


# Per-request memo for get_rank(). A single profile transform resolves the same
# global rank from several fields (global_rank, global_rank_percent,
# rank_change_since_30_days) and each one used to re-run the full ROW_NUMBER scan.
# Keyed weakly by the request's AsyncSession (so it clears when the session is
# dropped), then by (user_id, mode, country).
_rank_memo: "WeakKeyDictionary[AsyncSession, dict]" = WeakKeyDictionary()


# Active-only ranking windows (osu-style, shorter threshold). A player whose
# last passed play in a mode is older than ACTIVE_DAYS is dropped from that
# mode's ranking entirely (dense renumber, those below move up) until they play
# again.
#
# 2026-08: GREY_DAYS == ACTIVE_DAYS a proposito. Con los dos iguales la ventana
# gris queda VACIA y is_inactive nunca da True, asi que los rankings muestran
# solo gente que jugo dentro de ACTIVE_DAYS, sin filas atenuadas. Para volver a
# tener grises alcanza con bajar GREY_DAYS (ej. 10) y los de 10-15d se marcan.
# De referencia: osu oficial desrankea a los 90 dias; somos mucho mas estrictos
# a proposito, para que el board refleje quien juega HOY.
# These are the single source of truth for the thresholds.
ACTIVE_DAYS = 15
GREY_DAYS = 15


def active_cutoff() -> datetime:
    """last_played on/after this is still ranked; older drops out."""
    return utcnow() - timedelta(days=ACTIVE_DAYS)


def grey_cutoff() -> datetime:
    """last_played before this (but still active) is greyed."""
    return utcnow() - timedelta(days=GREY_DAYS)


async def get_rank(session: AsyncSession, statistics: UserStatistics, country: str | None = None) -> int | None:
    from .user import User

    cache_key = (statistics.user_id, statistics.mode, country)
    try:
        memo = _rank_memo.get(session)
        if memo is None:
            memo = _rank_memo[session] = {}
    except TypeError:
        # Session not weakly referenceable for some reason; just skip the memo.
        memo = None
    if memo is not None and cache_key in memo:
        return memo[cache_key]

    # Esto era un ROW_NUMBER() OVER (ORDER BY pp DESC) sobre TODOS los rankeados
    # del modo, del que despues se sacaba una sola fila. O sea que para saber el
    # puesto de UNO se ordenaba a los 10.786. Medido en prod: 1057 llamadas a
    # 1,1 ms en una tanda de cargas de perfil. Mismo patron que get_best_id.
    #
    # El puesto es cuantos elegibles tienen mas pp, mas uno. Con el indice
    # (mode, pp) es un rango contado, no un orden completo.
    def elegibles():
        """Los filtros del ranking, uno solo para las dos consultas de abajo: si
        se separan, el "estoy rankeado" y el "cuantos hay arriba" pueden dejar de
        coincidir y el puesto sale mal."""
        cond = [
            UserStatistics.mode == statistics.mode,
            UserStatistics.pp > 0,
            col(UserStatistics.is_ranked).is_(True),
            col(UserStatistics.user).has(is_active=True),
            ~User.is_restricted_query(col(UserStatistics.user_id)),
            # Active-only ranking: drop players with no play in this mode within
            # ACTIVE_DAYS, so the numbering closes over the remaining set and a
            # 30d+ inactive comes back as None (unranked) instead of a stale
            # position. They re-enter when their next play stamps last_played.
            # NULL last_played (never played) is also excluded.
            col(UserStatistics.last_played) >= active_cutoff(),
        ]
        if country is not None:
            cond.append(col(UserStatistics.user).has(country_code=country))
        return cond

    # Primero: ¿este jugador entra en el ranking? La ventana vieja contestaba esto
    # sola (si no entraba, el subquery no devolvia fila y salia None), y ese None
    # es lo que hace que el cliente lo muestre como no rankeado. Con un COUNT hay
    # que preguntarlo aparte, porque un COUNT siempre devuelve un numero.
    entra = (
        await session.exec(
            select(func.count()).where(*elegibles(), UserStatistics.user_id == statistics.user_id)
        )
    ).one()
    if not entra:
        if memo is not None:
            memo[cache_key] = None
        return None

    mi_pp = float(statistics.pp or 0)
    # El desempate por user_id va explicito: el ORDER BY pp DESC pelado no
    # desempataba, asi que dos pp identicos salian en cualquier orden.
    rank = (
        await session.exec(
            select(func.count()).where(
                *elegibles(),
                or_(
                    col(UserStatistics.pp) > mi_pp,
                    and_(
                        col(UserStatistics.pp) == mi_pp,
                        col(UserStatistics.user_id) < statistics.user_id,
                    ),
                ),
            )
        )
    ).one() + 1

    # get_rank() DOES NOT WRITE. It used to insert the daily rank_history
    # snapshot right here, and that is what took the server down on 2026-08-03.
    #
    # Every one of its callers is a response serialiser (global_rank,
    # global_rank_percent, rank_change_since_30_days, country_rank on
    # UserStatisticsModel). None of them owns a transaction and none of them
    # commits, so the INSERT turned a read request into an uncommitted WRITE
    # transaction: SQLAlchemy autoflushed it on the next query, InnoDB took the
    # row locks, and the connection went back to the pool still holding them.
    # 175 connections ended up asleep inside an open transaction, the oldest for
    # 5 hours, and the row locks blocked the spectator's
    # `UPDATE lazer_users SET is_online = 0` (DatabaseAccess.OfflineUser) until
    # it hit the 50s lock timeout INSIDE OnDisconnectedAsync. Players got
    # dropped and reconnected in a loop.
    #
    # A read path must not write. The daily snapshot belongs to the nightly
    # batch (tasks/calculate_all_user_rank), which owns its own transaction and
    # commits, and which covers every ranked active player anyway.
    if memo is not None:
        memo[cache_key] = rank
    return rank
