# Copyright (c) 2016 Cisco Systems
# All Rights Reserved.
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

"""Distributed SNAT Helper Mixin for ApicMechanismDriver.

This module provides methods for programming AIM policy resources required
for distributed SNAT functionality in group-based-policy.
"""

import logging

from aim.aim_lib import service_graph as aim_sg
from aim.api import resource as aim_resource
from aim.api import service_graph as aim_service_graph

from gbpservice.neutron.extensions import cisco_apic
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    constants as aim_cst)
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import exceptions

LOG = logging.getLogger(__name__)

COMMON_TENANT_NAME = aim_cst.COMMON_TENANT_NAME
UNROUTED_VRF_NAME = aim_cst.UNROUTED_VRF_NAME
DEFAULT_SNAT_PORT_MAX = aim_cst.DEFAULT_SNAT_PORT_MAX


class DistributedSnatHelper(object):
    """Mixin providing distributed SNAT policy programming methods."""

    def _sanitize_snat_name(self, value):
        """Return an AIM-safe name preserving semantic identity."""
        return ''.join(
            c if c.isalnum() or c in ('-', '_', '.') else '_'
            for c in str(value))

    def _snat_contract_name(self, subnet_id):
        return 'snat_' + self._sanitize_snat_name(subnet_id)

    def _snat_contract_subject_name(self, subnet_id, physdom):
        subnet_name = self._sanitize_snat_name(subnet_id)
        pdom_name = self._sanitize_snat_name(physdom)
        return ('snat_' + subnet_name[:len(subnet_name) // 2] +
                '_' + pdom_name)

    def _service_graph_name(self, subnet_id, physdom):
        ext_net_name = self._sanitize_snat_name(subnet_id)
        pdom_name = self._sanitize_snat_name(physdom)
        return ('sg_' + ext_net_name[:len(ext_net_name) // 2] +
                '_' + pdom_name)

    def _device_cluster_name(self, service_net_id, physdom):
        svc_net_name = self._sanitize_snat_name(service_net_id)
        pdom_name = self._sanitize_snat_name(physdom)
        return ('svc_' + svc_net_name[:len(svc_net_name) // 2] +
                '_' + pdom_name)

    def _service_network_bd_name(self, network_id):
        return 'svc_' + self._sanitize_snat_name(network_id)

    def _snat_external_network_name(self, subnet_id):
        return 'snat_epg_' + self._generate_snat_resource_name(subnet_id)

    def _snat_monitor_policy_name(self, name):
        return 'mon_pol_' + name

    def _generate_snat_resource_name(self, resource_id):
        """Generate deterministic compact resource name.

        Args:
            resource_id: String identifier (e.g., subnet UUID, network UUID)

        Returns:
            str: Shortened AIM-safe resource name
        """
        # Keep this helper for resources where compact deterministic names
        # are still preferred. Contract/service-graph/device-cluster naming
        # follows explicit requirement-based helpers above.
        return self._sanitize_snat_name(resource_id)[:12]

    def _get_unrouted_vrf_name(self):
        """Get name for SNAT VRF in common tenant.

        Returns:
            str: VRF name for SNAT resources
        """
        if getattr(self, 'apic_system_id', None):
            return '%s_%s' % (self.apic_system_id, UNROUTED_VRF_NAME)
        return UNROUTED_VRF_NAME

    def _network_display_name(self, network, fallback):
        return (network.get('name') or fallback)

    def _subnet_display_name(self, subnet, fallback):
        return (subnet.get('name') or fallback)

    # =========================================================================
    # SERVICE NETWORK METHODS
    # =========================================================================

    def _create_service_network_bd(self, aim_ctx, network, tenant_name):
        """Create BridgeDomain for service network.

        Service networks are initially createdn in common/UnroutedVRF with no
        EPG.  Once they are referenced by another OpenStack external network,
        they are reparented to the VRF for the L3 Out that maps to that
        OpenStack external network.

        Args:
            aim_ctx: AimContext for AIM operations
            network: Neutron service network dict
            tenant_name: ACI tenant for the resource
        """
        bd_name = self._service_network_bd_name(network['id'])
        vrf_name = self._get_unrouted_vrf_name()

        bd = aim_resource.BridgeDomain(
            tenant_name=tenant_name,
            name=bd_name,
            display_name=self._network_display_name(network, bd_name),
            vrf_name=vrf_name,
            ip_learning=False,
            limit_ip_learn_to_subnets=True,
            service_bd_routing_disable=True,
            enable_routing=False)
        self.aim.create(aim_ctx, bd)

        LOG.debug("Created service network BD: %s in tenant %s", bd_name,
                  tenant_name)
        return bd

    def _reparent_service_network_bd(self, aim_ctx, service_network_id,
                                     tenant_name, l3out, enable_routing):
        """Update the service-network BD to a new VRF parent.

        Args:
            aim_ctx: AimContext for AIM operations
            service_network_id: Neutron network ID of the service network
            tenant_name: Tenant containing the service-network BD
            l3out: AIM L3 Out resource for the Neutron external network
            enable_routing: Whether BD routing should be enabled
        """
        real_bd = None
        vrf_name = l3out.vrf_name if l3out else self._get_unrouted_vrf_name()
        l3out_names = [l3out.name] if l3out else []
        bd_name = self._service_network_bd_name(service_network_id)
        bd = aim_resource.BridgeDomain(
            tenant_name=tenant_name,
            name=bd_name)
        real_bd = self.aim.get(aim_ctx, bd)
        if real_bd:
            self.aim.update(aim_ctx, real_bd, vrf_name=vrf_name,
                            l3out_names=l3out_names,
                            enable_routing=enable_routing)

            LOG.debug("Reparented service network BD %s to VRF %s with "
                      "enable_routing=%s", bd_name, vrf_name, enable_routing)
        else:
            raise exceptions.ServiceNetworkBdReparentFailed(
                bd_name=bd_name, tenant_name=tenant_name, vrf_name=vrf_name)

    def _delete_service_network_bd(self, aim_ctx, tenant_name, bd_name):
        """Delete BridgeDomain for service network.

        Cleans up BD and VRF on service network delete.

        Args:
            aim_ctx: AimContext for AIM operations
            network: Neutron network dict
            tenant_name: Project tenant name
        """
        # Delete BD
        bd = aim_resource.BridgeDomain(
            tenant_name=tenant_name,
            name=bd_name)
        self.aim.delete(aim_ctx, bd)

        LOG.debug(f"Deleted service network BD: {bd_name}")

    def _create_service_subnet_bd_subnet(self, aim_ctx, subnet, cidr,
                                         tenant_name, bd_name):
        """Create BD Subnet for service network subnet.

        Service subnet is private (not externally advertised).

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            bd: AIM BridgeDomain object
        """
        subnet_id = subnet['id']

        bd_subnet = aim_resource.Subnet(
            tenant_name=tenant_name,
            bd_name=bd_name,
            gw_ip_mask=cidr,
            display_name=self._subnet_display_name(subnet, subnet_id),
            scope='private')
        self.aim.create(aim_ctx, bd_subnet)

        LOG.debug("Created service subnet BD-subnet: %s for BD %s", cidr,
                  bd_name)
        return bd_subnet

    def _delete_service_subnet_bd_subnet(self, aim_ctx, tenant_name, bd_name,
                                         subnet, cidr):
        """Create BD Subnet for service network subnet.

        Service subnet is private (not externally advertised).

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            bd: AIM BridgeDomain object
        """
        subnet_id = subnet['id']

        bd_subnet = aim_resource.Subnet(
            tenant_name=tenant_name,
            bd_name=bd_name,
            gw_ip_mask=cidr,
            display_name=self._subnet_display_name(subnet, subnet_id),
            scope='private')
        self.aim.delete(aim_ctx, bd_subnet)

        LOG.debug("Deleted subnet BD-subnet: %s for BD %s", cidr,
                  bd_name)

    # =========================================================================
    # SNAT SUBNET METHODS
    # =========================================================================

    def _create_snat_external_network(self, aim_ctx, subnet_id,
                                      tenant_name, l3out, contract_name):
        """Create AIM ExternalNetwork for a distributed SNAT subnet.

        Create the external network/EPG under the Layer 3 Out policy for
        distributed SNAT, as well as the provided and consumed contract
        references for the newly created external network/EPG, as well
        as the one for the OpenStack external network.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Subnet ID of SNAT subnet
            tenant_name: name of the ACI tenant
            contract_name: Contract for distributed SNAT
        """
        snat_name = self._snat_external_network_name(subnet_id)

        snat_ext_net = aim_resource.ExternalNetwork(
            tenant_name=tenant_name,
            l3out_name=l3out.name,
            name=snat_name,
            display_name=snat_name)
        self.aim.create(aim_ctx, snat_ext_net)
        # The SNAT neetwork/EPG is the contract consumer.
        c_con = aim_resource.ExternalNetworkConsumedContract(
                    tenant_name=tenant_name, l3out_name=l3out.name,
                    ext_net_name=snat_ext_net.name, name=contract_name)
        self.aim.create(aim_ctx, c_con)
        # The external network/EPG for the OpenStack external network
        # is the SNAT contract provider.
        p_con = aim_resource.ExternalNetworkProvidedContract(
                    tenant_name=tenant_name, l3out_name=l3out.name,
                    ext_net_name=l3out.name, name=contract_name)
        self.aim.create(aim_ctx, p_con)

        LOG.debug("Created SNAT ExternalNetwork: %s in tenant %s", snat_name,
                  tenant_name)
        return snat_ext_net

    def _delete_snat_external_network(self, aim_ctx, subnet_id,
                                      tenant_name, l3out, contract_name):
        """Delete AIM ExternalNetwork for a distributed SNAT subnet.

        Delete the external network/EPG under the Layer 3 Out policy for
        distributed SNAT. Also remove the prrovided and consumed contract
        references.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Subnet ID of SNAT subnet
            tenant_name: name of the ACI tenant
            l3ou: AIM Layer 3 out policy resource
            contract_name: Contract for distributed SNAT
        """
        snat_name = self._snat_external_network_name(subnet_id)

        contract_name = self._snat_contract_name(subnet_id)
        snat_ext_net = aim_resource.ExternalNetwork(
            tenant_name=tenant_name,
            l3out_name=l3out.name,
            name=snat_name,
            display_name=snat_name)

        # The SNAT neetwork/EPG is the contract consumer.
        c_con = aim_resource.ExternalNetworkConsumedContract(
                    tenant_name=tenant_name, l3out_name=l3out.name,
                    ext_net_name=snat_ext_net.name, name=contract_name)
        consumed = self.aim.get(aim_ctx, c_con)
        if consumed:
            self.aim.delete(aim_ctx, c_con)
        # The external network/EPG for the OpenStack external network
        # is the SNAT contract provider.
        p_con = aim_resource.ExternalNetworkProvidedContract(
                    tenant_name=tenant_name, l3out_name=l3out.name,
                    ext_net_name=l3out.name, name=contract_name)
        provided = self.aim.get(aim_ctx, p_con)
        if provided:
            self.aim.delete(aim_ctx, p_con)

        snat_net = self.aim.get(aim_ctx, snat_ext_net)
        if snat_net:
            self.aim.delete(aim_ctx, snat_ext_net)

        LOG.debug("Deleted SNAT ExternalNetwork: %s in tenant %s", snat_name,
                  tenant_name)

    def _create_snat_filters(self, aim_ctx, subnet, tenant_name):
        """Create provider/consumer filters for configured port ranges.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron distributed SNAT subnet dict
            tenant_name: Project tenant name

        Returns:
            dict: Dictionary with 'provider_filter' and 'consumer_filter' keys
        """
        subnet_id = subnet['id']
        filter_hash = self._generate_snat_resource_name(subnet_id)

        # Get configured distributed SNAT port range from subnet attrs.
        start_port = subnet.get(cisco_apic.DIST_SNAT_START_PORT)
        end_port = subnet.get(cisco_apic.DIST_SNAT_END_PORT)
        start_port = start_port if start_port is not None else 0
        end_port = end_port if end_port is not None else DEFAULT_SNAT_PORT_MAX

        filters = {}

        # Provider filter entries match destination ports.
        provider_filter_name = f'snat_provider_{filter_hash}'
        provider_filter = aim_resource.Filter(
            tenant_name=tenant_name,
            name=provider_filter_name,
            display_name=provider_filter_name)
        self.aim.create(aim_ctx, provider_filter)

        provider_tcp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=provider_filter_name,
            name='provider_tcp_port_range',
            ether_type='ip',
            ip_protocol='tcp',
            source_from_port=start_port,
            source_to_port=end_port)
        self.aim.create(aim_ctx, provider_tcp_entry)

        provider_udp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=provider_filter_name,
            name='provider_udp_port_range',
            ether_type='ip',
            ip_protocol='udp',
            source_from_port=start_port,
            source_to_port=end_port)
        self.aim.create(aim_ctx, provider_udp_entry)
        filters['provider_filter'] = provider_filter

        # Consumer filter entries match source ports.
        consumer_filter_name = f'snat_consumer_{filter_hash}'
        consumer_filter = aim_resource.Filter(
            tenant_name=tenant_name,
            name=consumer_filter_name,
            display_name=consumer_filter_name)
        self.aim.create(aim_ctx, consumer_filter)

        consumer_tcp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=consumer_filter_name,
            name='consumer_tcp_port_range',
            ether_type='ip',
            ip_protocol='tcp',
            dest_from_port=start_port,
            dest_to_port=end_port)
        self.aim.create(aim_ctx, consumer_tcp_entry)

        consumer_udp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=consumer_filter_name,
            name='consumer_udp_port_range',
            ether_type='ip',
            ip_protocol='udp',
            dest_from_port=start_port,
            dest_to_port=end_port)
        self.aim.create(aim_ctx, consumer_udp_entry)
        filters['consumer_filter'] = consumer_filter

        LOG.debug("Created SNAT filters for subnet %s: ports %s-%s",
                  subnet_id, start_port, end_port)
        return filters

    def _delete_snat_filters(self, aim_ctx, subnet, tenant_name):
        """Delete provider/consumer filters for configured port ranges.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            tenant_name: Project tenant name

        Returns:
            dict: Dictionary with 'provider_filter' and 'consumer_filter' keys
        """
        subnet_id = subnet['id']
        filter_hash = self._generate_snat_resource_name(subnet_id)

        # Get configured distributed SNAT port range from subnet attrs.
        start_port = subnet.get(cisco_apic.DIST_SNAT_START_PORT)
        end_port = subnet.get(cisco_apic.DIST_SNAT_END_PORT)
        start_port = start_port if start_port is not None else 0
        end_port = end_port if end_port is not None else DEFAULT_SNAT_PORT_MAX

        filters = {}

        # Provider filter entries match destination ports.
        provider_filter_name = f'snat_provider_{filter_hash}'
        provider_filter = aim_resource.Filter(
            tenant_name=tenant_name,
            name=provider_filter_name,
            display_name=provider_filter_name)
        self.aim.delete(aim_ctx, provider_filter)

        provider_tcp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=provider_filter_name,
            name='provider_tcp_port_range',
            ether_type='ip',
            ip_protocol='tcp',
            dest_from_port=start_port,
            dest_to_port=end_port)
        self.aim.delete(aim_ctx, provider_tcp_entry)

        provider_udp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=provider_filter_name,
            name='provider_udp_port_range',
            ether_type='ip',
            ip_protocol='udp',
            dest_from_port=start_port,
            dest_to_port=end_port)
        self.aim.delete(aim_ctx, provider_udp_entry)
        filters['provider_filter'] = provider_filter

        # Consumer filter entries match source ports.
        consumer_filter_name = f'snat_consumer_{filter_hash}'
        consumer_filter = aim_resource.Filter(
            tenant_name=tenant_name,
            name=consumer_filter_name,
            display_name=consumer_filter_name)
        self.aim.delete(aim_ctx, consumer_filter)

        consumer_tcp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=consumer_filter_name,
            name='consumer_tcp_port_range',
            ether_type='ip',
            ip_protocol='tcp',
            source_from_port=start_port,
            source_to_port=end_port)
        self.aim.delete(aim_ctx, consumer_tcp_entry)

        consumer_udp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=consumer_filter_name,
            name='consumer_udp_port_range',
            ether_type='ip',
            ip_protocol='udp',
            source_from_port=start_port,
            source_to_port=end_port)
        self.aim.delete(aim_ctx, consumer_udp_entry)
        filters['consumer_filter'] = consumer_filter

        LOG.debug("Created SNAT filters for subnet %s: ports %s-%s",
                  subnet_id, start_port, end_port)
        return filters

    def _create_snat_contract(self, aim_ctx, subnet_id, tenant_name):
        """Create SNAT contract

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Neutron subnet ID for distributed SNAT
            tenant_name: ACI tenant name

        Returns:
            aim_resource.Contract: Created contract
        """
        # Create contract
        contract_name = self._snat_contract_name(subnet_id)
        contract = aim_resource.Contract(
            tenant_name=tenant_name,
            name=contract_name,
            scope='global',
            display_name=contract_name)
        self.aim.create(aim_ctx, contract)

        LOG.debug(f"Created SNAT contract: {contract_name} in tenant "
                 f"{tenant_name}")
        return contract

    def _delete_snat_contract(self, aim_ctx, subnet_id, tenant_name):
        """Delete SNAT contract

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Neutron subnet id
            tenant_name: Project tenant name
        """
        # Delete contract
        contract_name = self._snat_contract_name(subnet_id)
        contract = aim_resource.Contract(
            tenant_name=tenant_name,
            name=contract_name,
            scope='global',
            display_name=contract_name)
        self.aim.delete(aim_ctx, contract)

        LOG.debug(f"Deleted SNAT contract: {contract_name} in tenant "
                 f"{tenant_name}")

    def _create_snat_contract_subject(self, aim_ctx, subnet_id,
                                      contract_name, physdom,
                                      tenant_name, filters):
        """Create SNAT contract subject with TCP/UDP filters.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Neutron subnet ID
            contract_name: name of the contract
            physdom: physdom associated with this contract subject
            tenant_name: Project tenant name
            filters: Dict with 'provider_filter' and 'consumer_filter'

        Returns:
            aim_resource.Contract: Created contract
        """
        subject_name = self._snat_contract_subject_name(subnet_id, physdom)
        service_graph_name = self._service_graph_name(subnet_id, physdom)

        # FIXME: Should make the create and delete API parameters consistent

        # Create contract subject, binding provider and consumer filters.
        subject = aim_resource.ContractSubject(
            tenant_name=tenant_name,
            contract_name=contract_name,
            name=subject_name,
            display_name=subject_name,
            reverse_filter_ports=False,
            in_service_graph_name=service_graph_name,
            out_service_graph_name=service_graph_name,
            in_filters=[filters['provider_filter'].name],
            out_filters=[filters['consumer_filter'].name])

        subject_obj = self.aim.create(aim_ctx, subject)

        LOG.debug(f"Created SNAT contract: {subject_name} in tenant "
                  f"{tenant_name}")
        return subject_obj or subject

    def _delete_snat_contract_subject(self, aim_ctx, subnet_id,
                                      contract_name, physdom,
                                      tenant_name):
        """Delete SNAT contract subject with TCP/UDP filters.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            contract_name: name of the contract
            physdom: name of the physdom
            tenant_name: Project tenant name

        Returns:
            aim_resource.Contract: Created contract
        """
        subject_name = self._snat_contract_subject_name(subnet_id, physdom)
        service_graph_name = self._service_graph_name(subnet_id, physdom)

        filter_hash = self._generate_snat_resource_name(subnet_id)
        provider_filter_name = f'snat_provider_{filter_hash}'
        consumer_filter_name = f'snat_consumer_{filter_hash}'

        # Delete contract subject, binding provider and consumer filters.
        subject = aim_resource.ContractSubject(
            tenant_name=tenant_name,
            contract_name=contract_name,
            name=subject_name,
            display_name=subject_name,
            reverse_filter_ports=False,
            in_service_graph_name=service_graph_name,
            out_service_graph_name=service_graph_name,
            in_filters=[provider_filter_name],
            out_filters=[consumer_filter_name])

        self.aim.delete(aim_ctx, subject)

        LOG.debug(f"Deleted SNAT contrac subject: {subject_name} in tenant "
                  f"{tenant_name}")

    def _create_service_graph(self, aim_ctx, subnet_id, physdom, tenant_name):
        """Create service graph for traffic steering.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: SNAT subnet ID
            physdom: physdom for the service graph
            tenant_name: Project tenant name

        Returns:
            aim_resource.ServiceGraph: Created service graph
        """
        sg_name = self._service_graph_name(subnet_id, physdom)

        # Create or update the ServiceGraph. Note that
        # the linear_chain_nodes is left empty, as the
        # default behavior in AIM is to create L2
        # adjacencies. The node will instead be
        # referenced using ServiceGraphConnection
        # resources.
        sg = aim_service_graph.ServiceGraph(
            tenant_name=tenant_name,
            name=sg_name,
            display_name=sg_name,
            linear_chain_nodes=[])
        existing_sg = self.aim.get(aim_ctx, sg)
        if existing_sg:
            sg = existing_sg
            self.aim.create(aim_ctx, sg, overwrite=True)
        else:
            self.aim.create(aim_ctx, sg)

        LOG.debug(f"Created service graph: {sg_name} in tenant "
                 f"{tenant_name}")
        return sg

    def _delete_service_graph(self, aim_ctx, subnet_id, physdom, tenant_name):
        """Delete service graph for traffic steering.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: SNAT subnet ID
            physdom: physdom for the service graph
            tenant_name: Project tenant name
        """
        sg_name = self._service_graph_name(subnet_id, physdom)

        # Now delete the graph itself
        sg = aim_service_graph.ServiceGraph(
            tenant_name=tenant_name,
            name=sg_name,
            display_name=sg_name,
            linear_chain_nodes=[])
        existing_sg = self.aim.get(aim_ctx, sg)
        if existing_sg:
            self.aim.delete(aim_ctx, existing_sg)

        LOG.debug(f"Deleted service graph: {sg_name} in tenant "
                 f"{tenant_name}")

    def _create_service_graph_node(self, aim_ctx, service_graph_name,
                                  tenant_name):
        """Create loadbalancer node in service graph.

        Node has function_type='GoTo' and routing_mode='Redirect' for
        traffic steering.

        Args:
            aim_ctx: AimContext for AIM operations
            service_graph_name: service graph name
            tenant_name: Project tenant name

        Returns:
            aim_resource.ServiceGraphNode: Created node
        """
        node_name = 'loadbalancer'

        node = aim_service_graph.ServiceGraphNode(
            tenant_name=tenant_name,
            service_graph_name=service_graph_name,
            name=node_name,
            display_name=node_name,
            function_type='GoTo',  # Type for traffic steering
            routing_mode='Redirect',    # Enable PBR redirect
            managed=False,
            connectors=[aim_sg.PROVIDER, aim_sg.CONSUMER],
            sequence_number='0')
        existing_node = self.aim.get(aim_ctx, node)
        if existing_node:
            node = existing_node
            self.aim.create(aim_ctx, node, overwrite=True)
        else:
            self.aim.create(aim_ctx, node)

        # Create provider/consumer connectors for SNAT traffic direction.
        provider_conn = aim_service_graph.ServiceGraphConnection(
            tenant_name=tenant_name,
            service_graph_name=service_graph_name,
            name=aim_sg.PROVIDER,
            unicast_route=True,
            connector_direction=aim_sg.PROVIDER,
            connector_dns=aim_sg.get_connection_connector_dns(
                tenant_name, service_graph_name, node_name, aim_sg.PROVIDER),
            adjacency_type='L3')
        if self.aim.get(aim_ctx, provider_conn):
            self.aim.create(aim_ctx, provider_conn, overwrite=True)
        else:
            self.aim.create(aim_ctx, provider_conn)

        consumer_conn = aim_service_graph.ServiceGraphConnection(
            tenant_name=tenant_name,
            service_graph_name=service_graph_name,
            name=aim_sg.CONSUMER,
            unicast_route=True,
            connector_direction=aim_sg.PROVIDER,
            connector_dns=aim_sg.get_connection_connector_dns(
                tenant_name, service_graph_name, node_name, aim_sg.CONSUMER),
            adjacency_type='L3')
        if self.aim.get(aim_ctx, consumer_conn):
            self.aim.create(aim_ctx, consumer_conn, overwrite=True)
        else:
            self.aim.create(aim_ctx, consumer_conn)

        LOG.debug(f"Created service graph node: {node_name} in graph "
                 f"{service_graph_name}")
        return node

    def _delete_service_graph_node(self, aim_ctx, service_graph_name,
                                   tenant_name):
        """Dreate loadbalancer node in service graph.

        Node has function_type='GoTo' and routing_mode='Redirect' for
        traffic steering.

        Args:
            aim_ctx: AimContext for AIM operations
            service_graph_name: service graph name
            tenant_name: Project tenant name

        Returns:
            aim_resource.ServiceGraphNode: Created node
        """
        node_name = 'loadbalancer'
        try:
            # Delete provider/consumer connectors for SNAT traffic direction.
            provider_conn = aim_service_graph.ServiceGraphConnection(
                tenant_name=tenant_name,
                service_graph_name=service_graph_name,
                name=aim_sg.PROVIDER,
                connector_direction=aim_sg.PROVIDER,
                connector_dns=aim_sg.get_connection_connector_dns(
                    tenant_name, service_graph_name,
                    node_name, aim_sg.PROVIDER),
                unicast_route=True,
                adjacency_type='L3')
            pcon = self.aim.get(aim_ctx, provider_conn)
            if pcon:
                self.aim.delete(aim_ctx, pcon)

            consumer_conn = aim_service_graph.ServiceGraphConnection(
                tenant_name=tenant_name,
                service_graph_name=service_graph_name,
                name=aim_sg.CONSUMER,
                connector_direction=aim_sg.CONSUMER,
                connector_dns=aim_sg.get_connection_connector_dns(
                    tenant_name, service_graph_name,
                    node_name, aim_sg.CONSUMER),
                unicast_route=True,
                adjacency_type='L3')
            ccon = self.aim.get(aim_ctx, consumer_conn)
            if ccon:
                self.aim.delete(aim_ctx, ccon)

            node = aim_service_graph.ServiceGraphNode(
                tenant_name=tenant_name,
                service_graph_name=service_graph_name,
                name=node_name,
                display_name=node_name,
                function_type='GoTo',  # Type for traffic steering
                routing_mode='Redirect',    # Enable PBR redirect
                managed=False,
                connectors=[aim_sg.PROVIDER, aim_sg.CONSUMER],
                sequence_number='0')
            existing_node = self.aim.get(aim_ctx, node)
            if existing_node:
                self.aim.delete(aim_ctx, existing_node)
        except Exception as e:
            LOG.error("Failed to delete ServiceGraphNode, %s", e)

    # =========================================================================
    # DEVICE CLUSTER METHODS
    # =========================================================================

    def _create_device_cluster(self, aim_ctx, service_net_id,
                               physdom_name, tenant_name):
        """Create device cluster for physical domain.

        One cluster per physical domain, contains concrete devices per host.

        Args:
            aim_ctx: AimContext for AIM operations
            service_net_id: Service Network ID
            physdom_name: Physical domain name
            tenant_name: Project tenant name (usually 'common' for SNAT)

        Returns:
            aim_resource.DeviceCluster: Created cluster
        """
        dc = None
        try:
            cluster_name = self._device_cluster_name(service_net_id,
                                                     physdom_name)

            cluster = aim_service_graph.DeviceCluster(
                tenant_name=tenant_name,
                name=cluster_name,
                display_name=cluster_name,
                physical_domain_name=physdom_name,
                managed=False,
                devices=[])  # Will be populated as hosts are added
            dc = self.aim.get(aim_ctx, cluster)
            if not dc:
                dc = self.aim.create(aim_ctx, cluster)
        except Exception as e:
            LOG.error("Failed to create DeviceCluster, %s", e)

        LOG.debug(f"Created device cluster: {cluster_name} for domain "
                 f"{physdom_name}")
        return dc if dc else cluster

    def _delete_device_cluster(self, aim_ctx, service_net_id,
                               physdom_name, tenant_name):
        """Delete device cluster for physical domain.

        One cluster per physical domain, contains concrete devices per host.

        Args:
            aim_ctx: AimContext for AIM operations
            service_net_id: Service Network ID
            physdom_name: Physical domain name
            tenant_name: Project tenant name (usually 'common' for SNAT)
        """
        dc = None
        try:
            cluster_name = self._device_cluster_name(service_net_id,
                                                     physdom_name)

            cluster = aim_service_graph.DeviceCluster(
                tenant_name=tenant_name,
                name=cluster_name,
                display_name=cluster_name,
                physical_domain_name=physdom_name,
                managed=False,
                devices=[])  # Will be populated as hosts are added
            dc = self.aim.get(aim_ctx, cluster)
            if dc:
                dc = self.aim.delete(aim_ctx, dc)
        except Exception as e:
            LOG.error("Failed to delete DeviceCluster, %s", e)

        LOG.debug(f"Deleted device cluster: {cluster_name} for domain "
                 f"{physdom_name}")

    def _create_concrete_device(self, aim_ctx, device_cluster, host_name):
        """Create concrete device for compute host in cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            device_cluster: AIM DeviceCluster object
            host_name: Compute host identifier

        Returns:
            aim_resource.ConcreteDevice: Created device
        """
        cdev = None
        try:
            device = aim_service_graph.ConcreteDevice(
                tenant_name=device_cluster.tenant_name,
                device_cluster_name=device_cluster.name,
                name=host_name,
                display_name=host_name)
            cdev = self.aim.get(aim_ctx, device)
            if not cdev:
                cdev = self.aim.create(aim_ctx, device)
        except Exception as e:
            LOG.error("Failed to create ConcreteDevice, %s", e)

        LOG.debug(f"Created concrete device: {host_name} in cluster "
                 f"{device_cluster.name}")
        return cdev if cdev else device

    def _delete_concrete_device(self, aim_ctx, device_cluster, host_name):
        """Delete concrete device for compute host in cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            device_cluster: AIM DeviceCluster object
            host_name: Compute host identifier
        """
        cdev = None
        try:
            device = aim_service_graph.ConcreteDevice(
                tenant_name=device_cluster.tenant_name,
                device_cluster_name=device_cluster.name,
                name=host_name,
                display_name=host_name)
            cdev = self.aim.get(aim_ctx, device)
            if cdev:
                cdev = self.aim.delete(aim_ctx, cdev)
        except Exception as e:
            LOG.error("Failed to delete ConcreteDevice, %s", e)

        LOG.debug(f"Deleted concrete device: {host_name} in cluster "
                 f"{device_cluster.name}")

    def _create_concrete_interface(self, aim_ctx, concrete_device,
                                 interface_name, path, host):
        """Create concrete interface for device.

        Args:
            aim_ctx: AimContext for AIM operations
            concrete_device: AIM ConcreteDevice object
            interface_name: Interface identifier
            path: APIC path to interface
            host: Host identifier

        Returns:
            aim_resource.ConcreteDeviceInterface: Created interface
        """
        cdi = None
        try:
            interface = aim_service_graph.ConcreteDeviceInterface(
                tenant_name=concrete_device.tenant_name,
                device_cluster_name=concrete_device.device_cluster_name,
                device_name=concrete_device.name,
                name=interface_name,
                display_name=interface_name,
                path=path,
                host=host)
            cdi = self.aim.get(aim_ctx, interface)
            if not cdi:
                cdi = self.aim.create(aim_ctx, interface)
        except Exception as e:
            LOG.error("Failed to create ConcreteDeviceInterface, %s", e)

        LOG.debug(f"Created concrete interface: {interface_name} for "
                 f"device {concrete_device.name}")
        return cdi if cdi else interface

    def _add_device_cluster_interfaces(self, aim_ctx, device_cluster,
                                       interface_name, concrete_interfaces,
                                       encap='unknonwn'):
        """Add concrete interfaces to a logical interface for device cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            device_cluster: AIM DeviceCluster object
            interface_name: Logical interface identifier
            concrete_interfaces: List of concrete interface DNs to attach
            encap: VLAN encapsulation (optional)

        Returns:
            aim_resource.DeviceClusterInterface: Updated logical interface
        """
        dci = None
        try:
            dev_ci = aim_service_graph.DeviceClusterInterface(
                tenant_name=device_cluster.tenant_name,
                device_cluster_name=device_cluster.name,
                name=interface_name,
                display_name=interface_name,
                encap=encap)
            dci = self.aim.get(aim_ctx, dev_ci)
            if dci:
                interfaces = set(concrete_interfaces)
                interfaces.update(dci.concrete_interfaces)
                dci = self.aim.update(aim_ctx, dci,
                                      concrete_interfaces=list(interfaces))
        except Exception as e:
            LOG.error("Failed to update DeviceClusterInterface, %s", e)

        LOG.debug(f"Updated device cluster interface: {interface_name} "
                 f"in cluster {device_cluster.name}")
        return dci if dci else dev_ci

    def _delete_concrete_interface(self, aim_ctx, concrete_device,
                                 interface_name, path, host):
        """Delete concrete interface for device.

        Args:
            aim_ctx: AimContext for AIM operations
            concrete_device: AIM ConcreteDevice object
            interface_name: Interface identifier
            path: APIC path to interface
            host: Host identifier
        """
        cdi = None
        try:
            interface = aim_service_graph.ConcreteDeviceInterface(
                tenant_name=concrete_device.tenant_name,
                device_cluster_name=concrete_device.device_cluster_name,
                device_name=concrete_device.name,
                name=interface_name,
                display_name=interface_name,
                path=path,
                host=host)
            cdi = self.aim.get(aim_ctx, interface)
            if cdi:
                cdi = self.aim.delete(aim_ctx, cdi)
        except Exception as e:
            LOG.error("Failed to delete ConcreteDeviceInterface, %s", e)

        LOG.debug(f"Deleted concrete interface: {interface_name} for "
                 f"device {concrete_device.name}")

    def _create_device_cluster_interface(self, aim_ctx, device_cluster,
                                        interface_name, concrete_interfaces,
                                        encap=None):
        """Create logical interface for device cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            device_cluster: AIM DeviceCluster object
            interface_name: Logical interface identifier
            concrete_interfaces: List of concrete interface DNs to attach
            encap: VLAN encapsulation (optional)

        Returns:
            aim_resource.DeviceClusterInterface: Created logical interface
        """
        if not encap:
            encap = 'unknown'

        dci = None
        try:
            dev_ci = aim_service_graph.DeviceClusterInterface(
                tenant_name=device_cluster.tenant_name,
                device_cluster_name=device_cluster.name,
                name=interface_name,
                display_name=interface_name,
                encap=encap,
                concrete_interfaces=concrete_interfaces)
            dci = self.aim.get(aim_ctx, dev_ci)
            if not dci:
                dci = self.aim.create(aim_ctx, dev_ci)
        except Exception as e:
            LOG.error("Failed to create DeviceClusterInterface, %s", e)

        LOG.debug(f"Created device cluster interface: {interface_name} "
                 f"in cluster {device_cluster.name}")
        return dci if dci else dev_ci

    def _delete_device_cluster_interface(self, aim_ctx, device_cluster,
                                        interface_name, concrete_interfaces,
                                        encap=None):
        """Delete logical interface for device cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            device_cluster: AIM DeviceCluster object
            interface_name: Logical interface identifier
            concrete_interfaces: List of concrete interface DNs to attach
            encap: VLAN encapsulation (optional)
        """
        if not encap:
            encap = 'unknown'

        dci = None
        try:
            dev_ci = aim_service_graph.DeviceClusterInterface(
                tenant_name=device_cluster.tenant_name,
                device_cluster_name=device_cluster.name,
                name=interface_name,
                display_name=interface_name,
                encap=encap,
                concrete_interfaces=concrete_interfaces)
            dci = self.aim.get(aim_ctx, dev_ci)
            if dci:
                dci = self.aim.delete(aim_ctx, dci)
        except Exception as e:
            LOG.error("Failed to delete DeviceClusterInterface, %s", e)

        LOG.debug(f"Deleted device cluster interface: {interface_name} "
                 f"in cluster {device_cluster.name}")

    def _create_device_cluster_context(self, aim_ctx, contract_name,
                                       service_graph_name, node_name,
                                       device_cluster, tenant_name):
        """Create DeviceClusterContext linking service graph to device cluster.

        DeviceClusterContext binds a service graph node to a device cluster,
        enabling traffic redirection through the cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            contract_name: Contract name associated with the service graph
            service_graph_name: Service graph name
            node_name: Service graph node name
            device_cluster: AIM DeviceCluster object
            tenant_name: Tenant name for the context

        Returns:
            aim_resource.DeviceClusterContext: Created context
        """
        try:
            dcc_pol = aim_service_graph.DeviceClusterContext(
                tenant_name=tenant_name,
                contract_name=contract_name,
                service_graph_name=service_graph_name,
                node_name=node_name,
                device_cluster_name=device_cluster.name,
                device_cluster_tenant_name=device_cluster.tenant_name)
            dcc = self.aim.get(aim_ctx, dcc_pol)
            if not dcc:
                dcc = self.aim.create(aim_ctx, dcc_pol)
        except Exception as e:
            LOG.error("Failed to create DeviceClusterContext, %s", e)

        LOG.debug(f"Created DeviceClusterContext: {contract_name}/"
                 f"{service_graph_name}/{node_name} in tenant {tenant_name}")
        return dcc if dcc else dcc_pol

    def _delete_device_cluster_context(self, aim_ctx, contract_name,
                                       service_graph_name, node_name,
                                       device_cluster, tenant_name):
        """Delete DeviceClusterContext linking service graph to device cluster.

        DeviceClusterContext binds a service graph node to a device cluster,
        enabling traffic redirection through the cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            contract_name: Contract name associated with the service graph
            service_graph_name: Service graph name
            node_name: Service graph node name
            device_cluster: AIM DeviceCluster object
            tenant_name: Tenant name for the context
        """
        try:
            dcc_pol = aim_service_graph.DeviceClusterContext(
                tenant_name=tenant_name,
                contract_name=contract_name,
                service_graph_name=service_graph_name,
                node_name=node_name,
                device_cluster_name=device_cluster.name,
                device_cluster_tenant_name=device_cluster.tenant_name)
            dcc = self.aim.get(aim_ctx, dcc_pol)
            if dcc:
                dcc = self.aim.delete(aim_ctx, dcc_pol)
        except Exception as e:
            LOG.error("Failed to create DeviceClusterContext, %s", e)

        LOG.debug(f"Deleted DeviceClusterContext: {contract_name}/"
                 f"{service_graph_name}/{node_name} in tenant {tenant_name}")

    def _create_device_cluster_interface_context(self, aim_ctx,
                                                contract_name,
                                                service_graph_name,
                                                node_name,
                                                connector_name,
                                                device_cluster_interface,
                                                service_redirect_policy,
                                                bridge_domain_dn,
                                                tenant_name):
        """Create DeviceClusterInterfaceContext for interface binding.

        DeviceClusterInterfaceContext is a child of DeviceClusterContext that
        binds a logical interface to a connector on the service graph.

        Args:
            aim_ctx: AimContext for AIM operations
            contract_name: Contract name (identity)
            service_graph_name: Service graph name (identity)
            node_name: Service graph node name (identity)
            connector_name: Connector name (provider or consumer)
            device_cluster_interface: AIM DeviceClusterInterface object
            service_redirect_policy: AIM ServiceRedirectPolicy object
            bridge_domain_dn: DN of the bridge domain
            tenant_name: Tenant name for the context

        Returns:
            aim_resource.DeviceClusterInterfaceContext: Created context
        """
        try:
            dcic_pol = aim_service_graph.DeviceClusterInterfaceContext(
                tenant_name=tenant_name,
                contract_name=contract_name,
                service_graph_name=service_graph_name,
                node_name=node_name,
                connector_name=connector_name,
                device_cluster_interface_dn=(
                    device_cluster_interface.dn
                    if hasattr(device_cluster_interface, 'dn')
                    else ''),
                service_redirect_policy_dn=(
                    service_redirect_policy.dn
                    if hasattr(service_redirect_policy, 'dn')
                    else ''),
                bridge_domain_dn=bridge_domain_dn)
            dcic = self.aim.get(aim_ctx, dcic_pol)
            if not dcic:
                dcic = self.aim.create(aim_ctx, dcic_pol)
        except Exception as e:
            LOG.error("Failed to create DeviceClusterContext, %s", e)

        LOG.debug(f"Created DeviceClusterInterfaceContext: {contract_name}/"
                 f"{service_graph_name}/{node_name}/{connector_name} "
                 f"in tenant {tenant_name}")
        return dcic

    def _delete_device_cluster_interface_context(self, aim_ctx,
                                                contract_name,
                                                service_graph_name,
                                                node_name,
                                                connector_name,
                                                device_cluster_interface,
                                                service_redirect_policy,
                                                bridge_domain_dn,
                                                tenant_name):
        """Delete DeviceClusterInterfaceContext for interface binding.

        DeviceClusterInterfaceContext is a child of DeviceClusterContext that
        binds a logical interface to a connector on the service graph.

        Args:
            aim_ctx: AimContext for AIM operations
            contract_name: Contract name (identity)
            service_graph_name: Service graph name (identity)
            node_name: Service graph node name (identity)
            connector_name: Connector name (provider or consumer)
            device_cluster_interface: AIM DeviceClusterInterface object
            service_redirect_policy: AIM ServiceRedirectPolicy object
            bridge_domain_dn: DN of the bridge domain
            tenant_name: Tenant name for the context
        """
        try:
            dcic_pol = aim_service_graph.DeviceClusterInterfaceContext(
                tenant_name=tenant_name,
                contract_name=contract_name,
                service_graph_name=service_graph_name,
                node_name=node_name,
                connector_name=connector_name,
                device_cluster_interface_dn=(
                    device_cluster_interface.dn
                    if hasattr(device_cluster_interface, 'dn')
                    else ''),
                service_redirect_policy_dn=(
                    service_redirect_policy.dn
                    if hasattr(service_redirect_policy, 'dn')
                    else ''),
                bridge_domain_dn=bridge_domain_dn)
            dcic = self.aim.get(aim_ctx, dcic_pol)
            if dcic:
                dcic = self.aim.delete(aim_ctx, dcic)
        except Exception as e:
            LOG.error("Failed to delete DeviceClusterContext, %s", e)

        LOG.debug(f"Deleted DeviceClusterInterfaceContext: {contract_name}/"
                 f"{service_graph_name}/{node_name}/{connector_name} "
                 f"in tenant {tenant_name}")
        return dcic

    def _create_pbr_monitor_pol(self, aim_ctx, tenant_name, mon_pol_name):
        """Create Policy-Based Redirect monitoring policy.

        Args:
            aim_ctx: AimContext for AIM operations
            tenant_name: Project tenant name
            name: Name for the PBR monitoring policy

        Returns:
            aim_resource.ServiceRedirectMonitoringPolicy: Created policy
        """
        sg_m_pol = None
        try:
            sg_mon_pol = aim_service_graph.ServiceRedirectMonitoringPolicy(
                tenant_name=tenant_name,
                name=mon_pol_name,
                frequency=5,
                display_name=mon_pol_name)
            sg_m_pol = self.aim.get(aim_ctx, sg_mon_pol)
            if not sg_m_pol:
                sg_m_pol = self.aim.create(aim_ctx, sg_mon_pol)
        except Exception as e:
            LOG.error("Failed to create PBR monitoring policy, %s", e)

        LOG.debug(f"Created PBR monitoring policy: {mon_pol_name}")
        return sg_m_pol if sg_m_pol else sg_mon_pol

    def _delete_pbr_monitor_pol(self, aim_ctx, tenant_name, mon_pol_name):
        """Delete Policy-Based Redirect monitoring policy.

        Args:
            aim_ctx: AimContext for AIM operations
            tenant_name: Project tenant name
            name: Name for the PBR monitoring policy
        """
        try:
            sg_mon_pol = aim_service_graph.ServiceRedirectMonitoringPolicy(
                tenant_name=tenant_name,
                name=mon_pol_name,
                frequency=5,
                display_name=mon_pol_name)
            sg_m_pol = self.aim.get(aim_ctx, sg_mon_pol)
            if sg_m_pol:
                self.aim.delete(aim_ctx, sg_mon_pol)
        except Exception as e:
            LOG.error("Failed to delete PBR monitoring policy, %s", e)

        LOG.debug(f"Deleted PBR monitoring policy: {mon_pol_name}")

    def _create_pbr_health_pol(self, aim_ctx, tenant_name, name):
        """Create Policy-Based Redirect health policy.

        Args:
            aim_ctx: AimContext for AIM operations
            tenant_name: Project tenant name
            name: Name for the PBR health policy

        Returns:
            aim_resource.ServiceRedirectHealthGroup: Created policy
        """
        try:
            sg_health_pol = aim_service_graph.ServiceRedirectHealthGroup(
                tenant_name=tenant_name,
                name=name,
                display_name=name)
            sg_h_pol = self.aim.get(aim_ctx, sg_health_pol)
            if not sg_h_pol:
                sg_h_pol = self.aim.create(aim_ctx, sg_health_pol)
        except Exception as e:
            LOG.error("Failed to create PBR health policy, %s", e)

        LOG.debug(f"Created PBR health policy: {name}")
        return sg_h_pol if sg_h_pol else sg_health_pol

    def _delete_pbr_health_pol(self, aim_ctx, tenant_name, name):
        """DeleteServiceRedirectMonitoringPolicy Policy-Based Redirect
           health policy.

        Args:
            aim_ctx: AimContext for AIM operations
            tenant_name: Project tenant name
            name: Name for the PBR health policy
        """
        try:
            sg_health_pol = aim_service_graph.ServiceRedirectHealthGroup(
                tenant_name=tenant_name,
                name=name)
            sg_h_pol = self.aim.get(aim_ctx, sg_health_pol)
            if sg_h_pol:
                self.aim.delete(aim_ctx, sg_health_pol)
        except Exception as e:
            LOG.error("Failed to delete PBR health policy, %s", e)

        LOG.debug(f"Deleted PBR health policy: {name}")

    def _health_group_dn_from_port(self, tenant_name, port):
        if ":" in port.get('name', ''):
            hg_name = port['name'].split(':')[1]
            if hg_name:
                sg_health_pol = aim_service_graph.ServiceRedirectHealthGroup(
                    tenant_name=tenant_name,
                    name=hg_name)
                return sg_health_pol.dn
        return None

    def _create_provider_pbr(self, aim_ctx, subnet_id, tenant_name,
                             service_ports, monitor_policy):
        """Create provider-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet ID
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC
            monitor_policy: PBR monitor policy name

        Returns:
            aim_resource.ServiceRedirectPolicy: Created PBR
        """
        pbr = None
        try:
            pbr_name = 'provider_pbr_' + self._generate_snat_resource_name(
                subnet_id)

            # Build destination list from service ports
            destinations = []
            if service_ports:
                for port in service_ports:
                    dest = {
                        'name': port.get('id', ''),
                        'ip': port.get('ip_address', ''),
                        'mac': port.get('mac_address', '')
                    }
                    hg_dn = self._health_group_dn_from_port(tenant_name, port)
                    if hg_dn:
                        dest.update({'redirect_health_group_dn': hg_dn})
                    destinations.append(dest)

            pbr_pol = aim_service_graph.ServiceRedirectPolicy(
                tenant_name=tenant_name,
                name=pbr_name,
                display_name=pbr_name,
                monitoring_policy_name=monitor_policy,
                monitoring_policy_tenant_name=tenant_name,
                resilient_hash_enabled=True,
                destinations=destinations)
            pbr = self.aim.get(aim_ctx, pbr_pol)
            if not pbr:
                pbr = self.aim.create(aim_ctx, pbr_pol, overwrite=True)
        except Exception as e:
            LOG.error("Failed to create Provider PBR, %s", e)

        LOG.debug(f"Created provider PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")
        return pbr if pbr else pbr_pol

    def _update_provider_pbr(self, aim_ctx, subnet_id, tenant_name,
                             service_ports):
        """Update provider-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet ID
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC

        Returns:
            aim_resource.ServiceRedirectPolicy: Updated PBR
        """
        pbr = None
        try:
            pbr_name = 'provider_pbr_' + self._generate_snat_resource_name(
                subnet_id)

            # Build destination list from service ports
            destinations = []
            if service_ports:
                for port in service_ports:
                    dest = {
                        'name': port.get('id', ''),
                        'ip': port.get('ip_address', ''),
                        'mac': port.get('mac_address', '')
                    }
                    hg_dn = self._health_group_dn_from_port(tenant_name, port)
                    if hg_dn:
                        dest.update({'redirect_health_group_dn': hg_dn})
                    destinations.append(dest)

            pbr_pol = aim_service_graph.ServiceRedirectPolicy(
                tenant_name=tenant_name,
                name=pbr_name)
            pbr = self.aim.get(aim_ctx, pbr_pol)
            if pbr:
                pbr = self.aim.update(aim_ctx, pbr, destinations=destinations)
        except Exception as e:
            LOG.error("Failed to update Provider PBR, %s", e)

        LOG.debug(f"Upeated provider PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")
        return pbr if pbr else pbr_pol

    def _delete_provider_pbr(self, aim_ctx, subnet_id, tenant_name,
                             service_ports, monitor_policy):
        """Delete provider-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Neutron subnet ID
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC
            monitor_policy: PBR monitor policy name

        Returns:
            aim_resource.ServiceRedirectPolicy: Created PBR
        """
        pbr = None
        try:
            pbr_name = 'provider_pbr_' + self._generate_snat_resource_name(
                subnet_id)

            # Build destination list from service ports
            destinations = []
            if service_ports:
                for port in service_ports:
                    dest = {
                        'name': port.get('id', ''),
                        'ip': port.get('ip_address', ''),
                        'mac': port.get('mac_address', '')
                    }
                    hg_dn = self._health_group_dn_from_port(tenant_name, port)
                    if hg_dn:
                        dest.update({'redirect_health_group_dn': hg_dn})
                    destinations.append(dest)

            pbr_pol = aim_service_graph.ServiceRedirectPolicy(
                tenant_name=tenant_name,
                name=pbr_name)
            pbr = self.aim.get(aim_ctx, pbr_pol)
            if pbr:
                self.aim.delete(aim_ctx, pbr)
        except Exception as e:
            LOG.error("Failed to delete Provider PBR, %s", e)

        LOG.debug(f"Deleted provider PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")

    def _create_consumer_pbr(self, aim_ctx, subnet_id, tenant_name,
                            service_ports, monitor_policy):
        """Create consumer-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Neutron subnet ID
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC
            monitor_policy: PBR monitor policy name

        Returns:
            aim_resource.ServiceRedirectPolicy: Created PBR
        """
        pbr = None
        try:
            pbr_name = 'consumer_pbr_' + self._generate_snat_resource_name(
                subnet_id)

            # Build destination list from service ports
            destinations = []
            if service_ports:
                for port in service_ports:
                    dest = {
                        'name': port.get('id', ''),
                        'ip': port.get('ip_address', ''),
                        'mac': port.get('mac_address', '')
                    }
                    hg_dn = self._health_group_dn_from_port(tenant_name, port)
                    if hg_dn:
                        dest.update({'redirect_health_group_dn': hg_dn})
                    destinations.append(dest)

            pbr_pol = aim_service_graph.ServiceRedirectPolicy(
                tenant_name=tenant_name,
                name=pbr_name,
                display_name=pbr_name,
                monitoring_policy_name=monitor_policy,
                monitoring_policy_tenant_name=tenant_name,
                resilient_hash_enabled=True,
                destinations=destinations)
            pbr = self.aim.get(aim_ctx, pbr_pol)
            if not pbr:
                pbr = self.aim.create(aim_ctx, pbr_pol, overwrite=True)
        except Exception as e:
            LOG.error("Failed to create Consumer PBR, %s", e)

        LOG.debug(f"Created consumer PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")
        return pbr if pbr else pbr_pol

    def _update_consumer_pbr(self, aim_ctx, subnet_id, tenant_name,
                             service_ports):
        """Update consumer-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Neutron subnet ID
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC

        Returns:
            aim_resource.ServiceRedirectPolicy: Updated PBR
        """
        pbr = None
        try:
            pbr_name = 'consumer_pbr_' + self._generate_snat_resource_name(
                subnet_id)

            # Build destination list from service ports
            destinations = []
            if service_ports:
                for port in service_ports:
                    dest = {
                        'name': port.get('id', ''),
                        'ip': port.get('ip_address', ''),
                        'mac': port.get('mac_address', '')
                    }
                    hg_dn = self._health_group_dn_from_port(tenant_name, port)
                    if hg_dn:
                        dest.update({'redirect_health_group_dn': hg_dn})
                    destinations.append(dest)

            pbr_pol = aim_service_graph.ServiceRedirectPolicy(
                tenant_name=tenant_name,
                name=pbr_name)
            pbr = self.aim.get(aim_ctx, pbr_pol)
            if pbr:
                pbr = self.aim.update(aim_ctx, pbr, destinations=destinations)
        except Exception as e:
            LOG.error("Failed to update Consumer PBR, %s", e)

        LOG.debug(f"Updated consumer PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")
        return pbr if pbr else pbr_pol

    def _delete_consumer_pbr(self, aim_ctx, subnet_id, tenant_name,
                            service_ports, monitor_policy):
        """Delete consumer-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet_id: Neutron subnet ID
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC
            monitor_policy: PBR monitor policy name

        Returns:
            aim_resource.ServiceRedirectPolicy: Created PBR
        """
        pbr = None
        try:
            pbr_name = 'consumer_pbr_' + self._generate_snat_resource_name(
                subnet_id)

            # Build destination list from service ports
            destinations = []
            if service_ports:
                for port in service_ports:
                    dest = {
                        'name': port.get('id', ''),
                        'ip': port.get('ip_address', ''),
                        'mac': port.get('mac_address', '')
                    }
                    hg_dn = self._health_group_dn_from_port(tenant_name, port)
                    if hg_dn:
                        dest.update({'redirect_health_group_dn': hg_dn})
                    destinations.append(dest)

            pbr_pol = aim_service_graph.ServiceRedirectPolicy(
                tenant_name=tenant_name,
                name=pbr_name)
            pbr = self.aim.get(aim_ctx, pbr_pol)
            if pbr:
                self.aim.delete(aim_ctx, pbr)
        except Exception as e:
            LOG.error("Failed to delete Consumer PBR, %s", e)

        LOG.debug(f"Deleted consumer PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")

    # =========================================================================
    # ROUTER GATEWAY METHODS
    # =========================================================================

    def _handle_dist_snat_gateway_add(self, aim_ctx, port, subnet, ext_net,
                                      tenant_name):
        """Handle router gateway port attachment for distributed SNAT.

        Adds /32 gateway IP as ExternalSubnet under SNAT EPG.

        Args:
            aim_ctx: AimContext for AIM operations
            port: Neutron port dict (router gateway port)
            subnet: Neutron subnet dict (SNAT subnet)
            ext_net: AIM ExternalNetwork for the gateway network
            tenant_name: Project tenant name
        """
        gateway_ip = None
        snat_name = self._snat_external_network_name(subnet['id'])
        for fixed_ip in port.get('fixed_ips', []):
            if fixed_ip.get('subnet_id') == subnet['id']:
                gateway_ip = fixed_ip.get('ip_address')
                break
        if not gateway_ip:
            LOG.warning('No gateway IP for %s', port['id'])
            return
        port_id = port['id']

        # Create /32 ExternalSubnet for gateway IP
        ext_subnet_name = (
            f'gw_{self._generate_snat_resource_name(port_id)}')
        ext_subnet = aim_resource.ExternalSubnet(
            tenant_name=ext_net.tenant_name,
            l3out_name=ext_net.l3out_name,
            external_network_name=snat_name,
            name=ext_subnet_name,
            cidr=f'{gateway_ip}/32',
            display_name=ext_subnet_name)
        self.aim.create(aim_ctx, ext_subnet)

        LOG.debug(f"Added gateway IP {gateway_ip}/32 as external subnet "
                 f"for port {port_id}")

    def _handle_dist_snat_gateway_remove(self, aim_ctx, port, subnet,
                                         ext_net, tenant_name):
        """Handle router gateway port detachment for distributed SNAT.

        Removes /32 gateway IP ExternalSubnet.

        Args:
            aim_ctx: AimContext for AIM operations
            port: Neutron port dict (router gateway port)
            subnet: Neutron subnet dict (SNAT subnet)
            ext_net: AIM ExternalNetwork for the gateway network
            tenant_name: Project tenant name
        """
        gateway_ip = None
        snat_name = self._snat_external_network_name(subnet['id'])
        for fixed_ip in port.get('fixed_ips', []):
            if fixed_ip.get('subnet_id') == subnet['id']:
                gateway_ip = fixed_ip.get('ip_address')
                break
        if not gateway_ip:
            return
        port_id = port['id']

        # Delete /32 ExternalSubnet for gateway IP
        ext_subnet_name = (
            f'gw_{self._generate_snat_resource_name(port_id)}')
        ext_subnet = aim_resource.ExternalSubnet(
            tenant_name=ext_net.tenant_name,
            l3out_name=ext_net.l3out_name,
            external_network_name=snat_name,
            name=ext_subnet_name,
            cidr=f'{gateway_ip}/32')
        self.aim.delete(aim_ctx, ext_subnet)

        LOG.debug(f"Removed gateway IP {gateway_ip}/32 external subnet "
                 f"for port {port_id}")

    # =========================================================================
    # CLEANUP METHODS
    # =========================================================================

    def _remove_concrete_device_for_host(self, aim_ctx, service_net_id,
                                         physdom_name, host_name, tenant_name):
        """Remove ConcreteDevice when the last VM on a host departs.

        Called when the last VM on a specific host is gone but other hosts
        on the same physical domain still exist (cluster remains).

        Args:
            aim_ctx: AimContext for AIM operations
            service_net_id: Service Network ID
            physdom_name: Physical domain name
            host_name: Compute host identifier to remove
            tenant_name: Project tenant name
        """
        cluster_name = self._device_cluster_name(service_net_id, physdom_name)
        device = aim_service_graph.ConcreteDevice(
            tenant_name=tenant_name,
            device_cluster_name=cluster_name,
            name=host_name)
        self.aim.delete(aim_ctx, device)
        LOG.debug(f"Removed concrete device: {host_name} from cluster "
                 f"{cluster_name}")

    def _cleanup_device_cluster_for_last_host(self, aim_ctx, service_net_id,
                                              physdom_name, host_name,
                                              tenant_name):
        """Clean up entire device cluster when last host is removed.

        Args:
            aim_ctx: AimContext for AIM operations
            service_net_id: Service Network ID
            physdom_name: Physical domain name
            host_name: Compute host identifier
            tenant_name: Project tenant name (usually 'common')
        """
        cluster_name = self._device_cluster_name(service_net_id, physdom_name)

        # Delete device cluster (cascades to concrete devices)
        cluster = aim_service_graph.DeviceCluster(
            tenant_name=tenant_name,
            name=cluster_name)
        self.aim.delete(aim_ctx, cluster)

        LOG.debug(f"Deleted device cluster: {cluster_name} for domain "
                 f"{physdom_name}")
