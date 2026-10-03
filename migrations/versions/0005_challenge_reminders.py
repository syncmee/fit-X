"""Challenge reminders: add UserChallenge.last_reminded_at for the daily
not-logged-today reminder throttle.

Revision ID: 0005_challenge_reminders
Revises: 0004_challenges
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0005_challenge_reminders"
down_revision = "0004_challenges"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return {row[1] for row in bind.execute(text(f'PRAGMA table_info("{table}")'))}
    return set(
        bind.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
            {"table": table},
        ).scalars()
    )


def upgrade() -> None:
    if "last_reminded_at" not in _existing_columns("user_challenge"):
        op.add_column("user_challenge", sa.Column("last_reminded_at", sa.DateTime()))


def downgrade() -> None:
    if "last_reminded_at" in _existing_columns("user_challenge"):
        try:
            op.drop_column("user_challenge", "last_reminded_at")
        except Exception:  # SQLite pre-3.35 has no DROP COLUMN
            pass
