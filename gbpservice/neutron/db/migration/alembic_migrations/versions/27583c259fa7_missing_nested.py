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

"""Fix missing nested domain DB migration

Revision ID: 27583c259fa7
Revises: 799f0516bc08
Create Date: 2020-05-27 14:18:11.909757

"""

# revision identifiers, used by Alembic.
revision = '27583c259fa7'
down_revision = '799f0516bc08'

import os
import sys

from neutron.db import migration

from oslo_utils import importutils

from gbpservice.neutron.db.migration import alembic_migrations as am

# This is a hack to get around the fact that the versions
# directory has no __init__.py
filepath = os.path.abspath(am.__file__)
basepath = filepath[:filepath.rfind("/")] + "/versions"
sys.path.append(basepath)

DB_4967af35820f = '4967af35820f_cisco_apic_nested_domain'


def ensure_4967af35820f_migration():
    if not migration.schema_has_table(
            'apic_aim_network_nested_domain_allowed_vlans'):
        db_4967af35820f = importutils.import_module(DB_4967af35820f)
        db_4967af35820f.upgrade()


def upgrade():
    ensure_4967af35820f_migration()
    # remove the appended path
    del sys.path[sys.path.index(basepath)]


def downgrade():
    pass
