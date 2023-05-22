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

"""sg_rule remote_group_id insertion

Revision ID: 7eaf537d927f
Revises: f28141ea1bbf
Create Date: 2020-05-06 00:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = '7eaf537d927f'
down_revision = 'f28141ea1bbf'

from alembic import op
from alembic import util
import sqlalchemy as sa


def upgrade():
    # See if AIM is being used, and if so, migrate data.
    bind = op.get_bind()
    insp = sa.engine.reflection.Inspector.from_engine(bind)
    if 'aim_tenants' in insp.get_table_names():
        try:
            # Note - this cannot be imported unless we know the
            # apic_aim mechanism driver is deployed, since the AIM
            # library may not be installed.
            from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
                data_migrations)

            session = sa.orm.Session(bind=bind, autocommit=True)
            data_migrations.do_sg_rule_remote_group_id_insertion(session)
        except ImportError:
            util.warning("AIM schema present, but failed to import AIM"
                        "libraries - SG rules remote_group_id not inserted.")
        except Exception as e:
            util.warning("Caught exception inserting SG rules remote_group_id:"
                        "%s" % e)


def downgrade():
    pass
