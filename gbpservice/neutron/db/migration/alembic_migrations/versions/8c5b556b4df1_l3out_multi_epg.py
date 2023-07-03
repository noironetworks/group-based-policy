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

"""add l3out multiple epgs network extension

Revision ID: 8c5b556b4df1
Revises: e29a84f6a15f

"""

# revision identifiers, used by Alembic.
revision = '8c5b556b4df1'
down_revision = 'e29a84f6a15f'

from alembic import op
import sqlalchemy as sa
from sqlalchemy import sql


def upgrade():
    op.add_column('apic_aim_network_extensions',
                  sa.Column('multi_ext_nets', sa.Boolean,
                            nullable=False, server_default=sql.false()))
    pass


def downgrade():
    pass
