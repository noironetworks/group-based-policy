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

"""add epg subnet extension for subnets

Revision ID: 90e1d92a49b2
Revises: 47b999aa98e3

"""

# revision identifiers, used by Alembic.
revision = '90e1d92a49b2'
down_revision = '47b999aa98e3'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('apic_aim_subnet_extensions',
                  sa.Column('epg_subnet',
                            sa.Boolean,
                            server_default=sa.false()))


def downgrade():
    pass
