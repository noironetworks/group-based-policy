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

"""add snat subnet only extension for subnets

Revision ID: f8dc1eb9deaf
Revises: 872bf4ba86a6

"""

# revision identifiers, used by Alembic.
revision = 'f8dc1eb9deaf'
down_revision = '872bf4ba86a6'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('apic_aim_subnet_extensions',
                  sa.Column('snat_subnet_only',
                            sa.Boolean))


def downgrade():
    pass
