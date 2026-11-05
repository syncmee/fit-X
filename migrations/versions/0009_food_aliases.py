"""Add portable food aliases for catalogue matching.

Revision ID: 0009_food_aliases
Revises: 0008_performance_and_request_safety
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_food_aliases"
down_revision = "0008_performance_and_request_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("food")}
    if "aliases" not in columns:
        op.add_column("food", sa.Column("aliases", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    if "aliases" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("food")}:
        op.drop_column("food", "aliases")
