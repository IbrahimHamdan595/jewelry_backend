"""add categories table and category_id to products

Revision ID: a1b2c3d4e5f6
Revises: 64874dd3486c
Create Date: 2026-05-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "64874dd3486c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name_en", sa.String(), nullable=False),
        sa.Column("name_ar", sa.String(), nullable=False, server_default=""),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name_en"),
        sa.UniqueConstraint("slug"),
    )

    # Seed categories from existing distinct product category strings
    op.execute("""
        INSERT INTO categories (id, name_en, name_ar, slug, is_active)
        SELECT
            encode(sha256(category::bytea), 'hex'),
            category,
            '',
            lower(regexp_replace(category, '[^a-zA-Z0-9]+', '-', 'g')),
            true
        FROM (SELECT DISTINCT category FROM products) AS sq
        ON CONFLICT DO NOTHING
    """)

    op.add_column("products", sa.Column("category_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_products_category_id",
        "products", "categories",
        ["category_id"], ["id"],
    )

    # Populate category_id for existing products
    op.execute("""
        UPDATE products p
        SET category_id = c.id
        FROM categories c
        WHERE c.name_en = p.category
    """)


def downgrade() -> None:
    op.drop_constraint("fk_products_category_id", "products", type_="foreignkey")
    op.drop_column("products", "category_id")
    op.drop_table("categories")
