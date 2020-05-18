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

"""Fix device ID and VM lengths in VM names table

Revision ID: 799f0516bc08
Revises: 7eaf537d927f
Create Date: 2020-05-15 14:18:11.909757

"""

# revision identifiers, used by Alembic.
revision = '799f0516bc08'
down_revision = '7eaf537d927f'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.alter_column('apic_aim_vm_names',
                    'device_id',
                    existing_type=sa.String(36),
                    type_=sa.String(255), nullable=False)
    op.alter_column('apic_aim_vm_names',
                    'vm_name',
                    existing_type=sa.String(64),
                    type_=sa.String(255), nullable=False)


def downgrade():
    pass
