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

"""HPP Table

Revision ID: fd3e605a7232
Revises: dc99863d1f2b
Create Date: 2023-09-19 02:08:54.252877

"""

# revision identifiers, used by Alembic.
revision = 'fd3e605a7232'
down_revision = 'dc99863d1f2b'

from alembic import op
from alembic import util
import sqlalchemy as sa


def upgrade():

    bind = op.get_bind()
    op.create_table(
        'apic_aim_hpp',
        sa.Column('hpp_normalized', sa.Boolean, nullable=False),
        sa.PrimaryKeyConstraint('hpp_normalized'))

    try:
        from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
             data_migrations)

        session = sa.orm.Session(bind=bind, autocommit=True)
        with session.begin(subtransactions=True):
            data_migrations.do_hpp_insertion(session)
    except Exception as e:
        util.warn("Caught exception while migrating data in %s: %s" %
            ('apic_aim_hpp', e))


def downgrade():
    pass
