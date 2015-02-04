# Copyright 2014 OpenStack Foundation
#
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

"""gbp_l2p_broadcast

Revision ID: f5f138aa6726
Revises: e8005b9b1efc

"""

# revision identifiers, used by Alembic.
revision = 'f5f138aa6726'
down_revision = 'e8005b9b1efc'


from alembic import op
import sqlalchemy as sa


def upgrade():

    op.add_column(
        'gp_l2_policies',
        sa.Column('allow_broadcast', sa.Boolean)
    )


def downgrade():
    op.drop_column('gp_l2_policies', 'allow_broadcast')
