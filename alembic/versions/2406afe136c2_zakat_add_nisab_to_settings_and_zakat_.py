"""zakat: add nisab to settings and zakat_snapshots table

Revision ID: 2406afe136c2
Revises: a1b2c3d4e5f7
Create Date: 2026-05-24 01:49:34.862850

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2406afe136c2'
down_revision: Union[str, None] = 'a1b2c3d4e5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add nisab_grams to settings. server_default backfills existing rows;
    #    column stays NOT NULL going forward.
    op.add_column(
        'settings',
        sa.Column(
            'nisab_grams',
            sa.Numeric(precision=10, scale=3),
            nullable=False,
            server_default='85.000',
        ),
    )

    # 2. Immutable zakat snapshots table.
    op.create_table(
        'zakat_snapshots',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('taken_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('assessment_date', sa.Date(), nullable=False),
        sa.Column('taken_by_user_id', sa.String(), nullable=False),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('gold_rate_24k_usd_per_gram', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('gold_rate_source', sa.String(), nullable=False),
        sa.Column('nisab_grams_used', sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column('total_au_grams', sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column('total_au_value_usd', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('zakat_au_grams', sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column('zakat_value_usd', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('meets_nisab', sa.Boolean(), nullable=False),
        sa.Column('breakdown_by_karat', sa.JSON(), nullable=False),
        sa.Column('integrity_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['taken_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_zakat_snapshots_assessment_date',
        'zakat_snapshots',
        ['assessment_date'],
        unique=False,
    )
    op.create_index(
        'ix_zakat_snapshots_taken_at',
        'zakat_snapshots',
        ['taken_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_zakat_snapshots_taken_at', table_name='zakat_snapshots')
    op.drop_index('ix_zakat_snapshots_assessment_date', table_name='zakat_snapshots')
    op.drop_table('zakat_snapshots')
    op.drop_column('settings', 'nisab_grams')
