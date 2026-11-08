"""Food macros to floats + per-unit serving data (unit_grams JSON).

Per-100 g macros rounded to integers lose real precision (e.g. 0.4 g protein
becomes 0), which the portion-scaling matcher then multiplies up. unit_grams
stores per-unit weights like {"roti": 40, "katori": 150, "egg": 50} so the
matcher can convert "2 rotis" without guessing a serving string.

Revision ID: 0010_food_macro_floats_unit_grams
Revises: 0009_food_aliases
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_food_macro_floats_unit_grams"
down_revision = "0009_food_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("food") as batch:
        batch.alter_column("protein", existing_type=sa.Integer(), type_=sa.Float(), existing_nullable=False)
        batch.alter_column("carbs", existing_type=sa.Integer(), type_=sa.Float(), existing_nullable=False)
        batch.alter_column("fats", existing_type=sa.Integer(), type_=sa.Float(), existing_nullable=False)
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("food")}
        if "unit_grams" not in columns:
            batch.add_column(sa.Column("unit_grams", sa.Text(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("food") as batch:
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("food")}
        if "unit_grams" in columns:
            batch.drop_column("unit_grams")
        batch.alter_column("protein", existing_type=sa.Float(), type_=sa.Integer(), existing_nullable=False)
        batch.alter_column("carbs", existing_type=sa.Float(), type_=sa.Integer(), existing_nullable=False)
        batch.alter_column("fats", existing_type=sa.Float(), type_=sa.Integer(), existing_nullable=False)
