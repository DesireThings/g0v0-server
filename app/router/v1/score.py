from datetime import datetime, timedelta
from typing import Annotated, Literal

from app.database.best_scores import BestScore
from app.database.score import Score, get_leaderboard
from app.database.user import User
from app.dependencies.database import Database
from app.models.mods import int_to_mods, mod_to_save, mods_to_int
from app.models.score import GameMode, LeaderboardType
from app.utils import utcnow

from .router import AllStrModel, router

from fastapi import HTTPException, Query
from sqlalchemy.orm import joinedload
from sqlmodel import col, exists, select


class V1Score(AllStrModel):
    beatmap_id: int | None = None
    username: str | None = None
    score_id: int
    score: int
    maxcombo: int | None = None
    count50: int
    count100: int
    count300: int
    countmiss: int
    countkatu: int
    countgeki: int
    perfect: bool
    enabled_mods: int
    user_id: int
    date: datetime
    rank: str
    pp: float
    replay_available: bool

    @classmethod
    async def from_db(cls, score: Score):
        return cls(
            beatmap_id=score.beatmap_id,
            username=score.user.username,
            score_id=score.id,
            score=score.total_score,
            maxcombo=score.max_combo,
            count50=score.n50,
            count100=score.n100,
            count300=score.n300,
            countmiss=score.nmiss,
            countkatu=score.nkatu,
            countgeki=score.ngeki,
            perfect=score.is_perfect_combo,
            enabled_mods=mods_to_int(score.mods),
            user_id=score.user_id,
            date=score.ended_at,
            rank=score.rank,
            pp=score.pp,
            replay_available=score.has_replay,
        )


@router.get(
    "/get_user_best",
    response_model=list[V1Score],
    name="Get User Best Scores",
    description="Get the best scores for the specified user.",
)
async def get_user_best(
    session: Database,
    user: Annotated[str, Query(..., alias="u", description="User")],
    ruleset_id: Annotated[int, Query(alias="m", description="Ruleset ID", ge=0)] = 0,
    type: Annotated[Literal["string", "id"] | None, Query(description="User lookup type: 'string' for username / 'id' for user ID")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Number of scores to return")] = 10,
):
    try:
        scores = (
            await session.exec(
                select(Score)
                .where(
                    Score.user_id == user if type == "id" or user.isdigit() else col(Score.user).has(username=user),
                    Score.gamemode == GameMode.from_int_extra(ruleset_id),
                    exists().where(col(BestScore.score_id) == Score.id),
                    ~User.is_restricted_query(col(Score.user_id)),
                )
                .order_by(col(Score.pp).desc())
                .options(joinedload(Score.beatmap))
                .limit(limit)
            )
        ).all()
        return [await V1Score.from_db(score) for score in scores]
    except KeyError:
        raise HTTPException(400, "Invalid request")


@router.get(
    "/get_user_recent",
    response_model=list[V1Score],
    name="Get User Recent Scores",
    description="Get recent scores for the specified user.",
)
async def get_user_recent(
    session: Database,
    user: Annotated[str, Query(..., alias="u", description="User")],
    ruleset_id: Annotated[int, Query(alias="m", description="Ruleset ID", ge=0)] = 0,
    type: Annotated[Literal["string", "id"] | None, Query(description="User lookup type: 'string' for username / 'id' for user ID")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Number of scores to return")] = 10,
):
    try:
        scores = (
            await session.exec(
                select(Score)
                .where(
                    Score.user_id == user if type == "id" or user.isdigit() else col(Score.user).has(username=user),
                    Score.gamemode == GameMode.from_int_extra(ruleset_id),
                    Score.ended_at > utcnow() - timedelta(hours=24),
                    ~User.is_restricted_query(col(Score.user_id)),
                )
                .order_by(col(Score.pp).desc())
                .options(joinedload(Score.beatmap))
                .limit(limit)
            )
        ).all()
        return [await V1Score.from_db(score) for score in scores]
    except KeyError:
        raise HTTPException(400, "Invalid request")


@router.get(
    "/get_scores",
    response_model=list[V1Score],
    name="Get Scores",
    description="Get scores for the specified beatmap.",
)
async def get_scores(
    session: Database,
    beatmap_id: Annotated[int, Query(alias="b", description="Beatmap ID")],
    user: Annotated[str | None, Query(alias="u", description="User")] = None,
    ruleset_id: Annotated[int, Query(alias="m", description="Ruleset ID", ge=0)] = 0,
    type: Annotated[Literal["string", "id"] | None, Query(description="User lookup type: 'string' for username / 'id' for user ID")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Number of scores to return")] = 10,
    mods: Annotated[int, Query(description="Score mods bitmask")] = 0,
):
    try:
        if user is not None:
            scores = (
                await session.exec(
                    select(Score)
                    .where(
                        Score.gamemode == GameMode.from_int_extra(ruleset_id),
                        Score.beatmap_id == beatmap_id,
                        Score.user_id == user if type == "id" or user.isdigit() else col(Score.user).has(username=user),
                        ~User.is_restricted_query(col(Score.user_id)),
                    )
                    .options(joinedload(Score.beatmap))
                    .order_by(col(Score.classic_total_score).desc())
                )
            ).all()
        else:
            scores, _, _ = await get_leaderboard(
                session,
                beatmap_id,
                GameMode.from_int_extra(ruleset_id),
                LeaderboardType.GLOBAL,
                mod_to_save(int_to_mods(mods)),
                limit=limit,
            )
        return [await V1Score.from_db(score) for score in scores]
    except KeyError:
        raise HTTPException(400, "Invalid request")
