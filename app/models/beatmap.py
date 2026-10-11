from enum import IntEnum
from typing import Annotated, Any, Literal

from app.config import settings

from .score import Rank

from pydantic import BaseModel, BeforeValidator, Field, PlainSerializer


class BeatmapRankStatus(IntEnum):
    GRAVEYARD = -2
    WIP = -1
    PENDING = 0
    RANKED = 1
    APPROVED = 2
    QUALIFIED = 3
    LOVED = 4

    def has_leaderboard(self) -> bool:
        return self in {
            BeatmapRankStatus.RANKED,
            BeatmapRankStatus.APPROVED,
            BeatmapRankStatus.QUALIFIED,
            BeatmapRankStatus.LOVED,
        }

    def has_pp(self) -> bool:
        return self in {
            BeatmapRankStatus.RANKED,
            BeatmapRankStatus.APPROVED,
        }

    def ranked(self) -> bool:
        # https://osu.ppy.sh/wiki/Gameplay/Score/Ranked_score
        return self in {BeatmapRankStatus.RANKED, BeatmapRankStatus.APPROVED, BeatmapRankStatus.LOVED}


def effective_rank_status(status: BeatmapRankStatus) -> BeatmapRankStatus:
    """
    Return API-facing rank status when global leaderboard/pp overrides are enabled.
    Keep database status untouched; only normalize public representation so clients
    treat non-ranked maps as scoreable/pp maps without rewriting historical data.
    """
    if settings.enable_all_beatmap_leaderboard or settings.enable_all_beatmap_pp:
        if status in {
            BeatmapRankStatus.GRAVEYARD,
            BeatmapRankStatus.WIP,
            BeatmapRankStatus.PENDING,
            BeatmapRankStatus.QUALIFIED,
            BeatmapRankStatus.LOVED,
        }:
            return BeatmapRankStatus.APPROVED
    return status


class Genre(IntEnum):
    ANY = 0
    UNSPECIFIED = 1
    VIDEO_GAME = 2
    ANIME = 3
    ROCK = 4
    POP = 5
    OTHER = 6
    NOVELTY = 7
    HIP_HOP = 9
    ELECTRONIC = 10
    METAL = 11
    CLASSICAL = 12
    FOLK = 13
    JAZZ = 14


class Language(IntEnum):
    ANY = 0
    UNSPECIFIED = 1
    ENGLISH = 2
    JAPANESE = 3
    CHINESE = 4
    INSTRUMENTAL = 5
    KOREAN = 6
    FRENCH = 7
    GERMAN = 8
    SWEDISH = 9
    ITALIAN = 10
    SPANISH = 11
    RUSSIAN = 12
    POLISH = 13
    OTHER = 14


def _parse_list(v: Any):
    if isinstance(v, str):
        return v.split(".")
    return v


class SearchQueryModel(BaseModel):
    # model_config = ConfigDict(serialize_by_alias=True)

    q: str = Field("", description="Search keyword")
    c: Annotated[
        list[Literal["recommended", "converts", "follows", "spotlights", "featured_artists"]],
        BeforeValidator(_parse_list),
        PlainSerializer(lambda x: ".".join(x)),
    ] = Field(
        default_factory=list,
        description=(
            "General: recommended Recommended difficulty / converts Include converts / follows Followed mappers / "
            "spotlights Spotlight beatmaps / featured_artists Featured artists"
        ),
    )
    m: int | None = Field(None, description="Mode", alias="m")
    s: Literal[
        "any",
        "leaderboard",
        "ranked",
        "qualified",
        "loved",
        "favourites",
        "pending",
        "wip",
        "graveyard",
        "mine",
    ] = Field(
        default="leaderboard",
        description=(
            "Category: any Any / leaderboard Has leaderboard / ranked Ranked / "
            "qualified Qualified / loved Loved / favourites Favourites / "
            "pending Pending / wip Work in progress / graveyard Graveyard / mine My beatmaps"
        ),
    )
    l: Literal[  # noqa: E741
        "any",
        "unspecified",
        "english",
        "japanese",
        "chinese",
        "instrumental",
        "korean",
        "french",
        "german",
        "swedish",
        "spanish",
        "italian",
        "russian",
        "polish",
        "other",
    ] = Field(
        default="any",
        description=(
            "Language: any Any / unspecified Unspecified / english English / japanese Japanese / "
            "chinese Chinese / instrumental Instrumental / korean Korean / "
            "french French / german German / swedish Swedish / "
            "spanish Spanish / italian Italian / russian Russian / "
            "polish Polish / other Other"
        ),
    )
    sort: Literal[
        "title_asc",
        "artist_asc",
        "difficulty_asc",
        "updated_asc",
        "ranked_asc",
        "rating_asc",
        "plays_asc",
        "favourites_asc",
        "relevance_asc",
        "nominations_asc",
        "title_desc",
        "artist_desc",
        "difficulty_desc",
        "updated_desc",
        "ranked_desc",
        "rating_desc",
        "plays_desc",
        "favourites_desc",
        "relevance_desc",
        "nominations_desc",
    ] = Field(
        ...,
        description=(
            "Sort: title Title / artist Artist / difficulty Difficulty / updated Updated"
            " / ranked Ranked time / rating Rating / plays Plays / favourites Favourites"
            " / relevance Relevance / nominations Nominations"
        ),
    )
    e: Annotated[
        list[Literal["video", "storyboard"]],
        BeforeValidator(_parse_list),
        PlainSerializer(lambda x: ".".join(x)),
    ] = Field(default_factory=list, description=("Extra: video Has video / storyboard Has storyboard"))
    r: Annotated[list[Rank], BeforeValidator(_parse_list), PlainSerializer(lambda x: ".".join(x))] = Field(
        default_factory=list, description="Scores"
    )
    played: bool = Field(
        default=False,
        description="Played",
    )
    nsfw: bool = Field(
        default=False,
        description="Explicit content",
    )
    is_local: bool = Field(
        default=False,
        description="query local beatmaps only",
    )
    ai: bool = Field(
        default=False,
        description="only maps generated with Mapperatorinator (implies is_local)",
    )
    hide_local: bool = Field(
        default=False,
        description="leave out maps uploaded to Torii (they are mixed into the listing by default)",
    )
    hide_ai: bool = Field(
        default=False,
        description="leave out maps generated with Mapperatorinator, from here or from bancho",
    )
    cursor_string: str | None = Field(
        default=None,
        description="Cursor string for pagination",
    )


SearchQueryModel.model_rebuild()

