"""Admin endpoints for managing the donations queue.

The Ko-fi webhook (``donations.py``) records every event verbatim and tries
to auto-match the donor to a Torii user via @username / kofi_display_name /
username heuristics. When that auto-match fails, the donation row sits with
``user_id = NULL`` and an amber Discord embed alerts the admin team. This
module is the queue admins work through to clear those pending rows:

  - ``GET  /admin/donations``                — paginated list, filterable by
                                                match status (unmatched /
                                                matched / all).
  - ``GET  /admin/donations/stats``          — top-line counters for the
                                                dashboard tile.
  - ``POST /admin/donations/{id}/match``     — link a donation to a Torii
                                                user, apply the supporter
                                                grant, and remember the
                                                Ko-fi display name so future
                                                donations from the same donor
                                                auto-match without admin
                                                intervention.

Match logic deliberately calls into ``apply_supporter_grant`` (the same
function the webhook uses) so the manual path and the auto path produce
byte-identical state. There's no parallel "manual grant" code path that
could drift out of sync with the webhook's behaviour.

We deliberately do NOT expose an "unmatch" endpoint in v1. Reversing a
grant cleanly is fiddly (donor_end_at could already have been extended by
a later donation, total_supporter_months would need to be decremented,
etc.) and the realistic recovery path for a wrong match is "match again
to the right user" — the original wrong user keeps the bonus month, which
is fine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from app.database.donation import Donation, apply_supporter_grant
from app.database.user import User
from app.dependencies.database import Database
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.log import log
from app.models.torii_groups import is_currently_supporting

from fastapi import HTTPException, Query, Security
from pydantic import BaseModel
from sqlmodel import col, func, select

from .admin import require_admin
from .router import router


logger = log("AdminDonations")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AdminDonationItem(BaseModel):
    """One row in the admin donations queue.

    The ``matched_user`` block is populated lazily only when the donation
    is linked — for unmatched rows the admin only needs the donor name /
    message to figure out who they are, so we save a join.
    """

    id: int
    provider: str
    provider_transaction_id: str
    amount_cents: int
    currency: str
    is_recurring: bool
    tier_name: str | None
    donor_display_name: str | None
    donor_message: str | None
    donor_message_is_public: bool
    months_granted: int
    received_at: datetime

    # None for unmatched donations.
    user_id: int | None
    matched_username: str | None = None


class AdminDonationListResp(BaseModel):
    items: list[AdminDonationItem]
    total: int
    unmatched_count: int


class AdminDonationStatsResp(BaseModel):
    """Top-line numbers for the admin dashboard donations card.

    ``totals_by_currency`` is a dict keyed by ISO currency code so we don't
    flatten USD + EUR + whatever Ko-fi returns into a single nonsense
    figure. Most Torii donors pay in USD; this just future-proofs.
    """

    total_donations: int
    unmatched_count: int
    totals_by_currency: dict[str, int]  # cents per currency
    active_supporters: int
    lifetime_donators: int


class MatchDonationReq(BaseModel):
    """Either a username or a user id is enough to identify the target.

    The frontend uses username (admin types it in a free-text input);
    user_id is accepted too for any future auto-suggest UI that already
    has the id in hand.
    """

    username: str | None = None
    user_id: int | None = None


class MatchDonationResp(BaseModel):
    donation_id: int
    user_id: int
    username: str
    months_granted: int
    total_supporter_months: int
    donor_end_at: datetime | None
    is_currently_supporting: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _serialise_donation(session: Database, donation: Donation) -> AdminDonationItem:
    """Hydrate a Donation row into the admin response shape, joining the
    matched user's username only when present."""
    matched_username: str | None = None
    if donation.user_id is not None:
        # Single get is cheap and avoids loading the whole user payload.
        user = await session.get(User, donation.user_id)
        if user is not None:
            matched_username = user.username
    return AdminDonationItem(
        id=donation.id or 0,
        provider=donation.provider,
        provider_transaction_id=donation.provider_transaction_id,
        amount_cents=donation.amount_cents,
        currency=donation.currency,
        is_recurring=donation.is_recurring,
        tier_name=donation.tier_name,
        donor_display_name=donation.donor_display_name,
        donor_message=donation.donor_message,
        donor_message_is_public=donation.donor_message_is_public,
        months_granted=donation.months_granted,
        received_at=donation.received_at,
        user_id=donation.user_id,
        matched_username=matched_username,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/admin/donations",
    name="List donations (admin)",
    tags=["Admin", "g0v0 API"],
    response_model=AdminDonationListResp,
)
async def list_donations(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    status: Annotated[Literal["unmatched", "matched", "all"], Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminDonationListResp:
    """Paginated donation queue for admins.

    ``status=unmatched`` is the most-used view — it's the queue the
    admin actually works through to clear pending rows. ``matched`` and
    ``all`` are for audit / history.
    """
    await require_admin(session, user_and_token)

    page_stmt = select(Donation)
    count_stmt = select(func.count()).select_from(Donation)
    if status == "unmatched":
        page_stmt = page_stmt.where(col(Donation.user_id).is_(None))
        count_stmt = count_stmt.where(col(Donation.user_id).is_(None))
    elif status == "matched":
        page_stmt = page_stmt.where(col(Donation.user_id).is_not(None))
        count_stmt = count_stmt.where(col(Donation.user_id).is_not(None))

    # Newest first — admins want to see the freshest unmatched donations
    # at the top of the queue.
    page_stmt = page_stmt.order_by(col(Donation.received_at).desc()).offset(offset).limit(limit)
    rows = (await session.exec(page_stmt)).all()

    # Two cheap aggregate queries for the response footer. We need both
    # the filtered-total (for pagination) and the unmatched-count (so
    # the badge in the sidebar can update without a second round trip).
    total = (await session.exec(count_stmt)).one()
    unmatched_count = (
        await session.exec(
            select(func.count()).select_from(Donation).where(col(Donation.user_id).is_(None))
        )
    ).one()

    items = [await _serialise_donation(session, d) for d in rows]

    return AdminDonationListResp(
        items=items,
        total=int(total or 0),
        unmatched_count=int(unmatched_count or 0),
    )


@router.get(
    "/admin/donations/stats",
    name="Donation stats (admin)",
    tags=["Admin", "g0v0 API"],
    response_model=AdminDonationStatsResp,
)
async def donation_stats(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
) -> AdminDonationStatsResp:
    """Top-line counters for the admin dashboard donations tile."""
    await require_admin(session, user_and_token)

    total_donations = int(
        (await session.exec(select(func.count()).select_from(Donation))).one() or 0
    )
    unmatched_count = int(
        (
            await session.exec(
                select(func.count()).select_from(Donation).where(col(Donation.user_id).is_(None))
            )
        ).one()
        or 0
    )

    # Break down totals by currency so a single-figure summary doesn't lie
    # when EUR donations show up next to USD.
    rows = (
        await session.exec(
            select(Donation.currency, func.sum(Donation.amount_cents)).group_by(Donation.currency)
        )
    ).all()
    totals_by_currency: dict[str, int] = {}
    for currency, total_cents in rows:
        if currency:
            totals_by_currency[currency] = int(total_cents or 0)

    # Active supporters = users with donor_end_at in the future. We can't
    # use the @ondemand resolver in a SQL aggregate, but we can replicate
    # its predicate at the query level — that's exactly what
    # is_currently_supporting checks.
    active_supporters = int(
        (
            await session.exec(
                select(func.count()).select_from(User).where(
                    col(User.donor_end_at).is_not(None),
                    col(User.donor_end_at) > func.utc_timestamp(),
                )
            )
        ).one()
        or 0
    )

    lifetime_donators = int(
        (
            await session.exec(
                select(func.count()).select_from(User).where(col(User.has_supported).is_(True))
            )
        ).one()
        or 0
    )

    return AdminDonationStatsResp(
        total_donations=total_donations,
        unmatched_count=unmatched_count,
        totals_by_currency=totals_by_currency,
        active_supporters=active_supporters,
        lifetime_donators=lifetime_donators,
    )


@router.post(
    "/admin/donations/{donation_id}/match",
    name="Match donation to user (admin)",
    tags=["Admin", "g0v0 API"],
    response_model=MatchDonationResp,
)
async def match_donation(
    donation_id: int,
    body: MatchDonationReq,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
) -> MatchDonationResp:
    """Link an unmatched donation to a Torii user and apply the supporter
    grant atomically.

    Effects (all in one DB transaction):
      1. donations.user_id = target user
      2. apply_supporter_grant(...) — bumps total_supporter_months,
         extends donor_end_at, flips is_supporter / has_supported,
         sets support_level (same logic the webhook uses, no drift).
      3. user.kofi_display_name = donor's Ko-fi name (only if the user
         doesn't already have one set) — so the NEXT donation from the
         same donor auto-matches via path 2 without admin intervention.

    Refuses to:
      - Match a donation that already has a user linked. Mistakes are
        recovered by manual SQL or by skipping (the wrong user keeps the
        bonus, which is fine — admins can flag the donor in chat).
      - Match to a missing username — returns 404 so the UI can show a
        clear "no such user" toast.
    """
    admin_user = await require_admin(session, user_and_token)

    if not body.username and body.user_id is None:
        raise HTTPException(status_code=400, detail="Provide username or user_id.")

    # --- Load the donation and reject double-matches ----------------------
    donation = await session.get(Donation, donation_id)
    if donation is None:
        raise HTTPException(status_code=404, detail="Donation not found.")
    if donation.user_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Donation already linked to another user. "
                "Re-matching is not supported — fix via SQL if absolutely needed."
            ),
        )

    # --- Resolve the target user -----------------------------------------
    target_user: User | None = None
    if body.user_id is not None:
        target_user = await session.get(User, body.user_id)
    elif body.username:
        target_user = (
            await session.exec(
                # Case-insensitive comparison so "Mash39" / "mash39" both work.
                select(User).where(
                    User.username.collate("utf8mb4_general_ci") == body.username  # type: ignore[attr-defined]
                )
            )
        ).first()

    if target_user is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # --- Atomic grant -----------------------------------------------------
    donation.user_id = target_user.id
    session.add(donation)

    if donation.months_granted > 0:
        await apply_supporter_grant(
            session, user=target_user, months_granted=donation.months_granted
        )

    # Remember the Ko-fi display name for future auto-matches. Only do
    # this if the user hasn't already set one — never overwrite a user's
    # explicit choice from the settings page.
    if not target_user.kofi_display_name and donation.donor_display_name:
        target_user.kofi_display_name = donation.donor_display_name
        session.add(target_user)

    await session.commit()
    await session.refresh(target_user)

    logger.info(
        "Admin {} linked donation {} to user {} ({} months granted)",
        admin_user.username,
        donation.id,
        target_user.username,
        donation.months_granted,
    )

    return MatchDonationResp(
        donation_id=donation.id or 0,
        user_id=target_user.id or 0,
        username=target_user.username,
        months_granted=donation.months_granted,
        total_supporter_months=target_user.total_supporter_months or 0,
        donor_end_at=target_user.donor_end_at,
        is_currently_supporting=is_currently_supporting(target_user),
    )
