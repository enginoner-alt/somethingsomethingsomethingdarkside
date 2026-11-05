"""transcript edits: segment originals, transcript revision, insight revision

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("segments", sa.Column("original_text", sa.Text(), nullable=True))
    op.add_column("segments", sa.Column("original_speaker", sa.String(length=50), nullable=True))
    op.add_column("transcripts", sa.Column("rev", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("insights", sa.Column("transcript_rev", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("insights", "transcript_rev")
    op.drop_column("transcripts", "rev")
    op.drop_column("segments", "original_speaker")
    op.drop_column("segments", "original_text")
