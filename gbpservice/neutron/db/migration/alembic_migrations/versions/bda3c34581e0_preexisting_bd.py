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

"""Pre-existing BD

Revision ID: bda3c34581e0
Revises: 42e3f4b77b54
Create Date: 2020-08-05 14:18:11.909757

"""

# revision identifiers, used by Alembic.
revision = 'bda3c34581e0'
down_revision = '42e3f4b77b54'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('apic_aim_network_extensions',
                  sa.Column('bridge_domain_dn', sa.String(1024),
                            nullable=True))


def downgrade():
    pass
