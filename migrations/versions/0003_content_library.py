"""Content library: exercise and food tables for admin CRUD (Step 3).

Guarded like the earlier revisions — fresh databases get both tables from
create_all, so every op checks before it acts.

Revision ID: 0003_content_library
Revises: 0002_user_management
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0003_content_library"
down_revision = "0002_user_management"
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


def upgrade() -> None:
    if not _table_exists("exercise"):
        op.create_table(
            "exercise",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("force", sa.String(length=20)),
            sa.Column("level", sa.String(length=20)),
            sa.Column("mechanic", sa.String(length=20)),
            sa.Column("equipment", sa.String(length=60)),
            sa.Column("category", sa.String(length=40)),
            sa.Column("primary_muscles", sa.String(length=200)),
            sa.Column("secondary_muscles", sa.String(length=200)),
            sa.Column("instructions", sa.Text()),
            sa.Column("image_url", sa.String(length=300)),
            sa.Column("source", sa.String(length=20), nullable=False, server_default="free-exercise-db"),
        )
        op.create_index("ix_exercise_name", "exercise", ["name"], unique=True)

    if not _table_exists("food"):
        op.create_table(
            "food",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("serving", sa.String(length=60), nullable=False, server_default="100 g"),
            sa.Column("calories", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("protein", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("carbs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fats", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_food_name", "food", ["name"], unique=True)


def downgrade() -> None:
    if _table_exists("food"):
        op.drop_table("food")
    if _table_exists("exercise"):
        op.drop_table("exercise")
