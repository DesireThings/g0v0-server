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


RANK_ALIAS = {
    "SS": "X",
    "SSH": "XH",
}


def _parse_ranks(v: Any):
    if isinstance(v, str):
        ranks = []
        for item in v.split("."):
            item_clean = item.strip().upper()
            ranks.append(RANK_ALIAS.get(item_clean, item_clean))
        return ranks
    if isinstance(v, list):
        return [RANK_ALIAS.get(str(item).strip().upper(), item) for item in v]
    return v


def _parse_played(v: Any) -> Literal["played", "unplayed"] | bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        v_clean = v.strip().lower()
        if v_clean in ("played", "true", "1"):
            return "played"
        if v_clean in ("unplayed",):
            return "unplayed"
        if v_clean in ("any", "all", "false", "0", ""):
            return False
    return False


LANGUAGE_INT_TO_NAME = {
    0: "any",
    1: "unspecified",
    2: "english",
    3: "japanese",
    4: "chinese",
    5: "instrumental",
    6: "korean",
    7: "french",
    8: "german",
    9: "swedish",
    10: "spanish",
    11: "italian",
    12: "russian",
    13: "polish",
    14: "other",
}


def _parse_language(v: Any) -> str:
    if v is None:
        return "any"
    if isinstance(v, int):
        return LANGUAGE_INT_TO_NAME.get(v, "any")
    if isinstance(v, str):
        v_clean = v.strip().lower()
        if v_clean.isdigit():
            return LANGUAGE_INT_TO_NAME.get(int(v_clean), "any")
        if v_clean in LANGUAGE_INT_TO_NAME.values():
            return v_clean
    return "any"


def _parse_genre(v: Any) -> int | None:
    if v is None or v == "" or v == 0 or v == "0":
        return None
    try:
        val = int(v)
        return val if val != 0 else None
    except (ValueError, TypeError):
        return None


class SearchQueryModel(BaseModel):
    # model_config = ConfigDict(serialize_by_alias=True)

    q: str = Field("", description="搜索关键词")
    c: Annotated[
        list[Literal["recommended", "converts", "follows", "spotlights", "featured_artists"]],
        BeforeValidator(_parse_list),
        PlainSerializer(lambda x: ".".join(x)),
    ] = Field(
        default_factory=list,
        description=(
            "常规：recommended 推荐难度 / converts 包括转谱 / follows 已关注谱师 / "
            "spotlights 聚光灯谱面 / featured_artists 精选艺术家"
        ),
    )
    m: int | None = Field(None, description="模式", alias="m")
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
            "分类：any 全部 / leaderboard 拥有排行榜 / ranked 上架 / "
            "qualified 过审 / loved 社区喜爱 / favourites 收藏 / "
            "pending 待定 / wip 制作中 / graveyard 坟场 / mine 我做的谱面"
        ),
    )
    l: Annotated[
        Literal[  # noqa: E741
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
        ],
        BeforeValidator(_parse_language),
    ] = Field(
        default="any",
        description=(
            "语言：any 全部 / unspecified 未指定 / english 英语 / japanese 日语 / "
            "chinese 中文 / instrumental 器乐 / korean 韩语 / "
            "french 法语 / german 德语 / swedish 瑞典语 / "
            "spanish 西班牙语 / italian 意大利语 / russian 俄语 / "
            "polish 波兰语 / other 其他"
        ),
    )
    g: Annotated[int | None, BeforeValidator(_parse_genre)] = Field(
        default=None,
        description="类别 ID (Genre IntEnum)",
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
            "排序方式： title 标题 / artist 艺术家 / difficulty 难度 / updated 更新时间"
            " / ranked 上架时间 / rating 评分 / plays 游玩次数 / favourites 收藏量"
            " / relevance 相关性 / nominations 提名"
        ),
    )
    e: Annotated[
        list[Literal["video", "storyboard"]],
        BeforeValidator(_parse_list),
        PlainSerializer(lambda x: ".".join(x)),
    ] = Field(default_factory=list, description=("其他：video 有视频 / storyboard 有故事板"))
    r: Annotated[list[Rank], BeforeValidator(_parse_ranks), PlainSerializer(lambda x: ".".join(x))] = Field(
        default_factory=list, description="成绩"
    )
    played: Annotated[
        Literal["played", "unplayed"] | bool,
        BeforeValidator(_parse_played),
    ] = Field(
        default=False,
        description="玩过状态：played 已玩 / unplayed 未玩 / false 未筛选",
    )
    nsfw: bool = Field(
        default=False,
        description="不良内容",
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
        description="游标字符串，用于分页",
    )


SearchQueryModel.model_rebuild()

