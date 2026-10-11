from collections.abc import Sequence
from datetime import date, datetime
import json
import math
import sys
from typing import TYPE_CHECKING, Any, ClassVar, NotRequired, TypedDict
import asyncio
from app.database.beatmap import calculate_beatmap_attributes
from app.calculators.score_multiplier import recompute_total_score
from app.dependencies.database import engine as db_engine

from app.calculator import (
    calculate_pp_weight,
    calculate_score_to_level,
    calculate_weighted_acc,
    calculate_weighted_pp,
    clamp,
    get_display_score,
    pre_fetch_and_calculate_pp,
)
from app.config import settings
from app.dependencies.database import get_redis
from app.log import log
from app.models.beatmap import BeatmapRankStatus
from app.models.model import (
    CurrentUserAttributes,
    PinAttributes,
    RespWithCursor,
    UTCBaseModel,
)
from app.models.mods import APIMod, get_speed_rate, mod_to_save, mods_can_get_pp
from app.models.score import (
    GameMode,
    HitResult,
    LeaderboardType,
    Rank,
    ScoreStatistics,
    SoloScoreSubmissionInfo,
)
from app.models.scoring_mode import ScoringMode
from app.storage import StorageService
from app.utils import utcnow
from app.service.suspicious_alert_service import SuspiciousAlertService
from app.service.anticheat_client import submit_for_analysis as _anticheat_submit
from app.service.trust_factor import compute_trust_factor as _compute_trust_factor

from ._base import DatabaseModel, OnDemand, included, ondemand
from .beatmap import Beatmap, BeatmapDict, BeatmapModel
from .beatmap_playcounts import BeatmapPlaycounts
from .beatmapset import BeatmapsetDict, BeatmapsetModel
from .best_scores import BestScore
from .counts import MonthlyPlaycounts
from .events import Event, EventType
from .playlist_best_score import PlaylistBestScore
from .relationship import (
    Relationship as DBRelationship,
    RelationshipType,
)
from .score_token import ScoreToken
from .team import TeamMember
from .total_score_best_scores import TotalScoreBestScore
from .user import User, UserDict, UserModel

from pydantic import BaseModel, field_serializer, field_validator
from redis.asyncio import Redis
from weakref import WeakKeyDictionary

from sqlalchemy import Boolean, Column, DateTime, Index, SmallInteger, TextClause, and_, exists, or_
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import Mapped, aliased, joinedload
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import (
    JSON,
    BigInteger,
    Field,
    ForeignKey,
    Relationship,
    SQLModel,
    col,
    func,
    select,
    text,
    true,
)
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from app.fetcher import Fetcher

logger = log("Score")


class ScoreDict(TypedDict):
    beatmap_id: int
    id: int
    rank: Rank
    type: str
    user_id: int
    accuracy: float
    build_id: int | None
    client_version: str | None
    ended_at: datetime
    has_replay: bool
    max_combo: int
    passed: bool
    pp: float
    started_at: datetime
    total_score: int
    maximum_statistics: ScoreStatistics
    mods: list[APIMod]
    classic_total_score: int | None
    preserve: bool
    processed: bool
    ranked: bool
    playlist_item_id: NotRequired[int | None]
    room_id: NotRequired[int | None]
    best_id: NotRequired[int | None]
    legacy_perfect: NotRequired[bool]
    is_perfect_combo: NotRequired[bool]
    ruleset_id: NotRequired[int]
    statistics: NotRequired[ScoreStatistics]
    beatmapset: NotRequired[BeatmapsetDict]
    beatmap: NotRequired[BeatmapDict]
    current_user_attributes: NotRequired[CurrentUserAttributes]
    position: NotRequired[int | None]
    scores_around: NotRequired["ScoreAround | None"]
    rank_country: NotRequired[int | None]
    rank_global: NotRequired[int | None]
    user: NotRequired[UserDict]
    weight: NotRequired[float | None]

    # ScoreResp fields
    legacy_total_score: NotRequired[int]


class ScoreModel(AsyncAttrs, DatabaseModel[ScoreDict]):
    # https://github.com/ppy/osu-web/blob/master/app/Transformers/ScoreTransformer.php#L72-L84
    MULTIPLAYER_SCORE_INCLUDE: ClassVar[list[str]] = ["playlist_item_id", "room_id", "solo_score_id"]
    # torii: sub-includes de cosmeticos del jugador (aura + name colour + grupos). van juntos en TODA
    # lista de scores para que cualquier surface con scores (leaderboard de song-select, perfil,
    # multiplayer, gameplay solo) muestre el aura/color del jugador — el cliente necesita
    # equipped_aura (Path 1) o groups (Path 2 fallback) para resolver el aura. Costo: el resolver de
    # aura solo pega a la DB si el aura equipada es una POSEIDA (regalada), raro en un leaderboard.
    COSMETIC_USER_INCLUDES: ClassVar[list[str]] = ["user.equipped_aura", "user.equipped_name_colour", "user.groups"]
    MULTIPLAYER_BASE_INCLUDES: ClassVar[list[str]] = [
        "user.country",
        "user.cover",
        "user.team",
        *COSMETIC_USER_INCLUDES,
        *MULTIPLAYER_SCORE_INCLUDE,
    ]
    # current_user_attributes trae el flag de pin (is_pinned). Sin esto el front
    # nunca sabe que un score esta pinneado -> siempre muestra "Pin" y no se puede
    # despinnear desde el perfil.
    USER_PROFILE_INCLUDES: ClassVar[list[str]] = ["beatmap", "beatmapset", "user", "current_user_attributes", *COSMETIC_USER_INCLUDES]

    DEFAULT_SCORE_INCLUDES: ClassVar[list[str]] = ["user", "user.country", "user.cover", "user.team", *COSMETIC_USER_INCLUDES]

    # Basic fields
    beatmap_id: int = Field(index=True, foreign_key="beatmaps.id")
    id: int = Field(default=None, sa_column=Column(BigInteger, autoincrement=True, primary_key=True))
    rank: Rank
    type: str
    user_id: int = Field(
        default=None,
        sa_column=Column(
            BigInteger,
            ForeignKey("lazer_users.id"),
            index=True,
        ),
    )
    accuracy: float
    build_id: int | None = Field(default=None)
    client_version: str | None = Field(default=None, max_length=255)
    ended_at: datetime = Field(sa_column=Column(DateTime))
    has_replay: bool = Field(sa_column=Column(Boolean))
    max_combo: int
    passed: bool = Field(sa_column=Column(Boolean))
    pp: float = Field(default=0.0)
    # Snapshot of how this score moved the user's UserStatistics.pp at
    # submission time (pp_after - pp_before, clamped to >= 0). Read at
    # O(1) by the server-pulse endpoint instead of recomputing the
    # weighted contribution on every poll. Captured in
    # _process_statistics right after calculate_user_pp runs.
    account_pp_delta: float = Field(default=0.0)

    # Touchscreen play-style classification, populated by the
    # /touchscreen/classify endpoint on osu-performance-server when a TD-
    # tagged score with a replay is submitted (or by the bulk-classify
    # script for legacy scores). Values:
    #   0 = Unknown  → no verdict, treat as default (TD penalty applies)
    #   1 = Tap      → FairTouchScreen — pp recalc strips the TD mod
    #   2 = Drag     → drag-tap cheese, TD penalty applies (default)
    #   3 = Mixed    → treated as Drag (conservative)
    # The pp pipeline reads this column when calling /performance and
    # passes the corresponding string in the td_play_style field so the
    # perf server can drop TD from the mod list before calculating pp.
    # Stored as TINYINT to keep the column cheap on the 50k+ scores table.
    td_play_style: int = Field(default=0, sa_column=Column(SmallInteger, default=0))
    # Confidence in [0, 1] from the classifier. NULL when never run.
    # Kept separately from td_play_style so we can re-tune thresholds and
    # re-derive the verdict without re-parsing replays.
    td_classification_confidence: float | None = Field(default=None)
    # cantidad de pausas en medio de la play (el cliente manda los timestamps).
    # se usa para el nerf de pp por pausar. TINYINT alcanza de sobra.
    pause_count: int = Field(default=0, sa_column=Column(SmallInteger, default=0))
    started_at: datetime = Field(sa_column=Column(DateTime))
    total_score: int = Field(default=0, sa_column=Column(BigInteger))
    maximum_statistics: ScoreStatistics = Field(sa_column=Column(JSON), default_factory=dict)
    mods: list[APIMod] = Field(sa_column=Column(JSON))
    total_score_without_mods: int = Field(default=0, sa_column=Column(BigInteger))

    # solo
    classic_total_score: int | None = Field(default=0, sa_column=Column(BigInteger))
    preserve: bool = Field(default=True, sa_column=Column(Boolean))
    processed: bool = Field(default=False)
    ranked: bool = Field(default=False)

    # multiplayer
    playlist_item_id: OnDemand[int | None] = Field(default=None)
    room_id: OnDemand[int | None] = Field(default=None)

    @included
    @staticmethod
    async def best_id(
        session: AsyncSession,
        score: "Score",
    ) -> int | None:
        return await get_best_id(session, score.id)

    @included
    @staticmethod
    async def legacy_perfect(
        _session: AsyncSession,
        score: "Score",
    ) -> bool:
        await score.awaitable_attrs.beatmap
        return score.max_combo == score.beatmap.max_combo

    @included
    @staticmethod
    async def is_perfect_combo(
        _session: AsyncSession,
        score: "Score",
    ) -> bool:
        await score.awaitable_attrs.beatmap
        return score.max_combo == score.beatmap.max_combo

    @included
    @staticmethod
    async def ruleset_id(
        _session: AsyncSession,
        score: "Score",
    ) -> int:
        return int(score.gamemode)

    @included
    @staticmethod
    async def statistics(
        _session: AsyncSession,
        score: "Score",
    ) -> ScoreStatistics:
        stats = {
            HitResult.MISS: score.nmiss,
            HitResult.MEH: score.n50,
            HitResult.OK: score.n100,
            HitResult.GREAT: score.n300,
            HitResult.PERFECT: score.ngeki,
            HitResult.GOOD: score.nkatu,
        }
        if score.nlarge_tick_miss is not None:
            stats[HitResult.LARGE_TICK_MISS] = score.nlarge_tick_miss
        if score.nslider_tail_hit is not None:
            stats[HitResult.SLIDER_TAIL_HIT] = score.nslider_tail_hit
        if score.nsmall_tick_hit is not None:
            stats[HitResult.SMALL_TICK_HIT] = score.nsmall_tick_hit
        if score.nlarge_tick_hit is not None:
            stats[HitResult.LARGE_TICK_HIT] = score.nlarge_tick_hit
        if score.nlarge_bonus is not None:
            stats[HitResult.LARGE_BONUS] = score.nlarge_bonus
        if score.nsmall_bonus is not None:
            stats[HitResult.SMALL_BONUS] = score.nsmall_bonus
        return stats

    @ondemand
    @staticmethod
    async def beatmapset(
        _session: AsyncSession,
        score: "Score",
        includes: list[str] | None = None,
    ) -> BeatmapsetDict:
        await score.awaitable_attrs.beatmap
        return await BeatmapsetModel.transform(score.beatmap.beatmapset, includes=includes)

    # reorder beatmapset and beatmap
    # https://github.com/ppy/osu/blob/d8900defd34690de92be3406003fb3839fc0df1d/osu.Game/Online/API/Requests/Responses/SoloScoreInfo.cs#L111-L112
    @ondemand
    @staticmethod
    async def beatmap(
        _session: AsyncSession,
        score: "Score",
        includes: list[str] | None = None,
    ) -> BeatmapDict:
        await score.awaitable_attrs.beatmap
        return await BeatmapModel.transform(score.beatmap, includes=includes)

    @ondemand
    @staticmethod
    async def current_user_attributes(
        _session: AsyncSession,
        score: "Score",
    ) -> CurrentUserAttributes:
        return CurrentUserAttributes(pin=PinAttributes(is_pinned=bool(score.pinned_order), score_id=score.id))

    @ondemand
    @staticmethod
    async def position(
        session: AsyncSession,
        score: "Score",
    ) -> int | None:
        return await get_score_position_by_id(
            session,
            score.beatmap_id,
            score.id,
            mode=score.gamemode,
            user=score.user,
        )

    # @ondemand
    # @staticmethod
    # async def scores_around(
    #     session: AsyncSession, _score: "Score", playlist_id: int, room_id: int, is_playlist: bool
    # ) -> "ScoreAround | None":
    #     scores = (
    #         await session.exec(
    #             select(PlaylistBestScore).where(
    #                 PlaylistBestScore.playlist_id == playlist_id,
    #                 PlaylistBestScore.room_id == room_id,
    #                 ~User.is_restricted_query(col(PlaylistBestScore.user_id)),
    #                 col(PlaylistBestScore.score).has(col(Score.passed).is_(True)) if not is_playlist else True,
    #             )
    #         )
    #     ).all()

    #     higher_scores = []
    #     lower_scores = []
    #     for score in scores:
    #         total_score = score.score.total_score
    #         resp = await ScoreModel.transform(score.score, includes=ScoreModel.MULTIPLAYER_BASE_INCLUDES)
    #         if score.total_score > total_score:
    #             higher_scores.append(resp)
    #         elif score.total_score < total_score:
    #             lower_scores.append(resp)

    #     return ScoreAround(
    #         higher=MultiplayerScores(scores=higher_scores),
    #         lower=MultiplayerScores(scores=lower_scores),
    #     )

    @ondemand
    @staticmethod
    async def scores_around(
        session: AsyncSession, _score: "Score", playlist_id: int, room_id: int, is_playlist: bool
    ) -> "ScoreAround | None":
        include_failed = room_id is not None  # si es MP, incluimos failed
        passed_clause = True if include_failed else col(PlaylistBestScore.score).has(col(Score.passed).is_(True))

        scores = (
            await session.exec(
                select(PlaylistBestScore).where(
                    PlaylistBestScore.playlist_id == playlist_id,
                    PlaylistBestScore.room_id == room_id,
                    ~User.is_restricted_query(col(PlaylistBestScore.user_id)),
                    (True if is_playlist else passed_clause),
                )
            )
        ).all()

        current_total = _score.total_score  # 🔴 ESTE ERA ELbug

        higher_scores = []
        lower_scores = []

        for pbs in scores:
            resp = await ScoreModel.transform(
                pbs.score,
                includes=ScoreModel.MULTIPLAYER_BASE_INCLUDES,
            )

            if pbs.total_score > current_total:
                higher_scores.append(resp)
            elif pbs.total_score < current_total:
                lower_scores.append(resp)

        return ScoreAround(
            higher=MultiplayerScores(scores=higher_scores),
            lower=MultiplayerScores(scores=lower_scores),
        )

    @ondemand
    @staticmethod
    async def rank_country(
        session: AsyncSession,
        score: "Score",
    ) -> int | None:
        return (
            await get_score_position_by_id(
                session,
                score.beatmap_id,
                score.id,
                score.gamemode,
                score.user,
                type=LeaderboardType.COUNTRY,
            )
            or None
        )

    @ondemand
    @staticmethod
    async def rank_global(
        session: AsyncSession,
        score: "Score",
    ) -> int | None:
        return (
            await get_score_position_by_id(
                session,
                score.beatmap_id,
                score.id,
                mode=score.gamemode,
                user=score.user,
            )
            or None
        )

    @ondemand
    @staticmethod
    async def user(
        _session: AsyncSession,
        score: "Score",
        includes: list[str] | None = None,
        show_nsfw_media: bool = False,
    ) -> UserDict:
        user_resp = await UserModel.transform(
            score.user,
            ruleset=score.gamemode,
            includes=includes or [],
            show_nsfw_media=show_nsfw_media,
        )
        return UserModel.apply_nsfw_media_policy(user_resp, show_nsfw_media)

    @ondemand
    @staticmethod
    async def weight(
        session: AsyncSession,
        score: "Score",
    ) -> float | None:
        best_id = await get_best_id(session, score.id)
        if best_id:
            return calculate_pp_weight(best_id - 1)
        return None

    @ondemand
    @staticmethod
    async def legacy_total_score(
        _session: AsyncSession,
        _score: "Score",
    ) -> int:
        return 0

    @field_validator("maximum_statistics", mode="before")
    @classmethod
    def validate_maximum_statistics(cls, v):
        """Handle string keys in maximum_statistics field and convert them to HitResult enum"""
        if isinstance(v, dict):
            converted = {}
            for key, value in v.items():
                if isinstance(key, str):
                    try:
                        # Try to convert string to HitResult enum
                        enum_key = HitResult(key)
                        converted[enum_key] = value
                    except ValueError:
                        # If conversion fails, skip this key-value pair
                        continue
                else:
                    converted[key] = value
            return converted
        return v

    @field_serializer("maximum_statistics", when_used="json")
    def serialize_maximum_statistics(self, v):
        """Serialize maximum_statistics field, ensuring enum values are correctly converted to strings"""
        if isinstance(v, dict):
            serialized = {}
            for key, value in v.items():
                if hasattr(key, "value"):
                    # If it is an enum, use its value
                    serialized[key.value] = value
                else:
                    # Otherwise use key directly
                    serialized[str(key)] = value
            return serialized
        return v

    @field_serializer("rank", when_used="json")
    def serialize_rank(self, v):
        """Serialize grade, ensuring enum value is correctly converted to string"""
        if hasattr(v, "value"):
            return v.value
        return str(v)

    # optional
    # TODO: current_user_attributes


class Score(ScoreModel, table=True):
    __tablename__: str = "scores"
    __table_args__ = (
        Index("idx_score_user_mode_pinned", "user_id", "gamemode", "pinned_order", "id"),
        Index("idx_score_user_mode_pp", "user_id", "gamemode", "pp", "id"),
        Index("idx_score_user_mode_date", "user_id", "gamemode", "ended_at", "id"),
        # Standalone ended_at index for time-window scans that carry no
        # user_id/gamemode prefix (server-pulse play counts, sparkline,
        # recent plays). The composite above can't serve those because
        # ended_at isn't its leading column.
        Index("idx_score_ended_at", "ended_at"),
    )

    # ScoreStatistics
    n300: int = Field(exclude=True)
    n100: int = Field(exclude=True)
    n50: int = Field(exclude=True)
    nmiss: int = Field(exclude=True)
    ngeki: int = Field(exclude=True)
    nkatu: int = Field(exclude=True)
    nlarge_tick_miss: int | None = Field(default=None, exclude=True)
    nlarge_tick_hit: int | None = Field(default=None, exclude=True)
    nslider_tail_hit: int | None = Field(default=None, exclude=True)
    nsmall_tick_hit: int | None = Field(default=None, exclude=True)
    nlarge_bonus: int | None = Field(default=None, exclude=True)  # spinner bonus
    nsmall_bonus: int | None = Field(default=None, exclude=True)  # spinner spin
    gamemode: GameMode = Field(index=True)
    pinned_order: int = Field(default=0, exclude=True)
    map_md5: str | None = Field(default=None, max_length=32, index=True, exclude=True)

    @field_validator("gamemode", mode="before")
    @classmethod
    def validate_gamemode(cls, v):
        """Convert string to GameMode enum"""
        if isinstance(v, str):
            try:
                return GameMode(v)
            except ValueError:
                # If conversion fails, return default value
                return GameMode.OSU
        return v

    @field_serializer("gamemode", when_used="json")
    def serialize_gamemode(self, v):
        """Serialize game mode, ensuring enum value is correctly converted to string"""
        if hasattr(v, "value"):
            return v.value
        return str(v)

    # optional
    beatmap: Mapped[Beatmap] = Relationship()
    user: Mapped[User] = Relationship(sa_relationship_kwargs={"lazy": "joined"})
    best_score: Mapped[TotalScoreBestScore | None] = Relationship(
        back_populates="score",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
        },
    )
    ranked_score: Mapped[BestScore | None] = Relationship(
        back_populates="score",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
        },
    )
    playlist_item_score: Mapped[PlaylistBestScore | None] = Relationship(
        back_populates="score",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
        },
    )

    @property
    def is_perfect_combo(self) -> bool:
        return self.max_combo == self.beatmap.max_combo

    @property
    def replay_filename(self) -> str:
        return f"replays/{self.id}_{self.beatmap_id}_{self.user_id}_lazer_replay.osr"

    def get_display_score(self, mode: ScoringMode | None = None) -> int:
        """
        Get the display score for this score based on the scoring mode.

        Args:
            mode: The scoring mode to use. If None, uses the global setting.

        Returns:
            The display score in the requested scoring mode
        """
        if mode is None:
            mode = settings.scoring_mode

        return get_display_score(
            ruleset_id=int(self.gamemode),
            total_score=self.total_score,
            mode=mode,
            maximum_statistics=self.maximum_statistics,
        )

    async def to_resp(
        self,
        session: AsyncSession,
        api_version: int,
        includes: list[str] = [],
        show_nsfw_media: bool = False,
    ) -> "ScoreDict | LegacyScoreResp":
        from app.const import NEW_SCORE_FORMAT_VER
        if api_version >= NEW_SCORE_FORMAT_VER:
            return await ScoreModel.transform(self, includes=includes, show_nsfw_media=show_nsfw_media)
        return await LegacyScoreResp.from_db(session, self)

    async def delete(
        self,
        session: AsyncSession,
        storage_service: StorageService,
    ):
        if await self.awaitable_attrs.best_score:
            assert self.best_score is not None
            await self.best_score.delete(session)
            await session.refresh(self)
        if await self.awaitable_attrs.ranked_score:
            assert self.ranked_score is not None
            await self.ranked_score.delete(session)
            await session.refresh(self)
        if await self.awaitable_attrs.playlist_item_score:
            await session.delete(self.playlist_item_score)

        await storage_service.delete_file(self.replay_filename)
        await session.delete(self)


MultiplayScoreDict = ScoreModel.generate_typeddict(tuple(Score.MULTIPLAYER_BASE_INCLUDES))  # pyright: ignore[reportGeneralTypeIssues]


class LegacyStatistics(BaseModel):
    count_300: int
    count_100: int
    count_50: int
    count_miss: int
    count_geki: int | None = None
    count_katu: int | None = None


class LegacyScoreResp(UTCBaseModel):
    id: int
    best_id: int
    user_id: int
    accuracy: float
    mods: list[str]  # acronym
    score: int
    max_combo: int
    perfect: bool = False
    statistics: LegacyStatistics
    passed: bool
    pp: float
    rank: Rank
    created_at: datetime
    mode: GameMode
    mode_int: int
    replay: bool

    @classmethod
    async def from_db(cls, session: AsyncSession, score: "Score") -> "LegacyScoreResp":
        await score.awaitable_attrs.beatmap
        return cls(
            accuracy=score.accuracy,
            best_id=await get_best_id(session, score.id) or 0,
            created_at=score.started_at,
            id=score.id,
            max_combo=score.max_combo,
            mode=score.gamemode,
            mode_int=int(score.gamemode),
            mods=[m["acronym"] for m in score.mods],
            passed=score.passed,
            pp=score.pp,
            rank=score.rank,
            replay=score.has_replay,
            score=score.total_score,
            statistics=LegacyStatistics(
                count_300=score.n300,
                count_100=score.n100,
                count_50=score.n50,
                count_miss=score.nmiss,
                count_geki=score.ngeki or 0,
                count_katu=score.nkatu or 0,
            ),
            user_id=score.user_id,
            perfect=score.is_perfect_combo,
        )


class MultiplayerScores(RespWithCursor):
    scores: list[MultiplayScoreDict] = Field(default_factory=list)  # pyright: ignore[reportInvalidTypeForm]
    params: dict[str, Any] = Field(default_factory=dict)


class ScoreAround(SQLModel):
    higher: MultiplayerScores | None = None
    lower: MultiplayerScores | None = None


# Memo por request para get_best_id(). Serializar un score lo pide DOS veces (el
# campo best_id y el campo weight, que es el mismo numero derivado). Con llave
# debil sobre la AsyncSession se limpia solo cuando muere la sesion.
_best_id_memo: "WeakKeyDictionary[AsyncSession, dict[int, int | None]]" = WeakKeyDictionary()


async def get_best_id(session: AsyncSession, score_id: int) -> int | None:
    """Que numero de top play es este score para su dueño (1 = el mejor).

    Antes esto era un ROW_NUMBER() OVER (PARTITION BY user_id, gamemode) sobre un
    subquery SIN WHERE, y recien afuera se filtraba por score_id. O sea: se
    calculaba la ventana sobre la tabla best_scores ENTERA, de todos los usuarios y
    todos los modos, para quedarse con una sola fila. MySQL no puede empujar el
    score_id adentro de la ventana, asi que no habia forma de que fuera barato.

    Medido en prod sobre 8 dias: 1.077.933 llamadas, 57.211 segundos, o sea casi
    16 horas de CPU y el 8% del tiempo de vida del server en esta funcion sola. Es
    la consulta mas cara que tenemos por lejos, y como se llama una vez por cada
    play que se muestra, era el motivo de que los perfiles cargaran lento.

    La posicion de un score no necesita ninguna ventana: es cuantos scores mejores
    tiene ese jugador en ese modo, mas uno. Con el indice
    (user_id, gamemode, pp) eso es un rango de a lo sumo el largo de su top list.

    El desempate por score_id ASC es explicito a proposito. El ORDER BY pp DESC
    pelado no desempata, asi que ante dos pp identicos la ventana devolvia
    cualquier cosa. Ahora el orden es total y determinista, y ES EL MISMO que usa
    el camino batcheado de router/v2/user.py: los dos tienen que coincidir o el
    mismo score sale con dos numeros distintos segun por donde se pida.
    """
    # Memo por sesion, mismo patron que get_rank en statistics.py.
    #
    # Serializar UN score llama a get_best_id DOS veces: una por el campo best_id y
    # otra por weight, que es el mismo numero. Y una pagina de perfil son 50 scores.
    # Medido en una tanda de cargas de perfil: 2506 + 2496 consultas. Con el memo se
    # va la mitad exacta, sin cambiar nada de lo que se devuelve.
    memo = _best_id_memo.get(session)
    if memo is None:
        memo = _best_id_memo[session] = {}
    if score_id in memo:
        return memo[score_id]

    fila = (
        await session.exec(
            select(BestScore.user_id, BestScore.gamemode, BestScore.pp).where(BestScore.score_id == score_id)
        )
    ).first()
    if fila is None:
        memo[score_id] = None
        return None

    user_id, gamemode, pp = fila

    mejores = (
        await session.exec(
            select(func.count()).where(
                BestScore.user_id == user_id,
                BestScore.gamemode == gamemode,
                or_(
                    col(BestScore.pp) > pp,
                    and_(col(BestScore.pp) == pp, col(BestScore.score_id) < score_id),
                ),
            )
        )
    ).one()

    resultado = mejores + 1
    memo[score_id] = resultado
    return resultado

def _base_mode(mode: GameMode) -> GameMode:
    match mode:
        case GameMode.OSURX | GameMode.OSUAP:
            return GameMode.OSU
        case GameMode.TAIKORX:
            return GameMode.TAIKO
        case GameMode.FRUITSRX:
            return GameMode.FRUITS
        case _:
            return mode

def _global_gamemodes_including_automation(base: GameMode) -> list[GameMode]:
    modes = [
        base,
        base.to_special_mode(["RX"]),
        base.to_special_mode(["AP"]),
    ]
    return list(dict.fromkeys(modes))

async def _score_where(
    type: LeaderboardType,
    beatmap: int,
    mode: GameMode,
    mods: list[str] | None = None,
    user: User | None = None,
) -> list[ColumnElement[bool] | TextClause] | None:
    mods = mods or []

    wheres: list[ColumnElement[bool] | TextClause] = [
        col(TotalScoreBestScore.beatmap_id) == beatmap,
        ~User.is_restricted_query(col(TotalScoreBestScore.user_id)),
    ]

    # ---- gamemode filtering ----
    if type == LeaderboardType.GLOBAL and not mods:
        base = _base_mode(mode)
        wheres.append(col(TotalScoreBestScore.gamemode).in_(_global_gamemodes_including_automation(base)))
    else:
        wheres.append(col(TotalScoreBestScore.gamemode) == mode)

    # ---- leaderboards de pais y amigos ----
    # en osu! oficial son supporter-only, en torii los ve cualquiera. lo unico
    # que pedimos es estar logueado, porque hace falta el country_code y la
    # lista de follows del que pregunta.
    if type == LeaderboardType.FRIENDS:
        if user:
            subq = (
                select(DBRelationship.target_id)
                .where(DBRelationship.type == RelationshipType.FOLLOW, DBRelationship.user_id == user.id)
                .subquery()
            )
            wheres.append(col(TotalScoreBestScore.user_id).in_(select(subq.c.target_id)))
        else:
            return None

    elif type == LeaderboardType.COUNTRY:
        if user:
            wheres.append(col(TotalScoreBestScore.user).has(col(User.country_code) == user.country_code))
        else:
            return None

    elif type == LeaderboardType.TEAM and user:
        team_membership = await user.awaitable_attrs.team_membership
        if team_membership:
            team_id = team_membership.team_id
            wheres.append(col(TotalScoreBestScore.user).has(col(User.team_membership).has(TeamMember.team_id == team_id)))

    # ---- mods filtering ----
    if mods:
        automation_mods = [m for m in mods if m in ("RX", "AP")]

        if automation_mods:
            # RX/AP: allow any combination that contains RX/AP
            for i, m in enumerate(automation_mods):
                wheres.append(
                    text(f"JSON_CONTAINS(total_score_best_scores.mods, :mod_{i})")
                    .params(**{f"mod_{i}": json.dumps(m)})
                )
        else:
            # Exact mod-set match:
            # - score contains all requested mods
            # - requested mods contains all score mods
            wheres.append(
                text(
                    "JSON_CONTAINS(total_score_best_scores.mods, :w) "
                    "AND JSON_CONTAINS(:w, total_score_best_scores.mods)"
                ).params(w=json.dumps(mods))
            )

    # else: no mods filter → GLOBAL includes everything (including RX/AP) because we handled gamemode above.

    return wheres


async def get_leaderboard(
    session: AsyncSession,
    beatmap: int,
    mode: GameMode,
    type: LeaderboardType = LeaderboardType.GLOBAL,
    mods: list[str] | None = None,
    user: User | None = None,
    limit: int = 50,
) -> tuple[list[Score], Score | None, int]:
    mods = mods or []
    mode = mode.to_special_mode(mods)

    wheres = await _score_where(type, beatmap, mode, mods, user)
    if wheres is None:
        return [], None, 0
    count = (
        await session.exec(
            select(func.count(func.distinct(col(TotalScoreBestScore.user_id)))).where(*wheres)
        )
    ).one()
    scores: dict[int, Score] = {}
    max_score = sys.maxsize
    while limit > 0:
        query = (
            select(TotalScoreBestScore)
            # Eager-load the linked score + its beatmap so the per-row to_resp()
            # doesn't lazy-load them one query at a time (N+1 on the leaderboard:
            # ~3 queries per entry x 50 = 150+). Score.user is already
            # lazy="joined". One query (joins) instead.
            .options(joinedload(TotalScoreBestScore.score).joinedload(Score.beatmap))
            .where(*wheres, TotalScoreBestScore.total_score < max_score)
            .limit(limit)
            .order_by(col(TotalScoreBestScore.total_score).desc())
        )
        extra_need = 0
        for s in await session.exec(query):
            if s.user_id in scores:
                # fila duplicada del mismo usuario (varios gamemodes en el leaderboard "global
                # including automation": osu + relax + autopilot). la deduplicamos para el DISPLAY,
                # pero NO tocamos `count`: `count` ya es COUNT(DISTINCT user_id), asi que restar aca
                # descontaba de mas y el total se iba a negativo (el "#1 of -7").
                extra_need += 1
                if s.total_score > scores[s.user_id].total_score:
                    scores[s.user_id] = s.score
            else:
                scores[s.user_id] = s.score
            if max_score > s.total_score:
                max_score = s.total_score
        limit = extra_need

    result_scores = sorted(scores.values(), key=lambda u: u.total_score, reverse=True)
    user_score = None
    if user:
        self_query = (
            select(TotalScoreBestScore)
            .options(joinedload(TotalScoreBestScore.score).joinedload(Score.beatmap))
            .where(TotalScoreBestScore.user_id == user.id)
            .where(col(TotalScoreBestScore.beatmap_id) == beatmap)
            .order_by(col(TotalScoreBestScore.total_score).desc())
            .limit(1)
        )

        if type == LeaderboardType.GLOBAL and not mods:
            base = _base_mode(mode)
            self_query = self_query.where(
                col(TotalScoreBestScore.gamemode).in_(_global_gamemodes_including_automation(base))
            )
        else:
            self_query = self_query.where(col(TotalScoreBestScore.gamemode) == mode)
        if mods:
            # Check if this is an automation mod filter (Relax or Autopilot)
            automation_mods = [m for m in mods if m in ("RX", "AP")]

            if automation_mods:
                # For automation mods (RX, AP), show all combinations containing that mod
                for mod in automation_mods:
                    # JSON_CONTAINS checks if the value exists in the array
                    self_query = self_query.where(
                        text(f"JSON_CONTAINS(total_score_best_scores.mods, :mod_{mod})").params(**{f"mod_{mod}": json.dumps(mod)})
                    )
            else:
                # For regular mods, use exact matching
                self_query = self_query.where(
                    text(
                        "JSON_CONTAINS(total_score_best_scores.mods, :w)"
                        " AND JSON_CONTAINS(:w, total_score_best_scores.mods)"
                    ).params(w=json.dumps(mods))
                )
        user_bs = (await session.exec(self_query)).first()
        if user_bs:
            user_score = user_bs.score
        if user_score and user_score not in result_scores:
            result_scores.append(user_score)
    return result_scores, user_score, count


async def get_score_position_by_user(
    session: AsyncSession,
    beatmap: int,
    user: User,
    mode: GameMode,
    type: LeaderboardType = LeaderboardType.GLOBAL,
    mods: list[str] | None = None,
) -> int:
    wheres = await _score_where(type, beatmap, mode, mods, user=user)
    if wheres is None:
        return 0

    partition_cols = _partition_cols_for_ranking(type, mods)

    rownum = (
        func.row_number()
        .over(
            partition_by=partition_cols,
            order_by=(col(TotalScoreBestScore.total_score).desc(), col(TotalScoreBestScore.score_id).desc()),
        )
        .label("row_number")
    )

    subq = select(TotalScoreBestScore.user_id, rownum).where(*wheres).subquery()
    stmt = select(subq.c.row_number).where(subq.c.user_id == user.id)

    result = await session.exec(stmt)
    s = result.first()
    return s if s else 0


async def get_score_position_by_id(
    session: AsyncSession,
    beatmap: int,
    score_id: int,
    mode: GameMode,
    user: User | None = None,
    type: LeaderboardType = LeaderboardType.GLOBAL,
    mods: list[str] | None = None,
) -> int:
    wheres = await _score_where(type, beatmap, mode, mods, user=user)
    if wheres is None:
        return 0

    # el total_score de ESTE score (mi mejor en el beatmap)
    my_total_score = (
        await session.exec(
            select(TotalScoreBestScore.total_score).where(col(TotalScoreBestScore.score_id) == score_id)
        )
    ).one_or_none()
    if my_total_score is None:
        return 0

    # posicion = 1 + usuarios DISTINTOS con un mejor score que el mio.
    # Antes usaba ROW_NUMBER sobre filas crudas: si alguien arriba tuyo tenia varias filas (osu +
    # relax + autopilot en el leaderboard "global including automation") contaba cada una y te
    # empujaba la posicion hacia abajo (#2 real mostraba #3). Contar user_id distintos lo alinea
    # con el count y con la lista deduplicada del leaderboard.
    higher = (
        await session.exec(
            select(func.count(func.distinct(col(TotalScoreBestScore.user_id)))).where(
                *wheres, col(TotalScoreBestScore.total_score) > my_total_score
            )
        )
    ).one()
    return int(higher or 0) + 1

def _partition_cols_for_ranking(type: LeaderboardType, mods: list[str] | None):
    mods = mods or []
    if type == LeaderboardType.GLOBAL and not mods:
        return (col(TotalScoreBestScore.beatmap_id),)
    return (col(TotalScoreBestScore.beatmap_id), col(TotalScoreBestScore.gamemode))

async def get_user_best_score_in_beatmap(
    session: AsyncSession,
    beatmap: int,
    user: int,
    mode: GameMode | None = None,
) -> TotalScoreBestScore | None:
    return (
        await session.exec(
            select(TotalScoreBestScore)
            .where(
                TotalScoreBestScore.gamemode == mode if mode is not None else true(),
                TotalScoreBestScore.beatmap_id == beatmap,
                TotalScoreBestScore.user_id == user,
            )
            .order_by(col(TotalScoreBestScore.total_score).desc())
        )
    ).first()


async def get_user_best_score_with_mod_in_beatmap(
    session: AsyncSession,
    beatmap: int,
    user: int,
    mod: list[str],
    mode: GameMode | None = None,
) -> TotalScoreBestScore | None:
    return (
        await session.exec(
            select(TotalScoreBestScore)
            .where(
                TotalScoreBestScore.gamemode == mode if mode is not None else True,
                TotalScoreBestScore.beatmap_id == beatmap,
                TotalScoreBestScore.user_id == user,
                text(
                    "JSON_CONTAINS(total_score_best_scores.mods, :w)"
                    " AND JSON_CONTAINS(:w, total_score_best_scores.mods)"
                ).params(w=json.dumps(mod)),
            )
            .order_by(col(TotalScoreBestScore.total_score).desc())
        )
    ).first()


async def get_user_first_scores(
    session: AsyncSession,
    user_id: int,
    mode: GameMode,
    limit: int = 5,
    offset: int = 0,
    cursor_id: int | None = None,
) -> list[TotalScoreBestScore]:
    # Alias for the subquery table
    s2 = aliased(TotalScoreBestScore)

    query = select(TotalScoreBestScore).where(
        TotalScoreBestScore.user_id == user_id,
        TotalScoreBestScore.gamemode == mode,
    )

    # Subquery for NOT EXISTS
    # Check if there is a score with same beatmap, same mode, but higher total_score
    subq = select(1).where(
        s2.beatmap_id == TotalScoreBestScore.beatmap_id,
        s2.gamemode == TotalScoreBestScore.gamemode,
        s2.total_score > TotalScoreBestScore.total_score,
    )

    query = query.where(~exists(subq))

    if cursor_id:
        query = query.where(TotalScoreBestScore.score_id < cursor_id)

    query = query.order_by(col(TotalScoreBestScore.score_id).desc()).limit(limit).offset(offset)

    result = await session.exec(query)
    return list(result.all())


async def get_user_first_score_count(session: AsyncSession, user_id: int, mode: GameMode) -> int:
    s2 = aliased(TotalScoreBestScore)
    query = select(func.count()).where(
        TotalScoreBestScore.user_id == user_id,
        TotalScoreBestScore.gamemode == mode,
    )
    subq = select(1).where(
        s2.beatmap_id == TotalScoreBestScore.beatmap_id,
        s2.gamemode == TotalScoreBestScore.gamemode,
        s2.total_score > TotalScoreBestScore.total_score,
    )
    query = query.where(~exists(subq))

    result = await session.exec(query)
    return result.one()


async def get_user_best_pp_in_beatmap(
    session: AsyncSession,
    beatmap: int,
    user: int,
    mode: GameMode,
) -> BestScore | None:
    return (
        await session.exec(
            select(BestScore).where(
                BestScore.beatmap_id == beatmap,
                BestScore.user_id == user,
                BestScore.gamemode == mode,
            )
        )
    ).first()


async def calculate_user_pp(session: AsyncSession, user_id: int, mode: GameMode) -> tuple[float, float]:
    pp_sum = 0
    acc_sum = 0
    bps = await get_user_best_pp(session, user_id, mode)
    for i, s in enumerate(bps):
        pp_sum += calculate_weighted_pp(s.pp, i)
        acc_sum += calculate_weighted_acc(s.acc, i)
    if len(bps):
        # https://github.com/ppy/osu-queue-score-statistics/blob/c538ae/osu.Server.Queues.ScoreStatisticsProcessor/Helpers/UserTotalPerformanceAggregateHelper.cs#L41-L45
        acc_sum *= 100 / (20 * (1 - math.pow(0.95, len(bps))))
    acc_sum = clamp(acc_sum, 0.0, 100.0)
    return pp_sum, acc_sum


async def get_user_best_pp(
    session: AsyncSession,
    user: int,
    mode: GameMode,
    limit: int = 1000,
) -> Sequence[BestScore]:
    return (
        await session.exec(
            select(BestScore)
            .where(BestScore.user_id == user, BestScore.gamemode == mode)
            .order_by(col(BestScore.pp).desc())
            .limit(limit)
        )
    ).all()


# https://github.com/ppy/osu-queue-score-statistics/blob/master/osu.Server.Queues.ScoreStatisticsProcessor/Helpers/PlayValidityHelper.cs
def get_play_length(score: "Score", beatmap_length: int):
    speed_rate = get_speed_rate(score.mods)
    # Guard: a maliciously-crafted (or just buggy) DT/HT/custom-rate
    # mod payload can land here with speed_change == 0, producing a
    # ZeroDivisionError that aborts the entire process_score path
    # mid-flight. The downstream effect is the spectator never gets
    # a Redis publish for the score, the rank/PP popup never fires
    # client-side, and the user thinks their play vanished. Clamp
    # to a tiny positive rate so the math still works; the play is
    # almost certainly going to fail the >8s validity check anyway.
    if speed_rate <= 0:
        speed_rate = 0.01
    length = beatmap_length / speed_rate
    return int(min(length, (score.ended_at - score.started_at).total_seconds()))


def calculate_playtime(score: "Score", beatmap_length: int) -> tuple[int, bool]:
    total_length = get_play_length(score, beatmap_length)
    total_obj_hited = (
        score.n300
        + score.n100
        + score.n50
        + score.ngeki
        + score.nkatu
        + (score.nlarge_tick_hit or 0)
        + (score.nlarge_tick_miss or 0)
        + (score.nslider_tail_hit or 0)
        + (score.nsmall_tick_hit or 0)
    )
    total_obj = 0
    for statistics, count in (score.maximum_statistics or {}).items():
        if not isinstance(statistics, HitResult):
            statistics = HitResult(statistics)
        if statistics.is_scorable():
            total_obj += count

    return total_length, score.passed or (
        total_length > 8 and score.total_score >= 5000 and total_obj_hited >= min(0.1 * total_obj, 20)
    )


async def process_score(
    user: User,
    beatmap_id: int,
    ranked: bool,
    score_token: ScoreToken,
    info: SoloScoreSubmissionInfo,
    session: AsyncSession,
) -> Score:
    # Normalizar los mods ANTES de tocar nada mas.
    #
    # Una sala de multi que EXIGE un mod mas el mismo mod elegido por el jugador
    # llegan como dos entradas del mismo acronimo. osu! oficial nunca guarda un
    # score asi (su cliente no te deja elegir un mod ya requerido), por eso su
    # cliente puede indexar los mods por tipo sin miedo. El nuestro hacia lo
    # mismo y se caia: ScoreMultiplierCalculator hace ToDictionary por tipo, que
    # tira ArgumentException con claves repetidas, asi que pasar el mouse por
    # encima de ese score en el leaderboard volteaba el juego entero.
    #
    # Se FUSIONAN, no se descarta uno. Quedarse con el primero borraria settings
    # que el jugador si uso: si la sala pide DA con AR 9 y el jugador agrega CS
    # 4, jugo con las dos cosas y el score tiene que decir las dos, o el pp se
    # calcula sobre una dificultad que nunca existio.
    #
    # Ante la misma clave gana la PRIMERA aparicion, que es la que impone la
    # sala: un requisito de la sala no lo puede pisar el jugador. Las claves que
    # la sala no fijo las aporta el jugador.
    if info.mods:
        fusionados: dict[str, dict] = {}
        orden: list[str] = []
        for mod in info.mods:
            acronimo = mod.get("acronym")
            if acronimo not in fusionados:
                fusionados[acronimo] = dict(mod)
                orden.append(acronimo)
                continue
            previo = fusionados[acronimo]
            # OJO con el nombre: llamar `settings` a esta variable la hace local a
            # TODA la funcion y rompe el `settings` global de config que se usa mas
            # abajo, con UnboundLocalError. Paso justo eso y tumbo el submit de
            # scores por 10 horas.
            ajustes = dict(mod.get("settings") or {})
            ajustes.update(previo.get("settings") or {})  # el primero pisa
            if ajustes:
                previo["settings"] = ajustes
        if len(orden) != len(info.mods):
            normalizados = [fusionados[a] for a in orden]
            logger.warning(
                "mods duplicados en el score de {user_id} en {beatmap_id}: {antes} -> {despues}",
                user_id=user.id,
                beatmap_id=beatmap_id,
                antes=info.mods,
                despues=normalizados,
            )
            info.mods = normalizados

    gamemode = GameMode.from_int(info.ruleset_id).to_special_mode(info.mods)
    mods_ranked = mods_can_get_pp(int(gamemode), info.mods)
    effective_ranked = ranked and mods_ranked

    logger.info(
        "Creating score for user {user_id} | beatmap={beatmap_id} ruleset={ruleset} passed={passed} total={total} ranked={ranked} mods_ranked={mods_ranked}",
        user_id=user.id,
        beatmap_id=beatmap_id,
        ruleset=gamemode,
        passed=info.passed,
        total=info.total_score,
        ranked=effective_ranked,
        mods_ranked=mods_ranked,
    )

    is_multiplayer = score_token.room_id is not None or score_token.playlist_item_id is not None
    preserve = True if is_multiplayer else bool(info.passed)

    # Server-authoritative total score (mod-multiplier rebalance). Recompute from the
    # raw, mod-free score so the new multipliers apply uniformly regardless of client
    # version, and a tampered/stale-client total can't inflate the leaderboard. Gated by
    # config so it can be switched on once the recalc has validated the multiplier values.
    total_score_value = info.total_score
    if settings.server_authoritative_total_score and info.total_score_without_mods is not None:
        bm = score_token.beatmap
        base_cs = float(getattr(bm, "cs", None) or 5.0)
        base_od = float(getattr(bm, "accuracy", None) or 5.0)
        total_score_value = recompute_total_score(
            info.ruleset_id, info.mods, info.total_score_without_mods, base_cs, base_od
        )
        if info.total_score and abs(total_score_value - info.total_score) > max(1000, 0.02 * total_score_value):
            logger.warning(
                "total_score mismatch user={user_id} beatmap={beatmap_id}: client={client} server={server} mods={mods}",
                user_id=user.id,
                beatmap_id=beatmap_id,
                client=info.total_score,
                server=total_score_value,
                mods=info.mods,
            )

    score = Score(
        accuracy=info.accuracy,
        max_combo=info.max_combo,
        mods=info.mods,
        passed=info.passed,
        rank=info.rank,
        pause_count=len(info.pauses),
        total_score=total_score_value,
        total_score_without_mods=info.total_score_without_mods,
        beatmap_id=beatmap_id,
        client_version=score_token.client_version,
        ended_at=utcnow(),
        gamemode=gamemode,
        started_at=score_token.created_at,
        user_id=user.id,
        preserve=preserve,
        map_md5=score_token.beatmap.checksum,
        has_replay=False,
        type="solo",
        n300=info.statistics.get(HitResult.GREAT, 0),
        n100=info.statistics.get(HitResult.OK, 0),
        n50=info.statistics.get(HitResult.MEH, 0),
        nmiss=info.statistics.get(HitResult.MISS, 0),
        ngeki=info.statistics.get(HitResult.PERFECT, 0),
        nkatu=info.statistics.get(HitResult.GOOD, 0),
        nlarge_tick_miss=info.statistics.get(HitResult.LARGE_TICK_MISS, 0),
        nsmall_tick_hit=info.statistics.get(HitResult.SMALL_TICK_HIT, 0),
        nlarge_tick_hit=info.statistics.get(HitResult.LARGE_TICK_HIT, 0),
        nslider_tail_hit=info.statistics.get(HitResult.SLIDER_TAIL_HIT, 0),
        nlarge_bonus=info.statistics.get(HitResult.LARGE_BONUS, 0),
        nsmall_bonus=info.statistics.get(HitResult.SMALL_BONUS, 0),
        playlist_item_id=score_token.playlist_item_id,
        room_id=score_token.room_id,
        maximum_statistics=info.maximum_statistics,
        processed=False,   # 👈 IMPORTANTE: acá SIEMPRE False
        ranked=effective_ranked,
    )

    session.add(score)
    await session.flush()  # Sends INSERT to DB and gets auto-increment ID without committing
    # Bind the token to this score in the SAME transaction so that if anything
    # fails before the commit, the token remains unbound and no orphan score exists.
    score_token.score_id = score.id
    await session.commit()  # Atomically commits: score row + score_token.score_id
    # NOTE: do NOT set score.processed = True here. The spectator's
    # ScoreProcessedSubscriber.RegisterForSingleScoreAsync uses processed=1 as
    # the signal that PP + statistics have finished computing, and it will
    # immediately broadcast UserScoreProcessed to the client when the flag is
    # set. If we flip it now (right after INSERT, before _process_score_pp /
    # _process_statistics have run), the client races _process_user, calls
    # /me, gets stale stats, and the rank/PP popup either doesn't appear or
    # shows a 0-delta update. The flag is flipped at the end of process_user(),
    # right before the redis "score:processed" publish.
    await session.refresh(score)

    return score



_RELAX_AP_MODES = frozenset({GameMode.OSURX, GameMode.TAIKORX, GameMode.FRUITSRX, GameMode.OSUAP})
_OSU_STANDARD_MODES = frozenset({GameMode.OSU, GameMode.OSURX, GameMode.OSUAP})


def _compute_effective_od_cs(mods: list[APIMod], base_od: float, base_cs: float) -> tuple[float, float]:
    """Pure helper: apply DA / HR / EZ adjustments to base OD and CS."""
    da_settings: dict = {}
    for mod in mods:
        if mod["acronym"] == "DA":
            da_settings = mod.get("settings", {}) or {}
            break

    od_override = da_settings.get("overall_difficulty")
    cs_override = da_settings.get("circle_size")

    if isinstance(od_override, (int, float)):
        eff_od = float(od_override)
    else:
        eff_od = base_od
        for mod in mods:
            if mod["acronym"] == "HR":
                eff_od = min(10.0, eff_od * 1.4)
                break
            elif mod["acronym"] == "EZ":
                eff_od = eff_od * 0.5
                break

    if isinstance(cs_override, (int, float)):
        eff_cs = float(cs_override)
    else:
        eff_cs = base_cs
        for mod in mods:
            if mod["acronym"] == "HR":
                eff_cs = min(10.0, eff_cs * 1.3)
                break
            elif mod["acronym"] == "EZ":
                eff_cs = eff_cs * 0.5
                break

    return eff_od, eff_cs


async def _get_effective_od_cs(score: "Score", session: AsyncSession) -> tuple[float, float] | None:
    """Return (effective_od, effective_cs) after mod adjustments (DA / HR / EZ).
    Returns None when the beatmap row cannot be found."""
    mods = score.mods
    da_settings: dict = {}
    for mod in mods:
        if mod["acronym"] == "DA":
            da_settings = mod.get("settings", {}) or {}
            break

    od_override = da_settings.get("overall_difficulty")
    cs_override = da_settings.get("circle_size")

    # DA fully overrides both — no DB lookup needed.
    if isinstance(od_override, (int, float)) and isinstance(cs_override, (int, float)):
        return _compute_effective_od_cs(mods, 0.0, 0.0)

    beatmap = (await session.exec(select(Beatmap).where(Beatmap.id == score.beatmap_id))).first()
    if beatmap is None:
        return None

    return _compute_effective_od_cs(mods, float(beatmap.accuracy), float(beatmap.cs))


async def _process_score_pp(score: "Score", session: AsyncSession, redis: Redis, fetcher: "Fetcher") -> str | None:
    if score.pp != 0:
        logger.debug(
            "Skipping PP calculation for score {score_id} | already set {pp:.2f}",
            score_id=score.id,
            pp=score.pp,
        )
        return

    # Flashlight custom-setting checks run FIRST — before mods_can_get_pp — so the
    # warning fires even when other mods (e.g. rate-changed DT) would fail the
    # whitelist check.
    if score.gamemode in _OSU_STANDARD_MODES:
        # FL settings floor: only vanilla Flashlight earns pp. If any setting is
        # changed from its default (size, delay, combo_based_size), award 0pp and warn.
        for _m in score.mods:
            if _m["acronym"] == "FL":
                _fl_settings = _m.get("settings") or {}
                _fl_size = _fl_settings.get("size_multiplier")
                _fl_delay = _fl_settings.get("follow_delay")
                _fl_combo = _fl_settings.get("combo_based_size")
                _fl_non_vanilla = (
                    (_fl_size is not None and abs(_fl_size - 1.0) > 0.001)
                    or (_fl_delay is not None and abs(_fl_delay - 1.0) > 0.001)
                    or (_fl_combo is not None and _fl_combo is not True)
                )
                if _fl_non_vanilla:
                    logger.debug(
                        "Skipping PP for score {score_id} | FL non-vanilla settings size={size} delay={delay} combo={combo}",
                        score_id=score.id,
                        size=_fl_size,
                        delay=_fl_delay,
                        combo=_fl_combo,
                    )
                    return f"fl_non_vanilla:size={_fl_size},delay={_fl_delay},combo={_fl_combo}"
                break

    can_get_pp = score.passed and score.ranked and mods_can_get_pp(int(score.gamemode), score.mods)
    if not can_get_pp:
        logger.debug(
            "Skipping PP calculation for score {score_id} | passed={passed} ranked={ranked} mods={mods}",
            score_id=score.id,
            passed=score.passed,
            ranked=score.ranked,
            mods=score.mods,
        )
        return

    # Accuracy floor for relax / autopilot modes: < 75% acc → 0 pp.
    if score.gamemode in _RELAX_AP_MODES and score.accuracy < 0.75:
        logger.debug(
            "Skipping PP for score {score_id} | RX/AP acc {acc:.1%} < 75%",
            score_id=score.id,
            acc=score.accuracy,
        )
        return f"rx_acc_too_low:{score.accuracy:.1%}"

    # ✅ 14★ cap (stars AFTER mods). If the map is > 14 stars, it awards 0pp.
    # NOTE: requires: from app.database.beatmap import calculate_beatmap_attributes
    try:
        attrs = await calculate_beatmap_attributes(
            score.beatmap_id,
            score.gamemode,
            score.mods,
            redis,
            fetcher,
        )
        if attrs.star_rating > 14:
            logger.warning(
                "High star rating detected %.2f (score_id=%s) - continuing PP calc",
                attrs.star_rating,
                score.id,
            )
    except Exception as e:
        # Don't block the PP pipeline if star calc fails; proceed to normal PP calc.
        logger.warning(
            "Failed to calculate star_rating for score {score_id} | err={err}",
            score_id=score.id,
            err=str(e),
        )

    pp, successed = await pre_fetch_and_calculate_pp(score, session, redis, fetcher)
    if not successed:
        await redis.rpush("score:need_recalculate", score.id)  # pyright: ignore[reportGeneralTypeIssues]
        logger.warning("Queued score {score_id} for PP recalculation", score_id=score.id)
        return

    score.pp = pp
    logger.info("Calculated PP for score {score_id} | pp={pp:.2f}", score_id=score.id, pp=pp)

    user_id = score.user_id
    beatmap_id = score.beatmap_id
    previous_pp_best = await get_user_best_pp_in_beatmap(session, beatmap_id, user_id, score.gamemode)
    if previous_pp_best is None or score.pp > previous_pp_best.pp:
        # Count existing top plays in this mode BEFORE adding the new one — the
        # points gate below needs the prior history count.
        existing_top_plays = (
            await session.exec(
                select(func.count())
                .select_from(BestScore)
                .where(BestScore.user_id == user_id, BestScore.gamemode == score.gamemode)
            )
        ).one()

        best_score = BestScore(
            user_id=user_id,
            score_id=score.id,
            beatmap_id=beatmap_id,
            gamemode=score.gamemode,
            pp=score.pp,
            acc=score.accuracy,
        )
        session.add(best_score)
        await session.delete(previous_pp_best) if previous_pp_best else None

        try:
            from app.service.suspicious_alert_service import SuspiciousAlertService

            usuario = await session.get(User, user_id)
            if usuario is not None:
                await SuspiciousAlertService.maybe_record_high_pp_alert(session, score, usuario)
        except Exception:
            logger.exception("fallo la alerta de pp alto para el score {sid}", sid=score.id)
        logger.info(
            "Updated PP best for user {user_id} | score_id={score_id} pp={pp:.2f}",
            user_id=user_id,
            score_id=score.id,
            pp=score.pp,
        )

        # Torii top-play points are awarded later in _process_statistics, once the
        # account-pp delta is known — the reward scales by the play's RANK in the
        # user's tops + the pp it added to their account total, not by raw pp here.



async def _process_score_events(score: "Score", session: AsyncSession):
    total_users = (await session.exec(select(func.count()).select_from(User))).one()
    rank_global = await get_score_position_by_id(
        session,
        score.beatmap_id,
        score.id,
        mode=score.gamemode,
        user=score.user,
    )

    if rank_global == 0 or total_users == 0:
        logger.debug(
            "Skipping event creation for score {score_id} | rank_global={rank_global} total_users={total_users}",
            score_id=score.id,
            rank_global=rank_global,
            total_users=total_users,
        )
        return
    logger.debug(
        "Processing events for score {score_id} | rank_global={rank_global} total_users={total_users}",
        score_id=score.id,
        rank_global=rank_global,
        total_users=total_users,
    )
    beatmap_url = f"{str(settings.web_url).rstrip('/')}/beatmaps/{score.beatmap_id}"
    if rank_global <= min(math.ceil(float(total_users) * 0.01), 50):
        rank_event = Event(
            created_at=utcnow(),
            type=EventType.RANK,
            user_id=score.user_id,
            user=score.user,
        )
        rank_event.event_payload = {
            "scorerank": score.rank.value,
            "rank": rank_global,
            "mode": score.gamemode.readable(),
            "beatmap": {
                "title": (
                    f"{score.beatmap.beatmapset.artist} - {score.beatmap.beatmapset.title} [{score.beatmap.version}]"
                ),
                "url": beatmap_url,
            },
            "user": {
                "username": score.user.username,
                "url": settings.web_url + "users/" + str(score.user.id),
            },
        }
        session.add(rank_event)
        logger.info(
            "Registered rank event for user {user_id} | score_id={score_id} rank={rank}",
            user_id=score.user_id,
            score_id=score.id,
            rank=rank_global,
        )
    if rank_global == 1:
        displaced_score = (
            await session.exec(
                select(TotalScoreBestScore)
                .where(
                    TotalScoreBestScore.beatmap_id == score.beatmap_id,
                    TotalScoreBestScore.gamemode == score.gamemode,
                )
                .order_by(col(TotalScoreBestScore.total_score).desc())
                .limit(1)
                .offset(1)
            )
        ).first()
        if displaced_score and displaced_score.user_id != score.user_id:
            username = (await session.exec(select(User.username).where(User.id == displaced_score.user_id))).one()

            # evento al #feed de discord: nuevo #1 destronando a alguien (los #1 sin
            # competencia previa no se anuncian, seria spam en mapas nuevos)
            try:
                from app.service.discord_feed import notify_new_number_one

                notify_new_number_one(
                    username=score.user.username,
                    user_id=score.user_id,
                    map_title=(
                        f"{score.beatmap.beatmapset.artist} - {score.beatmap.beatmapset.title} "
                        f"[{score.beatmap.version}]"
                    ),
                    beatmap_url=beatmap_url,
                    pp=score.pp,
                    accuracy=score.accuracy,
                    dethroned_username=username,
                )
            except Exception:
                pass

            rank_lost_event = Event(
                created_at=utcnow(),
                type=EventType.RANK_LOST,
                user_id=displaced_score.user_id,
            )
            rank_lost_event.event_payload = {
                "mode": score.gamemode.readable(),
                "beatmap": {
                    "title": (
                        f"{score.beatmap.beatmapset.artist} - {score.beatmap.beatmapset.title} "
                        f"[{score.beatmap.version}]"
                    ),
                    "url": beatmap_url,
                },
                "user": {
                    "username": username,
                    # Use the FK column directly. `displaced_score.user` would
                    # trigger a lazy load on a relationship that is not preloaded
                    # by `_process_score_events_background`'s joinedload chain,
                    # which crashes with `greenlet_spawn has not been called`.
                    "url": settings.web_url + "users/" + str(displaced_score.user_id),
                },
            }
            session.add(rank_lost_event)
            logger.info(
                "Registered rank lost event | displaced_user={user_id} new_score_id={score_id}",
                user_id=displaced_score.user_id,
                score_id=score.id,
            )
    logger.debug(
        "Event processing committed for score {score_id}",
        score_id=score.id,
    )


async def _process_statistics(
    session: AsyncSession,
    redis: Redis,
    user: User,
    score: "Score",
    score_token: int,
    beatmap_length: int,
    beatmap_status: BeatmapRankStatus,
):
    mods_ranked = mods_can_get_pp(int(score.gamemode), score.mods)
    has_pp = (beatmap_status.has_pp() or settings.enable_all_beatmap_pp) and mods_ranked
    ranked = (beatmap_status.ranked() or settings.enable_all_beatmap_pp) and mods_ranked
    has_leaderboard = (beatmap_status.has_leaderboard() or settings.enable_all_beatmap_leaderboard) and mods_ranked

    mod_for_save = mod_to_save(score.mods)
    should_check_best_scores = score.passed and (ranked or has_leaderboard)
    previous_score_best = None
    previous_score_best_mod = None

    if should_check_best_scores:
        previous_score_best = await get_user_best_score_in_beatmap(session, score.beatmap_id, user.id, score.gamemode)
        previous_score_best_mod = await get_user_best_score_with_mod_in_beatmap(
            session, score.beatmap_id, user.id, mod_for_save, score.gamemode
        )
    else:
        logger.debug(
            "Skipping best-score lookups for score {score_id} | passed={passed} ranked={ranked} leaderboard={leaderboard}",
            score_id=score.id,
            passed=score.passed,
            ranked=ranked,
            leaderboard=has_leaderboard,
        )

    logger.debug(
        "Existing best scores for user {user_id} | global={global_id} mod={mod_id}",
        user_id=user.id,
        global_id=previous_score_best.score_id if previous_score_best else None,
        mod_id=previous_score_best_mod.score_id if previous_score_best_mod else None,
    )
    add_to_db = False
    mouthly_playcount = (
        await session.exec(
            select(MonthlyPlaycounts).where(
                MonthlyPlaycounts.user_id == user.id,
                MonthlyPlaycounts.year == date.today().year,
                MonthlyPlaycounts.month == date.today().month,
            )
        )
    ).first()
    if mouthly_playcount is None:
        mouthly_playcount = MonthlyPlaycounts(user_id=user.id, year=date.today().year, month=date.today().month)
        add_to_db = True
    statistics = None
    for i in await user.awaitable_attrs.statistics:
        if i.mode == score.gamemode.value:
            statistics = i
            break
    if statistics is None:
        raise ValueError(f"User {user.id} does not have statistics for mode {score.gamemode.value}")

    # pc, pt, tth, tts
    # Get display scores based on configured scoring mode
    current_display_score = score.get_display_score()
    previous_display_score = previous_score_best.score.get_display_score() if previous_score_best else 0

    statistics.total_score += current_display_score
    difference = current_display_score - previous_display_score
    logger.debug(
        "Score delta computed for {score_id}: {difference} (display score in {mode} mode)",
        score_id=score.id,
        difference=difference,
        mode=settings.scoring_mode,
    )
    if difference > 0 and score.passed and ranked:
        match score.rank:
            case Rank.X:
                statistics.grade_ss += 1
            case Rank.XH:
                statistics.grade_ssh += 1
            case Rank.S:
                statistics.grade_s += 1
            case Rank.SH:
                statistics.grade_sh += 1
            case Rank.A:
                statistics.grade_a += 1
        if previous_score_best is not None:
            match previous_score_best.rank:
                case Rank.X:
                    statistics.grade_ss -= 1
                case Rank.XH:
                    statistics.grade_ssh -= 1
                case Rank.S:
                    statistics.grade_s -= 1
                case Rank.SH:
                    statistics.grade_sh -= 1
                case Rank.A:
                    statistics.grade_a -= 1
        statistics.ranked_score += difference
        statistics.level_current = calculate_score_to_level(statistics.total_score)
        statistics.maximum_combo = max(statistics.maximum_combo, score.max_combo)
    if score.passed and has_leaderboard:
        # Case 1: No best score record exists, add directly
        # Case 2: Best score record exists but no record for this mod combination, add new record
        if previous_score_best is None or previous_score_best_mod is None:
            session.add(
                TotalScoreBestScore(
                    user_id=user.id,
                    beatmap_id=score.beatmap_id,
                    gamemode=score.gamemode,
                    score_id=score.id,
                    total_score=score.total_score,
                    rank=score.rank,
                    mods=mod_for_save,
                )
            )
            logger.info(
                "Created new best score entry for user {user_id} | score_id={score_id} mods={mods}",
                user_id=user.id,
                score_id=score.id,
                mods=mod_for_save,
            )

        # Case 3: Both best score and mod combination records exist, and they are the same record; update if score is higher
        elif previous_score_best.score_id == previous_score_best_mod.score_id and difference > 0:
            previous_score_best.total_score = score.total_score
            previous_score_best.rank = score.rank
            previous_score_best.score_id = score.id
            logger.info(
                "Updated existing best score for user {user_id} | score_id={score_id} total={total}",
                user_id=user.id,
                score_id=score.id,
                total=score.total_score,
            )

        # Case 4: Both best score and mod combination records exist, but are different records
        elif previous_score_best.score_id != previous_score_best_mod.score_id:
            # Update global best record (if new score is higher)
            if difference > 0:
                # The if below will definitely trigger. Set high score to this score, delete self to prevent duplicate score_id
                logger.info(
                    "Replacing global best score for user {user_id} | old_score_id={old_score_id}",
                    user_id=user.id,
                    old_score_id=previous_score_best.score_id,
                )
                await session.delete(previous_score_best)

            # Update mod-specific best record (if new score is higher)
            mod_diff = score.total_score - previous_score_best_mod.total_score
            if mod_diff > 0:
                previous_score_best_mod.total_score = score.total_score
                previous_score_best_mod.rank = score.rank
                previous_score_best_mod.score_id = score.id
                logger.info(
                    "Replaced mod-specific best for user {user_id} | mods={mods} score_id={score_id}",
                    user_id=user.id,
                    mods=mod_for_save,
                    score_id=score.id,
                )

    playtime, is_valid = calculate_playtime(score, beatmap_length)

    # Active-only ranking: a PASSED score keeps the user ranked + active in this
    # mode for the next 30 days (independent of pp / playtime validity, matching
    # the migration backfill's passed=1). This is atomic with the rest of the
    # stats update committed by process_user, so a player returning from 30d+
    # inactivity re-enters the dense ranking on this very submit.
    if score.passed:
        statistics.last_played = score.ended_at

    if is_valid:
        await redis.xadd(f"score:existed_time:{score_token}", {"time": playtime})
        statistics.play_count += 1
        mouthly_playcount.count += 1
        statistics.play_time += playtime

        await _process_beatmap_playcount(session, score.beatmap_id, user.id)

        logger.debug(
            "Recorded playtime {playtime}s for score {score_id} (user {user_id})",
            playtime=playtime,
            score_id=score.id,
            user_id=user.id,
        )
    else:
        logger.debug(
            "Playtime {playtime}s for score {score_id} did not meet validity checks",
            playtime=playtime,
            score_id=score.id,
        )
    nlarge_tick_miss = score.nlarge_tick_miss or 0
    nsmall_tick_hit = score.nsmall_tick_hit or 0
    nlarge_tick_hit = score.nlarge_tick_hit or 0
    statistics.count_100 += score.n100 + score.nkatu
    statistics.count_300 += score.n300 + score.ngeki
    statistics.count_50 += score.n50
    statistics.count_miss += score.nmiss
    statistics.total_hits += (
        score.n300
        + score.n100
        + score.n50
        + score.ngeki
        + score.nkatu
        + nlarge_tick_hit
        + nlarge_tick_miss
        + nsmall_tick_hit
    )

    if score.gamemode in {GameMode.FRUITS, GameMode.FRUITSRX}:
        statistics.count_miss += nlarge_tick_miss
        statistics.count_50 += nsmall_tick_hit
        statistics.count_100 += nlarge_tick_hit

    if score.passed and has_pp:
        # Snapshot the user's pre-submission account pp so we can store
        # the delta this score introduced. Negative deltas (which can
        # only happen if calculate_user_pp shifts due to a concurrent
        # mutation, never from this score being added) are clamped to 0
        # so the column remains a "gain" semantically.
        pp_before = float(statistics.pp or 0.0)
        statistics.pp, statistics.hit_accuracy = await calculate_user_pp(session, statistics.user_id, score.gamemode)
        # The submission pipeline can re-run _process_statistics for the
        # same score (verify-leaderboard inline retry, background retry).
        # On those reruns pp_before is already the post-first-run value,
        # so the recomputed delta is 0 and would clobber the real gain
        # we captured the first time. Take max(existing, new) so the
        # column is monotone — once we've seen the genuine account
        # impact, subsequent idempotent reruns can't unset it.
        new_delta = max(0.0, float(statistics.pp) - pp_before)
        score.account_pp_delta = max(float(score.account_pp_delta or 0.0), new_delta)

        # Los milestones se pagan al cruzarlos, no por ya estar arriba.
        try:
            from app.models.torii_points import PP_MILESTONES

            if pp_before < float(statistics.pp) and float(statistics.pp) >= min(PP_MILESTONES):
                from app.service.points_service import award_pp_milestones

                await award_pp_milestones(
                    session,
                    statistics.user_id,
                    score.gamemode,
                    pp_before,
                    float(statistics.pp),
                )
        except Exception as _ms_err:
            logger.warning("PP milestone award failed for user {}: {}", statistics.user_id, _ms_err)

        # Torii points: reward a new top play, scaled by where it ranks in the
        # user's best plays + how much it added to their account pp. The trigger is
        # this score having become the user's best on its map (a new PB), detected
        # by a BestScore row pointing at it; a random low PB ranks far down and
        # pays ~nothing. account_pp_delta (the pp this play added to the total) is
        # known here, so this is the right place — not _process_score_pp.
        try:
            from app.service.points_service import award_top_play, is_main_mode

            is_new_best = (
                await session.exec(select(BestScore.score_id).where(BestScore.score_id == score.id))
            ).first() is not None
            if is_new_best:
                better = (
                    await session.exec(
                        select(func.count())
                        .select_from(BestScore)
                        .where(
                            BestScore.user_id == statistics.user_id,
                            BestScore.gamemode == score.gamemode,
                            BestScore.pp > score.pp,
                        )
                    )
                ).one()
                total_tops = (
                    await session.exec(
                        select(func.count())
                        .select_from(BestScore)
                        .where(
                            BestScore.user_id == statistics.user_id,
                            BestScore.gamemode == score.gamemode,
                        )
                    )
                ).one()
                # statistics.pp is already the new total for this mode.
                main_mode = await is_main_mode(
                    session,
                    statistics.user_id,
                    score.gamemode,
                    float(statistics.pp or 0.0),
                )
                await award_top_play(
                    session,
                    statistics.user_id,
                    better + 1,
                    total_tops,
                    int(round(score.account_pp_delta or 0.0)),
                    score.id,
                    score.gamemode,
                    main_mode=main_mode,
                )
        except Exception as _tp_err:
            logger.warning("Top-play points award failed for user {}: {}", statistics.user_id, _tp_err)

    if add_to_db:
        session.add(mouthly_playcount)
        logger.debug(
            "Created monthly playcount record for user {user_id} ({year}-{month})",
            user_id=user.id,
            year=mouthly_playcount.year,
            month=mouthly_playcount.month,
        )


async def _process_beatmap_playcount(session: AsyncSession, beatmap_id: int, user_id: int):
    beatmap_playcount = (
        await session.exec(
            select(BeatmapPlaycounts).where(
                BeatmapPlaycounts.beatmap_id == beatmap_id,
                BeatmapPlaycounts.user_id == user_id,
            )
        )
    ).first()
    if beatmap_playcount is None:
        beatmap_playcount = BeatmapPlaycounts(beatmap_id=beatmap_id, user_id=user_id, playcount=1)
        session.add(beatmap_playcount)
        logger.debug(
            "Created beatmap playcount record for user {user_id} on beatmap {beatmap_id}",
            user_id=user_id,
            beatmap_id=beatmap_id,
        )
    else:
        beatmap_playcount.playcount += 1
        logger.debug(
            "Incremented beatmap playcount for user {user_id} on beatmap {beatmap_id} to {count}",
            user_id=user_id,
            beatmap_id=beatmap_id,
            count=beatmap_playcount.playcount,
        )

async def _anticheat_no_replay_fallback(engine, score_id: int):
    """Wait up to 30s for the replay to upload. If it doesn't, run the
    anti-cheat check with replay=None so score_consistency still catches
    blatant payload tampering on scores submitted without a replay."""
    for _ in range(60):
        await asyncio.sleep(0.5)
        async with AsyncSession(engine) as s:
            row = (await s.exec(select(Score.has_replay).where(Score.id == score_id))).first()
            if row:
                return  # replay arrived, the replay-upload path will trigger the analysis
    await _submit_to_anticheat_background(engine, score_id)


def _parse_osu_breaks(raw: str) -> list[dict[str, float]]:
    """Pull break periods out of a raw .osu file's [Events] section.

    Break events are lines in [Events] of the form ``2,start,end`` or
    ``Break,start,end`` (event type 2 = break), with times in beatmap
    milliseconds. We parse them by hand rather than via osupyparser so the
    anticheat path doesn't depend on that library's object model — a few
    string splits over an already-cached file is cheap and self-contained.

    Returns a list of ``{"start_ms": float, "end_ms": float}`` dicts, which
    slitwrist uses to excuse the legitimate mid-map clock seek produced by
    the client's "skip break" feature. On any malformed input we just skip
    the offending line, so a weird beatmap can never break submission.
    """
    breaks: list[dict[str, float]] = []
    in_events = False

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("["):
            in_events = stripped.lower() == "[events]"
            continue
        if not in_events or stripped.startswith("//"):
            continue

        parts = stripped.split(",")
        if len(parts) < 3:
            continue
        if parts[0].strip() not in ("2", "Break"):
            continue

        try:
            start = float(parts[1])
            end = float(parts[2])
        except ValueError:
            continue

        if end > start:
            breaks.append({"start_ms": start, "end_ms": end})

    return breaks


async def _fetch_beatmap_breaks(beatmap) -> list[dict[str, float]]:
    """Best-effort fetch + parse of a beatmap's break periods for the
    anticheat payload. Fully fail-open: any error (no beatmap, fetch miss,
    timeout, parse error) yields an empty list, which simply disables the
    break-skip exemption on slitwrist's side rather than affecting the score.
    """
    beatmap_id = getattr(beatmap, "id", None)
    if not beatmap_id:
        return []

    try:
        import asyncio

        from app.dependencies.database import get_redis
        from app.dependencies.fetcher import get_fetcher

        fetcher = await get_fetcher()
        redis = get_redis()
        # The .osu is normally already warm in Redis (PP/difficulty calc
        # fetched it during this very submission). Bound it anyway so a cold
        # cache + slow mirror can't stall the advisory anticheat task.
        raw = await asyncio.wait_for(fetcher.get_or_fetch_beatmap_raw(redis, beatmap_id), timeout=8.0)
        if not raw:
            return []
        return _parse_osu_breaks(raw)
    except Exception:
        return []


async def _submit_to_anticheat_background(engine, score_id: int):
    """Fire-and-forget call to the external anti-cheat service (torii-slitwrist).

    Runs after the score is fully committed + processed, so anything that
    fails in here is purely advisory — never blocks submission, never rolls
    back state, never modifies the score row itself. The only persistent
    side effect is a SuspiciousAlert row when the service returns a
    verdict that crosses the alert threshold (configurable via
    settings.anticheat_critical_creates_alert).

    Lifecycle:
      1. Skip immediately if the feature is disabled (anticheat_url empty).
      2. Open a fresh AsyncSession — the request session is closed by now.
      3. Load Score + User + Beatmap joined.
      4. Compute the user's trust factor (5 small per-user queries).
      5. Build the payloads for the anti-cheat service.
      6. Optionally pull the replay binary from storage (gated by
         anticheat_include_replay AND score.has_replay).
      7. POST to the service, await verdict (timeout from config).
      8. If verdict ∈ {suspicious, critical} AND the alert flag is on,
         insert a SuspiciousAlert row deduplicated by score_id.

    Why this lives here (next to _process_score_events_background) rather
    than as a separate task chain: both have identical infrastructure needs
    (fresh session, joined load, error isolation). Keeping them adjacent
    in the file documents the "post-submit fan-out" pattern in one place.
    """
    from app.config import settings as _cfg
    from app.service.anticheat_client import submit_for_analysis as _ac_submit
    from app.service.trust_factor import compute_trust_factor as _ac_trust
    from app.database.suspicious_alert import SuspiciousAlert as _ACSuspiciousAlert

    if not (getattr(_cfg, "anticheat_url", "") or "").strip():
        return  # feature disabled — no log, this is the default for forks

    try:
        async with AsyncSession(engine) as bg_session:
            score_ = (
                await bg_session.exec(
                    select(Score)
                    .where(Score.id == score_id)
                    .options(
                        joinedload(Score.user),
                        joinedload(Score.beatmap),
                    )
                )
            ).first()

            if score_ is None or score_.user is None:
                return

            user = score_.user
            beatmap = score_.beatmap

            try:
                trust = await _ac_trust(bg_session, user.id)
            except Exception as trust_err:
                logger.warning(
                    "anticheat: trust factor computation failed for user {}: {}",
                    user.id, trust_err,
                )
                # Degrade gracefully — still call the service with a neutral
                # trust factor rather than skipping the check entirely.
                from app.service.trust_factor import TrustFactorBreakdown as _TFB
                trust = _TFB(
                    base=50.0, account_age_days=0, account_age_bonus=0.0,
                    play_count=0, play_count_bonus=0.0,
                    supporter_bonus=0.0, staff_bonus=0.0,
                    distinct_ip_count=0, distinct_ip_penalty=0.0,
                    prior_alert_count=0, prior_alert_penalty=0.0,
                    has_restriction_history=False, restriction_penalty=0.0,
                    final_score=50.0,
                )

            # Build the wire payload. Mirror the contract documented in
            # app/service/anticheat_client.py — adding a field here means
            # updating that file's docstring and the consuming service.
            score_payload = {
                "score_id": score_.id,
                "user_id": user.id,
                "beatmap_id": beatmap.id if beatmap else None,
                "passed": bool(score_.passed),
                "rank": getattr(score_, "rank", None),
                "total_score": int(getattr(score_, "total_score", 0) or 0),
                "max_combo": int(getattr(score_, "max_combo", 0) or 0),
                "accuracy": float(getattr(score_, "accuracy", 0.0) or 0.0),
                "n300": int(getattr(score_, "n300", 0) or 0),
                "n100": int(getattr(score_, "n100", 0) or 0),
                "n50": int(getattr(score_, "n50", 0) or 0),
                "nmiss": int(getattr(score_, "nmiss", 0) or 0),
                "ngeki": int(getattr(score_, "ngeki", 0) or 0),
                "nkatu": int(getattr(score_, "nkatu", 0) or 0),
                "mods": getattr(score_, "mods", None) or [],
                # ruleset_id is an @staticmethod on the Score model, not a
                # column, so getattr returns the bound method. Derive it
                # from the gamemode column instead.
                "ruleset_id": int(score_.gamemode) if score_.gamemode is not None else 0,
                "pp": float(getattr(score_, "pp", 0.0) or 0.0),
                "total_length_ms": (
                    int(beatmap.total_length * 1000)
                    if beatmap and getattr(beatmap, "total_length", None) is not None
                    else None
                ),
                "submitted_at": (
                    score_.ended_at.isoformat()
                    if getattr(score_, "ended_at", None) is not None
                    else None
                ),
            }

            # HWID context — opaque struct shipped to the detection
            # service. The values are summary metadata, not a contract
            # the public stub interprets. Failures here are non-fatal.
            hwid_summary: dict[str, Any] = {
                "known_hwids": [],
                "correlated_user_ids": [],
                "correlated_account_count": 0,
            }
            try:
                from app.service import hwid_tracker as _hwid
                from app.dependencies.database import get_redis as _gr
                _r = _gr()
                user_hwids = await _hwid.hwids_for(_r, user.id)
                correlated: set[int] = set()
                for h in user_hwids:
                    for u in await _hwid.users_for(_r, h):
                        if u != user.id:
                            correlated.add(u)
                # Cap the list we send across the wire; the count is the
                # signal that matters, the IDs are just for debug context.
                hwid_summary = {
                    "known_hwids": user_hwids[:8],
                    "correlated_user_ids": sorted(correlated)[:32],
                    "correlated_account_count": len(correlated),
                }
            except Exception as hwid_err:
                logger.debug(
                    "anticheat: hwid correlation lookup failed for user {}: {}",
                    user.id, hwid_err,
                )

            # Per-user behavioural baseline + submission velocity.
            # Baseline = how this user normally plays (shape of scores).
            # Velocity = how often they submit (rate). Both are
            # computed in one place so the queries share the DB session.
            baseline_payload: dict[str, Any] = {}
            velocity_payload: dict[str, Any] = {}
            try:
                from app.service.behavioral_profile import (
                    compute_submission_velocity,
                    compute_user_baseline,
                )
                baseline = await compute_user_baseline(
                    bg_session,
                    user_id=user.id,
                    gamemode=int(getattr(score_, "gamemode", 0) or 0),
                    exclude_score_id=score_.id,
                )
                baseline_payload = baseline.to_dict()
                velocity = await compute_submission_velocity(
                    bg_session,
                    user_id=user.id,
                    beatmap_id=score_.beatmap_id,
                )
                velocity_payload = velocity.to_dict()
            except Exception as bp_err:
                logger.debug(
                    "anticheat: baseline/velocity computation failed for user {}: {}",
                    user.id, bp_err,
                )

            user_payload = {
                "trust_factor": trust.final_score,
                "account_age_days": trust.account_age_days,
                "play_count_global": trust.play_count,
                "is_supporter": bool(trust.supporter_bonus > 0),
                "distinct_ip_count": trust.distinct_ip_count,
                "prior_alerts": trust.prior_alert_count,
                "has_restriction_history": trust.has_restriction_history,
                "hwid": hwid_summary,
                "baseline": baseline_payload,
                "velocity": velocity_payload,
            }

            beatmap_payload = {
                "object_count": (
                    (int(getattr(beatmap, "count_circles", 0) or 0)
                     + int(getattr(beatmap, "count_sliders", 0) or 0)
                     + int(getattr(beatmap, "count_spinners", 0) or 0))
                    if beatmap else None
                ),
                "circle_count": int(getattr(beatmap, "count_circles", 0) or 0) if beatmap else None,
                "slider_count": int(getattr(beatmap, "count_sliders", 0) or 0) if beatmap else None,
                "spinner_count": int(getattr(beatmap, "count_spinners", 0) or 0) if beatmap else None,
                "cs": float(getattr(beatmap, "cs", 0) or 0) if beatmap else None,
                "od": float(getattr(beatmap, "od", 0) or 0) if beatmap else None,
                "ar": float(getattr(beatmap, "ar", 0) or 0) if beatmap else None,
                "hp": float(getattr(beatmap, "hp", 0) or 0) if beatmap else None,
                # Break periods so slitwrist can excuse the legitimate mid-map
                # clock seek from the client's "skip break" feature instead of
                # flagging the resulting frame gap. Server-authoritative (parsed
                # from the .osu here), so it can't be spoofed by the client.
                "breaks": await _fetch_beatmap_breaks(beatmap) if beatmap else [],
            }

            # Replay forwarding: gated on both has_replay (DB flag — false
            # by default at time of writing because replay upload isn't
            # wired yet) AND the config toggle. Once replay storage is in,
            # this branch transparently activates.
            replay_b64: str | None = None
            include_replay = bool(getattr(_cfg, "anticheat_include_replay", True))
            if include_replay and getattr(score_, "has_replay", False):
                try:
                    import base64 as _b64
                    from app.dependencies.storage import get_storage_service
                    storage = get_storage_service()
                    raw = await storage.read_file(score_.replay_filename)
                    if raw:
                        replay_b64 = _b64.b64encode(raw).decode("ascii")
                except Exception as replay_err:
                    # Replay missing or storage failure is non-fatal —
                    # behavioural detectors still run on the score+user
                    # payload alone.
                    logger.warning(
                        "anticheat: failed to load replay for score {}: {}",
                        score_id, replay_err,
                    )

            verdict = await _ac_submit(
                score_payload=score_payload,
                user_payload=user_payload,
                beatmap_payload=beatmap_payload,
                replay_b64=replay_b64,
            )

            if verdict is None:
                # Service unreachable / errored — already logged inside
                # anticheat_client. Persist an "error" placeholder so the
                # admin can see this score was attempted but failed.
                await _upsert_anticheat_analysis(
                    bg_session,
                    score_id=score_.id,
                    user_id=user.id,
                    verdict_payload=None,
                    trust_applied=trust.final_score,
                    replay_was_available=replay_b64 is not None,
                    error="service unreachable or errored",
                )
                await bg_session.commit()
                return

            verdict_label = str(verdict.get("verdict", "ok")).lower()
            should_alert = bool(getattr(_cfg, "anticheat_critical_creates_alert", True))

            # Persist the full verdict for the admin replay browser. One
            # row per score; subsequent re-analyses overwrite it. Done
            # before the suspicious-alert branch so the cache is filled
            # regardless of whether an alert row was also created.
            await _upsert_anticheat_analysis(
                bg_session,
                score_id=score_.id,
                user_id=user.id,
                verdict_payload=verdict,
                trust_applied=trust.final_score,
                replay_was_available=replay_b64 is not None,
                error=None,
            )

            # Only "critical" reaches the moderator alert feed for now.
            # "suspicious" still gets cached in score_anticheat_analysis
            # for admin browsing but does NOT page the mods.
            if should_alert and verdict_label == "critical":
                # Fingerprint dedups per (score_id), so re-runs of the
                # background task for the same score (e.g. retries on
                # transient errors) never create duplicate alerts.
                fingerprint = f"anticheat:score:{score_.id}"
                existing = (
                    await bg_session.exec(
                        select(_ACSuspiciousAlert).where(
                            _ACSuspiciousAlert.fingerprint == fingerprint
                        )
                    )
                ).first()
                if existing is None:
                    detectors = verdict.get("detectors_fired", []) or []
                    reasons = verdict.get("reasons", []) or []
                    title = (
                        f"Anti-cheat [{verdict_label}] · score {score_.id} · "
                        + (", ".join(str(d) for d in detectors[:3]) or "pattern")
                    )[:200]
                    body_lines = [
                        "Triggered by torii-slitwrist external detection service.",
                        "",
                        f"Confidence: {float(verdict.get('confidence', 0.0) or 0.0):.2f}",
                        f"Trust factor applied: {trust.final_score:.1f}",
                        f"Detectors fired: {list(detectors)}",
                        "",
                    ]
                    if reasons:
                        body_lines.append("Reasons:")
                        for r in list(reasons)[:10]:
                            if isinstance(r, dict):
                                body_lines.append(
                                    f"  - [{r.get('severity', '?')}] "
                                    f"{r.get('detector', '?')}: {r.get('code', '?')}"
                                )
                            else:
                                body_lines.append(f"  - {r}")

                    alert_row = _ACSuspiciousAlert(
                        kind="anticheat_score",
                        severity=verdict_label,
                        fingerprint=fingerprint,
                        user_id=user.id,
                        score_id=score_.id,
                        beatmap_id=beatmap.id if beatmap else None,
                        title=title,
                        body="\n".join(body_lines),
                        payload={
                            "verdict": verdict,
                            "trust_breakdown": trust.to_dict(),
                        },
                    )
                    bg_session.add(alert_row)
            # Single commit at the end of the background task: covers
            # both the upserted analysis row and any new alert row.
            await bg_session.commit()

    except Exception as exc:
        # Belt-and-suspenders: any uncaught exception in the entire
        # background task tree gets swallowed here. Score processing is
        # already done; this task cannot affect it.
        logger.warning(
            "Background anticheat submit failed for score {}: {}",
            score_id, exc,
        )


async def _upsert_anticheat_analysis(
    session: "AsyncSession",
    *,
    score_id: int,
    user_id: int,
    verdict_payload: dict[str, Any] | None,
    trust_applied: float,
    replay_was_available: bool,
    error: str | None,
) -> None:
    """Insert or replace the cached analysis row for a score.

    Called from `_submit_to_anticheat_background` so every analysis the
    external service performs is persisted, regardless of whether it
    crossed the alert threshold. Failures are swallowed — the analysis
    cache is best-effort.
    """
    from app.database.score_anticheat_analysis import ScoreAnticheatAnalysis as _SAA
    try:
        existing = (
            await session.exec(
                select(_SAA).where(_SAA.score_id == score_id)
            )
        ).first()
        verdict_label = (
            str(verdict_payload.get("verdict", "ok")).lower()
            if verdict_payload else "errored"
        )
        confidence = float(verdict_payload.get("confidence", 0.0) or 0.0) if verdict_payload else 0.0
        detectors = list(verdict_payload.get("detectors_fired", []) or []) if verdict_payload else []
        reasons = list(verdict_payload.get("reasons", []) or []) if verdict_payload else []
        metrics = dict(verdict_payload.get("metrics", {}) or {}) if verdict_payload else {}
        now = utcnow()
        if existing is None:
            session.add(
                _SAA(
                    score_id=score_id,
                    user_id=user_id,
                    verdict=verdict_label,
                    confidence=confidence,
                    trust_factor_applied=trust_applied,
                    detectors_fired=detectors,
                    reasons=reasons,
                    metrics=metrics,
                    replay_was_available=replay_was_available,
                    analyzer_version="1",
                    error=error,
                    analyzed_at=now,
                )
            )
        else:
            existing.user_id = user_id
            existing.verdict = verdict_label
            existing.confidence = confidence
            existing.trust_factor_applied = trust_applied
            existing.detectors_fired = detectors
            existing.reasons = reasons
            existing.metrics = metrics
            existing.replay_was_available = replay_was_available
            existing.analyzer_version = "1"
            existing.error = error
            existing.analyzed_at = now
            session.add(existing)
    except Exception as e:
        logger.warning(
            "anticheat: failed to upsert analysis row for score {}: {}",
            score_id, e,
        )


async def _process_score_events_background(engine, score_id: int):
    """
    🔧 CHANGED (NEW):
    Run _process_score_events in the background using a fresh DB session.
    This prevents blocking the score submission response path.
    """
    try:
        # Create a NEW session so we don't reuse the request session
        async with AsyncSession(engine) as bg_session:
            # IMPORTANT: Load everything needed for event payload
            # _process_score_events uses:
            # - score.beatmap.beatmapset.artist/title
            # - score.beatmap.version
            # - score.user.username
            score_ = (
                await bg_session.exec(
                    select(Score)
                    .where(Score.id == score_id)
                    .options(
                        joinedload(Score.user),
                        joinedload(Score.beatmap).joinedload(Beatmap.beatmapset),
                    )
                )
            ).first()

            if score_ is None:
                logger.warning(
                    "Background event processing: score {score_id} not found, skipping",
                    score_id=score_id,
                )
                return

            await _process_score_events(score_, bg_session)
            await bg_session.commit()

            logger.info(
                "Background event processing finished for score {score_id}",
                score_id=score_id,
            )

    except Exception:
        # Don't crash the server if background event task fails
        logger.exception(
            "Background event processing failed for score {score_id}",
            score_id=score_id,
        )

async def process_user(
    session: AsyncSession,
    redis: Redis,
    fetcher: "Fetcher",
    user: User,
    score: "Score",
    score_token: int,
    beatmap_length: int,
    beatmap_status: BeatmapRankStatus,
):
    score_id = score.id
    user_id = user.id
    logger.info(
        "Processing score {score_id} for user {user_id} on beatmap {beatmap_id}",
        score_id=score_id,
        user_id=user_id,
        beatmap_id=score.beatmap_id,
    )

    # Torii points: first GENUINE ranked play of the (UTC) day pays a small daily
    # bonus + streak. Gated on a real ranked score (NOT a client-asserted "passed"
    # with no score, nor an unranked / loved / graveyard map) so the daily bonus
    # can't be farmed by fake/zero-effort submissions. Idempotent per day inside
    # the service; reduced for relax/autopilot via the gamemode.
    from app.models.torii_points import DAILY_PLAY_MIN_TOTAL_SCORE

    if score.passed and score.ranked and (score.total_score or 0) >= DAILY_PLAY_MIN_TOTAL_SCORE:
        try:
            from app.service.points_service import award_daily_play

            await award_daily_play(session, user_id, score.gamemode)
        except Exception as _pts_err:
            logger.warning("Daily-play points award failed for score {}: {}", score.id, _pts_err)

    # ---- Critical path (must be done before response) ----
    _pp_zero_reason = await _process_score_pp(score, session, redis, fetcher)
    await session.commit()
    await session.refresh(score)
    await session.refresh(user)
    alert_username = user.username
    alert_join_date = user.join_date

    # Send ToriiHalo private message if score was zeroed due to Flashlight/accuracy rules.
    #
    # torii: una sola vez por score. submit_score corre process_user inline y, si
    # despues no ve la fila del leaderboard (lo normal cuando el score NO mejoro
    # tu mejor, que es justo el caso de un relax con poca acc y 0pp), programa una
    # segunda pasada en background "por las dudas". Esa pasada recalcula la razon
    # y mandaba el mismo PM de nuevo. La llave NX en redis hace que el segundo
    # intento no mande nada.
    if _pp_zero_reason and not await redis.set(f"toriihalo:zero_pp_pm:{score_id}", 1, nx=True, ex=86400):
        _pp_zero_reason = None

    if _pp_zero_reason:
        try:
            from app.router.notification.banchobot import bot as _toriihalo
            from app.database.user import User as _User
            from app.dependencies.database import with_db as _with_db
            _reason_code = _pp_zero_reason.split(":")[0]
            _reason_data = _pp_zero_reason.split(":", 1)[1] if ":" in _pp_zero_reason else ""
            _pm_msgs: dict[str, str] = {
                "rx_acc_too_low": (
                    f"Your score gave 0pp! Your accuracy was {_reason_data}. "
                    "Relax and Autopilot scores need at least 75% accuracy to earn pp."
                ),
                "fl_non_vanilla": (
                    "Your score gave 0pp! You changed Flashlight settings. "
                    "Only default Flashlight settings earn pp. "
                    "Set size, delay and combo-based size back to their defaults."
                ),
            }
            _pm_text = _pm_msgs.get(
                _reason_code,
                "Your score gave 0pp. It did not meet the requirements to earn pp.",
            )
            async with _with_db() as _pm_session:
                _pm_user = await _pm_session.get(_User, user_id)
                if _pm_user is not None:
                    _pm_channel = await _toriihalo._ensure_pm_channel(_pm_user, _pm_session)
                    if _pm_channel is not None:
                        await _toriihalo._send_message(_pm_channel, _pm_text, _pm_session)
        except Exception as _notif_err:
            logger.warning("Failed to send 0pp warning PM for score {}: {}", score.id, _notif_err)

    await _process_statistics(
        session,
        redis,
        user,
        score,
        score_token,
        beatmap_length,
        beatmap_status,
    )

    # Commit stats/leaderboard/etc first, so any reads after publish see updated data
    await session.commit()

    try:
        alert_result = await SuspiciousAlertService.maybe_record_suspicious_score_alert(
            session,
            redis,
            score=score,
            user_id=user_id,
            username=alert_username,
            join_date=alert_join_date,
        )
        if alert_result.created:
            await session.commit()
    except Exception as suspicious_err:
        logger.warning("Failed to record suspicious score alert for score {}: {}", score.id, suspicious_err)

    await redis.publish(
    "osu-channel:user:invalidate",
    json.dumps({"user_id": user_id})
    )

    # Mark the score as fully processed *now*, after PP + statistics + leaderboard
    # commits. The spectator (ScoreProcessedSubscriber) uses processed=1 as its
    # immediate-fire shortcut for UserScoreProcessed, so the flip must happen
    # AFTER all stat updates are visible — otherwise the client refetches /me
    # before the new PP/rank are committed and the popup shows no change.
    score.processed = True
    await session.commit()

    # Notify client AFTER commit
    await redis.publish("osu-channel:score:processed", f'{{"ScoreId": {score_id}}}')

    # ---- Non-critical path (background) ----
    # 🔧 CHANGED: Run event creation async so score submission returns fast.
    # This avoids blocking the submit pipeline on expensive rank calculations / event generation.
    # engine = session.get_bind()  # AsyncEngine bound to this session
    asyncio.create_task(_process_score_events_background(db_engine, score_id))

    # Anti-cheat fan-out. Replay arrives on a separate endpoint after
    # this one returns, so the primary trigger lives in the replay
    # upload handler (router/lio.py). This here is the no-replay
    # fallback: wait briefly, and if no replay shows up, run a
    # score-shape-only check so we still catch payload tampering.
    if score.passed:
        asyncio.create_task(_anticheat_no_replay_fallback(db_engine, score_id))
    # asyncio.create_task(_process_score_events_background(engine, score_id))

    logger.info(
        "Finished processing score {score_id} for user {user_id} (events scheduled in background)",
        score_id=score_id,
        user_id=user_id,
    )
