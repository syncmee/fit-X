"""Challenge audience segments: challenge.audience_segment names the
SEGMENTS key (app/challenges.py) a segment-audience challenge targets.

Revision ID: 0007_challenge_audience
Revises: 0006_coach_telemetry
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0007_challenge_audience"
down_revision = "0006_coach_telemetry"
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
    if "audience_segment" not in _existing_columns("challenge"):
        op.add_column("challenge", sa.Column("audience_segment", sa.String(length=20)))


def downgrade() -> None:
    if "audience_segment" in _existing_columns("challenge"):
        try:
            op.drop_column("challenge", "audience_segment")
        except Exception:  # SQLite pre-3.35 has no DROP COLUMN
            pass
