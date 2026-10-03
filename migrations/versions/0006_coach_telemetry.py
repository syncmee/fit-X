"""Coach telemetry: provider, latency, error flag and token usage on
coach_message, feeding the admin AI-coach monitoring page (Step 10).

Revision ID: 0006_coach_telemetry
Revises: 0005_challenge_reminders
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0006_coach_telemetry"
down_revision = "0005_challenge_reminders"
branch_labels = None
depends_on = None

_NEW_COLUMNS = (
    ("provider", sa.String(length=20)),
    ("latency_ms", sa.Integer()),
    ("had_error", sa.Boolean()),
    ("prompt_tokens", sa.Integer()),
    ("completion_tokens", sa.Integer()),
)


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
    existing = _existing_columns("coach_message")
    for name, col_type in _NEW_COLUMNS:
        if name not in existing:
            kwargs = {}
            if name == "had_error":
                kwargs["server_default"] = sa.false()
            op.add_column("coach_message", sa.Column(name, col_type, **kwargs))


def downgrade() -> None:
    existing = _existing_columns("coach_message")
    for name, _col_type in _NEW_COLUMNS:
        if name in existing:
            try:
                op.drop_column("coach_message", name)
            except Exception:  # SQLite pre-3.35 has no DROP COLUMN
                pass
