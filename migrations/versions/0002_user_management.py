"""User management: user.status + nullable audit_log.admin_id.

Guarded like 0001 — create_all/boot bootstrap may already have applied parts
of this. `status` lets admins suspend/ban accounts (active | suspended |
banned); admin_id becomes nullable so deleting an admin user drops the
attribution instead of the audit history.

Revision ID: 0002_user_management
Revises: 0001_admin_foundation
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0002_user_management"
down_revision = "0001_admin_foundation"
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


def _column_is_nullable(table: str, column: str) -> bool:
    bind = _bind()
    if bind.dialect.name == "sqlite":
        for row in bind.execute(text(f'PRAGMA table_info("{table}")')):
            if row[1] == column:
                return not row[3]  # PRAGMA: (cid, name, type, notnull, ...)
        return True
    row = bind.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).first()
    return bool(row and row[0] == "YES")


def _index_exists(table: str, name: str) -> bool:
    bind = _bind()
    if bind.dialect.name == "sqlite":
        row = bind.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": name},
        ).first()
    else:
        row = bind.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :name"), {"name": name}
        ).first()
    return row is not None


def upgrade() -> None:
    if "status" not in _existing_columns("user"):
        op.add_column(
            "user",
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        )
        op.create_index("ix_user_status", "user", ["status"])
    elif not _index_exists("user", "ix_user_status"):
        op.create_index("ix_user_status", "user", ["status"])

    if not _column_is_nullable("audit_log", "admin_id"):
        with op.batch_alter_table("audit_log") as batch:
            batch.alter_column("admin_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    bind = _bind()
    if bind.dialect.name != "sqlite":
        # Attribution for deleted admins cannot be restored; best effort only.
        try:
            with op.batch_alter_table("audit_log") as batch:
                batch.alter_column("admin_id", existing_type=sa.Integer(), nullable=False)
        except Exception:
            pass

    if "status" in _existing_columns("user"):
        try:
            op.drop_index("ix_user_status", table_name="user")
        except Exception:
            pass
        op.drop_column("user", "status")
