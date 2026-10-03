"""Challenges data model: badge, challenge, user_challenge,
challenge_day_log, user_badge (Step 4).

Guarded like earlier revisions — fresh databases get these tables from
create_all, so every create checks first.

Revision ID: 0004_challenges
Revises: 0003_content_library
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0004_challenges"
down_revision = "0003_content_library"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        row = bind.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table"),
            {"table": table},
        ).first()
    else:
        row = bind.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
            {"table": table},
        ).first()
    return row is not None


def _create(table: str, *columns, indexes: tuple = ()) -> None:
    if _table_exists(table):
        return
    op.create_table(table, *columns)
    for name, cols, unique in indexes:
        op.create_index(name, table, cols, unique=unique)


def upgrade() -> None:
    _create(
        "badge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=200)),
        sa.Column("icon", sa.String(length=10)),
        indexes=(("ix_badge_name", ["name"], True),),
    )

    _create(
        "challenge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("banner_url", sa.String(length=300)),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("metric", sa.String(length=20), nullable=False),
        sa.Column("target_value", sa.Float(), nullable=False),
        sa.Column("daily_target", sa.Float()),
        sa.Column("duration_days", sa.Integer()),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("rolling", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("audience", sa.String(length=20), nullable=False, server_default="all"),
        sa.Column("badge_id", sa.Integer(), sa.ForeignKey("badge.id")),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        indexes=(
            ("ix_challenge_title", ["title"], False),
            ("ix_challenge_status", ["status"], False),
        ),
    )

    _create(
        "user_challenge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("challenge_id", sa.Integer(), sa.ForeignKey("challenge.id"), nullable=False),
        sa.Column("joined_at", sa.DateTime()),
        sa.Column("progress_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("current_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("longest_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("completed_at", sa.DateTime()),
        indexes=(
            ("ix_user_challenge_user_id", ["user_id"], False),
            ("ix_user_challenge_challenge_id", ["challenge_id"], False),
            ("uq_user_challenge_pair", ["user_id", "challenge_id"], True),
        ),
    )

    _create(
        "challenge_day_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_challenge_id", sa.Integer(), sa.ForeignKey("user_challenge.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        indexes=(
            ("ix_challenge_day_log_uc", ["user_challenge_id"], False),
            ("uq_user_challenge_day", ["user_challenge_id", "date"], True),
        ),
    )

    _create(
        "user_badge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("badge_id", sa.Integer(), sa.ForeignKey("badge.id"), nullable=False),
        sa.Column("challenge_id", sa.Integer(), sa.ForeignKey("challenge.id")),
        sa.Column("awarded_at", sa.DateTime()),
        indexes=(
            ("ix_user_badge_user_id", ["user_id"], False),
            ("uq_user_badge_pair", ["user_id", "badge_id"], True),
        ),
    )


def downgrade() -> None:
    for table in ("user_badge", "challenge_day_log", "user_challenge", "challenge", "badge"):
        if _table_exists(table):
            op.drop_table(table)
