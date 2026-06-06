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

from aim.api import resource as aim_resource
from aim.api import service_graph as aim_service_graph

from gbpservice.neutron.extensions import cisco_apic
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    constants as aim_cst)

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

    def _service_graph_name(self, network_id):
        return 'sg_' + self._sanitize_snat_name(network_id)

    def _device_cluster_name(self, physdom_name):
        return 'svc_' + self._sanitize_snat_name(physdom_name)

    def _service_network_bd_name(self, network_id):
        return 'svc_' + self._sanitize_snat_name(network_id)

    def _snat_external_network_name(self, subnet_id):
        return 'snat_epg_' + self._generate_snat_resource_name(subnet_id)

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
        return (network.get('description') or
                network.get('name') or fallback)

    def _subnet_display_name(self, subnet, fallback):
        return (subnet.get('description') or
                subnet.get('name') or fallback)

    # =========================================================================
    # SERVICE NETWORK METHODS
    # =========================================================================

    def _create_service_network_bd(self, aim_ctx, network, tenant_name):
        """Create BridgeDomain for service network.

        Service networks use an isolated BD in common/UnroutedVRF with no EPG.

        Args:
            aim_ctx: AimContext for AIM operations
            network: Neutron network dict
            tenant_name: Project tenant name
        """
        net_id = network['id']
        bd_name = self._service_network_bd_name(net_id)
        vrf_name = self._get_unrouted_vrf_name()

        bd = aim_resource.BridgeDomain(
            tenant_name=COMMON_TENANT_NAME,
            name=bd_name,
            display_name=self._network_display_name(network, bd_name),
            vrf_name=vrf_name,
            enable_routing=False,
            enable_arp_flood=True)
        self.aim.create(aim_ctx, bd)

        LOG.debug("Created service network BD: %s in tenant %s", bd_name,
                  COMMON_TENANT_NAME)
        return bd

    def _reparent_service_network_bd(self, aim_ctx, service_network_id,
                                     vrf_name, enable_routing):
        """Update the service-network BD to a new VRF parent.

        Args:
            aim_ctx: AimContext for AIM operations
            service_network_id: Neutron network ID of the service network
            vrf_name: AIM VRF name to parent the BD under
            enable_routing: Whether BD routing should be enabled
        """
        bd_name = self._service_network_bd_name(service_network_id)
        bd = aim_resource.BridgeDomain(
            tenant_name=COMMON_TENANT_NAME,
            name=bd_name)
        bd = self.aim.update(aim_ctx, bd, vrf_name=vrf_name,
                             enable_routing=enable_routing)

        LOG.debug("Reparented service network BD %s to VRF %s with "
                  "enable_routing=%s", bd_name, vrf_name, enable_routing)
        return bd

    def _create_service_subnet_bd_subnet(self, aim_ctx, subnet, bd):
        """Create BD Subnet for service network subnet.

        Service subnet is private (not externally advertised).

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            bd: AIM BridgeDomain object
        """
        subnet_id = subnet['id']
        cidr = subnet['cidr']

        bd_subnet = aim_resource.Subnet(
            tenant_name=bd.tenant_name,
            bd_name=bd.name,
            gw_ip_mask=cidr,
            display_name=self._subnet_display_name(subnet, subnet_id),
            scope='private')
        self.aim.create(aim_ctx, bd_subnet)

        LOG.debug("Created service subnet BD-subnet: %s for BD %s", cidr,
                  bd.name)
        return bd_subnet

    # =========================================================================
    # SNAT SUBNET METHODS
    # =========================================================================

    def _create_snat_external_network(self, aim_ctx, subnet, network,
                                      tenant_name):
        """Create AIM ExternalNetwork for a distributed SNAT subnet."""
        subnet_id = subnet['id']
        snat_name = self._snat_external_network_name(subnet_id)
        l3out, _, ns = self._get_aim_nat_strategy(network)
        if not ns:
            return None

        ext_net = aim_resource.ExternalNetwork(
            tenant_name=l3out.tenant_name,
            l3out_name=l3out.name,
            name=snat_name,
            display_name=self._network_display_name(
                network, self._subnet_display_name(subnet, subnet_id)))
        ns.create_external_network(aim_ctx, ext_net, epg_name=snat_name)
        LOG.debug("Created SNAT ExternalNetwork: %s in tenant %s", snat_name,
                  tenant_name)
        return ext_net

    def _create_snat_filters(self, aim_ctx, subnet, tenant_name):
        """Create provider/consumer filters for configured port ranges.

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
            display_name=(
                'SNAT provider filter %s' %
                self._subnet_display_name(subnet, subnet_id)))
        self.aim.create(aim_ctx, provider_filter)

        provider_tcp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=provider_filter_name,
            name='provider_tcp_port_range',
            ip_protocol='tcp',
            dest_from_port=start_port,
            dest_to_port=end_port,
            source_from_port=0,
            source_to_port=DEFAULT_SNAT_PORT_MAX,
            stateful=True)
        self.aim.create(aim_ctx, provider_tcp_entry)

        provider_udp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=provider_filter_name,
            name='provider_udp_port_range',
            ip_protocol='udp',
            dest_from_port=start_port,
            dest_to_port=end_port,
            source_from_port=0,
            source_to_port=DEFAULT_SNAT_PORT_MAX,
            stateful=True)
        self.aim.create(aim_ctx, provider_udp_entry)
        filters['provider_filter'] = provider_filter

        # Consumer filter entries match source ports.
        consumer_filter_name = f'snat_consumer_{filter_hash}'
        consumer_filter = aim_resource.Filter(
            tenant_name=tenant_name,
            name=consumer_filter_name,
            display_name=(
                'SNAT consumer filter %s' %
                self._subnet_display_name(subnet, subnet_id)))
        self.aim.create(aim_ctx, consumer_filter)

        consumer_tcp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=consumer_filter_name,
            name='consumer_tcp_port_range',
            ip_protocol='tcp',
            source_from_port=start_port,
            source_to_port=end_port,
            dest_from_port=0,
            dest_to_port=DEFAULT_SNAT_PORT_MAX,
            stateful=True)
        self.aim.create(aim_ctx, consumer_tcp_entry)

        consumer_udp_entry = aim_resource.FilterEntry(
            tenant_name=tenant_name,
            filter_name=consumer_filter_name,
            name='consumer_udp_port_range',
            ip_protocol='udp',
            source_from_port=start_port,
            source_to_port=end_port,
            dest_from_port=0,
            dest_to_port=DEFAULT_SNAT_PORT_MAX,
            stateful=True)
        self.aim.create(aim_ctx, consumer_udp_entry)
        filters['consumer_filter'] = consumer_filter

        LOG.debug("Created SNAT filters for subnet %s: ports %s-%s",
                  subnet_id, start_port, end_port)
        return filters

    def _create_snat_contract(self, aim_ctx, subnet, network,
                              tenant_name, filters):
        """Create SNAT contract with TCP/UDP filters.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            network: Neutron network dict
            tenant_name: Project tenant name
            filters: Dict with 'provider_filter' and 'consumer_filter'

        Returns:
            aim_resource.Contract: Created contract
        """
        subnet_id = subnet['id']
        contract_name = self._snat_contract_name(subnet_id)
        service_graph_name = self._service_graph_name(network['id'])

        # Create contract
        contract = aim_resource.Contract(
            tenant_name=tenant_name,
            name=contract_name,
            display_name=(
                'SNAT contract for subnet %s' %
                self._subnet_display_name(subnet, subnet_id)))
        self.aim.create(aim_ctx, contract)

        # Create contract subject, binding provider and consumer filters.
        subject_name = 'snat_subj'
        subject = aim_resource.ContractSubject(
            tenant_name=tenant_name,
            contract_name=contract_name,
            name=subject_name,
            display_name=(
                'SNAT subject for subnet %s' %
                self._subnet_display_name(subnet, subnet_id)),
            in_service_graph_name=service_graph_name,
            out_service_graph_name=service_graph_name,
            bi_filters=[filters['provider_filter'].name,
                        filters['consumer_filter'].name])
        self.aim.create(aim_ctx, subject)

        LOG.debug(f"Created SNAT contract: {contract_name} in tenant "
                 f"{tenant_name}")
        return contract

    def _create_service_graph(self, aim_ctx, network, tenant_name):
        """Create service graph for traffic steering.

        Args:
            aim_ctx: AimContext for AIM operations
            network: Neutron network dict
            tenant_name: Project tenant name

        Returns:
            aim_resource.ServiceGraph: Created service graph
        """
        net_id = network['id']
        sg_name = self._service_graph_name(net_id)

        sg = aim_service_graph.ServiceGraph(
            tenant_name=tenant_name,
            name=sg_name,
            display_name=self._network_display_name(network, net_id),
            linear_chain_nodes=[])  # Will be populated by node creation
        existing_sg = self.aim.get(aim_ctx, sg)
        if existing_sg:
            sg = existing_sg
            self.aim.create(aim_ctx, sg, overwrite=True)
        else:
            self.aim.create(aim_ctx, sg)

        # Create provider/consumer connectors for SNAT traffic direction.
        provider_conn = aim_service_graph.ServiceGraphConnection(
            tenant_name=tenant_name,
            service_graph_name=sg_name,
            name='provider',
            connector_direction='provider',
            connector_type='external',
            adjacency_type='L3')
        if self.aim.get(aim_ctx, provider_conn):
            self.aim.create(aim_ctx, provider_conn, overwrite=True)
        else:
            self.aim.create(aim_ctx, provider_conn)

        consumer_conn = aim_service_graph.ServiceGraphConnection(
            tenant_name=tenant_name,
            service_graph_name=sg_name,
            name='consumer',
            connector_direction='consumer',
            connector_type='external',
            adjacency_type='L3')
        if self.aim.get(aim_ctx, consumer_conn):
            self.aim.create(aim_ctx, consumer_conn, overwrite=True)
        else:
            self.aim.create(aim_ctx, consumer_conn)

        LOG.debug(f"Created service graph: {sg_name} in tenant "
                 f"{tenant_name}")
        return sg

    def _create_service_graph_node(self, aim_ctx, service_graph,
                                  device_cluster, tenant_name):
        """Create loadbalancer node in service graph.

        Node has function_type='GoTo' and routing_mode='Redirect' for
        traffic steering.

        Args:
            aim_ctx: AimContext for AIM operations
            service_graph: AIM ServiceGraph object
            device_cluster: AIM DeviceCluster object
            tenant_name: Project tenant name

        Returns:
            aim_resource.ServiceGraphNode: Created node
        """
        node_name = 'snat_lb_node'

        node = aim_service_graph.ServiceGraphNode(
            tenant_name=tenant_name,
            service_graph_name=service_graph.name,
            name=node_name,
            display_name='SNAT loadbalancer node',
            function_type='GoTo',  # Type for traffic steering
            routing_mode='Redirect',    # Enable PBR redirect
            managed=False,
            connectors=['provider', 'consumer'],
            device_cluster_name=(device_cluster.name if device_cluster
                                 else ''),
            device_cluster_tenant_name=(
                device_cluster.tenant_name if device_cluster else ''),
            sequence_number='0')
        existing_node = self.aim.get(aim_ctx, node)
        if existing_node:
            node = existing_node
            self.aim.create(aim_ctx, node, overwrite=True)
        else:
            self.aim.create(aim_ctx, node)

        # Update the service graph to include this node in linear_chain
        service_graph.linear_chain_nodes = [{
            'name': node_name,
            'device_cluster_name': (device_cluster.name
                                    if device_cluster else ''),
            'device_cluster_tenant_name': (
                device_cluster.tenant_name if device_cluster else '')
        }]
        self.aim.create(aim_ctx, service_graph, overwrite=True)

        LOG.debug(f"Created service graph node: {node_name} in graph "
                 f"{service_graph.name}")
        return node

    # =========================================================================
    # DEVICE CLUSTER METHODS
    # =========================================================================

    def _create_device_cluster(self, aim_ctx, physdom_name, tenant_name):
        """Create device cluster for physical domain.

        One cluster per physical domain, contains concrete devices per host.

        Args:
            aim_ctx: AimContext for AIM operations
            physdom_name: Physical domain name
            tenant_name: Project tenant name (usually 'common' for SNAT)

        Returns:
            aim_resource.DeviceCluster: Created cluster
        """
        cluster_name = self._device_cluster_name(physdom_name)

        cluster = aim_service_graph.DeviceCluster(
            tenant_name=tenant_name,
            name=cluster_name,
            display_name=f'SNAT device cluster for domain {physdom_name}',
            device_type='PHYSICAL',
            service_type='OTHERS',
            context_aware='Single-Context',
            physical_domain_name=physdom_name,
            managed=False,
            devices=[])  # Will be populated as hosts are added
        self.aim.create(aim_ctx, cluster)

        LOG.debug(f"Created device cluster: {cluster_name} for domain "
                 f"{physdom_name}")
        return cluster

    def _create_concrete_device(self, aim_ctx, device_cluster, host_name,
                               device_path=None):
        """Create concrete device for compute host in cluster.

        Args:
            aim_ctx: AimContext for AIM operations
            device_cluster: AIM DeviceCluster object
            host_name: Compute host identifier
            device_path: Optional path for device (default: host_name)

        Returns:
            aim_resource.ConcreteDevice: Created device
        """
        if not device_path:
            device_path = host_name

        device = aim_service_graph.ConcreteDevice(
            tenant_name=device_cluster.tenant_name,
            device_cluster_name=device_cluster.name,
            name=host_name,
            display_name=f'Concrete device for host {host_name}')
        self.aim.create(aim_ctx, device)

        LOG.debug(f"Created concrete device: {host_name} in cluster "
                 f"{device_cluster.name}")
        return device

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
        interface = aim_service_graph.ConcreteDeviceInterface(
            tenant_name=concrete_device.tenant_name,
            device_cluster_name=concrete_device.device_cluster_name,
            device_name=concrete_device.name,
            name=interface_name,
            display_name=f'Interface for {host}',
            path=path,
            host=host)
        self.aim.create(aim_ctx, interface)

        LOG.debug(f"Created concrete interface: {interface_name} for "
                 f"device {concrete_device.name}")
        return interface

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

        dci = aim_service_graph.DeviceClusterInterface(
            tenant_name=device_cluster.tenant_name,
            device_cluster_name=device_cluster.name,
            name=interface_name,
            display_name=f'Logical interface {interface_name}',
            encap=encap,
            concrete_interfaces=concrete_interfaces)
        self.aim.create(aim_ctx, dci)

        LOG.debug(f"Created device cluster interface: {interface_name} "
                 f"in cluster {device_cluster.name}")
        return dci

    def _create_provider_pbr(self, aim_ctx, subnet, tenant_name,
                            service_ports):
        """Create provider-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC

        Returns:
            aim_resource.ServiceRedirectPolicy: Created PBR
        """
        subnet_id = subnet['id']
        pbr_name = 'provider_pbr_' + self._generate_snat_resource_name(
            subnet_id)

        # Build destination list from service ports
        destinations = []
        for port in service_ports:
            dest = {
                'name': port.get('id', ''),
                'ip': port.get('ip_address', ''),
                'mac': port.get('mac_address', '')
            }
            destinations.append(dest)

        pbr = aim_service_graph.ServiceRedirectPolicy(
            tenant_name=tenant_name,
            name=pbr_name,
            display_name=(
                'Provider PBR for subnet %s' %
                self._subnet_display_name(subnet, subnet_id)),
            destinations=destinations)
        self.aim.create(aim_ctx, pbr)

        LOG.debug(f"Created provider PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")
        return pbr

    def _create_consumer_pbr(self, aim_ctx, subnet, tenant_name,
                            service_ports):
        """Create consumer-side Policy-Based Redirect.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            tenant_name: Project tenant name
            service_ports: List of service port objects with IP/MAC

        Returns:
            aim_resource.ServiceRedirectPolicy: Created PBR
        """
        subnet_id = subnet['id']
        pbr_name = 'consumer_pbr_' + self._generate_snat_resource_name(
            subnet_id)

        # Build destination list from service ports
        destinations = []
        for port in service_ports:
            dest = {
                'name': port.get('id', ''),
                'ip': port.get('ip_address', ''),
                'mac': port.get('mac_address', '')
            }
            destinations.append(dest)

        pbr = aim_service_graph.ServiceRedirectPolicy(
            tenant_name=tenant_name,
            name=pbr_name,
            display_name=(
                'Consumer PBR for subnet %s' %
                self._subnet_display_name(subnet, subnet_id)),
            destinations=destinations)
        self.aim.create(aim_ctx, pbr)

        LOG.debug(f"Created consumer PBR: {pbr_name} with "
                 f"{len(destinations)} destinations")
        return pbr

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
        for fixed_ip in port.get('fixed_ips', []):
            if fixed_ip.get('subnet_id') == subnet['id']:
                gateway_ip = fixed_ip.get('ip_address')
                break
        if not gateway_ip:
            return
        port_id = port['id']

        # Create /32 ExternalSubnet for gateway IP
        ext_subnet_name = (
            f'gw_{self._generate_snat_resource_name(port_id)}')
        ext_subnet = aim_resource.ExternalSubnet(
            tenant_name=ext_net.tenant_name,
            l3out_name=ext_net.l3out_name,
            external_network_name=ext_net.name,
            name=ext_subnet_name,
            cidr=f'{gateway_ip}/32',
            display_name=(
                'Gateway IP for %s' %
                self._subnet_display_name(subnet, port_id)))
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
            external_network_name=ext_net.name,
            name=ext_subnet_name,
            cidr=f'{gateway_ip}/32')
        self.aim.delete(aim_ctx, ext_subnet)

        LOG.debug(f"Removed gateway IP {gateway_ip}/32 external subnet "
                 f"for port {port_id}")

    # =========================================================================
    # CLEANUP METHODS
    # =========================================================================

    def _delete_snat_resources(self, aim_ctx, subnet, network, tenant_name):
        """Delete SNAT resources created for subnet.

        Cleans up ExternalNetwork, contract, filters, and service graph.

        Args:
            aim_ctx: AimContext for AIM operations
            subnet: Neutron subnet dict
            network: Neutron network dict
            tenant_name: Project tenant name
        """
        subnet_id = subnet['id']
        subnet_hash = self._generate_snat_resource_name(subnet_id)

        # Delete service graph
        net_id = subnet.get('network_id', '')
        if net_id:
            sg_name = self._service_graph_name(net_id)
            sg = aim_service_graph.ServiceGraph(
                tenant_name=tenant_name,
                name=sg_name)
            self.aim.delete(aim_ctx, sg)

        # Delete contract
        contract_name = self._snat_contract_name(subnet_id)
        contract = aim_resource.Contract(
            tenant_name=tenant_name,
            name=contract_name)
        self.aim.delete(aim_ctx, contract)

        # Delete provider/consumer filters.
        provider_filter_name = f'snat_provider_{subnet_hash}'
        provider_filter = aim_resource.Filter(
            tenant_name=tenant_name,
            name=provider_filter_name)
        self.aim.delete(aim_ctx, provider_filter)

        consumer_filter_name = f'snat_consumer_{subnet_hash}'
        consumer_filter = aim_resource.Filter(
            tenant_name=tenant_name,
            name=consumer_filter_name)
        self.aim.delete(aim_ctx, consumer_filter)

        # Delete SNAT ExternalNetwork through nat strategy.
        snat_name = self._snat_external_network_name(subnet_id)
        l3out, _, ns = self._get_aim_nat_strategy(network)
        if ns and l3out:
            snat_ext_net = aim_resource.ExternalNetwork(
                tenant_name=l3out.tenant_name,
                l3out_name=l3out.name,
                name=snat_name)
            ns.delete_external_network(aim_ctx, snat_ext_net,
                                       epg_name=snat_name)

        LOG.debug(f"Deleted SNAT resources for subnet {subnet_id}")

    def _delete_service_network_resources(self, aim_ctx, network,
                                         tenant_name):
        """Delete service network resources.

        Cleans up BD and VRF on service network delete.

        Args:
            aim_ctx: AimContext for AIM operations
            network: Neutron network dict
            tenant_name: Project tenant name
        """
        net_id = network['id']
        bd_name = self._service_network_bd_name(net_id)

        # Delete BD
        bd = aim_resource.BridgeDomain(
            tenant_name=COMMON_TENANT_NAME,
            name=bd_name)
        self.aim.delete(aim_ctx, bd)

        LOG.debug(f"Deleted service network BD: {bd_name}")

    def _remove_concrete_device_for_host(self, aim_ctx, physdom_name,
                                        host_name, tenant_name):
        """Remove ConcreteDevice when the last VM on a host departs.

        Called when the last VM on a specific host is gone but other hosts
        on the same physical domain still exist (cluster remains).

        Args:
            aim_ctx: AimContext for AIM operations
            physdom_name: Physical domain name
            host_name: Compute host identifier to remove
            tenant_name: Project tenant name
        """
        cluster_name = self._device_cluster_name(physdom_name)
        device = aim_service_graph.ConcreteDevice(
            tenant_name=tenant_name,
            device_cluster_name=cluster_name,
            name=host_name)
        self.aim.delete(aim_ctx, device)
        LOG.debug(f"Removed concrete device: {host_name} from cluster "
                 f"{cluster_name}")

    def _cleanup_device_cluster_for_last_host(self, aim_ctx, physdom_name,
                                             host_name, tenant_name):
        """Clean up entire device cluster when last host is removed.

        Args:
            aim_ctx: AimContext for AIM operations
            physdom_name: Physical domain name
            host_name: Compute host identifier
            tenant_name: Project tenant name (usually 'common')
        """
        cluster_name = self._device_cluster_name(physdom_name)

        # Delete device cluster (cascades to concrete devices)
        cluster = aim_service_graph.DeviceCluster(
            tenant_name=tenant_name,
            name=cluster_name)
        self.aim.delete(aim_ctx, cluster)

        LOG.debug(f"Deleted device cluster: {cluster_name} for domain "
                 f"{physdom_name}")
