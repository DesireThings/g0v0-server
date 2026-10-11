"""V1 API user related models"""

from pydantic import BaseModel, Field


class PlayerStatsHistory(BaseModel):
    """Player PP history data"""

    pp: list[float] = Field(default_factory=list)


class PlayerModeStats(BaseModel):
    """Player statistics data for a single mode"""

    id: int
    mode: int
    tscore: int  # total_score
    rscore: int  # ranked_score
    pp: float
    plays: int  # play_count
    playtime: int  # play_time
    acc: float  # accuracy
    max_combo: int  # maximum_combo
    total_hits: int
    replay_views: int  # replays_watched_by_others
    xh_count: int  # grade_ssh
    x_count: int  # grade_ss
    sh_count: int  # grade_sh
    s_count: int  # grade_s
    a_count: int  # grade_a
    level: int
    level_progress: int
    rank: int
    country_rank: int
    history: PlayerStatsHistory


class PlayerStatsResponse(BaseModel):
    """Player statistics response - contains all modes"""

    stats: dict[str, PlayerModeStats] = Field(default_factory=dict)


class PlayerEventItem(BaseModel):
    """Player event item"""

    userId: int  # noqa: N815
    name: str
    mapId: int | None = None  # noqa: N815
    setId: int | None = None  # noqa: N815
    artist: str | None = None
    title: str | None = None
    version: str | None = None
    mode: int | None = None
    rank: int | None = None
    grade: str | None = None
    event: str | None = None
    time: str | None = None


class PlayerEventsResponse(BaseModel):
    """Player events response"""

    events: list[PlayerEventItem] = Field(default_factory=list)


class PlayerInfo(BaseModel):
    """Player basic information"""

    id: int
    name: str
    safe_name: str
    priv: int
    country: str
    silence_end: int
    donor_end: int
    creation_time: int
    latest_activity: int
    clan_id: int
    clan_priv: int
    preferred_mode: int
    preferred_type: int
    play_style: int
    custom_badge_enabled: int
    custom_badge_name: str
    custom_badge_icon: str
    custom_badge_color: str
    userpage_content: str
    recentFailed: int  # noqa: N815
    social_discord: str | None = None
    social_youtube: str | None = None
    social_twitter: str | None = None
    social_twitch: str | None = None
    social_github: str | None = None
    social_osu: str | None = None
    username_history: list[str] = Field(default_factory=list)


class PlayerInfoResponse(BaseModel):
    """Player information response"""

    info: PlayerInfo


class PlayerAllResponse(BaseModel):
    """Player full information response - contains all data"""

    info: PlayerInfo
    stats: dict[str, PlayerModeStats] = Field(default_factory=dict)
    events: list[PlayerEventItem] = Field(default_factory=list)


class GetPlayerInfoResponse(BaseModel):
    """get_player_info endpoint response"""

    status: str = "success"
    player: PlayerStatsResponse | PlayerEventsResponse | PlayerInfoResponse | PlayerAllResponse


class PlayerCountData(BaseModel):
    """Player count data"""

    online: int
    total: int


class GetPlayerCountResponse(BaseModel):
    """get_player_count endpoint response"""

    status: str = "success"
    counts: PlayerCountData
