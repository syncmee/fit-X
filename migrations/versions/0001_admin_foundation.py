"""Admin foundation: user.is_admin / created_at / last_active_at + audit_log.

Guarded on purpose: the app's boot bootstrap (create_all + guarded ALTERs in
app/__init__.py) may have already applied some or all of this before Alembic
first runs, and a fresh database gets every table from create_all. Every op
therefore checks before it acts, so `flask db upgrade` is safe to run at any
point on any database state.

Revision ID: 0001_admin_foundation
Revises:
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0001_admin_foundation"
down_revision = None
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _existing_columns(table: str) -> set[str]:
    bind = _bind()
    if bind.dialect.name == "sqlite":
        return {row[1] for row in bind.execute(text(f'PRAGMA table_info("{table}")'))}
    return set(
        bind.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table"
            ),
            {"table": table},
        ).scalars()
    )


def _table_exists(table: str) -> bool:
    bind = _bind()
    if bind.dialect.name == "sqlite":
        row = bind.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table"),
            {"table": table},
        ).first()
    else:
        row = bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :table"
            ),
            {"table": table},
        ).first()
    return row is not None


def _backfill_created_at() -> None:
    """Signup stamps for pre-existing users: earliest known activity per
    user, falling back to now. Only fills NULLs, so it is idempotent."""
    activity = (
        ("weight_log", "date"),
        ("meal_entry", "logged_at"),
        ("water_log", "logged_at"),
        ("coach_message", "created_at"),
        ("scheduled_workout", "created_at"),
    )
    for table, column in activity:
        op.execute(
            f'UPDATE "user" SET created_at = (SELECT MIN({column}) FROM {table} '
            f'WHERE {table}.user_id = "user".id) WHERE created_at IS NULL'
        )
    op.execute('UPDATE "user" SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL')


def upgrade() -> None:
    user_columns = _existing_columns("user")

    if "is_admin" not in user_columns:
        op.add_column("user", sa.Column("is_admin", sa.Boolean(), server_default=sa.false()))
    if "created_at" not in user_columns:
        op.add_column("user", sa.Column("created_at", sa.DateTime()))
    if "last_active_at" not in user_columns:
        op.add_column("user", sa.Column("last_active_at", sa.DateTime()))

    _backfill_created_at()

    if not _table_exists("audit_log"):
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("admin_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
            sa.Column("action", sa.String(length=80), nullable=False),
            sa.Column("target", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("timestamp", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_audit_log_admin_id", "audit_log", ["admin_id"])
        op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])


def downgrade() -> None:
    # Best effort: SQLite pre-3.35 has no DROP COLUMN, so failures there are
    # tolerated rather than failing the rollback.
    if _table_exists("audit_log"):
        op.drop_table("audit_log")

    user_columns = _existing_columns("user")
    for name in ("is_admin", "created_at", "last_active_at"):
        if name in user_columns:
            try:
                op.drop_column("user", name)
            except Exception:
                pass
