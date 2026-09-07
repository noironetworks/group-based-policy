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

"""Drop deprecated NFP (Network Function Proxy) database tables and schemas.

The NFP subsystem was deprecated in April 2022 and removed from the codebase.
This migration drops all NFP-related database tables and Enum types.

Revision ID: 0079d13e4559
Revises: f0c1d2e3a4b5
Create Date: 2026-08-31 00:00:00.000000
"""

# revision identifiers, used by Alembic.
revision = '0079d13e4559'
down_revision = 'f0c1d2e3a4b5'


from alembic import op


def upgrade():
    """Drop all NFP-related tables and Enum types.

    Tables are dropped in reverse order of dependency to
    avoid constraint violations.
    """

    # Drop dependent tables before the NFP tables they reference. The
    # NCP mapping table was already removed by migration 47b999aa98e3.
    op.drop_table('nfp_network_function_device_interfaces')
    op.drop_table('nfd_cluster_mapping_info')
    op.drop_table('nfp_service_gateway_info')
    op.drop_table('nfp_nfi_dataport_associations')
    op.drop_table('nfp_network_function_instances')
    op.drop_table('nfp_network_function_devices')
    op.drop_table('nfp_network_functions')
    op.drop_table('nfp_network_infos')
    op.drop_table('nfp_port_infos')

    if op.get_bind().dialect.name == 'postgresql':
        op.execute("DROP TYPE IF EXISTS port_model")
        op.execute("DROP TYPE IF EXISTS port_classification")
        op.execute("DROP TYPE IF EXISTS port_role")
        op.execute("DROP TYPE IF EXISTS network_model")


def downgrade():
    """Downgrade is not supported.

    The NFP subsystem has been completely removed from the codebase.
    Downgrading would require re-introducing significant deprecated
    infrastructure. Manual intervention would be required to restore
    the NFP database schema.
    """
    pass
