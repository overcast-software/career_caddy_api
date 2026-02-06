"""Add favorite columns to models

Revision ID: add_favorite_columns
Revises: 
Create Date: 2026-01-13

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = 'add_favorite_columns'
down_revision = None  # Update this to your latest migration
branch_labels = None
depends_on = None

def upgrade():
    # Add favorite column to answer table
    op.add_column('answer', sa.Column('favorite', sa.Boolean(), nullable=False, server_default='false'))
    
    # Add favorite column to question table  
    op.add_column('question', sa.Column('favorite', sa.Boolean(), nullable=False, server_default='false'))
    
    # Add favorite column to cover_letter table
    op.add_column('cover_letter', sa.Column('favorite', sa.Boolean(), nullable=False, server_default='false'))
    
    # Add favorite column to resume table
    op.add_column('resume', sa.Column('favorite', sa.Boolean(), nullable=False, server_default='false'))

def downgrade():
    # Remove favorite columns
    op.drop_column('resume', 'favorite')
    op.drop_column('cover_letter', 'favorite') 
    op.drop_column('question', 'favorite')
    op.drop_column('answer', 'favorite')
