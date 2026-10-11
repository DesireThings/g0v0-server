from typing import Annotated

from app.database.relationship import Relationship as RelationshipTable, RelationshipType
from app.dependencies.database import Database
from app.dependencies.user import ClientUser

from .router import router

from fastapi import Path
from pydantic import BaseModel, Field
from sqlmodel import select


class CheckResponse(BaseModel):
    is_followed: bool = Field(..., description="Whether the target user is following the current user")
    is_following: bool = Field(..., description="Whether the current user is following the target user")
    mutual: bool = Field(..., description="Whether the current user and target user mutually follow each other")


@router.get(
    "/relationship/check/{user_id}",
    name="Check Relationship Status",
    description="Check relationship status between current user and specified user",
    response_model=CheckResponse,
    tags=["User Relationships", "g0v0 API"],
)
async def check_user_relationship(
    db: Database,
    user_id: Annotated[int, Path(..., description="Target user ID")],
    current_user: ClientUser,
):
    if user_id == current_user.id:
        return CheckResponse(
            is_followed=False,
            is_following=False,
            mutual=False,
        )

    my_relationship = (
        await db.exec(
            select(RelationshipTable).where(
                RelationshipTable.user_id == current_user.id,
                RelationshipTable.target_id == user_id,
            )
        )
    ).first()

    target_relationship = (
        await db.exec(
            select(RelationshipTable).where(
                RelationshipTable.user_id == user_id,
                RelationshipTable.target_id == current_user.id,
            )
        )
    ).first()

    is_followed = bool(target_relationship and target_relationship.type == RelationshipType.FOLLOW)
    is_following = bool(my_relationship and my_relationship.type == RelationshipType.FOLLOW)

    return CheckResponse(
        is_followed=is_followed,
        is_following=is_following,
        mutual=is_followed and is_following,
    )
