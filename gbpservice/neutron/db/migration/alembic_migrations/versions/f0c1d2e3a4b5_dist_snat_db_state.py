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

"""add distributed snat db state

Revision ID: f0c1d2e3a4b5
Revises: e70e4d426ce0

"""

# revision identifiers, used by Alembic.
revision = 'f0c1d2e3a4b5'
down_revision = 'e70e4d426ce0'

from alembic import op
import sqlalchemy as sa
from sqlalchemy import sql


def upgrade():
    op.add_column(
        'apic_aim_network_extensions',
        sa.Column('service_network_enable', sa.Boolean, nullable=False,
                  server_default=sql.false()))
    op.add_column(
        'apic_aim_subnet_extensions',
        sa.Column('service_network_id', sa.String(36)))
    op.add_column(
        'apic_aim_subnet_extensions',
        sa.Column('dist_snat_start_port', sa.Integer))
    op.add_column(
        'apic_aim_subnet_extensions',
        sa.Column('dist_snat_end_port', sa.Integer))
    op.add_column(
        'apic_aim_subnet_extensions',
        sa.Column('dist_snat_alloc_size', sa.Integer))
    op.create_table(
        'apic_aim_dist_snat_mappings',
        sa.Column('snat_ip', sa.String(64), nullable=False),
        sa.Column('host_name', sa.String(255), nullable=False),
        sa.Column('start_port', sa.Integer, nullable=False),
        sa.Column('end_port', sa.Integer, nullable=False),
        sa.Column('subnet_id', sa.String(36)),
        sa.Column('service_port_id', sa.String(36)),
        sa.PrimaryKeyConstraint('snat_ip', 'host_name', 'start_port'))
    pass


def downgrade():
    pass
