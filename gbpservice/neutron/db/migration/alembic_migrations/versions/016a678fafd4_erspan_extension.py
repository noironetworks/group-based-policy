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

"""port_erspan_extension

Revision ID: 016a678fafd4
Revises: bda3c34581e0
Create Date: 2020-11-03 00:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = '016a678fafd4'
down_revision = 'bda3c34581e0'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table(
        'apic_aim_port_erspan_configurations',
        sa.Column('port_id', sa.String(36), nullable=False),
        sa.Column('dest_ip', sa.String(64), nullable=False),
        sa.Column('flow_id', sa.Integer, nullable=False),
        sa.Column('direction', sa.Enum('in', 'out', 'both'), nullable=False),
        sa.ForeignKeyConstraint(
            ['port_id'], ['ports.id'],
            name='apic_aim_port_erspan_extensions_fk_port',
            ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('port_id', 'dest_ip', 'flow_id', 'direction'))


def downgrade():
    pass
