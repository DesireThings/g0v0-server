"""add changelog editor tables (streams / builds / entries)

Three tables that drive the admin Changelog Editor:

    changelog_streams 1 ── N changelog_builds 1 ── N changelog_entries

The v2 read endpoints (`GET /api/v2/changelog`, `GET /api/v2/changelog/{stream}/{version}`)
fall back to a hardcoded list of historical builds in code when the DB is
empty (zero-downtime cutover during deploy), but query the DB once any
streams + builds exist.

Seeds the canonical "lazer" stream so admins can immediately create
builds against it without having to POST to /streams first.

Revision ID: c1d2e3f4a5b6
Revises: b9c0d1e2f3a4
Create Date: 2026-05-03 04:00:00.000000

NOTE: parent is `b9c0d1e2f3a4` (rooms_type_ranked_play) rather than the
older merge head `f8a9b0c1d2e3`. Both b9c0d1e2f3a4 and an earlier draft
of this changelog migration originally pointed at the same merge head,
which gave alembic two parallel heads and broke startup ("Multiple head
revisions are present"). Re-parenting onto b9c0d1e2f3a4 puts the
migrations in a linear chain so prod can upgrade cleanly.
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "changelog_streams",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("is_featured", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("user_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_changelog_streams_name", "changelog_streams", ["name"], unique=True)
    op.create_index("ix_changelog_streams_id", "changelog_streams", ["id"], unique=False)

    op.create_table(
        "changelog_builds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stream_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("display_version", sa.String(100), nullable=False),
        sa.Column("users", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("github_url", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["stream_id"], ["changelog_streams.id"]),
    )
    op.create_index("ix_changelog_builds_id", "changelog_builds", ["id"], unique=False)
    op.create_index("ix_changelog_builds_stream_id", "changelog_builds", ["stream_id"], unique=False)
    op.create_index("ix_changelog_builds_created_at", "changelog_builds", ["created_at"], unique=False)

    op.create_table(
        "changelog_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("build_id", sa.Integer(), nullable=False),
        sa.Column("repository", sa.String(100), nullable=False, server_default="torii-osu"),
        sa.Column("github_pull_request_id", sa.Integer(), nullable=True),
        sa.Column("github_url", sa.String(500), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("type", sa.String(20), nullable=False, server_default="misc"),
        sa.Column("category", sa.String(20), nullable=False, server_default="other"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("message_html", sa.Text(), nullable=False),
        sa.Column("major", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("github_user", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["build_id"], ["changelog_builds.id"]),
    )
    op.create_index("ix_changelog_entries_id", "changelog_entries", ["id"], unique=False)
    op.create_index("ix_changelog_entries_build_id", "changelog_entries", ["build_id"], unique=False)
    op.create_index("ix_changelog_entries_type", "changelog_entries", ["type"], unique=False)
    op.create_index("ix_changelog_entries_category", "changelog_entries", ["category"], unique=False)

    # Seed the canonical "lazer" stream (matches the constants in
    # app/router/v2/changelog.py: _STREAM_NAME = "lazer", _STREAM_DISPLAY_NAME = "Torii").
    # We seed via raw SQL rather than ORM so the migration stays usable
    # even if the SQLModel definitions later evolve.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    op.execute(
        sa.text(
            """
            INSERT INTO changelog_streams (name, display_name, is_featured, user_count, created_at, updated_at)
            VALUES (:name, :display_name, :is_featured, :user_count, :now, :now)
            """
        ).bindparams(
            name="lazer",
            display_name="Torii",
            is_featured=True,
            user_count=0,
            now=now,
        )
    )


def downgrade() -> None:
    op.drop_table("changelog_entries")
    op.drop_table("changelog_builds")
    op.drop_table("changelog_streams")
