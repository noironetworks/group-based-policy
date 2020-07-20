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

"""Table for cisco_apic network master contracts extension attributes

Revision ID: 42e3f4b77b54
Revises: 27583c259fa7
Create Date: 2019-12-20 13:08:03.603312

"""

# revision identifiers, used by Alembic.
revision = '42e3f4b77b54'
down_revision = '27583c259fa7'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table(
        'apic_aim_network_epg_contract_masters',
        sa.Column('network_id', sa.String(36), nullable=False),
        sa.Column('app_profile_name', sa.String(64), nullable=False),
        sa.Column('name', sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ['network_id'], ['networks.id'],
            name='apic_aim_network_epg_master_extn_fk_network',
            ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('network_id', 'app_profile_name', 'name')
    )


def downgrade():
    pass
