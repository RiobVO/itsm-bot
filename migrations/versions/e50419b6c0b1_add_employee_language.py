"""add employee language

Revision ID: e50419b6c0b1
Revises: 94bb03b5d513
Create Date: 2026-09-14 15:44:39.585886

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e50419b6c0b1'
down_revision: Union[str, Sequence[str], None] = '94bb03b5d513'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Колонка и CHECK заводятся раздельно. `sa.Enum(create_constraint=True)` внутри
    batch-режима SQLite выписывает ограничение дважды — один раз из DDL колонки и
    один раз при пересборке таблицы, — и откат потом падает на копировании таблицы,
    в которой ограничение ссылается на уже удалённую колонку.
    """
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'language',
                sa.String(length=16),
                nullable=False,
                # Профили, заведённые до появления языка, отвечают по-русски.
                # Без server_default ALTER TABLE на непустой таблице не выполнится.
                server_default='ru',
            )
        )
        batch_op.create_check_constraint('language', "language IN ('ru', 'en')")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_constraint('language', type_='check')
        batch_op.drop_column('language')
