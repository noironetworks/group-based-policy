#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

"""add no nat cidrs network extension

Revision ID: e29a84f6a15f
Revises: 90e1d92a49b2

"""

# revision identifiers, used by Alembic.
revision = 'e29a84f6a15f'
down_revision = '90e1d92a49b2'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table(
        'apic_aim_network_no_nat_cidrs',
        sa.Column('network_id', sa.String(36), nullable=False),
        sa.Column('cidr', sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(['network_id'], ['networks.id'],
                        name='apic_aim_network_no_nat_cidrs_extn_fk_network',
                        ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('network_id', 'cidr')
    )


def downgrade():
    pass
