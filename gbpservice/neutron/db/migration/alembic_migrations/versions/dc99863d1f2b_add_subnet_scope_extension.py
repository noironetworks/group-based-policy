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

"""adds subnet scope extension support

Revision ID: dc99863d1f2b
Revises: 8c5b556b4df1

"""

# revision identifiers, used by Alembic.
revision = 'dc99863d1f2b'
down_revision = '8c5b556b4df1'

from alembic import op
import sqlalchemy as sa
from sqlalchemy import sql


def upgrade():
    op.add_column('apic_aim_subnet_extensions',
                  sa.Column('advertised_externally', sa.Boolean,
                            nullable=False, server_default=sql.true()))
    op.add_column('apic_aim_subnet_extensions',
                  sa.Column('shared_between_vrfs', sa.Boolean,
                            nullable=False, server_default=sql.false()))
    pass


def downgrade():
    pass
