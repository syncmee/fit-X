"""Add dashboard indexes, request idempotency, and coach response audit data.

Revision ID: 0008_performance_and_request_safety
Revises: 0007_challenge_audience
"""

import sqlalchemy as sa
from alembic import op


revision = "0008_performance_and_request_safety"
down_revision = "0007_challenge_audience"
branch_labels = None
depends_on = None


def _indexes(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for name, table, columns in (
        ("ix_meal_entry_user_logged_at", "meal_entry", ["user_id", "logged_at"]),
        ("ix_water_log_user_logged_at", "water_log", ["user_id", "logged_at"]),
        ("ix_weight_log_user_date", "weight_log", ["user_id", "date"]),
        ("ix_scheduled_workout_user_scheduled_for", "scheduled_workout", ["user_id", "scheduled_for"]),
        ("ix_coach_message_user_created_at", "coach_message", ["user_id", "created_at"]),
    ):
        if name not in _indexes(table):
            op.create_index(name, table, columns)
    if "raw_model_json" not in _columns("coach_message"):
        op.add_column("coach_message", sa.Column("raw_model_json", sa.Text(), nullable=True))
    if not sa.inspect(op.get_bind()).has_table("idempotency_key"):
        op.create_table(
            "idempotency_key",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),
        )
    if "ix_idempotency_key_created_at" not in _indexes("idempotency_key"):
        op.create_index("ix_idempotency_key_created_at", "idempotency_key", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_key_created_at", table_name="idempotency_key")
    op.drop_table("idempotency_key")
    op.drop_column("coach_message", "raw_model_json")
    for name, table in (
        ("ix_coach_message_user_created_at", "coach_message"),
        ("ix_scheduled_workout_user_scheduled_for", "scheduled_workout"),
        ("ix_weight_log_user_date", "weight_log"),
        ("ix_water_log_user_logged_at", "water_log"),
        ("ix_meal_entry_user_logged_at", "meal_entry"),
    ):
        op.drop_index(name, table_name=table)
