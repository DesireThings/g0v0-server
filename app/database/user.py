from datetime import datetime, timedelta
import json
from typing import TYPE_CHECKING, ClassVar, Literal, NotRequired, TypedDict, overload

from app.config import settings
from app.models.beatmap import BeatmapRankStatus
from app.models.notification import NotificationName
from app.models.score import GameMode
from app.models.torii_auras import (
    AURA_SENTINEL_DEFAULT,
    AURA_SENTINEL_NONE,
    is_aura_allowed_for_user,
    resolve_effective_aura_id,
)
from app.models.torii_groups import build_groups, is_currently_supporting
from app.models.user import Country, Page
from app.path import STATIC_DIR
from app.utils import utcnow

from ._base import DatabaseModel, OnDemand, included, ondemand
from .achievement import UserAchievement, UserAchievementResp
from .auth import TotpKeys
from .beatmap_playcounts import BeatmapPlaycounts
from .counts import CountResp, MonthlyPlaycounts, ReplayWatchedCount
from .daily_challenge import DailyChallengeStats, DailyChallengeStatsResp
from .events import Event
from .notification import Notification, UserNotification
from .rank_history import RankHistory, RankHistoryResp, RankTop
from .relationship import RelationshipModel
from .statistics import UserStatistics, UserStatisticsModel
from .team import Team, TeamMember
from .user_account_history import UserAccountHistory, UserAccountHistoryResp, UserAccountHistoryType
from .user_preference import DEFAULT_ORDER, UserPreference

from pydantic import field_validator
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import Mapped
from sqlmodel import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Field,
    Relationship,
    col,
    exists,
    func,
    select,
    text,
)
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from .favourite_beatmapset import FavouriteBeatmapset
    from .matchmaking import MatchmakingUserStats
    from .relationship import Relationship, RelationshipDict
    from .statistics import UserStatisticsDict


class Kudosu(TypedDict):
    available: int
    total: int


class RankHighest(TypedDict):
    rank: int
    updated_at: datetime


class UserProfileCover(TypedDict):
    # `custom_url` and `id` are NotRequired AND nullable: legacy rows store
    # them as JSON null instead of omitting the keys (e.g. user id=281 has
    # `{"id": null, "url": "", "custom_url": null}`), which would otherwise
    # fail Pydantic's strict TypedDict validation when re-validating the
    # cover dict in `DatabaseModel.transform`. Crash manifested as 500s on
    # any global leaderboard whose result set included such a user (country
    # leaderboards stayed up because they happened not to include them).
    url: str
    custom_url: NotRequired[str | None]
    id: NotRequired[str | None]


Badge = TypedDict(
    "Badge",
    {
        "awarded_at": datetime,
        "description": str,
        "image@2x_url": str,
        "image_url": str,
        "url": str,
    },
)

COUNTRIES = json.loads((STATIC_DIR / "iso3166.json").read_text())


class UserDict(TypedDict):
    avatar_url: str
    avatar_nsfw: NotRequired[bool]
    country_code: str
    id: int
    is_active: bool
    is_bot: bool
    is_supporter: bool
    last_visit: datetime | None
    pm_friends_only: bool
    profile_colour: str | None
    username: str
    is_online: bool
    g0v0_playmode: GameMode
    page: NotRequired[Page]
    previous_usernames: NotRequired[list[str]]
    support_level: NotRequired[int]
    badges: NotRequired[list[Badge]]
    cover: NotRequired[UserProfileCover]
    cover_nsfw: NotRequired[bool]
    beatmap_playcounts_count: NotRequired[int]
    playmode: NotRequired[GameMode]
    discord: NotRequired[str | None]
    has_supported: NotRequired[bool]
    interests: NotRequired[str | None]
    join_date: NotRequired[datetime]
    location: NotRequired[str | None]
    max_blocks: NotRequired[int]
    max_friends: NotRequired[int]
    occupation: NotRequired[str | None]
    playstyle: NotRequired[list[str]]
    profile_hue: NotRequired[int | None]
    title: NotRequired[str | None]
    title_url: NotRequired[str | None]
    twitter: NotRequired[str | None]
    website: NotRequired[str | None]
    comments_count: NotRequired[int]
    post_count: NotRequired[int]
    is_admin: NotRequired[bool]
    is_gmt: NotRequired[bool]
    is_qat: NotRequired[bool]
    is_bng: NotRequired[bool]
    groups: NotRequired[list[dict]]
    # Resolved aura id (torii_auras.TORII_AURAS key) currently rendered for
    # this user. None when the user has no eligible groups or has explicitly
    # opted out. The raw stored value (incl. sentinels) is exposed
    # separately as `equipped_aura_setting` on /me responses only.
    equipped_aura: NotRequired[str | None]
    # Bought name-colour cosmetic id (e.g. "name-crimson") the user has equipped,
    # or None. Broadcast so any client can paint the username; resolution into an
    # actual colour happens client-side from its name-colour catalog.
    equipped_name_colour: NotRequired[str | None]
    active_tournament_banners: NotRequired[list[dict]]
    graveyard_beatmapset_count: NotRequired[int]
    loved_beatmapset_count: NotRequired[int]
    mapping_follower_count: NotRequired[int]
    nominated_beatmapset_count: NotRequired[int]
    guest_beatmapset_count: NotRequired[int]
    pending_beatmapset_count: NotRequired[int]
    ranked_beatmapset_count: NotRequired[int]
    follow_user_mapping: NotRequired[list[int]]
    is_deleted: NotRequired[bool]
    country: NotRequired[Country]
    favourite_beatmapset_count: NotRequired[int]
    follower_count: NotRequired[int]
    scores_best_count: NotRequired[int]
    scores_pinned_count: NotRequired[int]
    scores_recent_count: NotRequired[int]
    scores_first_count: NotRequired[int]
    cover_url: NotRequired[str]
    profile_order: NotRequired[list[str]]
    user_preference: NotRequired[UserPreference | None]
    friends: NotRequired[list["RelationshipDict"]]
    team: NotRequired[Team | None]
    account_history: NotRequired[list[UserAccountHistoryResp]]
    daily_challenge_user_stats: NotRequired[DailyChallengeStatsResp | None]
    statistics: NotRequired["UserStatisticsDict | None"]
    statistics_rulesets: NotRequired[dict[str, "UserStatisticsDict"]]
    monthly_playcounts: NotRequired[list[CountResp]]
    replay_watched_counts: NotRequired[list[CountResp]]
    user_achievements: NotRequired[list[UserAchievementResp]]
    rank_history: NotRequired[RankHistoryResp | None]
    rank_highest: NotRequired[RankHighest | None]
    is_restricted: NotRequired[bool]
    kudosu: NotRequired[Kudosu]
    unread_pm_count: NotRequired[int]
    default_group: NotRequired[str]
    session_verified: NotRequired[bool]
    session_verification_method: NotRequired[Literal["totp", "mail"] | None]


class UserModel(DatabaseModel[UserDict]):
    DEFAULT_AVATAR_URL: ClassVar[str] = "https://lazer-data.g0v0.top/default.jpg"
    DEFAULT_COVER_URL: ClassVar[str] = "https://assets.ppy.sh/user-profile-covers/default.jpeg"

    # Torii ships its own default avatar set (storage/avatars/default-1..N.png).
    # A user without an uploaded avatar gets a stable pick from the set: varied
    # across users, but the same one each time for a given user. The stored
    # avatar_url stays DEFAULT_AVATAR_URL as the "no custom avatar" sentinel; we
    # only swap what the client is shown.
    DEFAULT_AVATAR_COUNT: ClassVar[int] = 20

    @classmethod
    def torii_default_avatar_url(cls, user_id: int) -> str:
        n = (int(user_id) % cls.DEFAULT_AVATAR_COUNT) + 1
        return f"{settings.server_url}file/avatars/default-{n}.png"

    @classmethod
    def torii_logo_avatar_url(cls) -> str:
        # Opt-out default: the plain Torii logo, for users who turned the
        # AI-made default-avatar set off in their settings.
        return f"{settings.server_url}file/avatars/default-halo.png"

    @classmethod
    def _masked_cover(cls, current_cover: dict | None = None) -> UserProfileCover:
        masked: UserProfileCover = UserProfileCover(url=cls.DEFAULT_COVER_URL)
        if isinstance(current_cover, dict):
            if "custom_url" in current_cover:
                masked["custom_url"] = cls.DEFAULT_COVER_URL
            if "id" in current_cover:
                masked["id"] = current_cover["id"]
        return masked

    @classmethod
    async def transform(
        cls,
        db_instance: "User",
        *,
        session: AsyncSession | None = None,
        includes: list[str] | None = None,
        show_nsfw_media: bool = False,
        **context,
    ) -> UserDict:
        user_resp = await super().transform(
            db_instance,
            session=session,
            includes=includes,
            show_nsfw_media=show_nsfw_media,
            **context,
        )

        # Live-resolve the supporter-state fields on every response. The
        # stored `is_supporter` / `support_level` columns drift in two
        # directions over time:
        #   - they get set on initial donation but are never flipped back
        #     when `donor_end_at` lapses (no cron, by design);
        #   - they were historically seeded to True at registration via
        #     ENABLE_SUPPORTER_FOR_ALL_USERS, leaking "fake supporter"
        #     status onto every fresh signup.
        # The single source of truth for "currently supporting" is
        # `is_currently_supporting()` (donor_end_at > now), so we
        # overwrite the dumped column values with the live computation
        # here, after the base transform has populated everything else.
        # Doing it in this override (rather than as @included resolvers
        # on the class) sidesteps the framework's `getattr(cls, fieldname)`
        # path, which returns the SQLAlchemy InstrumentedAttribute for
        # any name that matches a column instead of our function.
        live_supporter = is_currently_supporting(db_instance)
        # torii no cobra por features: el flag sale true para todos asi el cliente
        # deja de esconder cosas. la donacion real vive en support_level, que es lo
        # unico que pinta el corazon en el perfil.
        user_resp["is_supporter"] = True
        # support_level is OnDemand on the base model, so it only
        # appears in dumps that were materialised with an `includes`
        # list containing it. Mirror that gating here: if it wasn't in
        # the dict to begin with, don't introduce it.
        if "support_level" in user_resp:
            user_resp["support_level"] = 1 if live_supporter else 0

        # No uploaded avatar -> show a Torii default. Users who opted out of the
        # AI-made set in their settings get the plain Torii logo; everyone else
        # gets a stable pick from the set (varied by id).
        if not user_resp.get("avatar_url") or user_resp["avatar_url"] == cls.DEFAULT_AVATAR_URL:
            await db_instance.awaitable_attrs.user_preference
            pref = db_instance.user_preference
            if pref and (pref.extra or {}).get("default_avatar_use_logo"):
                user_resp["avatar_url"] = cls.torii_logo_avatar_url()
            else:
                user_resp["avatar_url"] = cls.torii_default_avatar_url(db_instance.id)

        if show_nsfw_media:
            return user_resp
        if db_instance.avatar_nsfw:
            user_resp["avatar_url"] = cls.torii_default_avatar_url(db_instance.id)
        if db_instance.cover_nsfw:
            user_resp["cover_url"] = cls.DEFAULT_COVER_URL
            user_resp["cover"] = cls._masked_cover(user_resp.get("cover"))
        return user_resp

    # https://github.com/ppy/osu-web/blob/d0407b1f2846dfd8b85ec0cf20e3fe3028a7b486/app/Transformers/UserCompactTransformer.php#L22-L39
    CARD_INCLUDES: ClassVar[list[str]] = [
        "country",
        "cover",
        "groups",
        # Resolved aura id (sentinel-translated) so any client rendering
        # this user's name knows which particle preset to attach. Cheap to
        # compute (pure CPU on already-loaded fields) — no extra query.
        "equipped_aura",
        # Bought name-colour cosmetic id, same idea: ships on every card so
        # any client can paint the username with the colour the user bought.
        "equipped_name_colour",
        "team",
    ]
    LIST_INCLUDES: ClassVar[list[str]] = [
        *CARD_INCLUDES,
        "statistics",
        "support_level",
    ]

    # https://github.com/ppy/osu-web/blob/d0407b1f2846dfd8b85ec0cf20e3fe3028a7b486/app/Transformers/UserTransformer.php#L36-L53
    USER_TRANSFORMER_INCLUDES: ClassVar[list[str]] = [
        "cover_url",
        "discord",
        "has_supported",
        "interests",
        "join_date",
        "location",
        "max_blocks",
        "max_friends",
        "occupation",
        "playmode",
        "playstyle",
        "post_count",
        "profile_hue",
        "profile_order",
        "title",
        "title_url",
        "twitter",
        "website",
        # https://github.com/ppy/osu-web/blob/d0407b1f2846dfd8b85ec0cf20e3fe3028a7b486/app/Transformers/UserTransformer.php#L13C22-L25
        "cover",
        "country",
        "is_admin",
        "is_bng",
        "is_full_bn",
        "is_gmt",
        "is_limited_bn",
        "is_moderator",
        "is_nat",
        "is_restricted",
        "is_silenced",
        "kudosu",
    ]

    # https://github.com/ppy/osu-web/blob/d0407b1f2846dfd8b85ec0cf20e3fe3028a7b486/app/Transformers/UserCompactTransformer.php#L41-L51
    PROFILE_HEADER_INCLUDES: ClassVar[list[str]] = [
        "active_tournament_banner",
        "active_tournament_banners",
        "badges",
        "comments_count",
        "follower_count",
        "groups",
        # Resolved aura also exposed on the profile-header path so the big
        # username on /users/{id} renders with it without an extra include.
        "equipped_aura",
        "equipped_name_colour",
        "mapping_follower_count",
        "previous_usernames",
        "support_level",
    ]

    # https://github.com/ppy/osu-web/blob/3f08fe12d70bcac1e32455c31e984eb6ef589b42/app/Http/Controllers/UsersController.php#L900-L937
    USER_INCLUDES: ClassVar[list[str]] = [
        # == apiIncludes ==
        # historical
        "beatmap_playcounts_count",
        "monthly_playcounts",
        "replays_watched_counts",
        "scores_recent_count",
        # beatmapsets
        "favourite_beatmapset_count",
        "graveyard_beatmapset_count",
        "guest_beatmapset_count",
        "loved_beatmapset_count",
        "nominated_beatmapset_count",
        "pending_beatmapset_count",
        "ranked_beatmapset_count",
        # top scores
        "scores_best_count",
        "scores_first_count",
        "scores_pinned_count",
        # others
        "account_history",
        "current_season_stats",
        "daily_challenge_user_stats",
        "page",
        "pending_beatmapset_count",
        "rank_highest",
        "rank_history",
        "statistics",
        "statistics.country_rank",
        "statistics.rank",
        "statistics.variants",
        "team",
        "user_achievements",
        *PROFILE_HEADER_INCLUDES,
        *USER_TRANSFORMER_INCLUDES,
    ]

    # https://github.com/ppy/osu-web/blob/d0407b1f2846dfd8b85ec0cf20e3fe3028a7b486/app/Transformers/UserCompactTransformer.php#L133-L150
    avatar_url: str = DEFAULT_AVATAR_URL
    avatar_nsfw: bool = False
    country_code: str = Field(default="CN", max_length=2, index=True)
    # ? default_group: str|None
    id: int = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True, index=True),
    )
    is_active: bool = True
    is_bot: bool = False
    is_supporter: bool = False
    is_online: bool = False
    last_visit: datetime | None = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    pm_friends_only: bool = False
    profile_colour: str | None = None
    username: str = Field(max_length=32, unique=True, index=True)

    # El saludo de bienvenida todavia no se entrego. Lo prende el registro y lo apaga
    # el primer login DESDE EL CLIENTE, no el registro mismo: si no, el que se anota
    # por la web y entra al juego recien al otro dia nunca lo ve. Arranca en False,
    # asi que las cuentas que ya existen no tienen nada pendiente.
    torii_welcome_pending: bool = False

    page: OnDemand[Page] = Field(sa_column=Column(JSON), default=Page(html="", raw=""))
    previous_usernames: OnDemand[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
    support_level: OnDemand[int] = Field(default=0)
    badges: OnDemand[list[Badge]] = Field(default_factory=list, sa_column=Column(JSON))

    # optional
    # blocks
    cover: OnDemand[UserProfileCover] = Field(
        default=UserProfileCover(url=""),
        sa_column=Column(JSON),
    )
    cover_nsfw: bool = False
    # kudosu

    # UserExtended
    playmode: OnDemand[GameMode] = Field(default=GameMode.OSU)
    discord: OnDemand[str | None] = Field(default=None)
    has_supported: OnDemand[bool] = Field(default=False)
    interests: OnDemand[str | None] = Field(default=None)
    join_date: OnDemand[datetime] = Field(default_factory=utcnow)
    location: OnDemand[str | None] = Field(default=None)
    max_blocks: OnDemand[int] = Field(default=50)
    max_friends: OnDemand[int] = Field(default=500)
    occupation: OnDemand[str | None] = Field(default=None)
    playstyle: OnDemand[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
    # TODO: post_count
    profile_hue: OnDemand[int | None] = Field(default=None)
    title: OnDemand[str | None] = Field(default=None)
    title_url: OnDemand[str | None] = Field(default=None)
    twitter: OnDemand[str | None] = Field(default=None)
    website: OnDemand[str | None] = Field(default=None)

    # undocumented
    comments_count: OnDemand[int] = Field(default=0)
    post_count: OnDemand[int] = Field(default=0)
    is_gmt: OnDemand[bool] = Field(default=False)
    is_qat: OnDemand[bool] = Field(default=False)
    is_bng: OnDemand[bool] = Field(default=False)

    # g0v0-extra
    g0v0_playmode: GameMode = GameMode.OSU

    @field_validator("playmode", mode="before")
    @classmethod
    def validate_playmode(cls, v):
        """Convert string to GameMode enum"""
        if isinstance(v, str):
            try:
                return GameMode(v)
            except ValueError:
                # If conversion fails, return default value
                return GameMode.OSU
        return v

    @ondemand
    @staticmethod
    async def groups(_session: AsyncSession, obj: "User") -> list[dict]:
        return build_groups(obj)

    @ondemand
    @staticmethod
    async def equipped_aura(_session: AsyncSession, obj: "User") -> str | None:
        # Return the *resolved* aura id every consumer should render. The
        # raw stored sentinel ("default" / "none") is intentionally hidden
        # here so other clients don't have to know about the sentinels —
        # only the /me settings endpoint exposes the raw stored value.
        # Also: aura only renders while the user is currently supporting,
        # so lapsed donors stop showing their aura even if their stored
        # equipped_aura value still says "supporter-aura".
        equipped = getattr(obj, "equipped_aura", None)

        # Entitlement is owned OR group-granted. Fast path: sentinels + picks a group
        # already grants never touch the DB (so serialising the N users of a leaderboard
        # doesn't fire N owned-cosmetic queries). Only when the equipped aura is a concrete
        # id that NO group grants — i.e. possibly a gifted/bought one — do we look up the
        # user's owned auras to see if they unlocked it.
        owned = None
        if (
            equipped
            and equipped not in (AURA_SENTINEL_DEFAULT, AURA_SENTINEL_NONE)
            and not is_aura_allowed_for_user(obj, equipped)
        ):
            from app.database.torii_store import owned_aura_ids

            owned = await owned_aura_ids(_session, obj.id)

        resolved = resolve_effective_aura_id(obj, equipped, owned)
        if resolved == "supporter-aura" and not is_currently_supporting(obj):
            return None
        return resolved

    @ondemand
    @staticmethod
    async def equipped_name_colour(_session: AsyncSession, obj: "User") -> str | None:
        # Raw stored bought-name-colour cosmetic id (e.g. "name-crimson"), or
        # None. No sentinel/resolution dance like the aura — the value is either
        # a concrete owned cosmetic id or NULL. Ownership is validated when the
        # user equips it (PATCH /me/equipped-name-colour), so whatever is stored
        # is always something they own; clients resolve it into a colour from
        # their own name-colour catalog.
        return getattr(obj, "equipped_name_colour", None)

    # NB: we deliberately do NOT register `is_supporter` / `support_level`
    # as @included/@ondemand resolver methods on this class — they share a
    # name with SQLModel/SQLAlchemy column descriptors, and `getattr(cls,
    # name)` inside the framework's transform helper resolves to the
    # InstrumentedAttribute, not our function, which then errors as
    # "InstrumentedAttribute is not callable". The live values are instead
    # injected by the User.transform override above (which post-processes
    # the data dict after super().transform finishes).

    @ondemand
    @staticmethod
    async def active_tournament_banners(_session: AsyncSession, _obj: "User") -> list[dict]:
        return []

    @ondemand
    @staticmethod
    async def graveyard_beatmapset_count(session: AsyncSession, obj: "User") -> int:
        from .beatmapset import Beatmapset

        owner_filter = (
            (col(Beatmapset.is_local).is_(True) & (Beatmapset.user_id == obj.id))
            | (
                col(Beatmapset.is_local).is_not(True)
                & (col(Beatmapset.creator) == obj.username)
            )
        )

        return (
            await session.exec(
                select(func.count())
                .select_from(Beatmapset)
                .where(
                    owner_filter,
                    Beatmapset.beatmap_status == BeatmapRankStatus.GRAVEYARD,
                )
            )
        ).one()

    @ondemand
    @staticmethod
    async def loved_beatmapset_count(session: AsyncSession, obj: "User") -> int:
        from .beatmapset import Beatmapset

        owner_filter = (
            (col(Beatmapset.is_local).is_(True) & (Beatmapset.user_id == obj.id))
            | (
                col(Beatmapset.is_local).is_not(True)
                & (col(Beatmapset.creator) == obj.username)
            )
        )

        return (
            await session.exec(
                select(func.count())
                .select_from(Beatmapset)
                .where(
                    owner_filter,
                    Beatmapset.beatmap_status == BeatmapRankStatus.LOVED,
                )
            )
        ).one()

    @ondemand
    @staticmethod
    async def mapping_follower_count(_session: AsyncSession, _obj: "User") -> int:
        return 0

    @ondemand
    @staticmethod
    async def nominated_beatmapset_count(_session: AsyncSession, _obj: "User") -> int:
        return 0

    @ondemand
    @staticmethod
    async def guest_beatmapset_count(_session: AsyncSession, _obj: "User") -> int:
        return 0

    @ondemand
    @staticmethod
    async def pending_beatmapset_count(session: AsyncSession, obj: "User") -> int:
        from .beatmapset import Beatmapset

        owner_filter = (
            (col(Beatmapset.is_local).is_(True) & (Beatmapset.user_id == obj.id))
            | (
                col(Beatmapset.is_local).is_not(True)
                & (col(Beatmapset.creator) == obj.username)
            )
        )

        return (
            await session.exec(
                select(func.count())
                .select_from(Beatmapset)
                .where(
                    owner_filter,
                    col(Beatmapset.beatmap_status).in_(
                        [BeatmapRankStatus.WIP, BeatmapRankStatus.PENDING]
                    ),
                )
            )
        ).one()

    @ondemand
    @staticmethod
    async def ranked_beatmapset_count(session: AsyncSession, obj: "User") -> int:
        from .beatmapset import Beatmapset

        owner_filter = (
            (col(Beatmapset.is_local).is_(True) & (Beatmapset.user_id == obj.id))
            | (
                col(Beatmapset.is_local).is_not(True)
                & (col(Beatmapset.creator) == obj.username)
            )
        )

        return (
            await session.exec(
                select(func.count())
                .select_from(Beatmapset)
                .where(
                    owner_filter,
                    col(Beatmapset.beatmap_status).in_(
                        [
                            BeatmapRankStatus.RANKED,
                            BeatmapRankStatus.APPROVED,
                            BeatmapRankStatus.QUALIFIED,
                        ]
                    ),
                )
            )
        ).one()

    @ondemand
    @staticmethod
    async def follow_user_mapping(_session: AsyncSession, _obj: "User") -> list[int]:
        return []

    @ondemand
    @staticmethod
    async def is_deleted(_session: AsyncSession, _obj: "User") -> bool:
        return False

    @included
    @staticmethod
    async def is_admin(_session, obj: "User") -> bool:
        return bool(getattr(obj, "is_admin", False))

    # @ondemand
    # @staticmethod
    # async def is_admin(_session, obj: "User") -> bool:
    #     return bool(getattr(obj, "is_admin", False))

    @ondemand
    @staticmethod
    async def country(_session: AsyncSession, obj: "User") -> Country:
        return Country(code=obj.country_code, name=COUNTRIES.get(obj.country_code, "Unknown"))

    @ondemand
    @staticmethod
    async def favourite_beatmapset_count(session: AsyncSession, obj: "User") -> int:
        from .favourite_beatmapset import FavouriteBeatmapset

        return (
            await session.exec(
                select(func.count()).select_from(FavouriteBeatmapset).where(FavouriteBeatmapset.user_id == obj.id)
            )
        ).one()

    @ondemand
    @staticmethod
    async def follower_count(session: AsyncSession, obj: "User") -> int:
        from .relationship import Relationship, RelationshipType

        stmt = (
            select(func.count())
            .select_from(Relationship)
            .where(
                Relationship.target_id == obj.id,
                Relationship.type == RelationshipType.FOLLOW,
            )
        )
        return (await session.exec(stmt)).one()

    @ondemand
    @staticmethod
    async def scores_best_count(
        session: AsyncSession,
        obj: "User",
        ruleset: GameMode | None = None,
    ) -> int:
        from .best_scores import BestScore

        mode = ruleset or obj.playmode
        stmt = (
            select(func.count())
            .select_from(BestScore)
            .where(
                BestScore.user_id == obj.id,
                BestScore.gamemode == mode,
            )
            .limit(200)
        )
        return (await session.exec(stmt)).one()

    @ondemand
    @staticmethod
    async def scores_pinned_count(
        session: AsyncSession,
        obj: "User",
        ruleset: GameMode | None = None,
    ) -> int:
        from .score import Score

        mode = ruleset or obj.playmode
        stmt = (
            select(func.count())
            .select_from(Score)
            .where(
                Score.user_id == obj.id,
                Score.gamemode == mode,
                Score.pinned_order > 0,
                col(Score.passed).is_(True),
            )
        )
        return (await session.exec(stmt)).one()

    @ondemand
    @staticmethod
    async def scores_recent_count(
        session: AsyncSession,
        obj: "User",
        ruleset: GameMode | None = None,
    ) -> int:
        from .score import Score

        mode = ruleset or obj.playmode
        stmt = (
            select(func.count())
            .select_from(Score)
            .where(
                Score.user_id == obj.id,
                Score.gamemode == mode,
                col(Score.passed).is_(True),
                Score.ended_at > utcnow() - timedelta(hours=24),
            )
        )
        return (await session.exec(stmt)).one()

    @ondemand
    @staticmethod
    async def scores_first_count(
        session: AsyncSession,
        obj: "User",
        ruleset: GameMode | None = None,
    ) -> int:
        from .score import get_user_first_score_count

        mode = ruleset or obj.playmode
        return await get_user_first_score_count(session, obj.id, mode)

    @ondemand
    @staticmethod
    async def beatmap_playcounts_count(session: AsyncSession, obj: "User") -> int:
        stmt = select(func.count()).select_from(BeatmapPlaycounts).where(BeatmapPlaycounts.user_id == obj.id)
        return (await session.exec(stmt)).one()

    @ondemand
    @staticmethod
    async def cover_url(
        _session: AsyncSession,
        obj: "User",
        show_nsfw_media: bool = False,
    ) -> str:
        if obj.cover_nsfw and not show_nsfw_media:
            return UserModel.DEFAULT_COVER_URL
        if not obj.cover:
            return ""
        if isinstance(obj.cover, dict):
            return str(obj.cover.get("url") or obj.cover.get("custom_url") or "")
        return ""

    @ondemand
    @staticmethod
    async def profile_order(_session: AsyncSession, obj: "User") -> list[str]:
        await obj.awaitable_attrs.user_preference
        if obj.user_preference:
            return list(obj.user_preference.extras_order)
        return list(DEFAULT_ORDER)

    @classmethod
    def apply_nsfw_media_policy(cls, user_resp: "UserDict", show_nsfw_media: bool) -> "UserDict":
        # Default avatar -> Torii default set. Runs for every viewer and covers
        # cached/canonical payloads that still hold the g0v0 default sentinel.
        uid = user_resp.get("id")
        if uid is not None and (not user_resp.get("avatar_url") or user_resp["avatar_url"] == cls.DEFAULT_AVATAR_URL):
            user_resp["avatar_url"] = cls.torii_default_avatar_url(uid)
        if show_nsfw_media:
            return user_resp
        if user_resp.get("avatar_nsfw"):
            user_resp["avatar_url"] = cls.torii_default_avatar_url(uid) if uid is not None else cls.DEFAULT_AVATAR_URL
        if user_resp.get("cover_nsfw"):
            user_resp["cover_url"] = cls.DEFAULT_COVER_URL
            user_resp["cover"] = cls._masked_cover(user_resp.get("cover"))
        return user_resp

    @staticmethod
    async def viewer_allows_nsfw_media(user: "User | None") -> bool:
        if user is None:
            return False
        await user.awaitable_attrs.user_preference
        return bool(user.user_preference and user.user_preference.profile_media_show_nsfw)

    @ondemand
    @staticmethod
    async def user_preference(_session: AsyncSession, obj: "User") -> UserPreference | None:
        await obj.awaitable_attrs.user_preference
        return obj.user_preference

    @ondemand
    @staticmethod
    async def friends(session: AsyncSession, obj: "User") -> list["RelationshipDict"]:
        from .relationship import Relationship, RelationshipType

        relationships = (
            await session.exec(
                select(Relationship).where(
                    Relationship.user_id == obj.id,
                    Relationship.type == RelationshipType.FOLLOW,
                )
            )
        ).all()
        return [await RelationshipModel.transform(rel, ruleset=obj.playmode) for rel in relationships]

    @ondemand
    @staticmethod
    async def team(_session: AsyncSession, obj: "User") -> Team | None:
        membership = await obj.awaitable_attrs.team_membership
        return membership.team if membership else None

    @ondemand
    @staticmethod
    async def account_history(_session: AsyncSession, obj: "User") -> list[UserAccountHistoryResp]:
        await obj.awaitable_attrs.account_history
        return [UserAccountHistoryResp.from_db(ah) for ah in obj.account_history]

    @ondemand
    @staticmethod
    async def daily_challenge_user_stats(_session: AsyncSession, obj: "User") -> DailyChallengeStatsResp | None:
        stats = await obj.awaitable_attrs.daily_challenge_stats
        return DailyChallengeStatsResp.from_db(stats) if stats else None

    @ondemand
    @staticmethod
    async def statistics(
        _session: AsyncSession,
        obj: "User",
        ruleset: GameMode | None = None,
        includes: list[str] | None = None,
    ) -> "UserStatisticsDict | None":
        mode = ruleset or obj.playmode
        for stat in await obj.awaitable_attrs.statistics:
            if stat.mode == mode:
                return await UserStatisticsModel.transform(stat, user_country=obj.country_code, includes=includes)
        return None

    @ondemand
    @staticmethod
    async def statistics_rulesets(
        _session: AsyncSession,
        obj: "User",
        includes: list[str] | None = None,
    ) -> dict[str, "UserStatisticsDict"]:
        stats = await obj.awaitable_attrs.statistics
        result: dict[str, UserStatisticsDict] = {}
        for stat in stats:
            # Torii behavior: merge catch relax into base catch profile mode.
            if stat.mode == GameMode.FRUITSRX:
                continue
            result[stat.mode.value] = await UserStatisticsModel.transform(
                stat, user_country=obj.country_code, includes=includes
            )
        return result

    @ondemand
    @staticmethod
    async def monthly_playcounts(_session: AsyncSession, obj: "User") -> list[CountResp]:
        playcounts = [CountResp.from_db(pc) for pc in await obj.awaitable_attrs.monthly_playcounts]
        if len(playcounts) == 1:
            d = playcounts[0].start_date
            playcounts.insert(0, CountResp(start_date=d - timedelta(days=20), count=0))
        return playcounts

    @ondemand
    @staticmethod
    async def replay_watched_counts(_session: AsyncSession, obj: "User") -> list[CountResp]:
        counts = [CountResp.from_db(rwc) for rwc in await obj.awaitable_attrs.replays_watched_counts]
        if len(counts) == 1:
            d = counts[0].start_date
            counts.insert(0, CountResp(start_date=d - timedelta(days=20), count=0))
        return counts

    @ondemand
    @staticmethod
    async def user_achievements(_session: AsyncSession, obj: "User") -> list[UserAchievementResp]:
        return [UserAchievementResp.from_db(ua) for ua in await obj.awaitable_attrs.achievement]

    @ondemand
    @staticmethod
    async def rank_history(
        session: AsyncSession,
        obj: "User",
        ruleset: GameMode | None = None,
    ) -> RankHistoryResp | None:
        mode = ruleset or obj.playmode
        rank_history = await RankHistoryResp.from_db(session, obj.id, mode)
        return rank_history if len(rank_history.data) != 0 else None

    @ondemand
    @staticmethod
    async def rank_highest(
        session: AsyncSession,
        obj: "User",
        ruleset: GameMode | None = None,
    ) -> RankHighest | None:
        mode = ruleset or obj.playmode
        rank_top = (await session.exec(select(RankTop).where(RankTop.user_id == obj.id, RankTop.mode == mode))).first()
        if not rank_top:
            return None
        return RankHighest(
            rank=rank_top.rank,
            updated_at=datetime.combine(rank_top.date, datetime.min.time()),
        )

    @ondemand
    @staticmethod
    async def is_restricted(session: AsyncSession, obj: "User") -> bool:
        return await obj.is_restricted(session)

    @ondemand
    @staticmethod
    async def kudosu(_session: AsyncSession, _obj: "User") -> Kudosu:
        return Kudosu(available=0, total=0)  # TODO

    @ondemand
    @staticmethod
    async def unread_pm_count(session: AsyncSession, obj: "User") -> int:
        return (
            await session.exec(
                select(func.count())
                .join(Notification, col(Notification.id) == UserNotification.notification_id)
                .select_from(UserNotification)
                .where(
                    col(UserNotification.is_read).is_(False),
                    UserNotification.user_id == obj.id,
                    Notification.name == NotificationName.CHANNEL_MESSAGE,
                    text("details->>'$.type' = 'pm'"),
                )
            )
        ).one()

    @included
    @staticmethod
    async def default_group(_session: AsyncSession, obj: "User") -> str:
        return "default" if not obj.is_bot else "bot"

    @ondemand
    @staticmethod
    async def session_verified(
        session: AsyncSession,
        obj: "User",
        token_id: int | None = None,
    ) -> bool:
        from app.service.verification_service import LoginSessionService

        return (
            not await LoginSessionService.check_is_need_verification(session, user_id=obj.id, token_id=token_id)
            if token_id
            else True
        )

    @ondemand
    @staticmethod
    async def session_verification_method(
        session: AsyncSession,
        obj: "User",
        token_id: int | None = None,
    ) -> Literal["totp", "mail"] | None:
        from app.dependencies.database import get_redis
        from app.service.verification_service import LoginSessionService

        if (settings.enable_totp_verification or settings.enable_email_verification) and token_id:
            redis = get_redis()
            if not await LoginSessionService.check_is_need_verification(session, user_id=obj.id, token_id=token_id):
                return None
            return await LoginSessionService.get_login_method(obj.id, token_id, redis)
        return None


class User(AsyncAttrs, UserModel, table=True):
    __tablename__: str = "lazer_users"

    email: str = Field(max_length=254, unique=True, index=True)
    priv: int = Field(default=1)
    is_admin: bool = Field(default=False)
    torii_titles: list[str] = Field(default=[], sa_column=Column(JSON))
    # Stored aura preset key. NULL / "default" → group-default mapping;
    # "none" → explicit opt-out; otherwise an entry in
    # app.models.torii_auras.TORII_AURAS. Resolution into the effective
    # aura is done by torii_auras.resolve_effective_aura_id so consumers
    # don't have to repeat the sentinel logic.
    equipped_aura: str | None = Field(default=None, max_length=64, nullable=True)
    # Bought name-colour cosmetic id the user has equipped (e.g. "name-crimson"),
    # broadcast to every client so others see it on their username. NULL = none
    # equipped → fall back to the group/role colour. Validated against ownership
    # when set (PATCH /me/equipped-name-colour), so the stored value is always a
    # cosmetic the user actually owns.
    equipped_name_colour: str | None = Field(default=None, max_length=64, nullable=True)
    # Cumulative months of Torii-supporter time this user has bought via
    # any donation provider. Stored as a permanent counter — never
    # decremented even after donor_end_at lapses. Used as a "thank-you"
    # statistic and as the unlock key for any future colour-variant
    # aura system (not in the schema yet, but the field is here so we
    # don't need a migration when we add one).
    total_supporter_months: int = Field(default=0, nullable=False)
    # Torii points: earned-only economy balance (play daily, top plays, daily
    # challenge, medals, redeemed codes). This is a cached running balance; the
    # authoritative history is torii_point_transactions. Spent in the cosmetics
    # store. Never buyable with money. See app.service.points_service.
    points: int = Field(default=0, nullable=False)
    # Optional override: if the user's Ko-fi display name differs from
    # their Torii username, they can set this so inbound donations match
    # without needing @username in the message field. Case-insensitive
    # comparison on the matcher side.
    kofi_display_name: str | None = Field(default=None, max_length=128, nullable=True)
    pw_bcrypt: str = Field(max_length=60)
    silence_end_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    donor_end_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    account_history: list[UserAccountHistory] = Relationship(back_populates="user")
    statistics: list[UserStatistics] = Relationship(back_populates="user")
    achievement: list[UserAchievement] = Relationship(back_populates="user")
    team_membership: TeamMember | None = Relationship(back_populates="user")
    daily_challenge_stats: DailyChallengeStats | None = Relationship(back_populates="user")
    matchmaking_stats: list["MatchmakingUserStats"] = Relationship(back_populates="user")
    monthly_playcounts: list[MonthlyPlaycounts] = Relationship(back_populates="user")
    replays_watched_counts: list[ReplayWatchedCount] = Relationship(back_populates="user")
    favourite_beatmapsets: list["FavouriteBeatmapset"] = Relationship(back_populates="user")
    rank_history: list[RankHistory] = Relationship(
        back_populates="user",
    )
    events: list[Event] = Relationship(back_populates="user")
    totp_key: TotpKeys | None = Relationship(back_populates="user")
    user_preference: UserPreference | None = Relationship(back_populates="user")

    async def is_user_can_pm(self, from_user: "User", session: AsyncSession) -> tuple[bool, str]:
        from .relationship import Relationship, RelationshipType

        from_relationship = (
            await session.exec(
                select(Relationship).where(
                    Relationship.user_id == from_user.id,
                    Relationship.target_id == self.id,
                )
            )
        ).first()
        if from_relationship and from_relationship.type == RelationshipType.BLOCK:
            return False, "You have blocked the target user."
        if from_user.pm_friends_only and (not from_relationship or from_relationship.type != RelationshipType.FOLLOW):
            return (
                False,
                "You have disabled non-friend communications and target user is not your friend.",
            )

        relationship = (
            await session.exec(
                select(Relationship).where(
                    Relationship.user_id == self.id,
                    Relationship.target_id == from_user.id,
                )
            )
        ).first()
        if relationship and relationship.type == RelationshipType.BLOCK:
            return False, "Target user has blocked you."
        if self.pm_friends_only and (not relationship or relationship.type != RelationshipType.FOLLOW):
            return False, "Target user has disabled non-friend communications"
        if await self.is_restricted(session):
            return False, "Target user is restricted"
        return True, ""

    @classmethod
    @overload
    def is_restricted_query(cls, user_id: int): ...

    @classmethod
    @overload
    def is_restricted_query(cls, user_id: Mapped[int]): ...

    @classmethod
    def is_restricted_query(cls, user_id: int | Mapped[int]):
        return exists().where(
            (col(UserAccountHistory.user_id) == user_id)
            & (col(UserAccountHistory.type) == UserAccountHistoryType.RESTRICTION)
            & (
                (col(UserAccountHistory.permanent).is_(True))
                | (
                    func.timestampadd(
                        text("SECOND"),
                        col(UserAccountHistory.length),
                        col(UserAccountHistory.timestamp),
                    )
                    > func.now()
                )
            ),
        )

    async def is_restricted(self, session: AsyncSession) -> bool:
        active_restrictions = (await session.exec(select(self.is_restricted_query(self.id)))).first()
        return active_restrictions or False


# For backwards compatibility, use User in SQL queries
# e.g.: select(User).where(User.id == 1)
# But use User in type annotations and return values
# e.g.: async def get_user() -> User | None:
#           return (await session.exec(select(User)...)).first()
