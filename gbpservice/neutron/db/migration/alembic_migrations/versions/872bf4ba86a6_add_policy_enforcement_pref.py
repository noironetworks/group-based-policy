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

"""add policy enforcement pref attribute

Revision ID: 872bf4ba86a6
Revises: 016a678fafd4

"""

# revision identifiers, used by Alembic.
revision = '872bf4ba86a6'
down_revision = '016a678fafd4'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('apic_aim_network_extensions',
                  sa.Column('policy_enforcement_pref',
                            sa.Enum('unenforced', 'enforced', ''),
                            server_default="unenforced",
                            nullable=False))


def downgrade():
    pass
