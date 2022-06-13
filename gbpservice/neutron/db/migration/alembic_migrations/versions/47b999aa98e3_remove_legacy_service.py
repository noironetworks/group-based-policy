# Copyright 2022 OpenStack Foundation
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

from alembic import op


"""remove_legacy_service

Revision ID: 47b999aa98e3
Revises: 68fcb81878c5
Create Date: 2022-05-23 16:17:16.315523

"""

# revision identifiers, used by Alembic.
revision = '47b999aa98e3'
down_revision = '68fcb81878c5'


def upgrade():
    op.drop_table('service_profiles')
    op.drop_table('sc_specs')
    op.drop_table('sc_instances')
    op.drop_table('sc_nodes')
    op.drop_table('sc_instance_spec_mappings')
    op.drop_table('sc_spec_node_associations')
    op.drop_table('ncp_node_instance_network_function_mappings')
    op.drop_table('ncp_node_instance_stacks')
    op.drop_table('gpm_ptgs_servicechain_mapping')
    op.drop_table('ncp_node_to_driver_mapping')
    op.drop_table('ncp_service_targets')
