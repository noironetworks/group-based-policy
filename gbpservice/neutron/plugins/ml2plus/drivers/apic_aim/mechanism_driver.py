# Copyright (c) 2016 Cisco Systems Inc.
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

from collections import defaultdict
from collections import namedtuple
import copy
from datetime import datetime
import os
import re

from aim.aim_lib.db import model as aim_lib_model
from aim.aim_lib import nat_strategy
from aim import aim_manager
from aim.api import infra as aim_infra
from aim.api import resource as aim_resource
from aim.common import utils
from aim import context as aim_context
from aim import exceptions as aim_exceptions
from aim import utils as aim_utils
import netaddr
from neutron.agent import securitygroups_rpc
from neutron.common import utils as n_utils
from neutron.db.models import address_group as ag_db
from neutron.db.models import address_scope as as_db
from neutron.db.models import allowed_address_pair as n_addr_pair_db
from neutron.db.models import l3 as l3_db
from neutron.db.models import securitygroup as sg_models
from neutron.db.models import segment as segments_model
from neutron.db import models_v2
from neutron.db import provisioning_blocks
from neutron.db import rbac_db_models
from neutron.db import segments_db
from neutron.plugins.ml2 import db as n_db
from neutron.plugins.ml2 import driver_context as ml2_context
from neutron.plugins.ml2 import models
from neutron.services.trunk import exceptions as trunk_exc
from neutron_lib.plugins.ml2 import ovs_constants as a_const
from neutron_lib.agent import topics as n_topics
from neutron_lib.api.definitions import external_net
from neutron_lib.api.definitions import portbindings
from neutron_lib.api.definitions import trunk
from neutron_lib.api.definitions import trunk_details
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_constants
from neutron_lib import context as n_context
from neutron_lib import exceptions as n_exceptions
from neutron_lib.plugins import constants as pconst
from neutron_lib.plugins import directory
from neutron_lib.plugins.ml2 import api
from neutron_lib import rpc as n_rpc
from neutron_lib.services.qos import constants as qos_consts
from neutron_lib.services.trunk import constants as trunk_consts
from neutron_lib.utils import net
from opflexagent import constants as ofcst
from opflexagent import rpc as ofrpc
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log
import oslo_messaging
from oslo_service import loopingcall
from oslo_utils import importutils
import sqlalchemy as sa
from sqlalchemy.ext import baked
from sqlalchemy import orm

from gbpservice.common import utils as gbp_utils
from gbpservice.neutron.db import api as db_api
from gbpservice.neutron.extensions import cisco_apic
from gbpservice.neutron.extensions import cisco_apic_l3 as a_l3
from gbpservice.neutron.plugins.ml2plus import driver_api as api_plus
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    constants as aim_cst)
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import apic_mapper
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import cache
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import config  # noqa
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import db
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import exceptions
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import extension_db
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import nova_client
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import rpc
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import trunk_driver

from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import qos_driver

# REVISIT: We need the aim_mapping policy driver's config until
# advertise_mtu and nested_host_vlan are moved to the mechanism
# driver's own config. Also, the noqa comment has to be on the same
# line as the entire import.
from gbpservice.neutron.services.grouppolicy.drivers.cisco.apic import config as pd_config  # noqa

LOG = log.getLogger(__name__)

BAKERY = baked.bakery(500, _size_alert=lambda c: LOG.warning(
    "sqlalchemy baked query cache size exceeded in %s", __name__))

ANY_FILTER_NAME = 'AnyFilter'
ANY_FILTER_ENTRY_NAME = 'AnyFilterEntry'
DEFAULT_VRF_NAME = 'DefaultVRF'
UNROUTED_VRF_NAME = 'UnroutedVRF'
COMMON_TENANT_NAME = 'common'
ROUTER_SUBJECT_NAME = 'route'
DEFAULT_SG_NAME = 'DefaultSecurityGroup'
SYSTEM_SG_NAME = 'SystemSecurityGroup'
L3OUT_NODE_PROFILE_NAME = 'NodeProfile'
L3OUT_IF_PROFILE_NAME = 'IfProfile'
L3OUT_IF_PROFILE_NAME6 = 'IfProfile6'
L3OUT_EXT_EPG = 'ExtEpg'
SYNC_STATE_TMP = 'synchronization_state_tmp'
AIM_RESOURCES_CNT = 'aim_resources_cnt'

SUPPORTED_HPB_SEGMENT_TYPES = (ofcst.TYPE_OPFLEX, n_constants.TYPE_VLAN)
SUPPORTED_VNIC_TYPES = [portbindings.VNIC_NORMAL,
                        portbindings.VNIC_BAREMETAL,
                        portbindings.VNIC_DIRECT]

AGENT_TYPE_DVS = 'DVS agent'
VIF_TYPE_DVS = 'dvs'
VIF_TYPE_FABRIC = 'fabric'
FABRIC_HOST_ID = 'fabric'

NO_ADDR_SCOPE = object()

DVS_AGENT_KLASS = 'networking_vsphere.common.dvs_agent_rpc_api.DVSClientAPI'
DEFAULT_HOST_DOMAIN = '*'

LL_INFO = 'local_link_information'

# TODO(kentwu): Move this to AIM utils maybe to avoid adding too much
# APIC logic to the mechanism driver
ACI_CHASSIS_DESCR_STRING = 'topology/pod-%s/node-%s'
ACI_PORT_DESCR_FORMATS = ('topology/pod-(\d+)/paths-(\d+)/pathep-'
                          '\[eth(\d+)/(\d+(\/\d+)*)\]')
ACI_VPCPORT_DESCR_FORMAT = ('topology/pod-(\d+)/protpaths-(\d+)-(\d+)/pathep-'
                            '\[(.*)\]')


InterfaceValidationInfo = namedtuple(
    'InterfaceValidationInfo',
    ['router_id', 'ip_address', 'subnet', 'scope_mapping'])


StaticPort = namedtuple(
    'StaticPort',
    ['link', 'encap', 'mode'])


class KeystoneNotificationEndpoint(object):
    filter_rule = oslo_messaging.NotificationFilter(
        event_type='^identity.project.[updated|deleted]')

    def __init__(self, mechanism_driver):
        self._driver = mechanism_driver
        self._dvs_notifier = None

    def info(self, ctxt, publisher_id, event_type, payload, metadata):
        tenant_id = payload.get('resource_info')
        # malformed notification?
        if not tenant_id:
            return None

        LOG.info("Keystone notification %(event_type)s received for "
                 "tenant %(tenant_id)s",
                 {'event_type': event_type,
                  'tenant_id': tenant_id})

        if event_type == 'identity.project.updated':
            prj_details = (self._driver.project_details_cache.
                    update_project_details(tenant_id))
            if not prj_details:
                return None

            # we only update tenants which have been created in APIC. For other
            # cases, their nameAlias will be set when the first resource is
            # being created under that tenant
            ctx = n_context.get_admin_context()
            # TODO(pulkit): replace with AIM writer context once API
            # supports it.
            with db_api.CONTEXT_WRITER.using(ctx):
                tenant_aname = self._driver.name_mapper.project(ctx.session,
                                                                tenant_id)
                aim_ctx = aim_context.AimContext(ctx.session)
                tenant = aim_resource.Tenant(name=tenant_aname)
                if not self._driver.aim.get(aim_ctx, tenant):
                    return None

                disp_name = aim_utils.sanitize_display_name(prj_details[0])
                descr = aim_utils.sanitize_description(prj_details[1])
                self._driver.aim.update(
                    aim_ctx, tenant, display_name=disp_name, descr=descr)
                return oslo_messaging.NotificationResult.HANDLED

        if event_type == 'identity.project.deleted':
            if not self._driver.enable_keystone_notification_purge:
                return None

            # REVISIT(kentwu): we should come up with our own version
            # of the gbp/neutron purge on the server side instead of
            # using gbp/neutron client API to do it which is not so
            # efficient.
            self._driver.project_details_cache.purge_gbp(tenant_id)

            # delete the tenant and AP in AIM also
            ctx = n_context.get_admin_context()
            # TODO(pulkit): replace with AIM writer context once API
            # supports it.
            with db_api.CONTEXT_WRITER.using(ctx):
                tenant_aname = self._driver.name_mapper.project(ctx.session,
                                                                tenant_id)
                aim_ctx = aim_context.AimContext(ctx.session)
                tenant = aim_resource.Tenant(name=tenant_aname)
                self._driver.aim.delete(aim_ctx, tenant, cascade=True)

                return oslo_messaging.NotificationResult.HANDLED


@registry.has_registry_receivers
class ApicMechanismDriver(api_plus.MechanismDriver,
                          db.DbMixin,
                          extension_db.ExtensionDbMixin,
                          rpc.ApicRpcHandlerMixin):
    NIC_NAME_LEN = 14

    def __init__(self):
        LOG.info("APIC AIM MD __init__")

    def initialize(self):
        LOG.info("APIC AIM MD initializing")
        self.project_details_cache = cache.ProjectDetailsCache()
        self.name_mapper = apic_mapper.APICNameMapper()
        self.aim = aim_manager.AimManager()
        self._core_plugin = None
        self._l3_plugin = None
        self._trunk_plugin = None
        self._gbp_plugin = None
        self._gbp_driver = None
        # Get APIC configuration and subscribe for changes
        self.enable_metadata_opt = (
            cfg.CONF.ml2_apic_aim.enable_optimized_metadata)
        self.enable_dhcp_opt = (
            cfg.CONF.ml2_apic_aim.enable_optimized_dhcp)
        # REVISIT: The following 2 items should be moved to
        # the ml2_apic_aim group.
        self.nested_host_vlan = cfg.CONF.aim_mapping.nested_host_vlan
        self.advertise_mtu = cfg.CONF.aim_mapping.advertise_mtu
        self.ap_name = 'OpenStack'
        self.apic_system_id = cfg.CONF.apic_system_id
        self.notifier = ofrpc.AgentNotifierApi(n_topics.AGENT)
        self.sg_enabled = securitygroups_rpc.is_firewall_enabled()
        self.keystone_notification_exchange = (cfg.CONF.ml2_apic_aim.
                                               keystone_notification_exchange)
        self.keystone_notification_topic = (cfg.CONF.ml2_apic_aim.
                                            keystone_notification_topic)
        self.keystone_notification_pool = (cfg.CONF.ml2_apic_aim.
                                           keystone_notification_pool)
        self._setup_keystone_notification_listeners()
        self.apic_optimized_dhcp_lease_time = (cfg.CONF.ml2_apic_aim.
                                               apic_optimized_dhcp_lease_time)
        self.enable_keystone_notification_purge = (cfg.CONF.ml2_apic_aim.
                                            enable_keystone_notification_purge)
        self.enable_iptables_firewall = (cfg.CONF.ml2_apic_aim.
                                         enable_iptables_firewall)
        self.vif_details = {portbindings.VIF_DETAILS_CONNECTIVITY:
                            self.connectivity}
        self.vif_details.update(self._update_binding_sg())
        self.l3_domain_dn = cfg.CONF.ml2_apic_aim.l3_domain_dn
        self.apic_nova_vm_name_cache_update_interval = (cfg.CONF.ml2_apic_aim.
                                    apic_nova_vm_name_cache_update_interval)
        self.allow_routed_vrf_subnet_overlap = (
            cfg.CONF.ml2_apic_aim.allow_routed_vrf_subnet_overlap)
        if self.allow_routed_vrf_subnet_overlap:
            LOG.warning("The allow_routed_vrf_subnet_overlap config option is "
                        "True, disabling checking for overlapping CIDRs "
                        "within a routed VRF. Overlapping CIDRs result in ACI "
                        "faults and loss of connectivity, so please eliminate "
                        "any existing overlap and set this option to False "
                        "(the default) as soon as possible.")
        self.host_id = 'id-%s-%s' % (net.get_hostname(), str(os.getpid()))
        self._setup_nova_vm_update()
        self._ensure_static_resources()
        trunk_driver.register()
        self.port_desc_re = re.compile(ACI_PORT_DESCR_FORMATS)
        self.vpcport_desc_re = re.compile(ACI_VPCPORT_DESCR_FORMAT)
        self.apic_router_id_pool = cfg.CONF.ml2_apic_aim.apic_router_id_pool
        self.apic_router_id_subnet = netaddr.IPSet([self.apic_router_id_pool])
        self.qos_driver = qos_driver.register(self)

    @property
    def connectivity(self):
        return portbindings.CONNECTIVITY_L2

    def start_rpc_listeners(self):
        LOG.info("APIC AIM MD starting RPC listeners")
        return self._start_rpc_listeners()

    def _setup_nova_vm_update(self):
        self.vm_update = loopingcall.FixedIntervalLoopingCall(
            self._update_nova_vm_name_cache)
        self.vm_update.start(
            interval=self.apic_nova_vm_name_cache_update_interval,
            stop_on_exception=False)

    def _update_nova_vm_name_cache(self):
        current_time = datetime.now()
        context = n_context.get_admin_context()
        with db_api.CONTEXT_READER.using(context) as session:
            vm_name_update = self._get_vm_name_update(session)
        is_full_update = True
        if vm_name_update:
            # The other controller is still doing the update actively
            if vm_name_update.host_id != self.host_id:
                delta_time = (current_time -
                              vm_name_update.last_incremental_update_time)
                if (delta_time.total_seconds() <
                        self.apic_nova_vm_name_cache_update_interval * 2):
                    return
            else:
                delta_time = (current_time -
                              vm_name_update.last_full_update_time)
                if (delta_time.total_seconds() <
                        self.apic_nova_vm_name_cache_update_interval * 10):
                    is_full_update = False

        nova_vms = nova_client.NovaClient().get_servers(
            is_full_update, self.apic_nova_vm_name_cache_update_interval * 10)
        # This means Nova API has thrown an exception
        if nova_vms is None:
            return

        try:
            with db_api.CONTEXT_WRITER.using(context) as session:
                self._set_vm_name_update(
                    session, vm_name_update, self.host_id, current_time,
                    current_time if is_full_update else None)
        # This means another controller is also adding an entry at the same
        # time and he has beat us. This is fine so we will just return.
        except Exception as e:
            LOG.info(e)
            return

        vm_list = []
        for vm in nova_vms:
            vm_list.append((vm.id, vm.name))
        nova_vms = set(vm_list)

        with db_api.CONTEXT_WRITER.using(context) as session:
            cached_vms = self._get_vm_names(session)
            cached_vms = set(cached_vms)

            # Only handle the deletion during full update otherwise we
            # don't know if the missing VMs are being deleted or just older
            # than 10 minutes as incremental update only queries Nova for
            # the past 10 mins.
            if is_full_update:
                removed_vms = cached_vms - nova_vms
                for device_id, _ in removed_vms:
                    self._delete_vm_name(session, device_id)

            added_vms = nova_vms - cached_vms
            update_ports = []
            for device_id, name in added_vms:
                self._set_vm_name(session, device_id, name)

                # Get the port_id for this device_id
                query = BAKERY(lambda s: s.query(
                    models_v2.Port.id))
                query += lambda q: q.filter(
                    models_v2.Port.device_id == sa.bindparam('device_id'))
                port_ids = [p[0] for p in
                      query(session).params(device_id=device_id)]
                if port_ids:
                    update_ports.extend(port_ids)

        if update_ports:
            self._notify_port_update_bulk(context, update_ports)

    def _allocate_apic_router_ids(self, aim_ctx, l3_out, node_path):
        aim_l3out_nodes = self._get_nodes_for_l3out_vrf(aim_ctx, l3_out)
        for aim_l3out_node in aim_l3out_nodes:
            if aim_l3out_node.node_path == node_path and (
                   aim_l3out_node.router_id):
                return aim_l3out_node.router_id
        used_ids = netaddr.IPSet([n.router_id for n in aim_l3out_nodes])
        available_ids = self.apic_router_id_subnet - used_ids
        for ip_address in available_ids:
            return str(ip_address)
        raise exceptions.ExhaustedApicRouterIdPool(
            pool=self.apic_router_id_pool)

    def _get_nodes_for_l3out_vrf(self, aim_ctx, l3_out):
        # First get the L3 out, as that has the VRF name.
        aim_l3out = self.aim.get(aim_ctx, l3_out)
        l3outs_in_vrf = self._get_l3outs_in_vrf_for_l3out(aim_ctx, aim_l3out)

        # Build the list of node profiles in this VRF by iterating
        # through the l3outs.
        aim_l3out_nodes = []
        for l3out in l3outs_in_vrf:
            l3out_nodes = self.aim.find(
                aim_ctx, aim_resource.L3OutNode,
                tenant_name=l3out.tenant_name,
                l3out_name=l3out.name)
            if l3out_nodes:
                aim_l3out_nodes.extend(l3out_nodes)
        return aim_l3out_nodes

    def _get_l3outs_in_vrf_for_l3out(self, aim_ctx, aim_l3out):
        # We need to find the VRF for this L3 out, since
        # we only have the VRF name. It's either in the same
        # ACI tenant, or it's in the common tenant. We have
        # to prioritize the non-common tenant case first,
        # since that's how APIC resolves VRF references.
        vrf = self.aim.get(aim_ctx, aim_resource.VRF(
                               tenant_name=aim_l3out.tenant_name,
                               name=aim_l3out.vrf_name))
        if (not vrf and
            aim_l3out.tenant_name != COMMON_TENANT_NAME):
            vrf = self.aim.get(aim_ctx, aim_resource.VRF(
                                   tenant_name=COMMON_TENANT_NAME,
                                   name=aim_l3out.vrf_name))
        if not vrf:
            return []
        if vrf.tenant_name == 'common':
            # L3outs in all tenants are candidates - locate all L3outs whose
            # vrf_name matches vrf.name, and exclude those that have a
            # local VRF aliasing the given VRF, since APIC will resolve
            # those L3 outs to the tenant-local VRF first.
            all_l3outs = self.aim.find(aim_ctx, aim_resource.L3Outside,
                                       vrf_name=vrf.name)
            l3out_tenants = set([l3out.tenant_name for l3out in all_l3outs])
            l3out_tenants = [t for t in l3out_tenants
                             if t == COMMON_TENANT_NAME or
                             not self.aim.get(
                                 aim_ctx, aim_resource.VRF(tenant_name=t,
                                                           name=vrf.name))]
            return [l3out for l3out in all_l3outs
                    if l3out.tenant_name in l3out_tenants]
        else:
            # VRF and L3out are visible only to L3out's tenant.
            return self.aim.find(aim_ctx, aim_resource.L3Outside,
                                 tenant_name=aim_l3out.tenant_name,
                                 vrf_name=vrf.name)
        # Other combinations of L3Out and VRF are not valid
        # configurations and can be excluded:
        # 1. L3out in common, VRF not in common: VRF is not
        #    visible to L3out.
        # 2. L3Out and VRF are in different non-common tenants:
        #    VRF is not visible to L3out.
        return []

    @db_api.retry_db_errors
    def _ensure_static_resources(self):
        ctx = n_context.get_admin_context()
        # TODO(pulkit): replace with AIM writer context once API supports it.
        with db_api.CONTEXT_WRITER.using(ctx):
            aim_ctx = aim_context.AimContext(ctx.session)
            self._ensure_common_tenant(aim_ctx)
            self._ensure_unrouted_vrf(aim_ctx)
            self._ensure_any_filter(aim_ctx)
            self._setup_default_arp_dhcp_security_group_rules(aim_ctx)

            # This is required for infra resources needed by ERSPAN
            check_topology = self.aim.find(aim_ctx, aim_resource.Topology)
            if not check_topology:
                topology_aim = aim_resource.Topology()
                self.aim.create(aim_ctx, topology_aim)
            check_infra = self.aim.find(aim_ctx, aim_resource.Infra)
            if not check_infra:
                infra_aim = aim_resource.Infra()
                self.aim.create(aim_ctx, infra_aim)

    def _setup_default_arp_dhcp_security_group_rules(self, aim_ctx):
        sg_name = self._default_sg_name
        dname = aim_utils.sanitize_display_name('DefaultSecurityGroup')
        sg = aim_resource.SecurityGroup(
            tenant_name=COMMON_TENANT_NAME, name=sg_name, display_name=dname)
        self.aim.create(aim_ctx, sg, overwrite=True)

        dname = aim_utils.sanitize_display_name('DefaultSecurityGroupSubject')
        sg_subject = aim_resource.SecurityGroupSubject(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name, name='default', display_name=dname)
        self.aim.create(aim_ctx, sg_subject, overwrite=True)

        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupArpEgressRule')
        arp_egress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='arp_egress',
            display_name=dname,
            direction='egress',
            ethertype='arp',
            conn_track='normal')
        self.aim.create(aim_ctx, arp_egress_rule, overwrite=True)

        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupArpIngressRule')
        arp_ingress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='arp_ingress',
            display_name=dname,
            direction='ingress',
            ethertype='arp',
            conn_track='normal')
        self.aim.create(aim_ctx, arp_ingress_rule, overwrite=True)

        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupDhcpEgressRule')
        dhcp_egress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='dhcp_egress',
            display_name=dname,
            direction='egress',
            ethertype='ipv4',
            ip_protocol=self.get_aim_protocol('udp'),
            from_port='67',
            to_port='67',
            conn_track='normal')
        self.aim.create(aim_ctx, dhcp_egress_rule, overwrite=True)

        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupDhcpIngressRule')
        dhcp_ingress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='dhcp_ingress',
            display_name=dname,
            direction='ingress',
            ethertype='ipv4',
            ip_protocol=self.get_aim_protocol('udp'),
            from_port='68',
            to_port='68',
            conn_track='normal')
        self.aim.create(aim_ctx, dhcp_ingress_rule, overwrite=True)

        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupDhcp6EgressRule')
        dhcp6_egress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='dhcp6_egress',
            display_name=dname,
            direction='egress',
            ethertype='ipv6',
            ip_protocol=self.get_aim_protocol('udp'),
            from_port='547',
            to_port='547',
            conn_track='normal')
        self.aim.create(aim_ctx, dhcp6_egress_rule, overwrite=True)

        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupDhcp6IngressRule')
        dhcp6_ingress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='dhcp6_ingress',
            display_name=dname,
            direction='ingress',
            ethertype='ipv6',
            ip_protocol=self.get_aim_protocol('udp'),
            from_port='546',
            to_port='546',
            conn_track='normal')
        self.aim.create(aim_ctx, dhcp6_ingress_rule, overwrite=True)

        # Need ICMPv6 rules for the SLAAC traffic to go through
        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupIcmp6IngressRule')
        icmp6_ingress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='icmp6_ingress',
            display_name=dname,
            direction='ingress',
            ethertype='ipv6',
            ip_protocol=self.get_aim_protocol('icmpv6'),
            conn_track='normal',
            remote_ips=['::/0'])
        self.aim.create(aim_ctx, icmp6_ingress_rule, overwrite=True)

        dname = aim_utils.sanitize_display_name(
            'DefaultSecurityGroupIcmp6EgressRule')
        icmp6_egress_rule = aim_resource.SecurityGroupRule(
            tenant_name=COMMON_TENANT_NAME,
            security_group_name=sg_name,
            security_group_subject_name='default',
            name='icmp6_egress',
            display_name=dname,
            direction='egress',
            ethertype='ipv6',
            ip_protocol=self.get_aim_protocol('icmpv6'),
            conn_track='normal',
            remote_ips=['::/0'])
        self.aim.create(aim_ctx, icmp6_egress_rule, overwrite=True)

    def _setup_keystone_notification_listeners(self):
        targets = [oslo_messaging.Target(
                    exchange=self.keystone_notification_exchange,
                    topic=self.keystone_notification_topic, fanout=True)]
        endpoints = [KeystoneNotificationEndpoint(self)]
        server = oslo_messaging.get_notification_listener(
            n_rpc.NOTIFICATION_TRANSPORT, targets, endpoints,
            executor='eventlet', pool=self.keystone_notification_pool)
        server.start()

    def ensure_tenant(self, plugin_context, project_id):
        LOG.debug("APIC AIM MD ensuring AIM Tenant for project_id: %s",
                  project_id)

        if not project_id:
            # The l3_db module creates gateway ports with empty string
            # project IDs in order to hide those ports from
            # users. Since we are not currently mapping ports to
            # anything in AIM, we can ignore these. Any other cases
            # where empty string project IDs are used may require
            # mapping AIM resources under some actual Tenant.
            return

        self.project_details_cache.ensure_project(project_id)

        # TODO(rkukura): Move the following to calls made from
        # precommit methods so AIM Tenants, ApplicationProfiles, and
        # Filters are [re]created whenever needed.
        with db_api.CONTEXT_WRITER.using(plugin_context) as session:
            tenant_aname = self.name_mapper.project(session, project_id)
            project_details = (self.project_details_cache.
                get_project_details(project_id))
            disp_name = aim_utils.sanitize_display_name(project_details[0])
            descr = aim_utils.sanitize_description(project_details[1])
            aim_ctx = aim_context.AimContext(session)
            tenant = aim_resource.Tenant(
                name=tenant_aname, descr=descr, display_name=disp_name)
            if not self.aim.get(aim_ctx, tenant):
                self.aim.create(aim_ctx, tenant)
            # REVISIT: Setting of display_name was added here to match
            # aim_lib behavior when it creates APs, but the
            # display_name aim_lib uses might vary.
            ap = aim_resource.ApplicationProfile(
                tenant_name=tenant_aname, name=self.ap_name,
                display_name=aim_utils.sanitize_display_name(self.ap_name))
            if not self.aim.get(aim_ctx, ap):
                self.aim.create(aim_ctx, ap)

    def _get_unique_domains(self, mappings):
        domains = []
        unique_domains = set()
        for mapping in mappings:
            if mapping.domain_name not in unique_domains:
                unique_domains.add(mapping.domain_name)
                domains.append({'type': mapping.domain_type,
                                'name': mapping.domain_name})
        return domains

    def _get_vmm_domains(self, aim_ctx, ns):
        domains = []
        if not isinstance(ns, nat_strategy.NoNatStrategy):
            aim_hd_mappings = self.aim.find(
                aim_ctx, aim_infra.HostDomainMappingV2,
                domain_type=utils.OPENSTACK_VMM_TYPE)
            if aim_hd_mappings:
                domains = self._get_unique_domains(aim_hd_mappings)
            if not domains:
                domains, _ = self.get_aim_domains(aim_ctx)
        return domains

    def _check_valid_preexisting_bd(self, aim_ctx, bd_dn):
        bd = aim_resource.BridgeDomain.from_dn(bd_dn)
        aim_bd = self.aim.get(aim_ctx, bd)
        # We should only use pre-existing BDs that weren't
        # created by this OpenStack installation
        if not aim_bd or aim_bd and not aim_bd.monitored:
            raise exceptions.InvalidPreexistingBdForNetwork()
        return aim_bd

    def _handle_qos_policy(self, context, policy, is_update=False):
        session = context.session
        aim_ctx = aim_context.AimContext(session)
        tenant_aname = self.name_mapper.project(session, context.tenant_id)
        bw_rules = []
        dscp = None
        egress_dpp_pol = None
        ingress_dpp_pol = None
        for rule in policy['rules']:
            if rule.rule_type == qos_consts.RULE_TYPE_DSCP_MARKING:
                dscp = rule.dscp_mark
            elif rule.rule_type == qos_consts.RULE_TYPE_BANDWIDTH_LIMIT:
                if rule.direction == 'egress':
                    egress_dpp_pol = rule.id
                    bw_rules.append({
                        'egress': True,
                        'burst': str(rule.max_burst_kbps),
                        'tenant_name': tenant_aname,
                        'name': rule.id, 'burst_unit': 'kilo',
                        'display_name': policy['name'] + '_egress',
                        'rate_unit': 'kilo',
                        'rate': rule.max_kbps})
                elif rule.direction == 'ingress':
                    ingress_dpp_pol = rule.id
                    bw_rules.append({
                        'egress': False,
                        'burst': str(rule.max_burst_kbps),
                        'tenant_name': tenant_aname,
                        'name': rule.id, 'burst_unit': 'kilo',
                        'display_name': policy['name'] + '_ingress',
                        'rate_unit': 'kilo', 'rate': rule.max_kbps})

        # REVIST: Should we just use self.aim.update() for update case then?
        if is_update:
            aim_qos_db = aim_resource.QosRequirement(
                             tenant_name=tenant_aname, name=policy['id'])
            aim_qos_db = self.aim.get(aim_ctx, aim_qos_db)
            if (aim_qos_db and aim_qos_db.egress_dpp_pol and
                    aim_qos_db.egress_dpp_pol != egress_dpp_pol):
                aim_bw = aim_resource.QosDppPol(tenant_name=tenant_aname,
                                                name=aim_qos_db.egress_dpp_pol)
                self.aim.delete(aim_ctx, aim_bw)
            if (aim_qos_db and aim_qos_db.ingress_dpp_pol and
                    aim_qos_db.ingress_dpp_pol != ingress_dpp_pol):
                aim_bw = aim_resource.QosDppPol(
                    tenant_name=tenant_aname, name=aim_qos_db.ingress_dpp_pol)
                self.aim.delete(aim_ctx, aim_bw)

        aim_qos = aim_resource.QosRequirement(
                    tenant_name=tenant_aname, name=policy['id'],
                    display_name=policy['name'],
                    dscp=dscp, egress_dpp_pol=egress_dpp_pol,
                    ingress_dpp_pol=ingress_dpp_pol)
        self.aim.create(aim_ctx, aim_qos, overwrite=True)
        for i in bw_rules:
            res = aim_resource.QosDppPol(**i)
            self.aim.create(aim_ctx, res, overwrite=True)

    def _create_external_epg_contract(self, aim_ctx, klass,
                                      ext_net, contract):
        new_contract = klass(
            tenant_name=ext_net.tenant_name,
            l3out_name=ext_net.l3out_name,
            ext_net_name=ext_net.name,
            name=contract.name)
        self.aim.create(aim_ctx, new_contract, overwrite=True)

    def _delete_external_epg_contract(self, aim_ctx, klass,
                                      ext_net, contract):
        old_contract = klass(
            tenant_name=ext_net.tenant_name,
            l3out_name=ext_net.l3out_name,
            ext_net_name=ext_net.name,
            name=contract.name)
        old_contract_db = self.aim.get(aim_ctx, old_contract)
        if old_contract_db:
            self.aim.delete(aim_ctx, old_contract_db)

    def create_qos_policy_precommit(self, context, policy):
        """Create a QoS policy.
        :param context: neutron api request context
        :type context: neutron_lib.context.Context
        :param policy: policy data to be applied
        :type policy: dict
        :returns: a QosPolicy object
        """
        self._handle_qos_policy(context, policy)

    def update_qos_policy_precommit(self, context, policy):
        """Create a QoS policy.
        :param context: neutron api request context
        :type context: neutron_lib.context.Context
        :param policy: policy data to be applied
        :type policy: dict
        :returns: a QosPolicy object
        """
        self._handle_qos_policy(context, policy, is_update=True)

    def delete_qos_policy_precommit(self, context, policy):
        session = context.session
        aim_ctx = aim_context.AimContext(session)
        tenant_aname = self.name_mapper.project(session, context.tenant_id)
        qos_aim = aim_resource.QosRequirement(
                    tenant_name=tenant_aname, name=policy['id'])
        aim_qos_db = self.aim.get(aim_ctx, qos_aim)
        if aim_qos_db and aim_qos_db.egress_dpp_pol:
            aim_bw = aim_resource.QosDppPol(tenant_name=tenant_aname,
                                            name=aim_qos_db.egress_dpp_pol)
            self.aim.delete(aim_ctx, aim_bw)
        if aim_qos_db and aim_qos_db.ingress_dpp_pol:
            aim_bw = aim_resource.QosDppPol(tenant_name=tenant_aname,
                                            name=aim_qos_db.ingress_dpp_pol)
            self.aim.delete(aim_ctx, aim_bw)
        self.aim.delete(aim_ctx, qos_aim, cascade=True)

    def create_network_precommit(self, context):
        current = context.current
        LOG.debug("APIC AIM MD creating network: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        is_ext = self._is_external(current)
        multi_ext_nets_enb = self._is_multi_ext_nets_enabled(current)
        wanted_epg_name = current['id'] if multi_ext_nets_enb else None
        is_svi = self._is_svi(current)

        if ((current[cisco_apic.EXTRA_PROVIDED_CONTRACTS] or
             current[cisco_apic.EXTRA_CONSUMED_CONTRACTS]) and
            (is_ext or is_svi)):
            raise exceptions.InvalidNetworkForExtraContracts()

        if (current[cisco_apic.EPG_CONTRACT_MASTERS] and (is_ext or is_svi)):
            raise exceptions.InvalidNetworkForEpgContractMaster()

        if current[cisco_apic.POLICY_ENFORCEMENT_PREF] == 'enforced' and \
           is_svi:
            raise exceptions.InvalidNetworkForPolicyEnforcementPref()

        if (current.get(qos_consts.QOS_POLICY_ID) and (is_ext or is_svi)):
            raise exceptions.InvalidNetworkForQos()

        if is_ext:
            l3out, ext_net, ns = self._get_aim_nat_strategy(current)
            if not ext_net:
                return  # Unmanaged external network
            domains = self._get_vmm_domains(aim_ctx, ns)
            ns.create_l3outside(aim_ctx, l3out, vmm_domains=domains,
                epg_name=wanted_epg_name)
            ns.create_external_network(aim_ctx, ext_net,
                epg_name=wanted_epg_name)
            # Get external CIDRs for all external networks that share
            # this APIC external network.
            cidrs = sorted(
                self.get_external_cidrs_by_ext_net_dn(
                    session, ext_net.dn))
            ns.update_external_cidrs(aim_ctx, ext_net, cidrs)

            for resource in ns.get_l3outside_resources(aim_ctx, l3out,
                epg_name=wanted_epg_name):
                if isinstance(resource, aim_resource.BridgeDomain):
                    bd = resource
                elif isinstance(resource, aim_resource.EndpointGroup):
                    epg = resource
                elif isinstance(resource, aim_resource.VRF):
                    vrf = resource
        elif is_svi:
            l3out, ext_net, _ = self._get_aim_external_objects(current)
            if ext_net:
                other_nets = set(
                    self.get_svi_network_ids_by_l3out_dn(
                        session, l3out.dn, lock_update=True))
                other_nets.discard(current['id'])
                if other_nets:
                    raise exceptions.PreExistingSVICannotUseSameL3out()

                aim_l3out_np = aim_resource.L3OutNodeProfile(
                    tenant_name=l3out.tenant_name, l3out_name=l3out.name,
                    name=L3OUT_NODE_PROFILE_NAME)
                self.aim.create(aim_ctx, aim_l3out_np, overwrite=True)
            # This means no DN is being provided. Then we should try to create
            # the l3out automatically
            else:
                tenant_aname = self.name_mapper.project(session,
                                                        current['tenant_id'])
                vrf = self._map_default_vrf(session, current)
                vrf = self._ensure_default_vrf(aim_ctx, vrf)
                aname = self.name_mapper.network(session, current['id'])
                dname = aim_utils.sanitize_display_name(current['name'])

                aim_l3out = aim_resource.L3Outside(
                    tenant_name=tenant_aname,
                    name=aname, display_name=dname, vrf_name=vrf.name,
                    l3_domain_dn=self.l3_domain_dn,
                    bgp_enable=self._is_bgp_enabled(current))
                self.aim.create(aim_ctx, aim_l3out)

                aim_l3out_np = aim_resource.L3OutNodeProfile(
                    tenant_name=tenant_aname, l3out_name=aname,
                    name=L3OUT_NODE_PROFILE_NAME)
                self.aim.create(aim_ctx, aim_l3out_np)

                aim_ext_net = aim_resource.ExternalNetwork(
                    tenant_name=tenant_aname,
                    l3out_name=aname, name=L3OUT_EXT_EPG)
                self.aim.create(aim_ctx, aim_ext_net)
                scope = "import-security"
                aggregate = ""
                if (self._is_bgp_enabled(current) and
                        current.get(cisco_apic.BGP_TYPE) == 'default_export'):
                    scope = "export-rtctrl,import-security"
                    aggregate = "export-rtctrl"
                aim_ext_subnet_ipv4 = aim_resource.ExternalSubnet(
                    tenant_name=tenant_aname,
                    l3out_name=aname,
                    external_network_name=L3OUT_EXT_EPG,
                    cidr='0.0.0.0/1',
                    scope=scope,
                    aggregate="")
                self.aim.create(aim_ctx, aim_ext_subnet_ipv4)
                aim_ext_subnet_ipv4 = aim_resource.ExternalSubnet(
                    tenant_name=tenant_aname,
                    l3out_name=aname,
                    external_network_name=L3OUT_EXT_EPG,
                    cidr='128.0.0.0/1',
                    scope=scope,
                    aggregate="")
                self.aim.create(aim_ctx, aim_ext_subnet_ipv4)
                aim_ext_subnet_ipv6 = aim_resource.ExternalSubnet(
                    tenant_name=tenant_aname,
                    l3out_name=aname,
                    external_network_name=L3OUT_EXT_EPG, cidr='::/0',
                    scope=scope,
                    aggregate=aggregate)
                self.aim.create(aim_ctx, aim_ext_subnet_ipv6)

                self._add_network_mapping(session, current['id'], None, None,
                                          vrf, aim_ext_net)
            return
        else:
            # See if the BD is pre-existing
            bd_dn = (current.get(cisco_apic.DIST_NAMES, {})
                     .get(cisco_apic.BD))
            preexisting_bd = (None if not bd_dn else
                self._check_valid_preexisting_bd(aim_ctx, bd_dn))
            bd, epg = self._map_network(session, current)

            dname = aim_utils.sanitize_display_name(current['name'])
            vrf = self._map_unrouted_vrf()

            bd.display_name = dname
            bd.vrf_name = vrf.name
            bd.enable_arp_flood = True
            bd.enable_routing = False
            bd.limit_ip_learn_to_subnets = True
            # REVISIT(rkukura): When AIM changes default
            # ep_move_detect_mode value to 'garp', remove it here.
            bd.ep_move_detect_mode = 'garp'
            if preexisting_bd:
                bd = self.aim.update(aim_ctx, preexisting_bd,
                    display_name=bd.display_name, vrf_name=bd.vrf_name,
                    enable_arp_flood=bd.enable_arp_flood,
                    enable_routing=bd.enable_routing,
                    limit_ip_learn_to_subnets=bd.limit_ip_learn_to_subnets,
                    ep_move_detect_mode=bd.ep_move_detect_mode,
                    monitored=False)
                epg.bd_name = preexisting_bd.name
            else:
                bd = self.aim.create(aim_ctx, bd)
                epg.bd_name = bd.name

            epg.display_name = dname
            epg.provided_contract_names = current[
                cisco_apic.EXTRA_PROVIDED_CONTRACTS]
            epg.consumed_contract_names = current[
                cisco_apic.EXTRA_CONSUMED_CONTRACTS]
            epg.epg_contract_masters = current[
                cisco_apic.EPG_CONTRACT_MASTERS]
            epg.policy_enforcement_pref = current[
                cisco_apic.POLICY_ENFORCEMENT_PREF]
            epg.qos_name = current.get(qos_consts.QOS_POLICY_ID, None)
            self.aim.create(aim_ctx, epg)

        self._add_network_mapping_and_notify(
            context._plugin_context, current['id'], bd, epg, vrf)

    def update_network_precommit(self, context):
        current = context.current
        original = context.original
        LOG.debug("APIC AIM MD updating network: %s", current)

        # TODO(amitbose) - Handle inter-conversion between external and
        # private networks

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        mapping = self._get_network_mapping(session, current['id'])

        is_ext = self._is_external(current)
        is_svi = self._is_svi(current)

        if (current.get(qos_consts.QOS_POLICY_ID) and (is_ext or is_svi)):
            raise exceptions.InvalidNetworkForQos()

        # Update name if changed. REVISIT: Remove is_ext from
        # condition and add UT for updating external network name.
        if (not is_ext and
            current['name'] != original['name']):
            dname = aim_utils.sanitize_display_name(current['name'])
            if not self._is_svi(current):
                bd = self._get_network_bd(mapping)
                self.aim.update(aim_ctx, bd, display_name=dname)
                epg = self._get_network_epg(mapping)
                self.aim.update(aim_ctx, epg, display_name=dname)
            else:
                l3out = self._get_network_l3out(mapping)
                if l3out:
                    self.aim.update(aim_ctx, l3out, display_name=dname)

        if (current.get(qos_consts.QOS_POLICY_ID) !=
            original.get(qos_consts.QOS_POLICY_ID)):
            epg = self._get_network_epg(mapping)
            self.aim.update(
                aim_ctx, epg,
                qos_name=current.get(qos_consts.QOS_POLICY_ID))

        # Update extra provided/consumed contracts if changed.
        curr_prov = set(current[cisco_apic.EXTRA_PROVIDED_CONTRACTS])
        curr_cons = set(current[cisco_apic.EXTRA_CONSUMED_CONTRACTS])
        orig_prov = set(original[cisco_apic.EXTRA_PROVIDED_CONTRACTS])
        orig_cons = set(original[cisco_apic.EXTRA_CONSUMED_CONTRACTS])
        if (curr_prov != orig_prov or curr_cons != orig_cons):
            if is_ext or is_svi:
                raise exceptions.InvalidNetworkForExtraContracts()

            added_prov = curr_prov - orig_prov
            removed_prov = orig_prov - curr_prov
            added_cons = curr_cons - orig_cons
            removed_cons = orig_cons - curr_cons

            # REVISIT: AIM needs methods to atomically add/remove
            # items to/from lists, as concurrent changes from router
            # operations are possible.
            epg = self.aim.get(aim_ctx, self._get_network_epg(mapping))

            if added_prov or removed_prov:
                contracts = ((set(epg.provided_contract_names) | added_prov) -
                             removed_prov)
                self.aim.update(
                    aim_ctx, epg, provided_contract_names=contracts)

            if added_cons or removed_cons:
                contracts = ((set(epg.consumed_contract_names) | added_cons) -
                             removed_cons)
                self.aim.update(
                    aim_ctx, epg, consumed_contract_names=contracts)

        # Update EPG contract masters if changed.
        curr_masters = current[cisco_apic.EPG_CONTRACT_MASTERS]
        orig_masters = original[cisco_apic.EPG_CONTRACT_MASTERS]
        if curr_masters != orig_masters:
            if is_ext or is_svi:
                raise exceptions.InvalidNetworkForEpgContractMaster()

            epg = self.aim.get(aim_ctx, self._get_network_epg(mapping))
            self.aim.update(
                aim_ctx, epg, epg_contract_masters=curr_masters)

        # Update Policy Enforcement Pref if changed.
        curr_masters = current[cisco_apic.POLICY_ENFORCEMENT_PREF]
        orig_masters = original[cisco_apic.POLICY_ENFORCEMENT_PREF]
        if curr_masters != orig_masters:
            if curr_masters == 'enforced' and is_svi:
                raise exceptions.InvalidNetworkForPolicyEnforcementPref()

            epg = self.aim.get(aim_ctx, self._get_network_epg(mapping))
            self.aim.update(
                aim_ctx, epg, policy_enforcement_pref=curr_masters)

        # Update no-NAT CIDRs if changed.
        curr_cidrs = current[cisco_apic.NO_NAT_CIDRS]
        orig_cidrs = original[cisco_apic.NO_NAT_CIDRS]
        ports_to_notify = set()
        if curr_cidrs != orig_cidrs:
            port_ids = self._get_non_router_ports_in_networks(
                session, [current['id']])
            ports_to_notify.update(port_ids)
        if ports_to_notify:
            self._add_postcommit_port_notifications(context._plugin_context,
                                                    ports_to_notify)

        if is_ext:
            _, ext_net, ns = self._get_aim_nat_strategy(current)
            if ext_net:
                old = sorted(original[cisco_apic.EXTERNAL_CIDRS])
                new = sorted(current[cisco_apic.EXTERNAL_CIDRS])
                if old != new:
                    # Get external CIDRs for all external networks that share
                    # this APIC external network.
                    cidrs = sorted(
                        self.get_external_cidrs_by_ext_net_dn(
                            session, ext_net.dn, lock_update=True))
                    ns.update_external_cidrs(aim_ctx, ext_net, cidrs)
                # TODO(amitbose) Propagate name updates to AIM
        else:
            # BGP config is supported only for svi networks.
            if not is_svi:
                return
            # Check for pre-existing l3out SVI.
            network_db = self.plugin._get_network(context._plugin_context,
                                                  current['id'])
            if network_db.aim_extension_mapping.external_network_dn:
                ext_net = aim_resource.ExternalNetwork.from_dn(
                    network_db.aim_extension_mapping.external_network_dn)
            # Handle BGP enable state update.
            bgp_enable_trigger = False
            if self._is_bgp_enabled(current) != original.get(cisco_apic.BGP):
                if self._is_bgp_enabled(current):
                    bgp_enable_trigger = True
                if not network_db.aim_extension_mapping.external_network_dn:
                    l3out = self._get_network_l3out(mapping)
                    self.aim.update(aim_ctx, l3out,
                                    bgp_enable=self._is_bgp_enabled(current))
            scope = "import-security"
            aggregate = ""
            # Handle pre-existing SVI where mapping is not present.
            if not network_db.aim_extension_mapping.external_network_dn:
                tenant_name = mapping.l3out_tenant_name
                l3out_name = mapping.l3out_name
                l3out_ext_subnet_v4 = (
                    self._get_network_l3out_default_ext_subnetv4(mapping))
                l3out_ext_subnet_v6 = (
                    self._get_network_l3out_default_ext_subnetv6(mapping))
            else:
                tenant_name = ext_net.tenant_name
                l3out_name = ext_net.l3out_name

            # Handle BGP disable trigger.
            if (not self._is_bgp_enabled(current) and
                    original.get(cisco_apic.BGP)):
                if not network_db.aim_extension_mapping.external_network_dn:
                    self.aim.update(aim_ctx, l3out_ext_subnet_v4, scope=scope,
                                    aggregate=aggregate)
                    self.aim.update(aim_ctx, l3out_ext_subnet_v6, scope=scope,
                                    aggregate=aggregate)
                l3out_bgp_peers = self.aim.find(
                    aim_ctx,
                    aim_resource.L3OutInterfaceBgpPeerP,
                    tenant_name=tenant_name,
                    l3out_name=l3out_name)
                for peer in l3out_bgp_peers:
                    if not peer.monitored:
                        self.aim.delete(aim_ctx, peer)
                return
            # When BGP is disabled, don't act on updates to bgp params.
            if not self._is_bgp_enabled(current):
                return
            # Handle BGP_ASN update.
            asn_changed = (current.get(cisco_apic.BGP_ASN) !=
                           original.get(cisco_apic.BGP_ASN))
            asn = (current.get(cisco_apic.BGP_ASN) if
                   cisco_apic.BGP_ASN in current else
                   original[cisco_apic.BGP_ASN])
            if asn_changed:
                l3out_bgp_peers = self.aim.find(
                    aim_ctx, aim_resource.L3OutInterfaceBgpPeerP,
                    tenant_name=tenant_name,
                    l3out_name=l3out_name)
                for peer in l3out_bgp_peers:
                    self.aim.update(aim_ctx, peer, asn=asn)
            if (current.get(cisco_apic.BGP_TYPE) != original.get(
                cisco_apic.BGP_TYPE)) or bgp_enable_trigger:
                if current.get(cisco_apic.BGP_TYPE) == 'default_export':
                    scope = "export-rtctrl,import-security"
                    aggregate = "export-rtctrl"
                    l3out_ifs = self.aim.find(
                        aim_ctx, aim_resource.L3OutInterface,
                        tenant_name=tenant_name,
                        l3out_name=l3out_name)
                    for l3out_if in l3out_ifs:
                        if not l3out_if.monitored:
                            primary = netaddr.IPNetwork(
                                l3out_if.primary_addr_a)
                            subnet = str(primary.cidr)
                            aim_bgp_peer_prefix = (
                                aim_resource.L3OutInterfaceBgpPeerP(
                                    tenant_name=l3out_if.tenant_name,
                                    l3out_name=l3out_if.l3out_name,
                                    node_profile_name=(
                                        l3out_if.node_profile_name),
                                    interface_profile_name=(
                                        l3out_if.interface_profile_name),
                                    interface_path=l3out_if.interface_path,
                                    addr=subnet,
                                    asn=asn))
                            self.aim.create(aim_ctx, aim_bgp_peer_prefix,
                                            overwrite=True)
                elif current.get(cisco_apic.BGP_TYPE) == '':
                    l3out_bgp_peers = self.aim.find(
                        aim_ctx,
                        aim_resource.L3OutInterfaceBgpPeerP,
                        tenant_name=tenant_name,
                        l3out_name=l3out_name)
                    for peer in l3out_bgp_peers:
                        if not peer.monitored:
                            self.aim.delete(aim_ctx, peer)
                if not network_db.aim_extension_mapping.external_network_dn:
                    self.aim.update(aim_ctx, l3out_ext_subnet_v4, scope=scope,
                                    aggregate=aggregate)
                    self.aim.update(aim_ctx, l3out_ext_subnet_v6, scope=scope,
                                    aggregate=aggregate)

    def update_network_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def delete_network_precommit(self, context):
        current = context.current
        LOG.debug("APIC AIM MD deleting network: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        if self._is_external(current):
            l3out, ext_net, ns = self._get_aim_nat_strategy(current)
            if not ext_net:
                return  # Unmanaged external network
            multi_ext_nets_enb = self._is_multi_ext_nets_enabled(current)

            if multi_ext_nets_enb:
                cidrs_to_delete = self.get_external_cidrs_by_net_id(
                    session, current['id'])
                ns.delete_external_network(aim_ctx, ext_net,
                        epg_name=current['id'],
                        cidrs=cidrs_to_delete)
                ns.delete_l3outside(aim_ctx, l3out, epg_name=current['id'],
                                    cidrs=cidrs_to_delete)
            else:
                # REVISIT: lock_update=True is needed to handle races. Find
                # alternative solutions since Neutron discourages using such
                # queries.
                cidrs_to_delete = self.get_external_cidrs_by_net_id(
                    session, current['id'])
                other_nets = set(
                    self.get_network_ids_by_ext_net_dn(
                        session, ext_net.dn, lock_update=True))
                other_nets.discard(current['id'])
                if not other_nets:
                    ns.delete_external_network(aim_ctx, ext_net,
                                               cidrs=cidrs_to_delete)
                other_nets = set(
                    self.get_network_ids_by_l3out_dn_filter_multi(
                        session, l3out.dn, False))
                other_nets.discard(current['id'])
                if not other_nets:
                    ns.delete_l3outside(aim_ctx, l3out,
                                        cidrs=cidrs_to_delete)

        elif self._is_svi(current):
            l3out, ext_net, _ = self._get_aim_external_objects(current)
            aim_l3out = self.aim.get(aim_ctx, l3out)
            if not aim_l3out:
                return
            # this means its pre-existing l3out
            if aim_l3out.monitored:
                # just delete everything under NodeProfile
                aim_l3out_np = aim_resource.L3OutNodeProfile(
                    tenant_name=l3out.tenant_name, l3out_name=l3out.name,
                    name=L3OUT_NODE_PROFILE_NAME)
                self.aim.delete(aim_ctx, aim_l3out_np, cascade=True)
            else:
                self.aim.delete(aim_ctx, l3out, cascade=True)
                # Before we can clean up the default vrf, we have to
                # remove the association in the network_mapping first.
                mapping = self._get_network_mapping(session, current['id'])
                if mapping:
                    self._set_network_vrf(mapping, self._map_unrouted_vrf())
                vrf = self._map_default_vrf(session, current)
                self._cleanup_default_vrf(aim_ctx, vrf)
        else:
            mapping = self._get_network_mapping(session, current['id'])
            if mapping:
                bd = self._get_network_bd(mapping)
                self.aim.delete(aim_ctx, bd)
                epg = self._get_network_epg(mapping)
                self.aim.delete(aim_ctx, epg)
                session.delete(mapping)

    def _merge_aim_status_bulk(self, aim_ctx, aim_resources_aggregate,
                               res_dict_by_aim_res_dn):
        for status in self.aim.get_statuses(aim_ctx, aim_resources_aggregate):
            res_dict, aim_status_track = res_dict_by_aim_res_dn.get(
                status.resource_dn, ({}, {}))
            if res_dict and aim_status_track:
                aim_status_track[SYNC_STATE_TMP] = self._merge_status(
                    aim_ctx,
                    aim_status_track.get(SYNC_STATE_TMP,
                        cisco_apic.SYNC_NOT_APPLICABLE),
                    None, status=status)
                aim_status_track[AIM_RESOURCES_CNT] -= 1
                if (aim_status_track[AIM_RESOURCES_CNT] == 0 or
                    (aim_status_track[SYNC_STATE_TMP] is
                        cisco_apic.SYNC_ERROR)):
                    # if this is zero then all the AIM resources corresponding,
                    # to this neutron resource are processed and we can
                    # accurately reflect the actual sync_state. Anytime we
                    # encounter an error - we reflect that immediately even
                    # if we are not done with the AIM resources processing.
                    res_dict[cisco_apic.SYNC_STATE] = (
                        aim_status_track[SYNC_STATE_TMP])

    def extend_port_dict_bulk(self, session, results, single=False):
        """Extend port resource with apic_aim extensions

        Add any extensions defined by the apic_aim mechanism
        driver. This method may get called before the mechanism
        driver precommit calls.
        """
        LOG.debug("APIC AIM MD extending dict bulk for port: %s",
                  results)

        # Gather db objects
        aim_ctx = aim_context.AimContext(session)
        aim_resources_aggregate = []
        res_dict_by_aim_res_dn = {}
        # template to track the status related info
        # for each resource.
        aim_status_track_template = {
            SYNC_STATE_TMP: cisco_apic.SYNC_NOT_APPLICABLE,
            AIM_RESOURCES_CNT: 0}

        for res_dict, port_db in results:
            aim_resources = []
            # Use a tmp field to aggregate the status across mapped
            # AIM objects, we set the actual sync_state only if we
            # are able to process all the status objects for these
            # corresponding AIM resources. If any status object is not
            # available then sync_state will be 'build'. On create,
            # subnets start in 'N/A'. The tracking object is added
            # along with the res_dict on the DN based res_dict_by_aim_res_dn
            # dict which maintains the mapping from status objs to res_dict.
            aim_status_track = copy.deepcopy(aim_status_track_template)

            res_dict[cisco_apic.SYNC_STATE] = cisco_apic.SYNC_NOT_APPLICABLE
            res_dict[cisco_apic.ERSPAN_CONFIG] = []
            res_dict_and_aim_status_track = (res_dict, aim_status_track)
            erspan_ext = port_db.aim_extension_erspan_configs
            if not erspan_ext and single:
                # Needed because of commit
                # d8c1e153f88952b7670399715c2f88f1ecf0a94a in Neutron that
                # put the extension call in Pike+ *before* the precommit
                # calls happen in port creation. I believe this is a bug
                # and should be discussed with the Neutron team.
                ext_dict = self.get_port_extn_db(session, port_db.id)
            else:
                ext_dict = self.make_port_extn_db_conf_dict(erspan_ext)
            if ext_dict:
                res_dict.update(ext_dict)

            # ERSPAN resources will only be valid if it's a bound port for
            # a compute instance on an opflex type network.
            cep_dn = self._map_port(session, port_db)
            resources = self._get_erspan_aim_resources_list(port_db, cep_dn)
            if resources:
                self.update_summary_resource(session, resources,
                                            port_db['network_id'])
                aim_resources.extend(resources)
            binding = (port_db.port_bindings[0]
                       if port_db.port_bindings else None)
            acc_name = self._get_acc_bundle_for_host(aim_ctx, binding.host)
            if resources and acc_name:
                acc_bundle = aim_resource.InfraAccBundleGroup(name=acc_name)
                aim_resources.append(acc_bundle)
                resources.append(acc_bundle)
            for resource in resources:
                res_dict_by_aim_res_dn[resource.dn] = (
                    res_dict_and_aim_status_track)

            # Track the number of AIM resources in aim_status_track,
            # decrement count each time we process a status obj related to
            # the resource. If the count hits zero then we have processed
            # the status objs for all of the associated AIM resources. Until
            # this happens, the sync_state is held as 'build' (unless it has
            # to be set to 'error').
            aim_resources = []
            for resource in resources:
                if resource not in aim_resources:
                    aim_resources.append(resource)

            aim_status_track[AIM_RESOURCES_CNT] = len(aim_resources)
            aim_resources_aggregate.extend(aim_resources)

        self._merge_aim_status_bulk(aim_ctx, aim_resources_aggregate,
                                    res_dict_by_aim_res_dn)

    def extend_port_dict(self, session, port_db, result):
        if result.get(api_plus.BULK_EXTENDED):
            return
        LOG.debug("APIC AIM MD extending dict for port: %s", result)
        self.extend_port_dict_bulk(session, [(result, port_db)],
                                   single=True)

    def extend_network_dict_bulk(self, session, results, single=False):
        # Gather db objects
        aim_ctx = aim_context.AimContext(session)
        aim_resources_aggregate = []
        res_dict_by_aim_res_dn = {}
        # template to track the status related info
        # for each resource.
        aim_status_track_template = {
            SYNC_STATE_TMP: cisco_apic.SYNC_NOT_APPLICABLE,
            AIM_RESOURCES_CNT: 0}

        for res_dict, net_db in results:
            aim_resources = []
            res_dict[cisco_apic.SYNC_STATE] = cisco_apic.SYNC_BUILD
            # Use a tmp field to aggregate the status across mapped
            # AIM objects, we set the actual sync_state only if we
            # are able to process all the status objects for these
            # corresponding AIM resources. If any status object is not
            # available then sync_state stays as 'build'. The tracking
            # object is added along with the res_dict on the DN based
            # res_dict_by_aim_res_dn dict which maintains the mapping
            # from status objs to res_dict.
            aim_status_track = copy.deepcopy(aim_status_track_template)

            res_dict[cisco_apic.DIST_NAMES] = {}
            res_dict_and_aim_status_track = (res_dict, aim_status_track)
            mapping = net_db.aim_mapping
            dist_names = res_dict.setdefault(cisco_apic.DIST_NAMES, {})
            if not mapping and single:
                # Needed because of commit
                # d8c1e153f88952b7670399715c2f88f1ecf0a94a in Neutron that
                # put the extension call in Pike+ *before* the precommit
                # calls happen in network creation. I believe this is a bug
                # and should be discussed with the Neutron team.
                mapping = self._get_network_mapping(session, net_db.id)
            if mapping:
                if mapping.epg_name:
                    bd = self._get_network_bd(mapping)
                    dist_names[cisco_apic.BD] = bd.dn
                    epg = self._get_network_epg(mapping)
                    dist_names[cisco_apic.EPG] = epg.dn
                    aim_resources.extend([bd, epg])
                    res_dict_by_aim_res_dn[epg.dn] = (
                        res_dict_and_aim_status_track)
                    res_dict_by_aim_res_dn[bd.dn] = (
                        res_dict_and_aim_status_track)

                elif mapping.l3out_name:
                    l3out_ext_net = self._get_network_l3out_ext_net(mapping)
                    dist_names[cisco_apic.EXTERNAL_NETWORK] = l3out_ext_net.dn
                    aim_resources.append(l3out_ext_net)
                    res_dict_by_aim_res_dn[l3out_ext_net.dn] = (
                        res_dict_and_aim_status_track)

                vrf = self._get_network_vrf(mapping)
                dist_names[cisco_apic.VRF] = vrf.dn
                aim_resources.append(vrf)
                res_dict_by_aim_res_dn[vrf.dn] = res_dict_and_aim_status_track
            if not net_db.aim_extension_mapping and single:
                # Needed because of commit
                # d8c1e153f88952b7670399715c2f88f1ecf0a94a in Neutron that
                # put the extension call in Pike+ *before* the precommit
                # calls happen in network creation. I believe this is a bug
                # and should be discussed with the Neutron team.
                ext_dict = self.get_network_extn_db(session, net_db.id)
            else:
                ext_dict = self.make_network_extn_db_conf_dict(
                    net_db.aim_extension_mapping,
                    net_db.aim_extension_cidr_mapping,
                    net_db.aim_extension_domain_mapping,
                    net_db.aim_extension_extra_contract_mapping,
                    net_db.aim_extension_epg_contract_masters,
                    net_db.aim_extension_no_nat_cidrs_mapping)
            if cisco_apic.EXTERNAL_NETWORK in ext_dict:
                dn = ext_dict.pop(cisco_apic.EXTERNAL_NETWORK)
                a_ext_net = aim_resource.ExternalNetwork.from_dn(dn)
                res_dict.setdefault(cisco_apic.DIST_NAMES, {})[
                    cisco_apic.EXTERNAL_NETWORK] = dn
                aim_resources.append(a_ext_net)
                res_dict_by_aim_res_dn[a_ext_net.dn] = (
                    res_dict_and_aim_status_track)
            if cisco_apic.BD in ext_dict:
                dn = ext_dict.pop(cisco_apic.BD)
                aim_bd = aim_resource.BridgeDomain.from_dn(dn)
                res_dict.setdefault(cisco_apic.DIST_NAMES, {})[
                    cisco_apic.BD] = dn
                aim_resources.append(aim_bd)
                res_dict_by_aim_res_dn[aim_bd.dn] = (
                    res_dict_and_aim_status_track)

            res_dict.update(ext_dict)
            # Track the number of AIM resources in aim_status_track,
            # decrement count each time we process a status obj related to
            # the resource. If the count hits zero then we have processed
            # the status objs for all of the associated AIM resources. Until
            # this happens, the sync_state is held as 'build' (unless it has
            # to be set to 'error').
            aim_status_track[AIM_RESOURCES_CNT] = len(aim_resources)
            aim_resources_aggregate.extend(aim_resources)

        self._merge_aim_status_bulk(aim_ctx, aim_resources_aggregate,
                                    res_dict_by_aim_res_dn)

    def extend_network_dict(self, session, network_db, result):
        if result.get(api_plus.BULK_EXTENDED):
            return
        LOG.debug("APIC AIM MD extending dict for network: %s", result)
        self.extend_network_dict_bulk(session, [(result, network_db)],
                                      single=True)

    def create_subnet_precommit(self, context):
        current = context.current
        LOG.debug("APIC AIM MD creating subnet: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        network_id = current['network_id']
        network_db = self.plugin._get_network(context._plugin_context,
                                              network_id)
        if network_db.external is not None and current['gateway_ip']:
            l3out, ext_net, ns = self._get_aim_nat_strategy_db(session,
                                                               network_db)
            if not ext_net:
                return  # Unmanaged external network
            # Check subnet overlap with subnets from other Neutron
            # external networks that map to the same APIC L3Out
            other_nets = set(
                self.get_network_ids_by_l3out_dn(
                    session, l3out.dn, lock_update=True))
            other_nets.discard(network_id)
            if other_nets:
                query = BAKERY(lambda s: s.query(
                    models_v2.Subnet.cidr))
                query += lambda q: q.filter(
                    models_v2.Subnet.network_id.in_(sa.bindparam(
                        'other_nets', expanding=True)))
                cidrs = query(session).params(
                    other_nets=list(other_nets)).all()

                cidrs = netaddr.IPSet([c[0] for c in cidrs])

                if cidrs & netaddr.IPSet([current['cidr']]):
                    raise exceptions.ExternalSubnetOverlapInL3Out(
                        cidr=current['cidr'], l3out=l3out.dn)
            if current[cisco_apic.EPG_SUBNET]:
                ns.create_epg_subnet(aim_ctx, l3out,
                    self._subnet_to_gw_ip_mask(current))
            else:
                ns.create_subnet(aim_ctx, l3out,
                                 self._subnet_to_gw_ip_mask(current))
            # Send a port update for those existing VMs because
            # SNAT info has been added.
            if current[cisco_apic.SNAT_HOST_POOL]:
                self._notify_existing_vm_ports(context._plugin_context,
                                               network_id)

        # Limit 1 subnet of each address family per SVI network as each
        # SVI interface in ACI can only have 1 primary addr
        if self._is_svi_db(network_db):
            ip_version = netaddr.IPNetwork(current['cidr']).version
            query = BAKERY(lambda s: s.query(
                models_v2.Subnet))
            query += lambda q: q.filter(
                models_v2.Subnet.network_id == sa.bindparam('network_id'),
                models_v2.Subnet.ip_version == sa.bindparam('ip_version'))
            subnets_size = query(session).params(
                network_id=network_id, ip_version=ip_version).count()

            # Supports one subnet of each address family
            if subnets_size > 1:
                raise exceptions.OnlyOneSubnetPerAddressFamilyInSVINetwork()

            if self._is_preexisting_svi_db(network_db):
                # pre-existing L3Out
                l3out, _, _ = self._get_aim_external_objects_db(session,
                    network_db)
            else:
                l3out = self.aim.get(aim_ctx,
                    self._get_network_l3out(network_db.aim_mapping))

            # Create Interface Profile for AF of subnet
            aim_l3out_ip = aim_resource.L3OutInterfaceProfile(
                tenant_name=l3out.tenant_name, l3out_name=l3out.name,
                node_profile_name=L3OUT_NODE_PROFILE_NAME,
                name=L3OUT_IF_PROFILE_NAME if ip_version == 4 else (
                    L3OUT_IF_PROFILE_NAME6))
            self.aim.create(aim_ctx, aim_l3out_ip, overwrite=True)

        if network_db.aim_mapping:
            # Provide VRF notifications if creating subnets in
            # unscoped networks.
            # REVISIT: We may need to handle VRF notifications for
            #          external networks as well.
            vrf = self._get_network_vrf(network_db.aim_mapping)
            if (vrf and
                (self._is_unrouted_vrf(vrf) or self._is_default_vrf(vrf)) and
                not network_db.external):
                self._add_postcommit_vrf_notification(
                    context._plugin_context, vrf)

        # Neutron subnets in non-external networks are mapped to AIM
        # Subnets as they are added to routers as interfaces.

    def create_subnet_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def _is_unrouted_vrf(self, vrf):
        return (vrf.tenant_name == COMMON_TENANT_NAME and
                vrf.name == self.apic_system_id + '_' + UNROUTED_VRF_NAME)

    def _is_default_vrf(self, vrf):
        return vrf.name == DEFAULT_VRF_NAME

    def update_subnet_precommit(self, context):
        current = context.current
        original = context.original
        LOG.debug("APIC AIM MD updating subnet: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        network_id = current['network_id']
        network_db = self.plugin._get_network(context._plugin_context,
                                              network_id)

        # This should apply to both external and internal networks
        if (current['host_routes'] != original['host_routes'] or
            current['dns_nameservers'] != original['dns_nameservers']):
            affected_port_ids = self._get_compute_dhcp_ports_in_subnets(
                                        session, [current['id']])
            self._add_postcommit_port_notifications(
                context._plugin_context, affected_port_ids)

        is_ext = network_db.external is not None
        if is_ext:
            # We have to allow this upon a customer request.
            if (original[cisco_apic.SNAT_HOST_POOL] !=
                    current[cisco_apic.SNAT_HOST_POOL]):
                # If subnet is no longer a SNAT pool, check if SNAT IP ports
                # are allocated.
                if (original[cisco_apic.SNAT_HOST_POOL] and
                        self._has_snat_ip_ports(context._plugin_context,
                                                current['id'])):
                    raise exceptions.SnatPortsInUse(subnet_id=current['id'])
                self._notify_existing_vm_ports(context._plugin_context,
                                               network_id)
            if current['gateway_ip'] != original['gateway_ip']:
                l3out, ext_net, ns = self._get_aim_nat_strategy_db(session,
                                                               network_db)
                if not ext_net:
                    return  # Unmanaged external network
                if original['gateway_ip']:
                    ns.delete_subnet(aim_ctx, l3out,
                                     self._subnet_to_gw_ip_mask(original))
                if current['gateway_ip']:
                    ns.create_subnet(aim_ctx, l3out,
                                     self._subnet_to_gw_ip_mask(current))
            return

        if current['name'] != original['name']:
            # Nothing to be done for SVI network.
            if self._is_svi(context.network.current):
                return
            bd = self._get_network_bd(network_db.aim_mapping)
            for gw_ip, router_id in self._subnet_router_ips(session,
                                                            current['id']):
                router_db = self.l3_plugin._get_router(context._plugin_context,
                                                       router_id)
                dname = aim_utils.sanitize_display_name(
                    router_db.name + "-" +
                    (current['name'] or current['cidr']))
                sn = self._map_subnet(current, gw_ip, bd)
                self.aim.update(aim_ctx, sn, display_name=dname)

    def update_subnet_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def delete_subnet_precommit(self, context):
        current = context.current
        LOG.debug("APIC AIM MD deleting subnet: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        network_id = current['network_id']
        network_db = self.plugin._get_network(context._plugin_context,
                                              network_id)
        if network_db.external is not None and current['gateway_ip']:
            l3out, ext_net, ns = self._get_aim_nat_strategy_db(session,
                                                               network_db)
            if not ext_net:
                return  # Unmanaged external network
            if current[cisco_apic.EPG_SUBNET]:
                ns.delete_epg_subnet(aim_ctx, l3out,
                    self._subnet_to_gw_ip_mask(current))
            else:
                ns.delete_subnet(aim_ctx, l3out,
                                 self._subnet_to_gw_ip_mask(current))
            # Send a port update for those existing VMs because
            # SNAT info has been deleted.
            if current[cisco_apic.SNAT_HOST_POOL]:
                self._notify_existing_vm_ports(context._plugin_context,
                                               network_id)

        if network_db.aim_mapping:
            # Provide VRF notifications if deleting subnets from
            # unscoped networks.
            # REVISIT: We may need to handle VRF notifications for
            #          external networks as well.
            vrf = self._get_network_vrf(network_db.aim_mapping)
            if (vrf and
                (self._is_unrouted_vrf(vrf) or self._is_default_vrf(vrf)) and
                not network_db.external):
                self._add_postcommit_vrf_notification(
                    context._plugin_context, vrf)

        # Non-external neutron subnets are unmapped from AIM Subnets as
        # they are removed from routers.

    def delete_subnet_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def extend_subnet_dict_bulk(self, session, results):
        LOG.debug("APIC AIM MD Bulk extending dict for subnet: %s", results)

        if not results:
            return
        aim_ctx = aim_context.AimContext(session)
        aim_resources_aggregate = []
        res_dict_by_aim_res_dn = {}
        # template to track the status related info
        # for each resource.
        aim_status_track_template = {
            SYNC_STATE_TMP: cisco_apic.SYNC_NOT_APPLICABLE,
            AIM_RESOURCES_CNT: 0}

        net_ids = []
        for result in results:
            net_ids.append(result[0]['network_id'])
        net_ids = list(set(net_ids))

        # TODO(sridar): Baked query - evaluate across branches,
        # with in_
        # REVISIT(sridar)
        # The noload option is being used to override lazy loading
        # of related tables which seems to create an issue later
        # during subnet update workflows. The issue manifests as expunge
        # not removing the object reference in the session identity_map
        # and an address mismatch between the object and the reference
        # in the identity_map. The override limits the objects tracked in
        # the identity_map and fixes the issue. This is being put in as
        # a workaround pending determination of the root cause.
        networks_db = (session.query(models_v2.Network).
                       options(orm.noload('subnets')).
                       filter(models_v2.Network.id.in_(net_ids)).all())
        net_map = {network['id']: network for network in networks_db}

        for res_dict, subnet_db in results:
            aim_resources = []
            res_dict[cisco_apic.SYNC_STATE] = cisco_apic.SYNC_NOT_APPLICABLE
            # Use a tmp field to aggregate the status across mapped
            # AIM objects, we set the actual sync_state only if we
            # are able to process all the status objects for these
            # corresponding AIM resources. If any status object is not
            # available then sync_state will be 'build'. On create,
            # subnets start in 'N/A'. The tracking object is added
            # along with the res_dict on the DN based res_dict_by_aim_res_dn
            # dict which maintains the mapping from status objs to res_dict.
            aim_status_track = copy.deepcopy(aim_status_track_template)

            res_dict[cisco_apic.DIST_NAMES] = {}
            res_dict_and_aim_status_track = (res_dict, aim_status_track)
            dist_names = res_dict[cisco_apic.DIST_NAMES]

            network_db = net_map.get(res_dict['network_id'], None)
            # TODO(sridar): Not sure if this can happen -  validate.
            if not network_db:
                LOG.warning("Network not found in extend_subnet_dict_bulk "
                            "for %s", subnet_db)
                continue

            if network_db.external is not None:
                l3out, ext_net, ns = self._get_aim_nat_strategy_db(session,
                                                               network_db)
                if ext_net:
                    sub = ns.get_subnet(aim_ctx, l3out,
                                        self._subnet_to_gw_ip_mask(subnet_db))
                    if sub:
                        dist_names[cisco_apic.SUBNET] = sub.dn
                        res_dict_by_aim_res_dn[sub.dn] = (
                            res_dict_and_aim_status_track)
                        aim_resources.append(sub)

                    epg_sub = ns.get_epg_subnet(aim_ctx, l3out,
                                        self._subnet_to_gw_ip_mask(subnet_db))
                    if epg_sub:
                        dist_names[cisco_apic.SUBNET] = epg_sub.dn
                        res_dict_by_aim_res_dn[epg_sub.dn] = (
                            res_dict_and_aim_status_track)
                        aim_resources.append(epg_sub)
            elif network_db.aim_mapping and network_db.aim_mapping.bd_name:
                bd = self._get_network_bd(network_db.aim_mapping)

                for gw_ip, router_id in self._subnet_router_ips(session,
                                                                subnet_db.id):
                    sn_ext = self.get_subnet_extn_db(session, subnet_db.id)
                    if sn_ext.get(cisco_apic.EPG_SUBNET, False):
                        epg = self._get_network_epg(network_db.aim_mapping)
                        epg_sn = self._map_epg_subnet(subnet_db, gw_ip, epg)
                        dist_names[gw_ip] = epg_sn.dn
                        res_dict_by_aim_res_dn[epg_sn.dn] = (
                            res_dict_and_aim_status_track)
                        aim_resources.append(epg_sn)
                    else:
                        sn = self._map_subnet(subnet_db, gw_ip, bd)
                        dist_names[gw_ip] = sn.dn
                        res_dict_by_aim_res_dn[sn.dn] = (
                            res_dict_and_aim_status_track)
                        aim_resources.append(sn)

            # Track the number of AIM resources in aim_status_track,
            # decrement count each time we process a status obj related to
            # the resource. If the count hits zero then we have processed
            # the status objs for all of the associated AIM resources. Until
            # this happens, the sync_state is held as 'build' (unless it has
            # to be set to 'error').
            aim_status_track[AIM_RESOURCES_CNT] = len(aim_resources)
            aim_resources_aggregate.extend(aim_resources)

        self._merge_aim_status_bulk(aim_ctx, aim_resources_aggregate,
                                    res_dict_by_aim_res_dn)

    def extend_subnet_dict(self, session, subnet_db, result):
        if result.get(api_plus.BULK_EXTENDED):
            return

        LOG.debug("APIC AIM MD extending dict for subnet: %s", result)

        self.extend_subnet_dict_bulk(session, [(result, subnet_db)])

    def _notify_vrf_for_scope(self, context):
        session = context._plugin_context.session
        scope_id = context.current['address_scope_id']
        mapping = self._get_address_scope_mapping(session, scope_id)
        if mapping:
            vrf = self._get_address_scope_vrf(mapping)
            if vrf:
                self._add_postcommit_vrf_notification(
                    context._plugin_context, vrf)

    def create_subnetpool_precommit(self, context):
        self._notify_vrf_for_scope(context)

    def create_subnetpool_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def update_subnetpool_precommit(self, context):
        current = context.current
        original = context.original
        LOG.debug("APIC AIM MD updating subnetpool: %s", current)

        if 'address_scope_id' not in current:
            # address_scope_id may not be returned with update,
            # when "Fields" parameter is specified
            # TODO(annak): verify this
            return
        session = context._plugin_context.session

        current_scope_id = current['address_scope_id']
        original_scope_id = original['address_scope_id']
        if current_scope_id != original_scope_id:
            # Find router interfaces involving subnets from this pool.
            pool_id = current['id']

            query = BAKERY(lambda s: s.query(
                l3_db.RouterPort))
            query += lambda q: q.join(
                models_v2.Port,
                models_v2.Port.id == l3_db.RouterPort.port_id)
            query += lambda q: q.join(
                models_v2.IPAllocation,
                models_v2.IPAllocation.port_id == models_v2.Port.id)
            query += lambda q: q.join(
                models_v2.Subnet,
                models_v2.Subnet.id == models_v2.IPAllocation.subnet_id)
            query += lambda q: q.filter(
                models_v2.Subnet.subnetpool_id == sa.bindparam('pool_id'),
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            rps = query(session).params(
                pool_id=pool_id).all()

            if rps:
                # TODO(rkukura): Implement moving the effected router
                # interfaces from one scope to another, from scoped to
                # unscoped, and from unscoped to scoped. This might
                # require moving the BDs and EPGs of routed networks
                # associated with the pool to the new scope's
                # project's Tenant. With multi-scope routing, it also
                # might result in individual routers being associated
                # with more or fewer scopes. Updates from scoped to
                # unscoped might still need to be rejected due to
                # overlap within a Tenant's default VRF. For now, we
                # just reject the update.
                raise exceptions.ScopeUpdateNotSupported()

        current_prefixes = set(current['prefixes'])
        original_prefixes = set(original['prefixes'])
        if current_scope_id and current_prefixes != original_prefixes:
            self._notify_vrf_for_scope(context)

    def update_subnetpool_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def delete_subnetpool_precommit(self, context):
        self._notify_vrf_for_scope(context)

    def delete_subnetpool_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def create_address_scope_precommit(self, context):
        current = context.current
        LOG.debug("APIC AIM MD creating address scope: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        id = current['id']

        # See if extension driver already created mapping.
        mapping = self._get_address_scope_mapping(session, id)
        if mapping:
            vrf = self._get_address_scope_vrf(mapping)
            scopes = self._get_address_scopes_owning_vrf(session, vrf)
            self._update_vrf_display_name(aim_ctx, vrf, scopes)
        else:
            dname = aim_utils.sanitize_display_name(current['name'])
            vrf = self._map_address_scope(session, current)
            vrf.display_name = dname
            self.aim.create(aim_ctx, vrf)
            self._add_address_scope_mapping(session, id, vrf)

        # ML2Plus does not extend address scope dict after precommit.
        sync_state = cisco_apic.SYNC_SYNCED
        sync_state = self._merge_status(aim_ctx, sync_state, vrf)
        current[cisco_apic.DIST_NAMES] = {cisco_apic.VRF: vrf.dn}
        current[cisco_apic.SYNC_STATE] = sync_state

    def update_address_scope_precommit(self, context):
        current = context.current
        original = context.original
        LOG.debug("APIC AIM MD updating address_scope: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        mapping = self._get_address_scope_mapping(session, current['id'])

        if current['name'] != original['name'] and mapping.vrf_owned:
            vrf = self._get_address_scope_vrf(mapping)
            scopes = self._get_address_scopes_owning_vrf(session, vrf)
            self._update_vrf_display_name(aim_ctx, vrf, scopes)

    def delete_address_scope_precommit(self, context):
        current = context.current
        LOG.debug("APIC AIM MD deleting address scope: %s", current)

        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        mapping = self._get_address_scope_mapping(session, current['id'])

        if mapping and mapping.vrf_owned:
            vrf = self._get_address_scope_vrf(mapping)
            session.delete(mapping)
            scopes = self._get_address_scopes_owning_vrf(session, vrf)
            self._update_vrf_display_name(aim_ctx, vrf, scopes)
            if not scopes:
                self.aim.delete(aim_ctx, vrf)

    def extend_address_scope_dict(self, session, scope, result):
        if result.get(api_plus.BULK_EXTENDED):
            return
        LOG.debug("APIC AIM MD extending dict for address scope: %s", result)

        # REVISIT: Consider moving to ApicExtensionDriver.

        sync_state = cisco_apic.SYNC_SYNCED
        dist_names = {}
        aim_ctx = aim_context.AimContext(session)

        mapping = self._get_address_scope_mapping(session, scope.id)
        if mapping:
            vrf = self._get_address_scope_vrf(mapping)
            dist_names[cisco_apic.VRF] = vrf.dn
            sync_state = self._merge_status(aim_ctx, sync_state, vrf)

        result[cisco_apic.DIST_NAMES] = dist_names
        result[cisco_apic.SYNC_STATE] = sync_state

    def _update_vrf_display_name(self, aim_ctx, vrf, scopes):
        # Assumes scopes is sorted by ip_version.
        if not scopes:
            return
        elif (len(scopes) == 1 or not scopes[1].name or
              scopes[0].name == scopes[1].name):
            dname = scopes[0].name
        elif not scopes[0].name:
            dname = scopes[1].name
        else:
            dname = scopes[0].name + '-' + scopes[1].name
        dname = aim_utils.sanitize_display_name(dname)
        self.aim.update(aim_ctx, vrf, display_name=dname)

    @registry.receives(resources.ROUTER, [events.PRECOMMIT_CREATE])
    def _create_router_precommit(self, resource, event, trigger, payload):
        context = payload.context
        router = payload.states
        router_id = payload.resource_id

        LOG.debug("APIC AIM MD creating router: %s", router)

        session = context.session
        aim_ctx = aim_context.AimContext(session)

        # Persist extension attributes.
        self.l3_plugin.set_router_extn_db(session, router_id, router[0])

        contract, subject = self._map_router(session, router[0])

        dname = aim_utils.sanitize_display_name(router[0]['name'])

        contract.display_name = dname
        self.aim.create(aim_ctx, contract)

        subject.display_name = dname
        subject.bi_filters = [self._any_filter_name]
        self.aim.create(aim_ctx, subject)

        # Creating the external gateway, if needed, is handled via
        # port_create_precommit.

    @registry.receives(resources.ROUTER, [events.PRECOMMIT_UPDATE])
    def _update_router_precommit(self, resource, event, trigger, payload):
        LOG.debug("APIC AIM MD updating router: %s", payload.resource_id)

        context = payload.context
        session = context.session
        aim_ctx = aim_context.AimContext(session)

        # Persist extension attribute updates.
        self.l3_plugin.set_router_extn_db(
            session, payload.resource_id, payload.request_body)

        original = payload.states[0]
        current = self.l3_plugin.get_router(
            payload.context, payload.resource_id)

        if current['name'] != original['name']:
            contract, subject = self._map_router(session, current)

            name = current['name']
            dname = aim_utils.sanitize_display_name(name)

            self.aim.update(aim_ctx, contract, display_name=dname)
            self.aim.update(aim_ctx, subject, display_name=dname)

            query = BAKERY(lambda s: s.query(
                models_v2.IPAllocation))
            query += lambda q: q.join(
                l3_db.RouterPort,
                l3_db.RouterPort.port_id == models_v2.IPAllocation.port_id)
            query += lambda q: q.filter(
                l3_db.RouterPort.router_id == sa.bindparam('router_id'),
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            for intf in query(session).params(
                    router_id=current['id']):

                # TODO(rkukura): Avoid separate queries for these.
                query = BAKERY(lambda s: s.query(
                    models_v2.Subnet))
                query += lambda q: q.filter_by(
                    id=sa.bindparam('subnet_id'))
                subnet_db = query(session).params(
                    subnet_id=intf.subnet_id).one()

                query = BAKERY(lambda s: s.query(
                    models_v2.Network))
                query += lambda q: q.filter_by(
                    id=sa.bindparam('network_id'))
                network_db = query(session).params(
                    network_id=subnet_db.network_id).one()

                if network_db.aim_mapping and network_db.aim_mapping.bd_name:
                    dname = aim_utils.sanitize_display_name(
                        name + "-" + (subnet_db.name or subnet_db.cidr))
                    bd = self._get_network_bd(network_db.aim_mapping)
                    sn = self._map_subnet(subnet_db, intf.ip_address, bd)
                    self.aim.update(aim_ctx, sn, display_name=dname)

        # Any changes to the external gateway are handled via
        # port_create_precommit and port_delete_precommit and are not
        # visible here. Therefore, just handle changes to extension
        # attributes that effect external connectivity for the
        # current gateway, if there is one.
        gateway_info = current.get('external_gateway_info')
        if not gateway_info or not gateway_info.get('network_id'):
            return

        def is_diff(old, new, attr):
            return sorted(old[attr]) != sorted(new[attr])

        if (is_diff(original, current, a_l3.EXTERNAL_PROVIDED_CONTRACTS) or
            is_diff(original, current, a_l3.EXTERNAL_CONSUMED_CONTRACTS)):

            self._update_router_external_connectivity(
                context, current, gateway_info['network_id'])

    def _update_router_external_connectivity(self, context, router, ext_net_id,
                                             notify=False, removing=False):
        session = context.session
        if self._get_router_intf_count(session, router):
            if not notify:
                affected_port_ids = []
            else:
                # SNAT information of ports on the subnet that interface
                # with the router will change because router's gateway
                # changed.
                sub_ids = self._get_router_interface_subnets(
                    session, [router['id']])
                affected_port_ids = self._get_compute_dhcp_ports_in_subnets(
                    session, sub_ids)

            ext_net = self.plugin.get_network(context, ext_net_id)
            old_net = ext_net if removing else None
            new_net = ext_net if not removing else None
            vrfs = self._get_vrfs_for_router(session, router['id'])
            for vrf in vrfs:
                self._manage_external_connectivity(
                    context, router, old_net, new_net, vrf)

            # Send a port update so that SNAT info may be recalculated for
            # affected ports in the interfaced subnets.
            #
            self._add_postcommit_port_notifications(context, affected_port_ids)

    @registry.receives(resources.ROUTER, [events.PRECOMMIT_DELETE])
    def _delete_router_precommit(self, resource, event, trigger, payload):
        context = payload.context
        router_id = payload.resource_id
        router_db = payload.states[0]

        LOG.debug("APIC AIM MD deleting router: %s", router_id)

        session = context.session
        aim_ctx = aim_context.AimContext(session)

        # Deleting the external gateway, if one exists, is handled via
        # port_delete_precommit.

        contract, subject = self._map_router(session, router_db)

        self.aim.delete(aim_ctx, subject)
        self.aim.delete(aim_ctx, contract)

    def extend_router_dict_bulk(self, session, results):
        LOG.debug("APIC AIM MD extending dict bulk for router: %s",
                  results)

        # Gather db objects
        aim_ctx = aim_context.AimContext(session)
        aim_resources_aggregate = []
        res_dict_by_aim_res_dn = {}
        # template to track the status related info
        # for each resource.
        aim_status_track_template = {
            SYNC_STATE_TMP: cisco_apic.SYNC_NOT_APPLICABLE,
            AIM_RESOURCES_CNT: 0}

        for res_dict in results:
            aim_resources = []
            res_dict[cisco_apic.SYNC_STATE] = cisco_apic.SYNC_NOT_APPLICABLE
            # Use a tmp field to aggregate the status across mapped
            # AIM objects, we set the actual sync_state only if we
            # are able to process all the status objects for these
            # corresponding AIM resources. If any status object is not
            # available then sync_state will be 'build'. On create,
            # subnets start in 'N/A'. The tracking object is added
            # along with the res_dict on the DN based res_dict_by_aim_res_dn
            # dict which maintains the mapping from status objs to res_dict.
            aim_status_track = copy.deepcopy(aim_status_track_template)

            res_dict[cisco_apic.DIST_NAMES] = {}
            res_dict_and_aim_status_track = (res_dict, aim_status_track)
            dist_names = res_dict[cisco_apic.DIST_NAMES]

            contract, subject = self._map_router(session, res_dict)
            dist_names[a_l3.CONTRACT] = contract.dn
            aim_resources.append(contract)
            res_dict_by_aim_res_dn[contract.dn] = res_dict_and_aim_status_track
            dist_names[a_l3.CONTRACT_SUBJECT] = subject.dn
            aim_resources.append(subject)
            res_dict_by_aim_res_dn[subject.dn] = res_dict_and_aim_status_track

            # Track the number of AIM resources in aim_status_track,
            # decrement count each time we process a status obj related to
            # the resource. If the count hits zero then we have processed
            # the status objs for all of the associated AIM resources. Until
            # this happens, the sync_state is held as 'build' (unless it has
            # to be set to 'error').
            aim_status_track[AIM_RESOURCES_CNT] = len(aim_resources)
            aim_resources_aggregate.extend(aim_resources)

        self._merge_aim_status_bulk(aim_ctx, aim_resources_aggregate,
                                    res_dict_by_aim_res_dn)

    def extend_router_dict(self, session, router_db, result):
        LOG.debug("APIC AIM MD extending dict for router: %s", result)
        self.extend_router_dict_bulk(session, [result])

    def _add_router_interface(self, context, router_db, port, subnets):
        LOG.debug("APIC AIM MD adding subnets %(subnets)s to router "
                  "%(router)s as interface port %(port)s",
                  {'subnets': subnets, 'router': router_db, 'port': port})

        session = context.session
        aim_ctx = aim_context.AimContext(session)

        router_id = router_db.id
        network_id = port['network_id']
        network_db = self.plugin._get_network(context, network_id)

        # SVI network with pre-existing l3out is not allowed to be
        # connected to a router at this moment
        if self._is_preexisting_svi_db(network_db):
            raise exceptions.PreExistingSVICannotBeConnectedToRouter()

        # We disallow connecting subnets that are on an external network
        # as router interfaces (can only use external networks for the
        # router gateway).
        if network_db.external:
            raise exceptions.ExternalSubnetNotAllowed(network_id=network_id)

        # We don't allow subnets marked with ACTIVE_ACTIVE_AAP flag to be
        # connected to a router.
        for subnet in subnets:
            if subnet[cisco_apic.ACTIVE_ACTIVE_AAP]:
                raise exceptions.ActiveActiveAAPSubnetConnectedToRouter(
                                                        subnet_id=subnet['id'])

        # Find the address_scope(s) for the new interface.
        #
        # REVISIT: If dual-stack interfaces allowed, process each
        # stack's scope separately, or at least raise an exception.
        scope_id = self._get_address_scope_id_for_subnets(context, subnets)

        # Find number of existing interface ports on the router for
        # this scope, excluding the one we are adding.
        router_intf_count = self._get_router_intf_count(
            session, router_db, scope_id)

        # Find up to two existing router interfaces for this
        # network. The interface currently being added is not
        # included, because the RouterPort has not yet been added to
        # the DB session.
        query = BAKERY(lambda s: s.query(
            l3_db.RouterPort.router_id,
            models_v2.Subnet))
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.port_id == l3_db.RouterPort.port_id)
        query += lambda q: q.join(
            models_v2.Subnet,
            models_v2.Subnet.id == models_v2.IPAllocation.subnet_id)
        query += lambda q: q.filter(
            models_v2.Subnet.network_id == sa.bindparam('network_id'),
            l3_db.RouterPort.port_type == n_constants.DEVICE_OWNER_ROUTER_INTF)
        query += lambda q: q.limit(2)
        net_intfs = query(session).params(
            network_id=network_id).all()

        if net_intfs:
            # Since the EPGs that provide/consume routers' contracts
            # are at network rather than subnet granularity,
            # topologies where different subnets on the same network
            # are interfaced to different routers, which are valid in
            # Neutron, would result in unintended routing. We
            # therefore require that all router interfaces for a
            # network share either the same router or the same subnet.
            #
            # REVISIT: Remove override flag when no longer needed for
            # GBP.
            if not context.override_network_routing_topology_validation:
                different_router = False
                different_subnet = False
                subnet_ids = [subnet['id'] for subnet in subnets]
                for existing_router_id, existing_subnet in net_intfs:
                    if router_id != existing_router_id:
                        different_router = True
                    for subnet_id in subnet_ids:
                        if subnet_id != existing_subnet.id:
                            different_subnet = True
                if different_router and different_subnet:
                    raise exceptions.UnsupportedRoutingTopology()

            # REVISIT: Remove this check for isomorphism once identity
            # NAT can be used to move IPv6 traffic from an IPv4 VRF to
            # the intended IPv6 VRF.
            _, subnet = net_intfs[0]
            existing_scope_id = (NO_ADDR_SCOPE if not subnet.subnetpool or
                                 not subnet.subnetpool.address_scope_id else
                                 subnet.subnetpool.address_scope_id)
            if scope_id != existing_scope_id:
                if (scope_id != NO_ADDR_SCOPE and
                    existing_scope_id != NO_ADDR_SCOPE):
                    scope_db = self._scope_by_id(session, scope_id)
                    vrf = self._get_address_scope_vrf(scope_db.aim_mapping)
                    existing_scope_db = self._scope_by_id(
                        session, existing_scope_id)
                    existing_vrf = self._get_address_scope_vrf(
                        existing_scope_db.aim_mapping)
                    if vrf.identity != existing_vrf.identity:
                        raise (exceptions.
                               NonIsomorphicNetworkRoutingUnsupported)
                else:
                    raise exceptions.NonIsomorphicNetworkRoutingUnsupported()

        nets_to_notify = set()
        ports_to_notify = set()
        router_topo_moved = False

        # Ensure that all the BDs and EPGs in the resulting topology
        # are mapped under the same Tenant so that the BDs can all
        # reference the topology's VRF and the EPGs can all provide
        # and consume the router's Contract. This is handled
        # differently for scoped and unscoped topologies.
        if scope_id != NO_ADDR_SCOPE:
            scope_db = self._scope_by_id(session, scope_id)
            vrf = self._get_address_scope_vrf(scope_db.aim_mapping)
        else:
            intf_topology = self._network_topology(session, network_db)
            router_topology = self._router_topology(session, router_id)

            intf_shared_net = self._topology_shared(intf_topology)
            router_shared_net = self._topology_shared(router_topology)

            intf_vrf = self._map_default_vrf(
                session, intf_shared_net or network_db)
            router_vrf = (
                self._map_default_vrf(
                    session,
                    router_shared_net or next(
                        iter(list(router_topology.values()))))
                if router_topology else None)

            # Choose VRF and move one topology if necessary.
            if router_vrf and intf_vrf.identity != router_vrf.identity:
                if intf_shared_net and router_shared_net:
                    raise exceptions.UnscopedSharedNetworkProjectConflict(
                        net1=intf_shared_net.id,
                        proj1=intf_shared_net.tenant_id,
                        net2=router_shared_net.id,
                        proj2=router_shared_net.tenant_id)
                elif intf_shared_net:
                    # Interface topology has shared network, so move
                    # router topology.
                    vrf = self._ensure_default_vrf(aim_ctx, intf_vrf)
                    self._move_topology(
                        context, aim_ctx, router_topology, router_vrf, vrf,
                        nets_to_notify)
                    router_topo_moved = True
                    self._cleanup_default_vrf(aim_ctx, router_vrf)
                elif router_shared_net:
                    # Router topology has shared network, so move
                    # interface topology, unless first interface for
                    # network.
                    vrf = router_vrf
                    if net_intfs:
                        self._move_topology(
                            context, aim_ctx, intf_topology, intf_vrf, vrf,
                            nets_to_notify)
                        self._cleanup_default_vrf(aim_ctx, intf_vrf)
                else:
                    # This should never happen.
                    LOG.error("Interface topology %(intf_topology)s and "
                              "router topology %(router_topology)s have "
                              "different VRFs, but neither is shared",
                              {'intf_topology': intf_topology,
                               'router_topology': router_topology})
                    raise exceptions.InternalError()
            else:
                vrf = self._ensure_default_vrf(aim_ctx, intf_vrf)

        epg = None
        # Associate or map network, depending on whether it has other
        # interfaces.
        if not net_intfs:
            # First interface for network.
            if network_db.aim_mapping.epg_name:
                bd, epg = self._associate_network_with_vrf(
                    context, aim_ctx, network_db, vrf, nets_to_notify,
                    scope_id)
            elif network_db.aim_mapping.l3out_name:
                l3out, epg = self._associate_network_with_vrf(
                    context, aim_ctx, network_db, vrf, nets_to_notify,
                    scope_id)
        else:
            # Network is already routed.
            #
            # REVISIT: For non-isomorphic dual-stack network, may need
            # to move the BD and EPG from already-routed v6 VRF to
            # newly-routed v4 VRF, and setup identity NAT for the v6
            # traffic.
            if network_db.aim_mapping.epg_name:
                bd = self._get_network_bd(network_db.aim_mapping)
                epg = self._get_network_epg(network_db.aim_mapping)
            elif network_db.aim_mapping.l3out_name:
                epg = self._get_network_l3out_ext_net(
                    network_db.aim_mapping)

        # If we are using an unscoped VRF, raise exception if it has
        # overlapping subnets, which could be due to this new
        # interface itself or to movement of either topology.
        if scope_id == NO_ADDR_SCOPE:
            self._check_vrf_for_overlap(session, vrf, subnets)

        if network_db.aim_mapping.epg_name:
            # Create AIM Subnet(s) for each added Neutron subnet.
            for subnet in subnets:
                gw_ip = self._ip_for_subnet(subnet, port['fixed_ips'])

                dname = aim_utils.sanitize_display_name(
                    router_db.name + "-" +
                    (subnet['name'] or subnet['cidr']))

                sn_ext = self.get_subnet_extn_db(session, subnet['id'])
                if sn_ext.get(cisco_apic.EPG_SUBNET, False):
                    epg_sn = self._map_epg_subnet(subnet, gw_ip, epg)
                    epg_sn.display_name = dname
                    epg_sn = self.aim.create(aim_ctx, epg_sn)
                else:
                    sn = self._map_subnet(subnet, gw_ip, bd)
                    sn.display_name = dname
                    sn = self.aim.create(aim_ctx, sn)

        # Ensure network's EPG provides/consumes router's Contract.
        contract = self._map_router(session, router_db, True)

        # this could be internal or external EPG
        epg = self.aim.get(aim_ctx, epg)
        if epg:
            if isinstance(epg, aim_resource.ExternalNetwork):
                self._create_external_epg_contract(aim_ctx,
                    aim_resource.ExternalNetworkProvidedContract,
                    epg, contract)
                self._create_external_epg_contract(aim_ctx,
                    aim_resource.ExternalNetworkConsumedContract,
                    epg, contract)
            else:
                contracts = epg.consumed_contract_names
                if contract.name not in contracts:
                    contracts.append(contract.name)
                    epg = self.aim.update(aim_ctx, epg,
                                          consumed_contract_names=contracts)
                contracts = epg.provided_contract_names
                if contract.name not in contracts:
                    contracts.append(contract.name)
                    epg = self.aim.update(aim_ctx, epg,
                                          provided_contract_names=contracts)

        # If external-gateway is set, handle external-connectivity changes.
        # External network is not supported for SVI network for now.
        if router_db.gw_port_id and not self._is_svi_db(network_db):
            net = self.plugin.get_network(
                context, router_db.gw_port.network_id)
            # If this is first interface-port, then that will determine
            # the VRF for this router. Setup external-connectivity for VRF.
            if not router_intf_count:
                self._manage_external_connectivity(
                    context, router_db, None, net, vrf)
            elif router_topo_moved:
                # Router moved from router_vrf to vrf, so
                # 1. Update router_vrf's external connectivity to exclude
                #    router
                # 2. Update vrf's external connectivity to include router
                self._manage_external_connectivity(
                    context, router_db, net, None, router_vrf)
                self._manage_external_connectivity(
                    context, router_db, None, net, vrf)

            aim_l3out, _, ns = self._get_aim_nat_strategy(net)
            if aim_l3out and ns:
                ns.set_bd_l3out(aim_ctx, bd, aim_l3out)

            # SNAT information of ports on the subnet will change because
            # of router interface addition. Send a port update so that it may
            # be recalculated.
            port_ids = self._get_compute_dhcp_ports_in_subnets(
                session,
                [subnet['id'] for subnet in subnets])
            ports_to_notify.update(port_ids)

        # Provide notifications for all affected ports.
        if nets_to_notify:
            port_ids = self._get_non_router_ports_in_networks(
                session, nets_to_notify)
            ports_to_notify.update(port_ids)
        if ports_to_notify:
            self._add_postcommit_port_notifications(context, ports_to_notify)

    def _remove_router_interface(self, context, router_db, port, subnets):
        LOG.debug("APIC AIM MD removing subnets %(subnets)s from router "
                  "%(router)s as interface port %(port)s",
                  {'subnets': subnets, 'router': router_db, 'port': port})

        session = context.session
        aim_ctx = aim_context.AimContext(session)

        router_id = router_db.id
        network_id = port['network_id']
        network_db = self.plugin._get_network(context, network_id)

        # Find the address_scope(s) for the old interface.
        #
        # REVISIT: If dual-stack interfaces allowed, process each
        # stack's scope separately, or at least raise an exception.
        scope_id = self._get_address_scope_id_for_subnets(context, subnets)

        query = BAKERY(lambda s: s.query(
            l3_db.Router))
        query += lambda q: q.filter_by(
            id=sa.bindparam('router_id'))
        router_db = query(session).params(
            router_id=router_id).one()

        contract = self._map_router(session, router_db, True)

        epg = None
        old_vrf = self._get_network_vrf(network_db.aim_mapping)
        if network_db.aim_mapping.epg_name:
            bd = self._get_network_bd(network_db.aim_mapping)
            epg = self._get_network_epg(network_db.aim_mapping)
            # Remove AIM Subnet(s) for each removed Neutron subnet.
            for subnet in subnets:
                gw_ip = self._ip_for_subnet(subnet, port['fixed_ips'])
                sn_ext = self.get_subnet_extn_db(session, subnet['id'])
                if sn_ext.get(cisco_apic.EPG_SUBNET, False):
                    epg_sn = self._map_epg_subnet(subnet, gw_ip, epg)
                    self.aim.delete(aim_ctx, epg_sn)
                else:
                    sn = self._map_subnet(subnet, gw_ip, bd)
                    self.aim.delete(aim_ctx, sn)
        # SVI network with auto l3out.
        elif network_db.aim_mapping.l3out_name:
            epg = self._get_network_l3out_ext_net(network_db.aim_mapping)

        # Find remaining routers with interfaces to this network.
        query = BAKERY(lambda s: s.query(
            l3_db.RouterPort.router_id))
        query += lambda q: q.join(
            models_v2.Port,
            models_v2.Port.id == l3_db.RouterPort.port_id)
        query += lambda q: q.filter(
            models_v2.Port.network_id == sa.bindparam('network_id'),
            l3_db.RouterPort.port_type == n_constants.DEVICE_OWNER_ROUTER_INTF)
        query += lambda q: q.distinct()
        router_ids = [r[0] for r in
                      query(session).params(
                          network_id=network_id)]

        # If network is no longer connected to this router, stop
        # network's EPG from providing/consuming this router's
        # Contract.
        if router_id not in router_ids and epg:
            epg = self.aim.get(aim_ctx, epg)
            if isinstance(epg, aim_resource.ExternalNetwork):
                self._delete_external_epg_contract(aim_ctx,
                    aim_resource.ExternalNetworkProvidedContract,
                    epg, contract)
                self._delete_external_epg_contract(aim_ctx,
                    aim_resource.ExternalNetworkConsumedContract,
                    epg, contract)
            else:
                contracts = [name for name in epg.consumed_contract_names
                             if name != contract.name]
                epg = self.aim.update(aim_ctx, epg,
                                      consumed_contract_names=contracts)

                contracts = [name for name in epg.provided_contract_names
                             if name != contract.name]
                epg = self.aim.update(aim_ctx, epg,
                                      provided_contract_names=contracts)

        nets_to_notify = set()
        ports_to_notify = set()
        router_topo_moved = False

        # If unscoped topologies have split, move VRFs as needed.
        #
        # REVISIT: For non-isomorphic dual-stack network, may need to
        # move the BD and EPG from the previously-routed v4 VRF to the
        # still-routed v6 VRF, and disable identity NAT for the v6
        # traffic.
        if scope_id == NO_ADDR_SCOPE:
            # If the interface's network has not become unrouted, see
            # if its topology must be moved.
            if router_ids:
                intf_topology = self._network_topology(session, network_db)
                intf_shared_net = self._topology_shared(intf_topology)
                intf_vrf = self._map_default_vrf(
                    session, intf_shared_net or network_db)
                if old_vrf.identity != intf_vrf.identity:
                    intf_vrf = self._ensure_default_vrf(aim_ctx, intf_vrf)
                    self._move_topology(
                        context, aim_ctx, intf_topology, old_vrf, intf_vrf,
                        nets_to_notify)
                    self._check_vrf_for_overlap(session, intf_vrf)

            # See if the router's topology must be moved.
            router_topology = self._router_topology(session, router_db.id)
            if router_topology:
                router_shared_net = self._topology_shared(router_topology)
                router_vrf = self._map_default_vrf(
                    session,
                    router_shared_net or next(
                        iter(list(router_topology.values()))))
                if old_vrf.identity != router_vrf.identity:
                    router_vrf = self._ensure_default_vrf(aim_ctx, router_vrf)
                    self._move_topology(
                        context, aim_ctx, router_topology, old_vrf, router_vrf,
                        nets_to_notify)
                    self._check_vrf_for_overlap(session, router_vrf)
                    router_topo_moved = True

        # If network is no longer connected to any router, make the
        # network's BD unrouted.
        if not router_ids:
            self._dissassociate_network_from_vrf(
                context, aim_ctx, network_db, old_vrf, nets_to_notify,
                scope_id)
            if scope_id == NO_ADDR_SCOPE:
                self._cleanup_default_vrf(aim_ctx, old_vrf)

        # If external-gateway is set, handle external-connectivity changes.
        # External network is not supproted for SVI network for now.
        if router_db.gw_port_id and not self._is_svi_db(network_db):
            net = self.plugin.get_network(context,
                                          router_db.gw_port.network_id)
            # If this was the last interface for this VRF for this
            # router, update external-conectivity to exclude this
            # router.
            if not self._get_router_intf_count(session, router_db, scope_id):
                self._manage_external_connectivity(
                    context, router_db, net, None, old_vrf)
            elif router_topo_moved:
                # Router moved from old_vrf to router_vrf, so
                # 1. Update old_vrf's external connectivity to exclude router
                # 2. Update router_vrf's external connectivity to include
                #    router
                self._manage_external_connectivity(context, router_db, net,
                                                   None, old_vrf)
                self._manage_external_connectivity(context, router_db, None,
                                                   net, router_vrf)

            # If network is no longer connected to this router
            if router_id not in router_ids:
                aim_l3out, _, ns = self._get_aim_nat_strategy(net)
                if aim_l3out and ns:
                    ns.unset_bd_l3out(aim_ctx, bd, aim_l3out)

            # SNAT information of ports on the subnet will change because
            # of router interface removal. Send a port update so that it may
            # be recalculated.
            port_ids = self._get_compute_dhcp_ports_in_subnets(
                session,
                [subnet['id'] for subnet in subnets])
            ports_to_notify.update(port_ids)

        # Provide notifications for all affected ports.
        if nets_to_notify:
            port_ids = self._get_non_router_ports_in_networks(
                session, nets_to_notify)
            ports_to_notify.update(port_ids)
        if ports_to_notify:
            self._add_postcommit_port_notifications(context, ports_to_notify)

    def _check_vrf_for_overlap(self, session, vrf, new_subnets=None):
        if self.allow_routed_vrf_subnet_overlap:
            return

        query = BAKERY(lambda s: s.query(
            models_v2.Subnet.id,
            models_v2.Subnet.cidr))
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.subnet_id ==
            models_v2.Subnet.id)
        query += lambda q: q.join(
            l3_db.RouterPort,
            l3_db.RouterPort.port_id ==
            models_v2.IPAllocation.port_id)
        query += lambda q: q.join(
            db.NetworkMapping,
            db.NetworkMapping.network_id ==
            models_v2.Subnet.network_id)
        query += lambda q: q.filter(
            l3_db.RouterPort.port_type ==
            n_constants.DEVICE_OWNER_ROUTER_INTF,
            db.NetworkMapping.vrf_name ==
            sa.bindparam('vrf_name'),
            db.NetworkMapping.vrf_tenant_name ==
            sa.bindparam('vrf_tenant_name'))
        subnets = [(id, netaddr.IPNetwork(cidr))
                   for id, cidr in query(session).params(
                           vrf_name=vrf.name,
                           vrf_tenant_name=vrf.tenant_name)]

        if new_subnets:
            subnets.extend(
                [(s['id'], netaddr.IPNetwork(s['cidr']))
                 for s in new_subnets])

        subnets.sort(key=lambda s: s[1])
        for (id1, cidr1), (id2, cidr2) in zip(subnets[:-1], subnets[1:]):
            if id2 != id1 and cidr2 in cidr1:
                raise exceptions.SubnetOverlapInRoutedVRF(
                    id1=id1, cidr1=cidr1, id2=id2, cidr2=cidr2, vrf=vrf)

    def bind_port(self, context):
        port = context.current
        LOG.debug("Attempting to bind port %(port)s on network %(net)s",
                  {'port': port['id'],
                   'net': context.network.current['id']})

        # Check the VNIC type.
        vnic_type = port.get(portbindings.VNIC_TYPE,
                             portbindings.VNIC_NORMAL)
        if vnic_type not in SUPPORTED_VNIC_TYPES:
            LOG.debug("Refusing to bind due to unsupported vnic_type: %s",
                      vnic_type)
            return

        if port[portbindings.HOST_ID].startswith(FABRIC_HOST_ID):
            for segment in context.segments_to_bind:
                context.set_binding(segment[api.ID],
                                    VIF_TYPE_FABRIC,
                                    {portbindings.CAP_PORT_FILTER: False},
                                    status=n_constants.PORT_STATUS_ACTIVE)
                return

        is_vm_port = port['device_owner'].startswith('compute:')

        if (is_vm_port and self.gbp_driver and not
            self.gbp_driver.check_allow_vm_names(context, port)):
            return

        if vnic_type in [portbindings.VNIC_NORMAL]:
            if is_vm_port:
                # For compute ports, try to bind DVS agent first.
                if self._agent_bind_port(context, AGENT_TYPE_DVS,
                                         self._dvs_bind_port):
                    return

            # Try to bind OpFlex agent.
            if self._agent_bind_port(context, ofcst.AGENT_TYPE_OPFLEX_OVS,
                                     self._opflex_bind_port):
                return

            # Try to bind OpFlex VPP agent.
            if self._agent_bind_port(context, ofcst.AGENT_TYPE_OPFLEX_VPP,
                                     self._opflex_bind_port):
                return

        if self._is_baremetal_vnic_type(context.current):
            self._bind_baremetal_vnic(context)
            return

        # If we reached here, it means that either there is no active opflex
        # agent running on the host, this was not a baremetal VNIC, or the
        # agent on the host is not configured for this physical network.
        # Treat the host as a physical node (i.e. has no OpFlex agent running)
        # and try binding hierarchically if the network-type is OpFlex.
        self._bind_physical_node(context)

    def _update_sg_rule_with_remote_group_set(self, context, port):
        security_groups = port['security_groups']
        original_port = context.original
        if original_port:
            removed_sgs = (set(original_port['security_groups']) -
                           set(security_groups))
            added_sgs = (set(security_groups) -
                         set(original_port['security_groups']))
            self._really_update_sg_rule_with_remote_group_set(
                                context, port, removed_sgs, is_delete=True)
            self._really_update_sg_rule_with_remote_group_set(
                                context, port, added_sgs, is_delete=False)
            self._really_update_sg_rule_with_remote_address_group_set(
                                context, port, removed_sgs, is_delete=True)
            self._really_update_sg_rule_with_remote_address_group_set(
                                context, port, added_sgs, is_delete=False)

    def _really_update_sg_rule_with_remote_group_set(
                    self, context, port, security_groups, is_delete):
        if not security_groups:
            return
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        query = BAKERY(lambda s: s.query(
            sg_models.SecurityGroupRule))
        query += lambda q: q.filter(
            sg_models.SecurityGroupRule.remote_group_id.in_(
                sa.bindparam('security_groups', expanding=True)))
        sg_rules = query(session).params(
            security_groups=list(security_groups)).all()

        fixed_ips = [x['ip_address'] for x in port['fixed_ips']]
        sg_to_tenant = {}
        for sg_rule in sg_rules:
            sg_id = sg_rule['security_group_id']
            if sg_id in sg_to_tenant:
                tenant_id = sg_to_tenant[sg_id]
            else:
                tenant_id = self._get_sg_rule_tenant_id(session, sg_rule)
                sg_to_tenant[sg_id] = tenant_id
            tenant_aname = self.name_mapper.project(session, tenant_id)
            sg_rule_aim = aim_resource.SecurityGroupRule(
                tenant_name=tenant_aname,
                security_group_name=sg_rule['security_group_id'],
                security_group_subject_name='default',
                name=sg_rule['id'])
            aim_sg_rule = self.aim.get(aim_ctx, sg_rule_aim)
            if not aim_sg_rule:
                continue
            ip_version = 0
            if sg_rule['ethertype'] == 'IPv4':
                ip_version = 4
            elif sg_rule['ethertype'] == 'IPv6':
                ip_version = 6
            for fixed_ip in fixed_ips:
                if is_delete:
                    if fixed_ip in aim_sg_rule.remote_ips:
                        aim_sg_rule.remote_ips.remove(fixed_ip)
                elif ip_version == netaddr.IPAddress(fixed_ip).version:
                    if fixed_ip not in aim_sg_rule.remote_ips:
                        aim_sg_rule.remote_ips.append(fixed_ip)
            self.aim.update(aim_ctx, sg_rule_aim,
                            remote_ips=aim_sg_rule.remote_ips)

    def _really_update_sg_rule_with_remote_address_group_set(
            self, context, port, security_groups, is_delete):
        if not security_groups:
            return
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        query = BAKERY(lambda s: s.query(
                        sg_models.SecurityGroupRule,
                        ag_db.AddressGroup))
        query += lambda q: q.filter(
            sg_models.SecurityGroupRule.remote_address_group_id ==
            ag_db.AddressGroup.id)
        res = query(session).params(
            security_groups=list(security_groups)).all()
        sg_to_tenant = {}
        for sg in res:
            sg_rule = sg[0]
            address_group = sg[1]
            sg_id = sg_rule['security_group_id']
            if sg_id in sg_to_tenant:
                tenant_id = sg_to_tenant[sg_id]
            else:
                tenant_id = self._get_sg_rule_tenant_id(session, sg_rule)
                sg_to_tenant[sg_id] = tenant_id
            tenant_aname = self.name_mapper.project(session, tenant_id)
            sg_rule_aim = aim_resource.SecurityGroupRule(
                        tenant_name=tenant_aname,
                        security_group_name=sg_rule['security_group_id'],
                        security_group_subject_name='default',
                        name=sg_rule['id'])
            aim_sg_rule = self.aim.get(aim_ctx, sg_rule_aim)
            if not aim_sg_rule:
                continue
            for ag_address in address_group['addresses']:
                address = str(ag_address.address)
                if is_delete:
                    if address in aim_sg_rule.remote_ips:
                        aim_sg_rule.remote_ips.remove(address)
                else:
                    if address not in aim_sg_rule.remote_ips:
                        aim_sg_rule.remote_ips.append(address)
            self.aim.update(aim_ctx, sg_rule_aim,
                            remote_ips=aim_sg_rule.remote_ips)

    def _check_active_active_aap(self, context, port):
        aap_current = port.get('allowed_address_pairs', [])
        aap_original = []
        if context.original:
            aap_original = context.original.get('allowed_address_pairs', [])
        curr_ips = [aap['ip_address'] for aap in aap_current]
        orig_ips = [aap['ip_address'] for aap in aap_original]
        added = list(set(curr_ips) - set(orig_ips))
        if not added:
            return

        session = context._plugin_context.session
        query = BAKERY(lambda s: s.query(
            n_addr_pair_db.AllowedAddressPair.port_id,
            n_addr_pair_db.AllowedAddressPair.ip_address,
            models_v2.IPAllocation.subnet_id))
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.port_id ==
            n_addr_pair_db.AllowedAddressPair.port_id)
        query += lambda q: q.filter(
            n_addr_pair_db.AllowedAddressPair.port_id != sa.bindparam(
                'port_id'),
            models_v2.IPAllocation.network_id == sa.bindparam('network_id'))
        addr_pairs = query(session).params(
            network_id=port['network_id'],
            port_id=port['id']).all()
        if not addr_pairs:
            return

        affected_ports = {}
        cidr_aap = netaddr.IPSet()
        for added_ip in added:
            cidr_aap.add(netaddr.IPNetwork(added_ip))
        for a_pair in addr_pairs:
            port_id, ip_address, subnet_id = a_pair
            cidr = netaddr.IPSet(netaddr.IPNetwork(ip_address))
            if cidr & cidr_aap:
                affected_ports.setdefault(port_id, [])
                if subnet_id not in affected_ports[port_id]:
                    affected_ports[port_id].append(subnet_id)
        if not affected_ports:
            return

        # Make sure all these ports belong to the same
        # active_active_aap mode.
        subnet_ids = [x['subnet_id'] for x in port['fixed_ips']]
        active_aap_mode = self._query_active_active_aap(session, subnet_ids)
        for port_id, other_subnet_ids in list(affected_ports.items()):
            other_active_aap_mode = self._query_active_active_aap(
                                                    session, other_subnet_ids)
            if active_aap_mode != other_active_aap_mode:
                raise exceptions.AAPNotAllowedOnDifferentActiveActiveAAPSubnet(
                    subnet_ids=subnet_ids, other_subnet_ids=other_subnet_ids)

    def _get_erspan_aim_resources(self, port_id, cep_dn, erspan_config):
        dest_ip = erspan_config[cisco_apic.ERSPAN_DEST_IP]
        flow_id = erspan_config[cisco_apic.ERSPAN_FLOW_ID]
        direction = erspan_config[cisco_apic.ERSPAN_DIRECTION]
        source_group_name = self._erspan_source_group_name(port_id,
                                                           erspan_config)
        dest_group_name = self._erspan_dest_group_name(erspan_config)

        source_group = aim_resource.SpanVsourceGroup(
            name=source_group_name)
        dest_group = aim_resource.SpanVdestGroup(name=dest_group_name)
        source = aim_resource.SpanVsource(vsg_name=source_group_name,
            name=source_group_name, dir=direction, src_paths=[cep_dn])
        dest = aim_resource.SpanVdest(vdg_name=dest_group_name,
                                      name=dest_group_name)
        summary = aim_resource.SpanVepgSummary(vdg_name=dest_group_name,
            vd_name=dest_group_name, dst_ip=dest_ip, flow_id=flow_id)
        label = aim_resource.SpanSpanlbl(vsg_name=source_group_name,
            name=dest_group_name, tag='yellow-green')
        return (source_group, dest_group, source, dest, summary, label)

    def _get_erspan_aim_resources_list(self, port, cep_dn):
        erspan_configs = port.aim_extension_erspan_configs or []
        resources = []
        for erspan_config in erspan_configs:
            resources.extend(self._get_erspan_aim_resources(
                port['id'], cep_dn, erspan_config))
        return resources

    def update_summary_resource(self, session, resources, network_id):
        for resource in resources:
            if type(resource) == aim_resource.SpanVepgSummary:
                with db_api.CONTEXT_READER.using(session):
                    query = BAKERY(lambda s: s.query(
                                models_v2.Network))
                    query += lambda q: q.filter_by(
                                id=network_id)
                    net = query(session).params().first()
                    if net:
                        resource.mtu = net.mtu

    def _get_acc_bundle_for_host(self, aim_ctx, host_name):
        if not host_name:
            return None
        host_links = self.aim.find(aim_ctx, aim_infra.HostLink,
            host_name=host_name)
        # Extract the interface policy group names from the DNs.
        grpNames = []
        for host_link in host_links:
            # topology is a tuple consisting of:
            # (is_vpc, pod_id, nodes, node_paths, module/bundle, None/port)
            topology = self._get_topology_from_path(host_link.path)
            # This currently only supports opflex devices connected via
            # interface policy groups that are either port channel or
            # virtual port channel interfaces.
            if not topology[0]:
                continue
            grpNames.append(topology[4])
        # VPCs will have two entries, so remove duplicates.
        return list(set(grpNames))[0] if grpNames else None

    def _create_erspan_aim_config(self, context, cep_dn, port):
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(db_session=session)

        agent = context.host_agents(ofcst.AGENT_TYPE_OPFLEX_OVS)
        if not agent:
            LOG.warning("Port %s is not bound to an opflex host, "
                        "so an RSPAN session can't be established.",
                        port['id'])
            return

        acc_name = self._get_acc_bundle_for_host(aim_ctx,
            port['binding:host_id'])
        if acc_name:
            acc_bundle = aim_resource.InfraAccBundleGroup(name=acc_name)
        else:
            LOG.warning("A interface port group for port %s "
                        "could not be found - ERSPAN session "
                        "can't be established.", port['id'])
            return

        for erspan_config in port.get(cisco_apic.ERSPAN_CONFIG, []):
            resources = self._get_erspan_aim_resources(port['id'], cep_dn,
                                                       erspan_config)
            if resources:
                self.update_summary_resource(session, resources,
                                             port['network_id'])
            # Create ERSPAN source group and source
            if not self.aim.get(aim_ctx, resources[0]):
                self.aim.create(aim_ctx, resources[0])
                # Create ERSPAN source and destination.
                self.aim.create(aim_ctx, resources[2])

            # Create the dest group, dest, and summary
            # resources if needed.
            if not self.aim.get(aim_ctx, resources[1]):
                self.aim.create(aim_ctx, resources[1])
                self.aim.create(aim_ctx, resources[3])
                self.aim.create(aim_ctx, resources[4])

            # Update the bundle group.
            curr_bundle = self.aim.get(aim_ctx, acc_bundle)
            source_groups = curr_bundle.span_vsource_group_names
            dest_groups = curr_bundle.span_vdest_group_names
            if resources[0].name not in source_groups:
                source_groups.append(resources[0].name)
            if resources[1].name not in dest_groups:
                dest_groups.append(resources[1].name)
            self.aim.update(aim_ctx, curr_bundle,
                span_vsource_group_names=source_groups,
                span_vdest_group_names=dest_groups)
            # Create the ERSPAN label.
            if not self.aim.get(aim_ctx, resources[5]):
                self.aim.create(aim_ctx, resources[5])

    def _erspan_source_group_name(self, port_id, erspan_config):
        return port_id + '-' + erspan_config['direction']

    def _erspan_dest_group_name(self, erspan_config):
        return erspan_config['dest_ip'] + '-' + str(erspan_config['flow_id'])

    def _delete_erspan_aim_config(self, context, port, erspan_configs=None):
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(db_session=session)
        acc_name = self._get_acc_bundle_for_host(aim_ctx,
            port.get('binding:host_id'))
        erspan_configs = (erspan_configs if erspan_configs else
            port.get(cisco_apic.ERSPAN_CONFIG, []))
        for erspan_config in erspan_configs:
            source_group = self._erspan_source_group_name(port['id'],
                                                          erspan_config)
            dest_group = self._erspan_dest_group_name(erspan_config)
            # The destination state can be shared, so check to see if
            # any other label sources reference it.
            labels = self.aim.find(aim_ctx, aim_resource.SpanSpanlbl,
                                   name=dest_group)
            other_sources = [label.name for label in labels
                             if label.vsg_name != source_group]
            if acc_name:
                # Remove the source from the bundle group, but only
                # remove the destination if no other sources are using it.
                curr_bg = self.aim.get(aim_ctx,
                    aim_resource.InfraAccBundleGroup(name=acc_name))
                vsource_group_names = list(
                    set(curr_bg.span_vsource_group_names) -
                    set([source_group]))
                vdest_group_names = list(set(curr_bg.span_vdest_group_names) -
                                         set([dest_group]) if not other_sources
                                         else curr_bg.span_vdest_group_names)
                self.aim.update(aim_ctx, aim_resource.InfraAccBundleGroup(
                    name=acc_name),
                    span_vsource_group_names=vsource_group_names,
                    span_vdest_group_names=vdest_group_names)

            # We can delete the source state and label, as those
            # are only dependent on this neutron port.
            self.aim.delete(aim_ctx, aim_resource.SpanSpanlbl(
                vsg_name=source_group, name=dest_group))

            self.aim.delete(aim_ctx, aim_resource.SpanVsource(
                vsg_name=source_group, name=source_group))
            self.aim.delete(aim_ctx, aim_resource.SpanVsourceGroup(
                name=source_group))

            # No other sources share this dest, so we can delete it.
            if not other_sources:
                self.aim.delete(aim_ctx, aim_resource.SpanVepgSummary(
                    vdg_name=dest_group, vd_name=dest_group))
                self.aim.delete(aim_ctx, aim_resource.SpanVdest(
                    vdg_name=dest_group, name=dest_group))
                self.aim.delete(aim_ctx, aim_resource.SpanVdestGroup(
                    name=dest_group))

    def _check_valid_erspan_config(self, port):
        # Currently only supported on instance ports
        if not port['device_owner'].startswith('compute:'):
            raise exceptions.InvalidPortForErspanSession()

        # Not supported on SVI networks
        ctx = n_context.get_admin_context()
        net_db = self.plugin._get_network(ctx, port['network_id'])
        if self._is_svi_db(net_db):
            raise exceptions.InvalidNetworkForErspanSession()

    def create_port_precommit(self, context):
        port = context.current
        self._check_active_active_aap(context, port)
        if port.get(cisco_apic.ERSPAN_CONFIG):
            self._check_valid_erspan_config(port)
        self._really_update_sg_rule_with_remote_group_set(
            context, port, port['security_groups'], is_delete=False)
        self._really_update_sg_rule_with_remote_address_group_set(
            context, port, port['security_groups'], is_delete=False)
        self._insert_provisioning_block(context)

        # Handle router gateway port creation.
        if self._is_port_router_gateway(port):
            router = self.l3_plugin.get_router(
                context._plugin_context, port['device_id'])
            self._update_router_external_connectivity(
                context._plugin_context, router, port['network_id'],
                notify=True)

        # Handle router interface port creation.
        if self._is_port_router_interface(port):
            self._process_router_interface_port(
                context, port['fixed_ips'], [])

    def create_port_postcommit(self, context):
        self._send_postcommit_notifications(context._plugin_context)

    def _insert_provisioning_block(self, context):
        # we insert a status barrier to prevent the port from transitioning
        # to active until the agent reports back that the wiring is done
        port = context.current
        if (not context.host or
                port['status'] == n_constants.PORT_STATUS_ACTIVE):
            # no point in putting in a block if the status is already ACTIVE
            return

        # Check the VNIC type.
        vnic_type = port.get(portbindings.VNIC_TYPE,
                             portbindings.VNIC_NORMAL)
        if vnic_type not in SUPPORTED_VNIC_TYPES:
            LOG.debug("No provisioning_block due to unsupported vnic_type: %s",
                      vnic_type)
            return

        if (context.host_agents(ofcst.AGENT_TYPE_OPFLEX_OVS) or
                context.host_agents(AGENT_TYPE_DVS)):
            provisioning_blocks.add_provisioning_component(
                context._plugin_context, port['id'], resources.PORT,
                provisioning_blocks.L2_AGENT_ENTITY)

    def _check_allowed_address_pairs(self, context, port):
        aap_current = context.current.get('allowed_address_pairs', [])
        aap_original = context.original.get('allowed_address_pairs', [])
        # If there was a change in configured AAPs, then we may need
        # to clean up the owned IPs table
        p_context = context._plugin_context
        if aap_current != aap_original:
            curr_ips = [aap['ip_address'] for aap in aap_current]
            orig_ips = [aap['ip_address'] for aap in aap_original]
            removed = list(set(orig_ips) - set(curr_ips))
            for aap in removed:
                cidr = netaddr.IPNetwork(aap)
                with db_api.CONTEXT_WRITER.using(p_context) as session:
                    # Get all the owned IP addresses for the port, and if
                    # they match a removed AAP entry, delete that entry
                    # from the DB
                    ha_ips = self.get_ha_ipaddresses_for_port(
                        port['id'], session=session)
                    for ip in ha_ips:
                        if ip in cidr:
                            self.delete_port_id_for_ha_ipaddress(
                                port['id'], ip, session=session)

    def _update_svi_ports_for_added_subnet(self, ctx, new_subnets,
                                           original_port):
        """ Subnet added to bound port on SVI net, add to SVI ports if needed.

            We have to ensure that the SVI ports are updated with the added
            subnet _before_ we get into updating the static paths to generate
            the Interface Profile for the AF of tne new subnet. Doing this
            in the context of the BEFORE_UPDATE event guarantees that there
            is no race condition.

        """
        # Find the SVI ports corresponding to the host in the bound
        # port. Then update with new subnet.
        session = ctx.session
        aim_ctx = aim_context.AimContext(db_session=session)
        host_links = self.aim.find(aim_ctx, aim_infra.HostLink,
            host_name=original_port['binding:host_id'])
        allnodes = set()
        for host_link in host_links:
            _, _, nodes, _, _, _ = self._get_topology_from_path(host_link.path)
            allnodes.update(nodes)

        filters = {'network_id': [original_port['network_id']],
                   'name': ['apic-svi-port:node-%s' % node
                        for node in allnodes]}
        svi_ports = self.plugin.get_ports(ctx, filters)

        for svi_port in svi_ports:
            subnet_added = False
            svi_port_subnets = [s['subnet_id']
                for s in svi_port['fixed_ips']]
            # update the SVI port if the added subnet is not present.
            for s in new_subnets:
                if s not in svi_port_subnets:
                    svi_port['fixed_ips'].append(
                        {'subnet_id': s})
                    subnet_added = True
            if subnet_added:
                port_info = {
                    'port': {'fixed_ips': svi_port['fixed_ips']}
                }
                self.plugin.update_port(ctx, svi_port['id'], port_info)

    # NOTE: This event is generated by the ML2 plugin in three places,
    # all outside of transactions. It is generated from update_port
    # and from _update_individual_port_db_status without the
    # orig_binding and new_binding kwargs, and it is generated with
    # those args from _commit_port_binding.
    @registry.receives(resources.PORT, [events.BEFORE_UPDATE])
    def _before_update_port(self, resource, event, trigger, payload,
                            orig_binding=None, new_binding=None):
        context = payload.context
        original_port = payload.states[0]
        port = payload.states[1]
        if payload.metadata:
            orig_binding = payload.metadata['orig_binding']
            new_binding = payload.metadata['new_binding']

        if self._is_port_bound(original_port) and 'fixed_ips' in port:
            # When a bound port is updated with a subnet, if the port
            # is on a SVI network, we need to ensure that the SVI ports
            # which are used for creating the Interface Profile for each
            # AF also gets updated with the new subnet.
            #
            # Since we are not in the port binding workflow, can't get the
            # bind_context

            curr = [s['subnet_id']
                for s in port['fixed_ips']]
            orig = [s['subnet_id']
                for s in original_port['fixed_ips']]
            new_subnets = list(set(curr) - set(orig))
            if not new_subnets:
                return
            ctx = n_context.get_admin_context()
            net_db = self.plugin._get_network(ctx, original_port['network_id'])
            if self._is_svi_db(net_db):
                self._update_svi_ports_for_added_subnet(ctx, new_subnets,
                    original_port)

        # We are only interested in port update callbacks from
        # _commit_port_binding, in which the port is about to become
        # bound. Any other callbacks using this event are ignored.
        if not (orig_binding and
                orig_binding.vif_type == portbindings.VIF_TYPE_UNBOUND and
                new_binding and
                new_binding.vif_type != portbindings.VIF_TYPE_UNBOUND):
            return

        # The bind_context containing the binding levels is not passed
        # to this callback, and the binding levels haven't been
        # persisted yet, so find the _commit_port_binding method's
        # bind_context local variable on the stack. NOTE: This may
        # require updating when forward-porting to newer upstream
        # releases.
        bind_context = gbp_utils.get_function_local_from_stack(
            '_commit_port_binding', 'bind_context')
        if not bind_context:
            LOG.warning("Unabled to find bind_context on stack in "
                        "_before_update_port.")
            return

        # Nothing to do unless the network is SVI.
        if not (bind_context.network and
                bind_context.network.current and
                self._is_svi(bind_context.network.current)):
            return

        # Get the static ports for the new binding.
        with db_api.CONTEXT_READER.using(context):
            static_ports = self._get_static_ports(
                context, bind_context.host, bind_context.bottom_bound_segment,
                port_context=bind_context)

        # Make sure SVI IPs are allocated for each node of each
        # static_port.
        self._ensure_svi_ips_for_static_ports(
            context, static_ports, bind_context.network.current)

    def update_port_precommit(self, context):
        session = context._plugin_context.session
        orig = context.original
        port = context.current
        self._check_active_active_aap(context, port)
        if port.get(cisco_apic.ERSPAN_CONFIG):
            self._check_valid_erspan_config(port)
        if context.original_host and context.original_host != context.host:
            self.disassociate_domain(context, use_original=True)
            if self._use_static_path(context.original_bottom_bound_segment):
                # remove static binding for old host
                self._update_static_path(context, host=context.original_host,
                    segment=context.original_bottom_bound_segment, remove=True)
                self._release_dynamic_segment(context, use_original=True)
            # The port is either being unbound or rebound. We need the host
            # from the original port binding, so pass that as the port
            self._delete_erspan_aim_config(context, orig,
                                           port.get(cisco_apic.ERSPAN_CONFIG))
        if self._is_port_bound(port):
            if self._use_static_path(context.bottom_bound_segment):
                self._associate_domain(context, is_vmm=False)
                self._update_static_path(context)
            elif (context.bottom_bound_segment and
                  self._is_opflex_type(
                        context.bottom_bound_segment[api.NETWORK_TYPE])):
                self._associate_domain(context, is_vmm=True)
            # Handle changes in ERSPAN configuration.
            if (port.get(cisco_apic.ERSPAN_CONFIG) or
                    orig.get(cisco_apic.ERSPAN_CONFIG)):
                erspan_deletions = []
                for erspan in orig.get(cisco_apic.ERSPAN_CONFIG, []):
                    if erspan not in port[cisco_apic.ERSPAN_CONFIG]:
                        erspan_deletions.append(erspan)
                cep_dn = self._map_port(session, port)
                self._delete_erspan_aim_config(context, port,
                                               erspan_deletions)
                self._create_erspan_aim_config(context, cep_dn, port)

        self._update_sg_rule_with_remote_group_set(context, port)
        self._check_allowed_address_pairs(context, port)
        self._insert_provisioning_block(context)
        registry.publish(aim_cst.GBP_PORT, events.PRECOMMIT_UPDATE,
                         self,
                         payload=events.DBEventPayload(context))

        # No need to handle router gateway port update, as we don't
        # care about its fixed_ips.

        # Handle router interface port update.
        if (self._is_port_router_interface(port) or
            self._is_port_router_interface(context.original)):
            self._process_router_interface_port(
                context,
                port['fixed_ips']
                if self._is_port_router_interface(port) else [],
                context.original['fixed_ips']
                if self._is_port_router_interface(context.original) else [])

    def update_port_postcommit(self, context):
        orig = context.original
        port = context.current
        if (port.get('binding:vif_details') and
                port['binding:vif_details'].get('dvs_port_group_name')) and (
                self.dvs_notifier):
            self.dvs_notifier.update_postcommit_port_call(
                context.current,
                context.original,
                context.bottom_bound_segment,
                context.host
            )
        elif (self._is_baremetal_vnic_type(port) and
              port.get(trunk_details.TRUNK_DETAILS) and
              self._is_port_bound(port) != self._is_port_bound(orig)):
            # For trunk parent ports that are baremetal VNICs, and have a
            # port binding state transition, handle binding of any subports
            # and setting the value of the trunk's status.
            subport_ids = [sp['port_id'] for sp in
                port[trunk_details.TRUNK_DETAILS][trunk.SUB_PORTS]]
            owner = ('' if not port['binding:host_id']
                     else trunk_consts.TRUNK_SUBPORT_OWNER)
            self._update_trunk_status_and_subports(context._plugin_context,
                port[trunk_details.TRUNK_DETAILS]['trunk_id'],
                port[portbindings.HOST_ID], subport_ids, owner,
                binding_profile=port[portbindings.PROFILE])

        self._send_postcommit_notifications(context._plugin_context)

    def _update_floatingip_status(self, session, port_id):
        query = (sa.update(l3_db.FloatingIP).
                where(l3_db.FloatingIP.fixed_port_id == port_id).
                values(status=n_constants.FLOATINGIP_STATUS_DOWN))
        session.execute(query)

    def delete_port_precommit(self, context):
        port = context.current
        if self._is_port_bound(port):
            if self._use_static_path(context.bottom_bound_segment):
                self._update_static_path(context, remove=True)
                self.disassociate_domain(context)
                self._release_dynamic_segment(context)
            elif (context.bottom_bound_segment and
                  self._is_opflex_type(
                      context.bottom_bound_segment[api.NETWORK_TYPE])):
                self.disassociate_domain(context)
            self._delete_erspan_aim_config(context, port)
        self._really_update_sg_rule_with_remote_group_set(
            context, port, port['security_groups'], is_delete=True)
        self._really_update_sg_rule_with_remote_address_group_set(
            context, port, port['security_groups'], is_delete=True)

        # Set status of floating ip DOWN.
        self._update_floatingip_status(
            context._plugin_context.session, port['id'])

        # Handle router gateway port deletion.
        if self._is_port_router_gateway(port):
            router = self.l3_plugin.get_router(
                context._plugin_context, port['device_id'])
            self._update_router_external_connectivity(
                context._plugin_context, router, port['network_id'],
                notify=True, removing=True)

        # Handle router interface port deletion.
        if self._is_port_router_interface(port):
            # The RouterPort would be cascade-deleted with this Port,
            # but delete it now so its not considered by queries while
            # removing the interface.
            self._delete_router_port(
                context._plugin_context.session, port['id'])
            self._process_router_interface_port(
                context, [], port['fixed_ips'])

    def delete_port_postcommit(self, context):
        port = context.current
        plugin_context = context._plugin_context
        if (port.get('binding:vif_details') and
                port['binding:vif_details'].get('dvs_port_group_name')) and (
                self.dvs_notifier):
            self.dvs_notifier.delete_port_call(
                context.current,
                context.original,
                context.bottom_bound_segment,
                context.host
            )

        # Handle router gateway port deletion.
        if self._is_port_router_gateway(port):
            self._delete_unneeded_snat_ip_ports(
                plugin_context, port['network_id'])

        # Handle router interface port deletion.
        if self._is_port_router_interface(port):
            router_db = self.l3_plugin._get_router(
                plugin_context, port['device_id'])
            if router_db and router_db.gw_port:
                self._delete_unneeded_snat_ip_ports(
                    plugin_context, router_db.gw_port.network_id)

        self._send_postcommit_notifications(context._plugin_context)

    def _is_port_router_gateway(self, port):
        return (
            port['device_owner'] == n_constants.DEVICE_OWNER_ROUTER_GW and
            port['device_id'])

    def _is_port_router_interface(self, port):
        return (
            port['device_owner'] == n_constants.DEVICE_OWNER_ROUTER_INTF and
            port['device_id'])

    def _process_router_interface_port(self, context, new_ips, old_ips):
        router_id = context.current['device_id']
        if not router_id:
            return
        new_subnet_ids = set([ip['subnet_id'] for ip in new_ips])
        old_subnet_ids = set([ip['subnet_id'] for ip in old_ips])
        added_subnet_ids = list(new_subnet_ids - old_subnet_ids)
        removed_subnet_ids = list(old_subnet_ids - new_subnet_ids)
        if added_subnet_ids or removed_subnet_ids:
            router_db = self.l3_plugin._get_router(
                context._plugin_context, router_id)
        if added_subnet_ids:
            subnets = self.plugin.get_subnets(
                context._plugin_context.elevated(),
                filters={'id': added_subnet_ids})
            self._add_router_interface(
                context._plugin_context, router_db, context.current, subnets)
        if removed_subnet_ids:
            subnets = self.plugin.get_subnets(
                context._plugin_context.elevated(),
                filters={'id': removed_subnet_ids})
            self._remove_router_interface(
                context._plugin_context, router_db, context.current, subnets)

    def _delete_router_port(self, session, port_id):
        with db_api.CONTEXT_WRITER.using(session):
            query = BAKERY(lambda s: s.query(
                l3_db.RouterPort))
            query += lambda q: q.filter_by(
                port_id=sa.bindparam('port_id'))
            db_obj = query(session).params(
                port_id=port_id).one()
            session.delete(db_obj)

    def create_security_group_precommit(self, context):
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)

        sg = context.current
        tenant_aname = self.name_mapper.project(session, sg['tenant_id'])
        sg_aim = aim_resource.SecurityGroup(
            tenant_name=tenant_aname, name=sg['id'],
            display_name=aim_utils.sanitize_display_name(sg['name']))
        self.aim.create(aim_ctx, sg_aim)
        # Always create this default subject
        sg_subject = aim_resource.SecurityGroupSubject(
            tenant_name=tenant_aname,
            security_group_name=sg['id'], name='default')
        self.aim.create(aim_ctx, sg_subject)

        # Create those implicit rules
        for sg_rule in sg.get('security_group_rules', []):
            sg_rule_aim = aim_resource.SecurityGroupRule(
                tenant_name=tenant_aname,
                security_group_name=sg['id'],
                security_group_subject_name='default',
                name=sg_rule['id'],
                direction=sg_rule['direction'],
                ethertype=sg_rule['ethertype'].lower(),
                ip_protocol=(sg_rule['protocol'] if sg_rule['protocol']
                             else 'unspecified'),
                remote_ips=(sg_rule['remote_ip_prefix']
                            if sg_rule['remote_ip_prefix'] else ''),
                from_port=(sg_rule['port_range_min']
                           if sg_rule['port_range_min'] else 'unspecified'),
                to_port=(sg_rule['port_range_max']
                         if sg_rule['port_range_max'] else 'unspecified'),
                remote_group_id=(sg_rule['remote_group_id']
                                 if sg_rule['remote_group_id'] else ''))
            self.aim.create(aim_ctx, sg_rule_aim)

    def update_security_group_precommit(self, context):
        # Only display_name change makes sense here
        sg = context.current
        original_sg = context.original
        if sg.get('name') == original_sg.get('name'):
            return
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        tenant_aname = self.name_mapper.project(session, sg['tenant_id'])
        sg_aim = aim_resource.SecurityGroup(
            tenant_name=tenant_aname, name=sg['id'])
        self.aim.update(aim_ctx, sg_aim,
                        display_name=aim_utils.sanitize_display_name(
                            sg['name']))

    def delete_security_group_precommit(self, context):
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        sg = context.current
        tenant_aname = self.name_mapper.project(session, sg['tenant_id'])
        sg_aim = aim_resource.SecurityGroup(tenant_name=tenant_aname,
                                            name=sg['id'])
        self.aim.delete(aim_ctx, sg_aim, cascade=True)

    def _get_sg_rule_tenant_id(self, session, sg_rule):
        # There is a bug in Neutron that sometimes the tenant_id contained
        # within the sg_rule is pointing to the wrong tenant. So here we have
        # to query DB to get the tenant_id of the SG then use that instead.
        query = BAKERY(lambda s: s.query(
            sg_models.SecurityGroup.tenant_id))
        query += lambda q: q.filter(
            sg_models.SecurityGroup.id == sa.bindparam('sg_id'))
        tenant_id = query(session).params(
            sg_id=sg_rule['security_group_id']).first()[0]

        return tenant_id

    def create_security_group_rule_precommit(self, context):
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        sg_rule = context.current
        tenant_id = self._get_sg_rule_tenant_id(session, sg_rule)
        tenant_aname = self.name_mapper.project(session, tenant_id)
        if sg_rule.get('remote_group_id'):
            remote_ips = []

            query = BAKERY(lambda s: s.query(
                models_v2.Port))
            query += lambda q: q.join(
                sg_models.SecurityGroupPortBinding,
                sg_models.SecurityGroupPortBinding.port_id ==
                models_v2.Port.id)
            query += lambda q: q.filter(
                sg_models.SecurityGroupPortBinding.security_group_id ==
                sa.bindparam('sg_id'))
            sg_ports = query(session).params(
                sg_id=sg_rule['remote_group_id']).all()

            ip_version = 0
            if sg_rule['ethertype'] == 'IPv4':
                ip_version = 4
            elif sg_rule['ethertype'] == 'IPv6':
                ip_version = 6

            for sg_port in sg_ports:
                for fixed_ip in sg_port['fixed_ips']:
                    if ip_version == netaddr.IPAddress(
                        fixed_ip['ip_address']).version:
                        remote_ips.append(fixed_ip['ip_address'])

            remote_group_id = sg_rule['remote_group_id']
        else:
            remote_ips = ([sg_rule['remote_ip_prefix']]
                          if sg_rule['remote_ip_prefix'] else '')
            remote_group_id = ''

        sg_rule_aim = aim_resource.SecurityGroupRule(
            tenant_name=tenant_aname,
            security_group_name=sg_rule['security_group_id'],
            security_group_subject_name='default',
            name=sg_rule['id'],
            direction=sg_rule['direction'],
            ethertype=sg_rule['ethertype'].lower(),
            ip_protocol=self.get_aim_protocol(sg_rule['protocol']),
            remote_ips=remote_ips,
            icmp_code=(sg_rule['port_range_max']
                       if (sg_rule['port_range_max'] and
                          (sg_rule['protocol'].lower() == 'icmp' or
                           sg_rule['protocol'] == '1'))
                       else 'unspecified'),
            icmp_type=(sg_rule['port_range_min']
                       if (sg_rule['port_range_min'] and
                          (sg_rule['protocol'].lower() == 'icmp' or
                           sg_rule['protocol'] == '1'))
                       else 'unspecified'),
            from_port=(sg_rule['port_range_min']
                       if sg_rule['port_range_min'] else 'unspecified'),
            to_port=(sg_rule['port_range_max']
                     if sg_rule['port_range_max'] else 'unspecified'),
            remote_group_id=remote_group_id)
        self.aim.create(aim_ctx, sg_rule_aim)

    def delete_security_group_rule_precommit(self, context):
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        sg_rule = context.current
        tenant_id = self._get_sg_rule_tenant_id(session, sg_rule)
        tenant_aname = self.name_mapper.project(session, tenant_id)
        sg_rule_aim = aim_resource.SecurityGroupRule(
            tenant_name=tenant_aname,
            security_group_name=sg_rule['security_group_id'],
            security_group_subject_name='default',
            name=sg_rule['id'])
        self.aim.delete(aim_ctx, sg_rule_aim)

    def create_floatingip(self, context, current):
        if current['port_id']:
            current['status'] = n_constants.FLOATINGIP_STATUS_ACTIVE
            self._notify_port_update_for_fip(context, current['port_id'])
        else:
            current['status'] = n_constants.FLOATINGIP_STATUS_DOWN

    def update_floatingip(self, context, original, current):
        if (original['port_id'] and
            original['port_id'] != current['port_id']):
            self._notify_port_update_for_fip(context, original['port_id'])
        if current['port_id']:
            current['status'] = n_constants.FLOATINGIP_STATUS_ACTIVE
            self._notify_port_update_for_fip(context, current['port_id'])
        else:
            current['status'] = n_constants.FLOATINGIP_STATUS_DOWN

    def delete_floatingip(self, context, current):
        if current['port_id']:
            self._notify_port_update_for_fip(context, current['port_id'])

    # Topology RPC method handler
    def update_link(self, context, host, interface, mac,
                    switch, module, port, pod_id='1', port_description=''):
        LOG.debug('Topology RPC: update_link: %s',
                  ', '.join([str(p) for p in
                             (host, interface, mac, switch, module, port,
                              pod_id, port_description)]))
        if not switch:
            return
        with db_api.CONTEXT_WRITER.using(context) as session:
            aim_ctx = aim_context.AimContext(db_session=session)
            hlink = self.aim.get(aim_ctx,
                                 aim_infra.HostLink(host_name=host,
                                                    interface_name=interface))
            if hlink and hlink.path == port_description:
                # There was neither a change nor a refresh required.
                return
            # Create or Update hostlink in AIM
            attrs = dict(interface_mac=mac,
                         switch_id=switch, module=module, port=port,
                         path=port_description, pod_id=pod_id)
            if hlink:
                self.aim.update(aim_ctx, hlink, **attrs)
            else:
                hlink = aim_infra.HostLink(host_name=host,
                                           interface_name=interface,
                                           **attrs)
                self.aim.create(aim_ctx, hlink, overwrite=True)
        self._update_network_links(context, host)

    # Topology RPC method handler
    def delete_link(self, context, host, interface, mac, switch, module, port):
        LOG.debug('Topology RPC: delete_link: %s',
                  ', '.join([str(p) for p in
                             (host, interface, mac, switch, module, port)]))
        with db_api.CONTEXT_WRITER.using(context) as session:
            aim_ctx = aim_context.AimContext(db_session=session)
            hlink = self.aim.get(aim_ctx,
                                 aim_infra.HostLink(host_name=host,
                                                    interface_name=interface))
            if not hlink:
                # Host link didn't exist to begin with, nothing to do here.
                return

            self.aim.delete(aim_ctx, hlink)
        self._update_network_links(context, host)

    def _update_network_links(self, context, host):
        # Update static paths of all EPGs with ports on the host.
        # For correctness, rebuild tha static paths for the entire host
        # instead of the specific interface. We could do it in a
        # per-interface basis once we can correlate existing paths to
        # the (host, interface) hence avoiding leaking entries. Although
        # this is all good in theory, it would require some extra design
        # due to the fact that VPC interfaces have the same path but
        # two different ifaces assigned to them.
        with db_api.CONTEXT_WRITER.using(context) as session:
            aim_ctx = aim_context.AimContext(db_session=session)
            hlinks = self.aim.find(aim_ctx, aim_infra.HostLink, host_name=host)
            nets_segs = self._get_non_opflex_segments_on_host(context, host)
            registry.publish(aim_cst.GBP_NETWORK_LINK, events.PRECOMMIT_UPDATE,
                             self,
                             payload=events.DBEventPayload(
                                 context,
                                 metadata={
                                     'networks_map': nets_segs,
                                     'host_links': hlinks, 'host': host}))
        for network, segment in nets_segs:
            self._rebuild_host_path_for_network(
                context, network, segment, host, hlinks)

    def _agent_bind_port(self, context, agent_type, bind_strategy):
        current = context.current
        for agent in context.host_agents(agent_type) or []:
            LOG.debug("Checking agent: %s", agent)
            if agent['alive']:
                for segment in context.segments_to_bind:
                    if bind_strategy(context, segment, agent):
                        LOG.debug("Bound using segment: %s", segment)
                        return True
            else:
                LOG.warning("Refusing to bind port %(port)s to dead "
                            "agent: %(agent)s",
                            {'port': current['id'], 'agent': agent})

    def _opflex_bind_port(self, context, segment, agent):
        network_type = segment[api.NETWORK_TYPE]
        if self._is_opflex_type(network_type):
            opflex_mappings = agent['configurations'].get('opflex_networks')
            LOG.debug("Checking segment: %(segment)s "
                      "for physical network: %(mappings)s ",
                      {'segment': segment, 'mappings': opflex_mappings})
            if (opflex_mappings is not None and
                segment[api.PHYSICAL_NETWORK] not in opflex_mappings):
                return False
        elif network_type == n_constants.TYPE_VLAN:
            vlan_mappings = agent['configurations'].get('vlan_networks')
            LOG.debug("Checking segment: %(segment)s "
                      "for physical network: %(mappings)s ",
                      {'segment': segment, 'mappings': vlan_mappings})
            if (vlan_mappings is not None and
                segment[api.PHYSICAL_NETWORK] not in vlan_mappings):
                return False
        elif network_type != 'local':
            return False
        context.set_binding(
            segment[api.ID], self._opflex_get_vif_type(agent),
            self._opflex_get_vif_details(context, agent))
        return True

    def _dvs_bind_port(self, context, segment, agent):
        """Populate VIF type and details for DVS VIFs.

           For DVS VIFs, provide the portgroup along
           with the security groups setting. Note that
           DVS port binding always returns true. This
           is because it should only be called when the
           host ID matches the agent's host ID, where
           host ID is not an actual host, but a psuedo-
           host that only exists to match the host ID
           for the related DVS agent (i.e. for port-
           binding).
        """
        # Use default security groups from MD
        aim_ctx = aim_context.AimContext(
            db_session=context._plugin_context.session)
        session = aim_ctx.db_session
        port = context.current
        if self.gbp_driver:
            epg = self.gbp_driver._get_port_epg(context._plugin_context, port)
        else:
            mapping = self._get_network_mapping(session, port['network_id'])
            epg = self._get_network_epg(mapping)
        vif_details = {'dvs_port_group_name': ('%s|%s|%s' %
                                               (epg.tenant_name,
                                                epg.app_profile_name,
                                                epg.name)),
                       portbindings.CAP_PORT_FILTER: self.sg_enabled}
        currentcopy = copy.copy(context.current)
        currentcopy['portgroup_name'] = (
            vif_details['dvs_port_group_name'])
        booked_port_info = None
        if self.dvs_notifier:
            booked_port_info = self.dvs_notifier.bind_port_call(
                currentcopy,
                [context.bottom_bound_segment],
                context.network.current,
                context.host
            )
        if booked_port_info:
            vif_details['dvs_port_key'] = booked_port_info['key']

        context.set_binding(segment[api.ID],
                            VIF_TYPE_DVS, vif_details)
        return True

    def _is_baremetal_vnic_type(self, port):
        return port.get(portbindings.VNIC_TYPE) == portbindings.VNIC_BAREMETAL

    def _bind_baremetal_vnic(self, context):
        """Bind ports with VNIC type of baremtal.

        :param context : Port context instance

        Support binding baremetal VNIC types to networks that have
        an opflex type static segment, or networks with vlan type segments.
        For vlan type segments, these can be static segments or dynamically
        created segments from HPB.  Topology information is contained in the
        binding:profile property of the port. The topology information
        for the port should contain the physical_network that the baremetal
        VNIC is connected to. This is used to match the segment when binding
        the port to vlan type segments, and as the physical_network when
        allocating vlan type segments for HPB. Trunk parent ports are handled
        like normal baremetal VNICs, using any available VLAN segmentation_id,
        while trunk subports must use the VLAN segmentation_id determined by
        the trunk service.
        """
        _, _, baremetal_physnet, _ = self._get_baremetal_topology(
            context._plugin_context, context.current)
        if not baremetal_physnet:
            return False
        # The requested_vlan_id will be None if this isn't a trunk subport.
        requested_vlan_id = self._get_subport_segmentation_id(context)

        # First attempt binding to any existing vlan type segments on the
        # baremetal physical_network. These could be static segments, or
        # segments dynamically allocated by the loop below.
        for seg in context.segments_to_bind:
            net_type = seg[api.NETWORK_TYPE]
            if (seg[api.PHYSICAL_NETWORK] == baremetal_physnet and
                    net_type == n_constants.TYPE_VLAN):
                # If the port is a trunk subport and the segmentation_id
                # doesn't match, skip this segment, but don't fail the
                # binding - we will still try to bind it using HPB in
                # the second pass below.
                if (requested_vlan_id and
                        requested_vlan_id != seg[api.SEGMENTATION_ID]):
                    continue
                context.set_binding(seg[api.ID],
                    portbindings.VIF_TYPE_OTHER, {},
                    status=n_constants.PORT_STATUS_ACTIVE)
                return True
        # Attempt dynamically allocating a VLAN type segment on the baermetal
        # physical_network, if there is a supported segment type (TYPE_VLAN or
        # TYPE_OPFLEX). If the port is a subport of a trunk, we have the
        # segmentation_id, so we add it to the request when dynamically
        # allocating the segment. If it is not a subport, then any available
        # segmentation_id will do.
        for seg in context.segments_to_bind:
            net_type = seg[api.NETWORK_TYPE]
            if net_type in SUPPORTED_HPB_SEGMENT_TYPES:
                seg_args = {api.NETWORK_TYPE: n_constants.TYPE_VLAN,
                            api.PHYSICAL_NETWORK: baremetal_physnet}
                # If the port is a trunk subport, then we need to use
                # the segmentation_id from the subport.
                if requested_vlan_id:
                    seg_args.update({api.SEGMENTATION_ID: requested_vlan_id})
                try:
                    dyn_seg = context.allocate_dynamic_segment(seg_args)
                    LOG.info('Allocated dynamic-segment %(s)s for port %(p)s',
                             {'s': dyn_seg, 'p': context.current['id']})
                    context.continue_binding(seg[api.ID], [dyn_seg])
                    return True
                except n_exceptions.VlanIdInUse:
                    query = BAKERY(lambda s: s.query(
                        segments_model.NetworkSegment))
                    query += lambda q: q.filter(
                        segments_model.NetworkSegment.physical_network ==
                        sa.bindparam('physical_network'))
                    query += lambda q: q.filter(
                        segments_model.NetworkSegment.segmentation_id ==
                        sa.bindparam('vlan_id'))
                    result = query(context._plugin_context.session).params(
                        physical_network=baremetal_physnet,
                        vlan_id=requested_vlan_id).one()
                    net_id = result['network_id']
                    LOG.info('Cannot bind trunk subport port %(port_id)s '
                             'on network %(net_id)s to baremetal '
                             'physical_network %(phys)s because requested '
                             'type %(type)s segmentation_id %(id)s is already'
                             ' allocated to network %(other_net)s',
                             {'port_id': context.current['id'],
                              'net_id': seg[api.NETWORK_ID],
                              'phys': baremetal_physnet,
                              'type': seg[api.NETWORK_TYPE],
                              'id': seg[api.SEGMENTATION_ID],
                              'other_net': net_id})
                    return False
                except n_exceptions.NoNetworkAvailable:
                    LOG.info('Cannot bind port %(port_id)s on network '
                             '%(net_id)s to baremetal physical_network '
                             '%(phys)s because it has no available %(type)s '
                             'segmentaton_ids',
                             {'port_id': context.current['id'],
                              'net_id': seg[api.NETWORK_ID],
                              'phys': baremetal_physnet,
                              'type': net_type})
                    return False
        return False

    def _bind_physical_node(self, context):
        # Bind physical nodes hierarchically by creating a dynamic segment.
        for segment in context.segments_to_bind:
            net_type = segment[api.NETWORK_TYPE]
            if self._is_opflex_type(net_type):
                # TODO(amitbose) Consider providing configuration options
                # for picking network-type and physical-network name
                # for the dynamic segment
                seg_args = {api.NETWORK_TYPE: n_constants.TYPE_VLAN,
                            api.PHYSICAL_NETWORK:
                            segment[api.PHYSICAL_NETWORK]}
                dyn_seg = context.allocate_dynamic_segment(seg_args)
                LOG.info('Allocated dynamic-segment %(s)s for port %(p)s',
                         {'s': dyn_seg, 'p': context.current['id']})
                dyn_seg['aim_ml2_created'] = True
                context.continue_binding(segment[api.ID], [dyn_seg])
                return True
            elif segment.get('aim_ml2_created'):
                # Complete binding if another driver did not bind the
                # dynamic segment that we created.
                context.set_binding(segment[api.ID], portbindings.VIF_TYPE_OVS,
                    self._update_binding_sg())
                return True

    def _opflex_get_vif_type(self, agent):
        if agent['agent_type'] == ofcst.AGENT_TYPE_OPFLEX_VPP:
            return portbindings.VIF_TYPE_VHOST_USER
        else:
            if (agent['configurations'].get('datapath_type') ==
            a_const.OVS_DATAPATH_NETDEV):
                return portbindings.VIF_TYPE_VHOST_USER
            else:
                return portbindings.VIF_TYPE_OVS

    @staticmethod
    def _agent_vhu_sockpath(agent, port_id):
        """Return the agent's vhost-user socket path for a given port"""
        sockdir = agent['configurations'].get('vhostuser_socket_dir',
                                              a_const.VHOST_USER_SOCKET_DIR)
        sock_name = (n_constants.VHOST_USER_DEVICE_PREFIX +
                     port_id)[:ApicMechanismDriver.NIC_NAME_LEN]
        return os.path.join(sockdir, sock_name)

    def _get_vhost_mode(self):
        # REVISIT(kshastri):  this function converts the ovs vhost user
        # driver mode into the qemu vhost user mode. If OVS is the server,
        # qemu is the client and vice-versa. For ACI MD, we will need to
        # support agent capabilities field to choose client-mode. As of
        # now only support server mode for nova.
        return portbindings.VHOST_USER_MODE_SERVER

    def _opflex_get_vif_details(self, context, agent):
        vif_type = self._opflex_get_vif_type(agent)
        details = {}
        if vif_type == portbindings.VIF_TYPE_VHOST_USER:
            sock_path = self._agent_vhu_sockpath(agent,
                                                context.current['id'])
            mode = self._get_vhost_mode()
            details = {portbindings.VHOST_USER_MODE: mode,
                       portbindings.VHOST_USER_SOCKET: sock_path}
            if agent['agent_type'] == ofcst.AGENT_TYPE_OPFLEX_VPP:
                details.update({portbindings.CAP_PORT_FILTER: False,
                                portbindings.OVS_HYBRID_PLUG: False,
                                portbindings.VHOST_USER_OVS_PLUG: False,
                                ofcst.VHOST_USER_VPP_PLUG: True})
            else:
                details.update({portbindings.OVS_DATAPATH_TYPE:
                                a_const.OVS_DATAPATH_NETDEV,
                                portbindings.VHOST_USER_OVS_PLUG: True})

        if agent['agent_type'] == ofcst.AGENT_TYPE_OPFLEX_OVS:
            details.update(self._update_binding_sg())
        return details

    def _update_binding_sg(self):
        enable_firewall = False
        if self.enable_iptables_firewall:
            enable_firewall = self.sg_enabled
        return {portbindings.CAP_PORT_FILTER: enable_firewall,
                portbindings.OVS_HYBRID_PLUG: enable_firewall}

    @property
    def plugin(self):
        if not self._core_plugin:
            self._core_plugin = directory.get_plugin()
        return self._core_plugin

    @property
    def l3_plugin(self):
        if not self._l3_plugin:
            self._l3_plugin = directory.get_plugin(pconst.L3)
        return self._l3_plugin

    @property
    def trunk_plugin(self):
        if not self._trunk_plugin:
            self._trunk_plugin = directory.get_plugin('trunk')
        return self._trunk_plugin

    @property
    def dvs_notifier(self):
        if not self._dvs_notifier:
            self._dvs_notifier = importutils.import_object(
                DVS_AGENT_KLASS,
                n_context.get_admin_context_without_session()
            )
        return self._dvs_notifier

    @property
    def gbp_plugin(self):
        if not self._gbp_plugin:
            self._gbp_plugin = directory.get_plugin("GROUP_POLICY")
        return self._gbp_plugin

    @property
    def gbp_driver(self):
        if not self._gbp_driver and self.gbp_plugin:
            self._gbp_driver = (self.gbp_plugin.policy_driver_manager.
                                policy_drivers['aim_mapping'].obj)
        return self._gbp_driver

    def _merge_status(self, aim_ctx, sync_state, resource, status=None):
        status = status or self.aim.get_status(aim_ctx, resource,
                                               create_if_absent=False)
        if not status:
            # REVISIT(rkukura): This should only occur if the AIM
            # resource has not yet been created when
            # extend_<resource>_dict() runs at the begining of a
            # create operation. In this case, the real sync_state
            # value will be generated, either in
            # create_<resource>_precommit() or in a 2nd call to
            # extend_<resource>_dict() after the precommit phase,
            # depending on the resource. It might be safer to force
            # sync_state to a SYNC_MISSING value here that is not
            # overwritten on subsequent calls to _merge_status(), in
            # case the real sync_state value somehow does not get
            # generated. But sync_state handling in general needs to
            # be revisited (and properly tested), so we can deal with
            # this at that time.
            return sync_state
        if status.is_error():
            sync_state = cisco_apic.SYNC_ERROR
        elif status.is_build() and sync_state is not cisco_apic.SYNC_ERROR:
            sync_state = cisco_apic.SYNC_BUILD
        return (cisco_apic.SYNC_SYNCED
                if sync_state is cisco_apic.SYNC_NOT_APPLICABLE
                else sync_state)

    def _get_vrfs_for_router(self, session, router_id):
        # REVISIT: Persist router/VRF relationship?

        # Find the unique VRFs for the scoped interfaces, accounting
        # for isomorphic scopes.
        vrfs = {}

        query = BAKERY(lambda s: s.query(
            as_db.AddressScope))
        query += lambda q: q.join(
            models_v2.SubnetPool,
            models_v2.SubnetPool.address_scope_id == as_db.AddressScope.id)
        query += lambda q: q.join(
            models_v2.Subnet,
            models_v2.Subnet.subnetpool_id == models_v2.SubnetPool.id)
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.subnet_id == models_v2.Subnet.id)
        query += lambda q: q.join(
            l3_db.RouterPort,
            l3_db.RouterPort.port_id == models_v2.IPAllocation.port_id)
        query += lambda q: q.filter(
            l3_db.RouterPort.router_id == sa.bindparam('router_id'))
        query += lambda q: q.filter(
            l3_db.RouterPort.port_type == n_constants.DEVICE_OWNER_ROUTER_INTF)
        query += lambda q: q.distinct()
        scope_dbs = query(session).params(
            router_id=router_id)

        for scope_db in scope_dbs:
            vrf = self._get_address_scope_vrf(scope_db.aim_mapping)
            vrfs[tuple(vrf.identity)] = vrf

        # Find VRF for first unscoped interface.
        query = BAKERY(lambda s: s.query(
            models_v2.Network))
        query += lambda q: q.join(
            models_v2.Subnet,
            models_v2.Subnet.network_id == models_v2.Network.id)
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.subnet_id == models_v2.Subnet.id)
        query += lambda q: q.outerjoin(
            models_v2.SubnetPool,
            models_v2.SubnetPool.id == models_v2.Subnet.subnetpool_id)
        query += lambda q: q.join(
            l3_db.RouterPort,
            l3_db.RouterPort.port_id == models_v2.IPAllocation.port_id)
        query += lambda q: q.filter(
            l3_db.RouterPort.router_id == sa.bindparam('router_id'),
            l3_db.RouterPort.port_type == n_constants.DEVICE_OWNER_ROUTER_INTF)
        query += lambda q: q.filter(
            sa.or_(models_v2.Subnet.subnetpool_id.is_(None),
                   models_v2.SubnetPool.address_scope_id.is_(None)))
        query += lambda q: q.limit(1)
        network_db = query(session).params(
            router_id=router_id).first()

        if network_db:
            vrf = self._get_network_vrf(network_db.aim_mapping)
            vrfs[tuple(vrf.identity)] = vrf

        return list(vrfs.values())

    # Used by policy driver.
    def _get_address_scope_ids_for_vrf(self, session, vrf):
        mappings = self._get_address_scope_mappings_for_vrf(session, vrf)
        return [mapping.scope_id for mapping in mappings]

    def _get_network_ids_for_vrf(self, session, vrf):
        mappings = self._get_network_mappings_for_vrf(session, vrf)
        return [mapping.network_id for mapping in mappings]

    def _get_routers_for_vrf(self, session, vrf):
        # REVISIT: Persist router/VRF relationship?

        scope_ids = self._get_address_scope_ids_for_vrf(session, vrf)
        if scope_ids:
            query = BAKERY(lambda s: s.query(
                l3_db.Router))
            query += lambda q: q.join(
                l3_db.RouterPort,
                l3_db.RouterPort.router_id == l3_db.Router.id)
            query += lambda q: q.join(
                models_v2.IPAllocation,
                models_v2.IPAllocation.port_id == l3_db.RouterPort.port_id)
            query += lambda q: q.join(
                models_v2.Subnet,
                models_v2.Subnet.id == models_v2.IPAllocation.subnet_id)
            query += lambda q: q.join(
                models_v2.SubnetPool,
                models_v2.SubnetPool.id == models_v2.Subnet.subnetpool_id)
            query += lambda q: q.filter(
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            query += lambda q: q.filter(
                models_v2.SubnetPool.address_scope_id.in_(
                    sa.bindparam('scope_ids', expanding=True)))
            query += lambda q: q.distinct()
            rtr_dbs = query(session).params(
                scope_ids=scope_ids)
        else:
            net_ids = self._get_network_ids_for_vrf(session, vrf)
            if not net_ids:
                return []

            query = BAKERY(lambda s: s.query(
                l3_db.Router))
            query += lambda q: q.join(
                l3_db.RouterPort,
                l3_db.RouterPort.router_id == l3_db.Router.id)
            query += lambda q: q.join(
                models_v2.Port,
                models_v2.Port.id == l3_db.RouterPort.port_id)
            query += lambda q: q.filter(
                models_v2.Port.network_id.in_(
                    sa.bindparam('net_ids', expanding=True)),
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            query += lambda q: q.distinct()
            rtr_dbs = query(session).params(
                net_ids=net_ids)
        return rtr_dbs

    def _associate_network_with_vrf(self, ctx, aim_ctx, network_db, new_vrf,
                                    nets_to_notify, scope_id):
        LOG.debug("Associating previously unrouted network %(net_id)s named "
                  "'%(net_name)s' in project %(net_tenant)s with VRF %(vrf)s",
                  {'net_id': network_db.id, 'net_name': network_db.name,
                   'net_tenant': network_db.tenant_id, 'vrf': new_vrf})

        # NOTE: Must only be called for networks that are not yet
        # attached to any router.

        if not self._is_svi_db(network_db):
            bd = self._get_network_bd(network_db.aim_mapping)
            epg = self._get_network_epg(network_db.aim_mapping)
            tenant_name = bd.tenant_name
        else:
            l3out = self._get_network_l3out(network_db.aim_mapping)
            tenant_name = l3out.tenant_name

        if (new_vrf.tenant_name != COMMON_TENANT_NAME and
            tenant_name != new_vrf.tenant_name):
            # Move BD and EPG to new VRF's Tenant, set VRF, and make
            # sure routing is enabled.
            LOG.debug("Moving network from tenant %(old)s to tenant %(new)s",
                      {'old': tenant_name, 'new': new_vrf.tenant_name})
            if not self._is_svi_db(network_db):
                bd = self.aim.get(aim_ctx, bd)
                self.aim.delete(aim_ctx, bd)
                bd.tenant_name = new_vrf.tenant_name
                bd.enable_routing = True
                bd.vrf_name = new_vrf.name
                bd = self.aim.create(aim_ctx, bd)
                self._set_network_bd(network_db.aim_mapping, bd)
                epg = self.aim.get(aim_ctx, epg)
                self.aim.delete(aim_ctx, epg)
                # ensure app profile exists in destination tenant
                ap = aim_resource.ApplicationProfile(
                    tenant_name=new_vrf.tenant_name, name=self.ap_name)
                if not self.aim.get(aim_ctx, ap):
                    self.aim.create(aim_ctx, ap)
                epg.tenant_name = new_vrf.tenant_name
                epg = self.aim.create(aim_ctx, epg)
                self._set_network_epg_and_notify(ctx, network_db.aim_mapping,
                                                 epg)
            else:
                old_l3out = self.aim.get(aim_ctx, l3out)
                l3out = copy.copy(old_l3out)
                l3out.tenant_name = new_vrf.tenant_name
                l3out.vrf_name = new_vrf.name
                l3out = self.aim.create(aim_ctx, l3out)
                self._set_network_l3out(network_db.aim_mapping,
                                        l3out)
                for old_child in self.aim.get_subtree(aim_ctx, old_l3out):
                    new_child = copy.copy(old_child)
                    new_child.tenant_name = new_vrf.tenant_name
                    new_child = self.aim.create(aim_ctx, new_child)
                    self.aim.delete(aim_ctx, old_child)
                self.aim.delete(aim_ctx, old_l3out)
        else:
            # Just set VRF and enable routing.
            if not self._is_svi_db(network_db):
                bd = self.aim.update(aim_ctx, bd, enable_routing=True,
                                     vrf_name=new_vrf.name)
            else:
                l3out = self.aim.update(aim_ctx, l3out,
                                        vrf_name=new_vrf.name)

        self._set_network_vrf_and_notify(ctx, network_db.aim_mapping, new_vrf)

        # All non-router ports on this network need to be notified
        # since their BD's VRF and possibly their BD's and EPG's
        # Tenants have changed.
        nets_to_notify.add(network_db.id)

        # Notify VRFs not associated with address_scopes that the
        # subnet CIDRs within them have changed.
        old_vrf = self._get_network_vrf(network_db.aim_mapping)
        if self._is_default_vrf(new_vrf):
            self._add_postcommit_vrf_notification(ctx, new_vrf)
        self._add_postcommit_vrf_notification(ctx, old_vrf)

        if not self._is_svi_db(network_db):
            return bd, epg
        else:
            ext_net = self._get_network_l3out_ext_net(network_db.aim_mapping)
            return l3out, ext_net

    def _dissassociate_network_from_vrf(self, ctx, aim_ctx, network_db,
                                        old_vrf, nets_to_notify, scope_id):
        LOG.debug("Dissassociating network %(net_id)s named '%(net_name)s' in "
                  "project %(net_tenant)s from VRF %(vrf)s",
                  {'net_id': network_db.id, 'net_name': network_db.name,
                   'net_tenant': network_db.tenant_id, 'vrf': old_vrf})

        session = aim_ctx.db_session

        if not self._is_svi_db(network_db):
            new_vrf = self._map_unrouted_vrf()
        else:
            new_vrf = self._map_default_vrf(session, network_db)
        new_tenant_name = self.name_mapper.project(
            session, network_db.tenant_id)

        # REVISIT(rkukura): Share code with _associate_network_with_vrf?
        if (old_vrf.tenant_name != COMMON_TENANT_NAME and
            old_vrf.tenant_name != new_tenant_name):
            # Move BD and EPG to network's Tenant, set unrouted VRF,
            # and disable routing.
            LOG.debug("Moving network from tenant %(old)s to tenant %(new)s",
                      {'old': old_vrf.tenant_name, 'new': new_tenant_name})

            if not self._is_svi_db(network_db):
                bd = self._get_network_bd(network_db.aim_mapping)
                bd = self.aim.get(aim_ctx, bd)
                self.aim.delete(aim_ctx, bd)
                bd.tenant_name = new_tenant_name
                bd.enable_routing = False
                bd.vrf_name = new_vrf.name
                bd = self.aim.create(aim_ctx, bd)
                self._set_network_bd(network_db.aim_mapping, bd)
                epg = self._get_network_epg(network_db.aim_mapping)
                epg = self.aim.get(aim_ctx, epg)
                self.aim.delete(aim_ctx, epg)
                epg.tenant_name = new_tenant_name
                epg = self.aim.create(aim_ctx, epg)
                self._set_network_epg_and_notify(ctx, network_db.aim_mapping,
                                                 epg)
            else:
                l3out = self._get_network_l3out(network_db.aim_mapping)
                old_l3out = self.aim.get(aim_ctx, l3out)
                l3out = copy.copy(old_l3out)
                l3out.tenant_name = new_tenant_name
                l3out.vrf_name = new_vrf.name
                l3out = self.aim.create(aim_ctx, l3out)
                self._set_network_l3out(network_db.aim_mapping,
                                        l3out)
                for old_child in self.aim.get_subtree(aim_ctx, old_l3out):
                    new_child = copy.copy(old_child)
                    new_child.tenant_name = new_tenant_name
                    new_child = self.aim.create(aim_ctx, new_child)
                    self.aim.delete(aim_ctx, old_child)
                self.aim.delete(aim_ctx, old_l3out)
        else:
            # Just set unrouted VRF and disable routing.
            if not self._is_svi_db(network_db):
                bd = self._get_network_bd(network_db.aim_mapping)
                bd = self.aim.update(aim_ctx, bd, enable_routing=False,
                                     vrf_name=new_vrf.name)
            else:
                l3out = self._get_network_l3out(network_db.aim_mapping)
                l3out = self.aim.update(aim_ctx, l3out,
                                        vrf_name=new_vrf.name)

        self._set_network_vrf_and_notify(ctx, network_db.aim_mapping, new_vrf)

        # All non-router ports on this network need to be notified
        # since their BD's VRF and possibly their BD's and EPG's
        # Tenants have changed.
        nets_to_notify.add(network_db.id)

        # Notify VRFs not associated with address_scopes that the
        # subnet CIDRs within them have changed
        if self._is_default_vrf(old_vrf):
            self._add_postcommit_vrf_notification(ctx, old_vrf)
        self._add_postcommit_vrf_notification(ctx, new_vrf)

    def _move_topology(self, ctx, aim_ctx, topology, old_vrf, new_vrf,
                       nets_to_notify):
        LOG.info("Moving routed networks %(topology)s from VRF "
                 "%(old_vrf)s to VRF %(new_vrf)s",
                 {'topology': list(topology.keys()),
                  'old_vrf': old_vrf,
                  'new_vrf': new_vrf})

        # TODO(rkukura): Validate that nothing in new_vrf overlaps
        # with topology.

        for network_db in list(topology.values()):
            if old_vrf.tenant_name != new_vrf.tenant_name:
                # New VRF is in different Tenant, so move BD, EPG, and
                # all Subnets to new VRF's Tenant and set BD's VRF.
                LOG.debug("Moving network %(net)s from tenant %(old)s to "
                          "tenant %(new)s",
                          {'net': network_db.id,
                           'old': old_vrf.tenant_name,
                           'new': new_vrf.tenant_name})
                if network_db.aim_mapping.epg_name:
                    bd = self._get_network_bd(network_db.aim_mapping)
                    old_bd = self.aim.get(aim_ctx, bd)
                    new_bd = copy.copy(old_bd)
                    new_bd.tenant_name = new_vrf.tenant_name
                    new_bd.vrf_name = new_vrf.name
                    bd = self.aim.create(aim_ctx, new_bd)
                    self._set_network_bd(network_db.aim_mapping, bd)
                    for subnet in self.aim.find(
                            aim_ctx, aim_resource.Subnet,
                            tenant_name=old_bd.tenant_name,
                            bd_name=old_bd.name):
                        self.aim.delete(aim_ctx, subnet)
                        subnet.tenant_name = bd.tenant_name
                        subnet = self.aim.create(aim_ctx, subnet)
                    self.aim.delete(aim_ctx, old_bd)

                    epg = self._get_network_epg(network_db.aim_mapping)
                    epg = self.aim.get(aim_ctx, epg)
                    self.aim.delete(aim_ctx, epg)
                    epg.tenant_name = new_vrf.tenant_name
                    epg = self.aim.create(aim_ctx, epg)
                    self._set_network_epg_and_notify(ctx,
                                                     network_db.aim_mapping,
                                                     epg)
                # SVI network with auto l3out
                elif network_db.aim_mapping.l3out_name:
                    l3out = self._get_network_l3out(network_db.aim_mapping)
                    old_l3out = self.aim.get(aim_ctx, l3out)
                    l3out = copy.copy(old_l3out)
                    l3out.tenant_name = new_vrf.tenant_name
                    l3out.vrf_name = new_vrf.name
                    l3out = self.aim.create(aim_ctx, l3out)
                    self._set_network_l3out(network_db.aim_mapping,
                                            l3out)
                    for old_child in self.aim.get_subtree(aim_ctx, old_l3out):
                        new_child = copy.copy(old_child)
                        new_child.tenant_name = new_vrf.tenant_name
                        new_child = self.aim.create(aim_ctx, new_child)
                        self.aim.delete(aim_ctx, old_child)
                    self.aim.delete(aim_ctx, old_l3out)
            else:
                if network_db.aim_mapping.epg_name:
                    # New VRF is in same Tenant, so just set BD's VRF.
                    bd = self._get_network_bd(network_db.aim_mapping)
                    bd = self.aim.update(aim_ctx, bd, vrf_name=new_vrf.name)
                elif network_db.aim_mapping.l3out_name:
                    # New VRF is in same Tenant, so just set l3out's VRF.
                    l3out = self._get_network_l3out(network_db.aim_mapping)
                    l3out = self.aim.update(aim_ctx, l3out,
                                            vrf_name=new_vrf.name)

            self._set_network_vrf_and_notify(ctx, network_db.aim_mapping,
                                             new_vrf)

        # All non-router ports on all networks in topology need to be
        # notified since their BDs' VRFs and possibly their BDs' and
        # EPGs' Tenants have changed.
        nets_to_notify.update(list(topology.keys()))

        self._add_postcommit_vrf_notification(ctx, old_vrf)
        self._add_postcommit_vrf_notification(ctx, new_vrf)

    def _router_topology(self, session, router_id):
        LOG.debug("Getting topology for router %s", router_id)
        visited_networks = {}
        visited_router_ids = set()
        self._expand_topology_for_routers(
            session, visited_networks, visited_router_ids, [router_id])
        LOG.debug("Returning router topology %s", visited_networks)
        return visited_networks

    def _network_topology(self, session, network_db):
        LOG.debug("Getting topology for network %s", network_db.id)
        visited_networks = {}
        visited_router_ids = set()
        self._expand_topology_for_networks(
            session, visited_networks, visited_router_ids, [network_db])
        LOG.debug("Returning network topology %s", visited_networks)
        return visited_networks

    def _expand_topology_for_routers(self, session, visited_networks,
                                     visited_router_ids, new_router_ids):
        LOG.debug("Adding routers %s to topology", new_router_ids)
        added_ids = set(new_router_ids) - visited_router_ids
        if added_ids:
            visited_router_ids |= added_ids
            LOG.debug("Querying for networks interfaced to routers %s",
                      added_ids)

            query = BAKERY(lambda s: s.query(
                models_v2.Network,
                models_v2.Subnet))
            query += lambda q: q.join(
                models_v2.Subnet,
                models_v2.Subnet.network_id == models_v2.Network.id)
            query += lambda q: q.join(
                models_v2.IPAllocation,
                models_v2.IPAllocation.subnet_id == models_v2.Subnet.id)
            query += lambda q: q.join(
                l3_db.RouterPort,
                l3_db.RouterPort.port_id == models_v2.IPAllocation.port_id)
            query += lambda q: q.filter(
                l3_db.RouterPort.router_id.in_(
                    sa.bindparam('added_ids', expanding=True)))
            if visited_networks:
                query += lambda q: q.filter(
                    ~models_v2.Network.id.in_(
                        sa.bindparam('visited_networks', expanding=True)))
            query += lambda q: q.filter(
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            query += lambda q: q.distinct()
            results = query(session).params(
                added_ids=list(added_ids),
                visited_networks=list(visited_networks.keys())).all()

            self._expand_topology_for_networks(
                session, visited_networks, visited_router_ids,
                [network for network, subnet in results if not
                 (subnet.subnetpool and subnet.subnetpool.address_scope_id)])

    def _expand_topology_for_networks(self, session, visited_networks,
                                      visited_router_ids, new_networks):
        LOG.debug("Adding networks %s to topology",
                  [new_net.id for new_net in new_networks])
        added_ids = []
        for new_net in new_networks:
            if new_net.id not in visited_networks:
                visited_networks[new_net.id] = new_net
                added_ids.append(new_net.id)
        if added_ids:
            LOG.debug("Querying for routers interfaced to networks %s",
                      added_ids)

            query = BAKERY(lambda s: s.query(
                l3_db.RouterPort.router_id))
            query += lambda q: q.join(
                models_v2.Port,
                models_v2.Port.id == l3_db.RouterPort.port_id)
            query += lambda q: q.filter(
                models_v2.Port.network_id.in_(
                    sa.bindparam('added_ids', expanding=True)))
            if visited_router_ids:
                query += lambda q: q.filter(
                    ~l3_db.RouterPort.router_id.in_(
                        sa.bindparam('visited_router_ids', expanding=True)))
            query += lambda q: q.filter(
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            query += lambda q: q.distinct()
            results = query(session).params(
                added_ids=list(added_ids),
                visited_router_ids=list(visited_router_ids)).all()

            self._expand_topology_for_routers(
                session, visited_networks, visited_router_ids,
                [result[0] for result in results])

    def _topology_shared(self, topology):
        for network_db in list(topology.values()):
            if self._network_shared(network_db):
                return network_db

    def _network_shared(self, network_db):
        for entry in network_db.rbac_entries:
            # Access is enforced by Neutron itself, and we only
            # care whether or not the network is shared, so we
            # ignore the entry's target_tenant.
            if entry.action == rbac_db_models.ACCESS_SHARED:
                return True

    def _ip_for_subnet(self, subnet, fixed_ips):
        subnet_id = subnet['id']
        for fixed_ip in fixed_ips:
            if fixed_ip['subnet_id'] == subnet_id:
                return fixed_ip['ip_address']

    def _subnet_router_ips(self, session, subnet_id):
        query = BAKERY(lambda s: s.query(
            models_v2.IPAllocation.ip_address,
            l3_db.RouterPort.router_id))
        query += lambda q: q.join(
            l3_db.RouterPort,
            l3_db.RouterPort.port_id == models_v2.IPAllocation.port_id)
        query += lambda q: q.filter(
            models_v2.IPAllocation.subnet_id == sa.bindparam('subnet_id'),
            l3_db.RouterPort.port_type == n_constants.DEVICE_OWNER_ROUTER_INTF)
        return query(session).params(
            subnet_id=subnet_id)

    def _scope_by_id(self, session, scope_id):
        query = BAKERY(lambda s: s.query(
            as_db.AddressScope))
        query += lambda q: q.filter_by(
            id=sa.bindparam('scope_id'))
        return query(session).params(
            scope_id=scope_id).one_or_none()

    def _map_port(self, session, port):
        # REVISIT: GBP workflow isn't supported in this release. If
        #          we do add support for GBP, getting the EPG requires
        #          determining which workflow was used, and get the
        #          EPG accordingly.
        mapping = self._get_network_mapping(session, port['network_id'])
        # If it's not a network we support (e.g. SVI), we can't look
        # up the CEP
        if not mapping or not mapping.epg_name:
            return None
        epg = self._get_network_epg(mapping)
        # The ERSPAN source ocnfiguration requires the DN
        # for the EP, so we construct it.
        return ('uni/tn-' + epg.tenant_name +
                '/ap-OpenStack/epg-' + epg.name +
                '/cep-' + port['mac_address'].upper())

    def _map_network(self, session, network, vrf=None, preexisting_bd_dn=None):
        tenant_aname = (vrf.tenant_name if vrf and vrf.tenant_name != 'common'
                        else self.name_mapper.project(
                                session, network['tenant_id']))
        id = network['id']
        aname = self.name_mapper.network(session, id)

        if preexisting_bd_dn:
            bd = aim_resource.BridgeDomain.from_dn(preexisting_bd_dn)
        else:
            bd = aim_resource.BridgeDomain(tenant_name=tenant_aname,
                                           name=aname)
        epg = aim_resource.EndpointGroup(tenant_name=tenant_aname,
                                         app_profile_name=self.ap_name,
                                         name=aname)
        return bd, epg

    def _map_subnet(self, subnet, gw_ip, bd):
        prefix_len = subnet['cidr'].split('/')[1]
        gw_ip_mask = gw_ip + '/' + prefix_len

        sn = aim_resource.Subnet(tenant_name=bd.tenant_name,
                                 bd_name=bd.name,
                                 gw_ip_mask=gw_ip_mask)
        return sn

    def _map_epg_subnet(self, subnet, gw_ip, epg):
        prefix_len = subnet['cidr'].split('/')[1]
        gw_ip_mask = gw_ip + '/' + prefix_len

        epg_sn = aim_resource.EPGSubnet(tenant_name=epg.tenant_name,
                                 app_profile_name=epg.app_profile_name,
                                 epg_name=epg.name,
                                 gw_ip_mask=gw_ip_mask)
        return epg_sn

    def _map_address_scope(self, session, scope):
        id = scope['id']
        tenant_aname = self.name_mapper.project(session, scope['tenant_id'])
        aname = self.name_mapper.address_scope(session, id)

        vrf = aim_resource.VRF(tenant_name=tenant_aname, name=aname)
        return vrf

    def _map_router(self, session, router, contract_only=False):
        id = router['id']
        aname = self.name_mapper.router(session, id)

        contract = aim_resource.Contract(tenant_name=COMMON_TENANT_NAME,
                                         name=aname)
        if contract_only:
            return contract
        subject = aim_resource.ContractSubject(tenant_name=COMMON_TENANT_NAME,
                                               contract_name=aname,
                                               name=ROUTER_SUBJECT_NAME)
        return contract, subject

    def _map_default_vrf(self, session, network):
        tenant_aname = self.name_mapper.project(session, network['tenant_id'])

        vrf = aim_resource.VRF(tenant_name=tenant_aname,
                               name=DEFAULT_VRF_NAME)
        return vrf

    def _map_unrouted_vrf(self):
        vrf = aim_resource.VRF(
            tenant_name=COMMON_TENANT_NAME,
            name=self.apic_system_id + '_' + UNROUTED_VRF_NAME)
        return vrf

    def _ensure_common_tenant(self, aim_ctx):
        attrs = aim_resource.Tenant(
            name=COMMON_TENANT_NAME, monitored=True, display_name='')
        tenant = self.aim.get(aim_ctx, attrs)
        if not tenant:
            LOG.info("Creating common tenant")
            tenant = self.aim.create(aim_ctx, attrs)
        return tenant

    def _ensure_unrouted_vrf(self, aim_ctx):
        attrs = self._map_unrouted_vrf()
        vrf = self.aim.get(aim_ctx, attrs)
        if not vrf:
            attrs.display_name = (
                aim_utils.sanitize_display_name('CommonUnroutedVRF'))
            LOG.info("Creating common unrouted VRF")
            vrf = self.aim.create(aim_ctx, attrs)
        return vrf

    def _ensure_any_filter(self, aim_ctx):
        filter_name = self._any_filter_name
        dname = aim_utils.sanitize_display_name("AnyFilter")
        filter = aim_resource.Filter(tenant_name=COMMON_TENANT_NAME,
                                     name=filter_name,
                                     display_name=dname)
        if not self.aim.get(aim_ctx, filter):
            LOG.info("Creating common Any Filter")
            self.aim.create(aim_ctx, filter)

        dname = aim_utils.sanitize_display_name("AnyFilterEntry")
        entry = aim_resource.FilterEntry(tenant_name=COMMON_TENANT_NAME,
                                         filter_name=filter_name,
                                         name=ANY_FILTER_ENTRY_NAME,
                                         display_name=dname)
        if not self.aim.get(aim_ctx, entry):
            LOG.info("Creating common Any FilterEntry")
            self.aim.create(aim_ctx, entry)

        return filter

    @property
    def _any_filter_name(self):
        return self.apic_system_id + '_' + ANY_FILTER_NAME

    @property
    def _default_sg_name(self):
        return self.apic_system_id + '_' + DEFAULT_SG_NAME

    @property
    def _system_sg_name(self):
        return self.apic_system_id + '_' + SYSTEM_SG_NAME

    def _ensure_default_vrf(self, aim_ctx, attrs):
        vrf = self.aim.get(aim_ctx, attrs)
        if not vrf:
            attrs.display_name = (
                aim_utils.sanitize_display_name('DefaultRoutedVRF'))
            LOG.info("Creating default VRF for %s", attrs.tenant_name)
            vrf = self.aim.create(aim_ctx, attrs)
        return vrf

    def _cleanup_default_vrf(self, aim_ctx, vrf):
        if not self._is_vrf_used_by_networks(aim_ctx.db_session, vrf):
            LOG.info("Deleting default VRF for %s", vrf.tenant_name)
            self.aim.delete(aim_ctx, vrf)

    # Used by policy driver.
    def get_bd_for_network(self, session, network):
        mapping = self._get_network_mapping(session, network['id'])
        return mapping and self._get_network_bd(mapping)

    # Used by policy driver.
    def get_epg_for_network(self, session, network):
        mapping = self._get_network_mapping(session, network['id'])
        return mapping and self._get_network_epg(mapping)

    def get_aim_domains(self, aim_ctx):
        vmms = [{'name': x.name, 'type': x.type}
                for x in self.aim.find(aim_ctx, aim_resource.VMMDomain)
                if x.type == utils.OPENSTACK_VMM_TYPE]
        phys = [{'name': x.name}
                for x in self.aim.find(aim_ctx, aim_resource.PhysicalDomain)]
        return vmms, phys

    def _is_external(self, network):
        return network.get('router:external')

    def _is_svi(self, network):
        return network.get(cisco_apic.SVI)

    def _is_svi_db(self, network_db):
        if (network_db.aim_extension_mapping and
                network_db.aim_extension_mapping.svi):
            return True
        return False

    def _is_preexisting_svi_db(self, network_db):
        if (network_db.aim_extension_mapping and
                network_db.aim_extension_mapping.svi and
                network_db.aim_extension_mapping.external_network_dn):
            return True
        return False

    def _is_bgp_enabled(self, network):
        return network.get(cisco_apic.BGP)

    def _is_multi_ext_nets_enabled(self, network):
        return network.get(cisco_apic.MULTI_EXT_NETS)

    def _nat_type_to_strategy(self, nat_type):
        ns_cls = nat_strategy.DistributedNatStrategy
        if nat_type == '':
            ns_cls = nat_strategy.NoNatStrategy
        elif nat_type == 'edge':
            ns_cls = nat_strategy.EdgeNatStrategy
        ns = ns_cls(self.aim)
        ns.app_profile_name = self.ap_name
        ns.common_scope = self.apic_system_id
        return ns

    def _get_aim_external_objects(self, network):
        ext_net_dn = (network.get(cisco_apic.DIST_NAMES, {})
                      .get(cisco_apic.EXTERNAL_NETWORK))
        if not ext_net_dn:
            return None, None, None
        nat_type = network.get(cisco_apic.NAT_TYPE)
        aim_ext_net = aim_resource.ExternalNetwork.from_dn(ext_net_dn)
        aim_l3out = aim_resource.L3Outside(
            tenant_name=aim_ext_net.tenant_name, name=aim_ext_net.l3out_name)
        return aim_l3out, aim_ext_net, self._nat_type_to_strategy(nat_type)

    def _get_aim_nat_strategy(self, network):
        if not self._is_external(network):
            return None, None, None
        return self._get_aim_external_objects(network)

    def _get_aim_external_objects_db(self, session, network_db):
        extn_info = self.get_network_extn_db(session, network_db.id)
        if extn_info and cisco_apic.EXTERNAL_NETWORK in extn_info:
            dn = extn_info[cisco_apic.EXTERNAL_NETWORK]
            a_ext_net = aim_resource.ExternalNetwork.from_dn(dn)
            a_l3out = aim_resource.L3Outside(
                tenant_name=a_ext_net.tenant_name,
                name=a_ext_net.l3out_name)
            ns = self._nat_type_to_strategy(
                    extn_info.get(cisco_apic.NAT_TYPE))
            return a_l3out, a_ext_net, ns
        return None, None, None

    def _get_aim_nat_strategy_db(self, session, network_db):
        if network_db.external is not None:
            return self._get_aim_external_objects_db(session, network_db)
        return None, None, None

    def _subnet_to_gw_ip_mask(self, subnet):
        cidr = subnet['cidr'].split('/')
        return aim_resource.Subnet.to_gw_ip_mask(
            subnet['gateway_ip'] or cidr[0], int(cidr[1]))

    def _get_router_intf_count(self, session, router, scope_id=None):
        if not scope_id:
            query = BAKERY(lambda s: s.query(
                l3_db.RouterPort))
            query += lambda q: q.filter(
                l3_db.RouterPort.router_id == sa.bindparam('router_id'))
            query += lambda q: q.filter(
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            result = query(session).params(
                router_id=router['id']).count()
        elif scope_id == NO_ADDR_SCOPE:
            query = BAKERY(lambda s: s.query(
                l3_db.RouterPort))
            query += lambda q: q.join(
                models_v2.IPAllocation,
                models_v2.IPAllocation.port_id == l3_db.RouterPort.port_id)
            query += lambda q: q.join(
                models_v2.Subnet,
                models_v2.Subnet.id == models_v2.IPAllocation.subnet_id)
            query += lambda q: q.outerjoin(
                models_v2.SubnetPool,
                models_v2.SubnetPool.id == models_v2.Subnet.subnetpool_id)
            query += lambda q: q.filter(
                l3_db.RouterPort.router_id == sa.bindparam('router_id'))
            query += lambda q: q.filter(
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            query += lambda q: q.filter(
                sa.or_(models_v2.Subnet.subnetpool_id.is_(None),
                       models_v2.SubnetPool.address_scope_id.is_(None)))
            result = query(session).params(
                router_id=router['id']).count()
        else:
            # Include interfaces for isomorphic scope.
            mapping = self._get_address_scope_mapping(session, scope_id)
            vrf = self._get_address_scope_vrf(mapping)
            mappings = self._get_address_scope_mappings_for_vrf(session, vrf)
            scope_ids = [mapping.scope_id for mapping in mappings]
            if not scope_ids:
                return 0

            query = BAKERY(lambda s: s.query(
                l3_db.RouterPort))
            query += lambda q: q.join(
                models_v2.IPAllocation,
                models_v2.IPAllocation.port_id == l3_db.RouterPort.port_id)
            query += lambda q: q.join(
                models_v2.Subnet,
                models_v2.Subnet.id == models_v2.IPAllocation.subnet_id)
            query += lambda q: q.join(
                models_v2.SubnetPool,
                models_v2.SubnetPool.id == models_v2.Subnet.subnetpool_id)
            query += lambda q: q.filter(
                l3_db.RouterPort.router_id == sa.bindparam('router_id'))
            query += lambda q: q.filter(
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            query += lambda q: q.filter(
                models_v2.SubnetPool.address_scope_id.in_(
                    sa.bindparam('scope_ids', expanding=True)))
            result = query(session).params(
                router_id=router['id'],
                scope_ids=scope_ids).count()

        return result

    def _get_address_scope_id_for_subnets(self, context, subnets):
        # Assuming that all the subnets provided are consistent w.r.t.
        # address-scope, use the first available subnet to determine
        # address-scope. If subnets is a mix of v4 and v6 subnets,
        # then v4 subnets are given preference.
        subnets = sorted(subnets, key=lambda x: x['ip_version'])

        scope_id = NO_ADDR_SCOPE
        subnetpool_id = subnets[0]['subnetpool_id'] if subnets else None
        if subnetpool_id:
            subnetpool_db = self.plugin._get_subnetpool(context,
                                                        subnetpool_id)
            scope_id = (subnetpool_db.address_scope_id or NO_ADDR_SCOPE)
        return scope_id

    def _manage_external_connectivity(self, context, router, old_network,
                                      new_network, vrf):
        session = context.session
        aim_ctx = aim_context.AimContext(db_session=session)

        # Keep only the identity attributes of the VRF so that calls to
        # nat-library have consistent resource values. This
        # is mainly required to ease unit-test verification.
        vrf = aim_resource.VRF(tenant_name=vrf.tenant_name, name=vrf.name)
        rtr_dbs = self._get_routers_for_vrf(session, vrf)

        prov_dict = {}
        cons_dict = {}

        def update_contracts(router, ext_net):
            contract = self._map_router(session, router, True)
            prov_dict[
                contract.name] = aim_resource.ExternalNetworkProvidedContract(
                    tenant_name=ext_net.tenant_name,
                    l3out_name=ext_net.l3out_name,
                    ext_net_name=ext_net.name,
                    name=contract.name)
            cons_dict[
                contract.name] = aim_resource.ExternalNetworkConsumedContract(
                    tenant_name=ext_net.tenant_name,
                    l3out_name=ext_net.l3out_name,
                    ext_net_name=ext_net.name,
                    name=contract.name)

            r_info = self.get_router_extn_db(session, router['id'])
            for p_con in r_info[a_l3.EXTERNAL_PROVIDED_CONTRACTS]:
                prov_dict[
                    p_con] = aim_resource.ExternalNetworkProvidedContract(
                        tenant_name=ext_net.tenant_name,
                        l3out_name=ext_net.l3out_name,
                        ext_net_name=ext_net.name, name=p_con)
            for c_con in r_info[a_l3.EXTERNAL_CONSUMED_CONTRACTS]:
                cons_dict[
                    c_con] = aim_resource.ExternalNetworkConsumedContract(
                        tenant_name=ext_net.tenant_name,
                        l3out_name=ext_net.l3out_name,
                        ext_net_name=ext_net.name, name=c_con)

        if old_network:
            _, ext_net, ns = self._get_aim_nat_strategy(old_network)
            if ext_net:
                # Find Neutron networks that share the APIC external network.
                eqv_nets = self.get_network_ids_by_ext_net_dn(
                    session, ext_net.dn, lock_update=True)
                rtr_old = [r for r in rtr_dbs
                           if (r.gw_port_id and
                               r.gw_port.network_id in eqv_nets)]
                prov_dict = {}
                cons_dict = {}
                for r in rtr_old:
                    update_contracts(r, ext_net)

                if rtr_old:
                    prov_list = list(prov_dict.values())
                    cons_list = list(cons_dict.values())
                    provided = sorted(prov_list, key=lambda x: x.name)
                    consumed = sorted(cons_list, key=lambda x: x.name)
                    ns.connect_vrf(aim_ctx, ext_net, vrf,
                        provided_contracts=provided,
                        consumed_contracts=consumed)
                else:
                    ns.disconnect_vrf(aim_ctx, ext_net, vrf)
        if new_network:
            _, ext_net, ns = self._get_aim_nat_strategy(new_network)
            if ext_net:
                # Find Neutron networks that share the APIC external network.
                eqv_nets = self.get_network_ids_by_ext_net_dn(
                    session, ext_net.dn, lock_update=True)
                rtr_new = [r for r in rtr_dbs
                           if (r.gw_port_id and
                               r.gw_port.network_id in eqv_nets)]
                prov_dict = {}
                cons_dict = {}
                for r in rtr_new:
                    update_contracts(r, ext_net)
                update_contracts(router, ext_net)
                prov_list = list(prov_dict.values())
                cons_list = list(cons_dict.values())
                provided = sorted(prov_list, key=lambda x: x.name)
                consumed = sorted(cons_list, key=lambda x: x.name)
                ns.connect_vrf(aim_ctx, ext_net, vrf,
                    provided_contracts=provided,
                    consumed_contracts=consumed)

    def _is_port_bound(self, port):
        return port.get(portbindings.VIF_TYPE) not in [
            portbindings.VIF_TYPE_UNBOUND,
            portbindings.VIF_TYPE_BINDING_FAILED]

    @n_utils.transaction_guard
    def _notify_port_update(self, plugin_context, port_id):
        port = self.plugin.get_port(plugin_context.elevated(), port_id)
        if self._is_port_bound(port):
            LOG.debug("Enqueing notify for port %s", port['id'])
            # REVISIT: Check to see if the standard_attr_id
            # should be leveraged by the plugin.
            if 'standard_attr_id' in port:
                del port['standard_attr_id']
            self.notifier.port_update(plugin_context, port)

    def _notify_port_update_for_fip(self, plugin_context, port_id):
        # REVISIT: Replace get_port() call with joins in query below.
        port = self.plugin.get_port(plugin_context.elevated(), port_id)
        ports_to_notify = [port_id]
        fixed_ips = [x['ip_address'] for x in port['fixed_ips']]
        if fixed_ips:
            with db_api.CONTEXT_READER.using(plugin_context):
                query = BAKERY(lambda s: s.query(
                    n_addr_pair_db.AllowedAddressPair))
                query += lambda q: q.join(
                    models_v2.Port,
                    models_v2.Port.id == (
                        n_addr_pair_db.AllowedAddressPair.port_id))
                query += lambda q: q.filter(
                    models_v2.Port.network_id == sa.bindparam('network_id'))
                addr_pair = query(plugin_context.session).params(
                    network_id=port['network_id']).all()
                notify_pairs = []
            # In order to support use of CIDRs in allowed-address-pairs,
            # we can't include the fxied IPs in the DB query, and instead
            # have to qualify that with post-DB processing
            for a_pair in addr_pair:
                cidr = netaddr.IPNetwork(a_pair['ip_address'])
                for addr in fixed_ips:
                    if addr in cidr:
                        notify_pairs.append(a_pair)

            ports_to_notify.extend([x['port_id'] for x in set(notify_pairs)])
        for p in sorted(ports_to_notify):
            try:
                self._notify_port_update(plugin_context, p)
            except n_exceptions.PortNotFound:
                pass

    def _notify_port_update_bulk(self, plugin_context, port_ids):
        # REVISIT: Is a single query for all ports possible?
        for p_id in port_ids:
            self._notify_port_update(plugin_context, p_id)

    @n_utils.transaction_guard
    def _notify_vrf_update(self, plugin_context, vrfs_to_notify):
        for vrf in vrfs_to_notify:
            self.notifier.opflex_notify_vrf(plugin_context, vrf)

    def _add_postcommit_port_notifications(self, plugin_context, ports):
        ports_to_notify = getattr(plugin_context, '_ports_to_notify', None)
        if not ports_to_notify:
            ports_to_notify = plugin_context._ports_to_notify = set()
        ports_to_notify.update(ports)

    def _add_postcommit_vrf_notification(self, plugin_context, vrf):
        vrfs_to_notify = getattr(plugin_context, '_vrfs_to_notify', None)
        if not vrfs_to_notify:
            vrfs_to_notify = plugin_context._vrfs_to_notify = set()
        vrfs_to_notify.add('%s %s' % (vrf.tenant_name, vrf.name))

    def _send_postcommit_notifications(self, plugin_context):
        ports = getattr(plugin_context, '_ports_to_notify', None)
        if ports:
            self._notify_port_update_bulk(plugin_context, ports)
            plugin_context._ports_to_notify = set()

        vrfs = getattr(plugin_context, '_vrfs_to_notify', None)
        if vrfs:
            self._notify_vrf_update(plugin_context, vrfs)
            plugin_context._vrfs_to_notify = set()

    def get_or_allocate_snat_ip(self, plugin_context, host_or_vrf,
                                ext_network):
        """Fetch or allocate SNAT IP on the external network.

        IP allocation is done by creating a port on the external network,
        and associating an owner with it. The owner could be the ID of
        a host (or VRF) if SNAT IP allocation per host (or per VRF) is
        desired.
        If IP was found or successfully allocated, returns a dict like:
            {'host_snat_ip': <ip_addr>,
             'host_snat_mac': <mac_addr>,
             'gateway_ip': <gateway_ip of subnet>,
             'prefixlen': <prefix_length_of_subnet>}
        """
        with db_api.CONTEXT_READER.using(plugin_context) as session:
            # Query for existing SNAT port.
            query = BAKERY(lambda s: s.query(
                models_v2.IPAllocation.ip_address,
                models_v2.Port.mac_address,
                models_v2.Subnet.gateway_ip,
                models_v2.Subnet.cidr))
            query += lambda q: q.join(
                models_v2.Subnet,
                models_v2.Subnet.id == models_v2.IPAllocation.subnet_id)
            query += lambda q: q.join(
                models_v2.Port,
                models_v2.Port.id == models_v2.IPAllocation.port_id)
            query += lambda q: q.filter(
                models_v2.Port.network_id == sa.bindparam('network_id'),
                models_v2.Port.device_id == sa.bindparam('device_id'),
                models_v2.Port.device_owner == aim_cst.DEVICE_OWNER_SNAT_PORT)
            result = query(session).params(
                network_id=ext_network['id'],
                device_id=host_or_vrf).first()
            if result:
                return {'host_snat_ip': result[0],
                        'host_snat_mac': result[1],
                        'gateway_ip': result[2],
                        'prefixlen': int(result[3].split('/')[1])}

            # None found, so query for subnets on which to allocate
            # SNAT port.
            extn_db_sn = extension_db.SubnetExtensionDb

            query = BAKERY(lambda s: s.query(
                models_v2.Subnet))
            query += lambda q: q.join(
                extn_db_sn,
                extn_db_sn.subnet_id == models_v2.Subnet.id)
            query += lambda q: q.filter(
                models_v2.Subnet.network_id == sa.bindparam('network_id'))
            query += lambda q: q.filter(
                extn_db_sn.snat_host_pool.is_(True))
            snat_subnets = query(session).params(
                network_id=ext_network['id']).all()

            if not snat_subnets:
                LOG.info('No subnet in external network %s is marked as '
                         'SNAT-pool',
                         ext_network['id'])
                return

        # Outside the transaction, try allocating SNAT port from
        # available subnets.
        #
        # REVISIT: Although unlikely, creating the SNAT port outside
        # this transaction means multiple SNAT ports could be created
        # by different threads for the same host on the same external
        # network. Furthermore, once created, any of these ports could
        # be found and used by other threads, so checking after the
        # port is created is not sufficient. To eliminate this race
        # condition, consider having create_port_precommit validate
        # that no other equivalent SNAT port already exists. If that
        # validation resulted in an exception from create_port, a loop
        # encompassing this entire method would retry the initial
        # query, which should then find the existing equivalent SNAT
        # port.
        for snat_subnet in snat_subnets:
            try:
                # REVISIT:  This is a temporary fix and needs to be redone.
                # We need to make sure that we do a proper bind.  Currently
                # the SNAT endpoint is created by looking at VM port bind.
                attrs = {'device_id': host_or_vrf,
                         'device_owner': aim_cst.DEVICE_OWNER_SNAT_PORT,
                         'tenant_id': ext_network['tenant_id'],
                         'name': 'snat-pool-port:%s' % host_or_vrf,
                         'network_id': ext_network['id'],
                         'mac_address': n_constants.ATTR_NOT_SPECIFIED,
                         'fixed_ips': [{'subnet_id': snat_subnet.id}],
                         'status': "ACTIVE",
                         'admin_state_up': True}
                port = self.plugin.create_port(
                    plugin_context, {'port': attrs})
                if port and port['fixed_ips']:
                    snat_ip = port['fixed_ips'][0]['ip_address']
                    return {'host_snat_ip': snat_ip,
                            'host_snat_mac': port['mac_address'],
                            'gateway_ip': snat_subnet['gateway_ip'],
                            'prefixlen':
                            int(snat_subnet['cidr'].split('/')[1])}
            except n_exceptions.IpAddressGenerationFailure:
                LOG.info('No more addresses available in subnet %s '
                         'for SNAT IP allocation',
                         snat_subnet['id'])

        # Failed to allocate SNAT port.
        LOG.warning("Failed to allocate SNAT IP on external network %s",
                    ext_network['id'])

    def _has_snat_ip_ports(self, plugin_context, subnet_id):
        session = plugin_context.session

        query = BAKERY(lambda s: s.query(
            models_v2.Port))
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.port_id == models_v2.Port.id)
        query += lambda q: q.filter(
            models_v2.IPAllocation.subnet_id == sa.bindparam('subnet_id'))
        query += lambda q: q.filter(
            models_v2.Port.device_owner == aim_cst.DEVICE_OWNER_SNAT_PORT)
        return query(session).params(
            subnet_id=subnet_id).first()

    def _delete_unneeded_snat_ip_ports(self, plugin_context, ext_network_id):
        snat_port_ids = []
        with db_api.CONTEXT_READER.using(plugin_context) as session:
            # Query for any interfaces of routers with gateway ports
            # on this external network.
            query = BAKERY(lambda s: s.query(
                l3_db.RouterPort.port_id))
            query += lambda q: q.join(
                l3_db.Router,
                l3_db.Router.id == l3_db.RouterPort.router_id)
            query += lambda q: q.join(
                models_v2.Port,
                models_v2.Port.id == l3_db.Router.gw_port_id)
            query += lambda q: q.filter(
                models_v2.Port.network_id == sa.bindparam('ext_network_id'),
                models_v2.Port.device_owner ==
                n_constants.DEVICE_OWNER_ROUTER_GW,
                l3_db.RouterPort.port_type ==
                n_constants.DEVICE_OWNER_ROUTER_INTF)
            if not query(session).params(
                ext_network_id=ext_network_id).first():
                # No such interfaces exist, so query for any SNAT
                # ports on this external network.
                query = BAKERY(lambda s: s.query(
                    models_v2.Port.id))
                query += lambda q: q.filter(
                    models_v2.Port.network_id ==
                    sa.bindparam('ext_network_id'),
                    models_v2.Port.device_owner ==
                    aim_cst.DEVICE_OWNER_SNAT_PORT)
                snat_port_ids = [p[0] for p in query(session).params(
                    ext_network_id=ext_network_id).all()]

        # Outside the transaction, delete any unneeded SNAT ports.
        #
        # REVISIT: Although unlikely, deleting an SNAT port outside
        # the transaction means it could become needed again before
        # it is actually deleted. To eliminate this race condition,
        # consider having delete_port_precommit validate that the SNAT
        # port is still not needed by querying again for interfaces of
        # routers with gateway ports on that network, and if any are
        # found, raising an exception that would be silently ignored
        # below.
        e_context = plugin_context.elevated()
        for port_id in snat_port_ids:
            try:
                self.plugin.delete_port(e_context, port_id)
            except n_exceptions.NeutronException as ne:
                LOG.warning("Failed to delete SNAT port %(port)s: %(ex)s",
                            {'port': port_id, 'ex': ne})

    # Called by l3_plugin.
    def check_floatingip_external_address(self, context, floatingip):
        session = context.session
        if floatingip.get('subnet_id'):
            sn_ext = self.get_subnet_extn_db(session, floatingip['subnet_id'])
            if sn_ext.get(cisco_apic.SNAT_HOST_POOL, False):
                raise exceptions.SnatPoolCannotBeUsedForFloatingIp()
        elif floatingip.get('floating_ip_address'):
            extn_db_sn = extension_db.SubnetExtensionDb

            query = BAKERY(lambda s: s.query(
                models_v2.Subnet.cidr))
            query += lambda q: q.join(
                extn_db_sn,
                extn_db_sn.subnet_id == models_v2.Subnet.id)
            query += lambda q: q.filter(
                models_v2.Subnet.network_id == sa.bindparam('network_id'))
            query += lambda q: q.filter(extn_db_sn.snat_host_pool.is_(True))
            cidrs = query(session).params(
                network_id=floatingip['floating_network_id']).all()

            cidrs = netaddr.IPSet([c[0] for c in cidrs])
            if floatingip['floating_ip_address'] in cidrs:
                raise exceptions.SnatPoolCannotBeUsedForFloatingIp()

    # Called by l3_plugin.
    def get_subnets_for_fip(self, context, floatingip):
        session = context.session
        extn_db_sn = extension_db.SubnetExtensionDb

        query = BAKERY(lambda s: s.query(
            models_v2.Subnet.id))
        query += lambda q: q.outerjoin(
            extn_db_sn,
            extn_db_sn.subnet_id == models_v2.Subnet.id)
        query += lambda q: q.filter(
            models_v2.Subnet.network_id == sa.bindparam('network_id'))
        query += lambda q: q.filter(
            sa.or_(extn_db_sn.snat_host_pool.is_(False),
                   extn_db_sn.snat_host_pool.is_(None)))
        other_sn = query(session).params(
            network_id=floatingip['floating_network_id']).all()

        return [s[0] for s in other_sn]

    def _is_opflex_type(self, net_type):
        return net_type == ofcst.TYPE_OPFLEX

    def _is_supported_non_opflex_type(self, net_type):
        return net_type in [n_constants.TYPE_VLAN]

    def _use_static_path(self, bound_segment):
        return (bound_segment and
                self._is_supported_non_opflex_type(
                    bound_segment[api.NETWORK_TYPE]))

    def _segment_to_vlan_encap(self, segment):
        encap = None
        if segment:
            if segment.get(api.NETWORK_TYPE) in [n_constants.TYPE_VLAN]:
                encap = 'vlan-%s' % segment[api.SEGMENTATION_ID]
            else:
                LOG.debug('Unsupported segmentation type for static path '
                          'binding: %s',
                          segment.get(api.NETWORK_TYPE))
        return encap

    # Used by the AIM SFC driver.
    # REVISIT: We should explore better options for this API.
    def _filter_host_links_by_segment(self, session, segment, host_links):
        # All host links must belong to the same host
        filtered_host_links = []
        if host_links:
            aim_ctx = aim_context.AimContext(db_session=session)
            host_link_net_labels = self.aim.find(
                aim_ctx, aim_infra.HostLinkNetworkLabel,
                host_name=host_links[0].host_name,
                network_label=segment[api.PHYSICAL_NETWORK])
            # This segment uses specific host interfaces
            if host_link_net_labels:
                ifaces = set([x.interface_name for x in host_link_net_labels])
                filtered_host_links = [
                    x for x in host_links if x.interface_name in
                    ifaces and x.path]
        # If the filtered host link list is empty, return the original one.
        # TODO(ivar): we might want to raise an exception if there are not
        # host link available instead of falling back to the full list.
        return filtered_host_links or host_links

    def _rebuild_host_path_for_network(self, plugin_context, network, segment,
                                       host, host_links):
        # Look up the static ports for this host and segment.
        with db_api.CONTEXT_READER.using(plugin_context):
            static_ports = self._get_static_ports(
                plugin_context, host, segment)

        # If network is SVI, make sure SVI IPs are allocated for each
        # node of each static_port.
        if self._is_svi(network):
            self._ensure_svi_ips_for_static_ports(
                plugin_context, static_ports, network)

        # Rebuild the static paths.
        with db_api.CONTEXT_WRITER.using(plugin_context) as session:
            aim_ctx = aim_context.AimContext(db_session=session)
            if self._is_svi(network):
                l3out, _, _ = self._get_aim_external_objects(network)
                # Nuke existing interfaces for host
                search_args = {
                    'tenant_name': l3out.tenant_name,
                    'l3out_name': l3out.name,
                    'node_profile_name': L3OUT_NODE_PROFILE_NAME,
                    'interface_profile_name': L3OUT_IF_PROFILE_NAME,
                    'host': host
                }
                for aim_l3out_if in self.aim.find(
                        aim_ctx, aim_resource.L3OutInterface, **search_args):
                    self.aim.delete(aim_ctx, aim_l3out_if, cascade=True)
                for static_port in static_ports:
                    self._update_static_path_for_svi(
                        session, plugin_context, network, l3out, static_port)
            else:
                epg = self.get_epg_for_network(session, network)
                if not epg:
                    LOG.info('Network %s does not map to any EPG',
                             network['id'])
                    return
                epg = self.aim.get(aim_ctx, epg)
                # Update old host values.
                paths = set([(x['path'], x['encap'], x['host'])
                             for x in epg.static_paths if x['host'] != host])
                # Add new static ports.
                paths |= set([(x.link.path, x.encap, x.link.host_name)
                              for x in static_ports])
                self.aim.update(aim_ctx, epg, static_paths=[
                    {'path': x[0], 'encap': x[1], 'host': x[2]}
                    for x in paths])

    def _get_topology_from_path(self, path):
        """Convert path string to toplogy elements.

        Given a static path DN, convert it into the individual
        elements that make up the path. Static paths can be for
        VPCs or individual ports. Extract the switch IDs from
        the path, along with any relevant interface information.
        Returns a tuple with one of two formats:

            (is_vpc, pod_id, nodes, node_paths, module, port)

                                OR

            (is_vpc, pod_id, nodes, node_paths, bundle, vpcmodule)
        where:
            is_vpc: is the static path for a VPC
            pod_id: the pod ID in the static path
            nodes: list of switch nodes declared in the path
            node_paths: list of prefixes, which is the DN up
                        to the switch component
            module: the module on an individual switch
            port: the port ID on a switch module
            bundle: the bundle that make up a VPC
            vpcmodule: the VPC module name
        """
        nodes = []
        node_paths = []
        match = self.port_desc_re.match(path)
        # The L3 Out model requires all the nodes supporting an L3
        # Out are configured under the L3 Out's node profile. In the
        # case where the static path is a VPC, then both nodes used
        # in the VPC must be added, so their IDs must be extracted
        # from the static path.
        if match:
            pod_id, switch, module, port = match.group(1, 2, 3, 4)
            nodes.append(switch)
            node_paths.append(ACI_CHASSIS_DESCR_STRING % (pod_id, switch))
            return (False, pod_id, nodes, node_paths, module, port)
        else:
            # In the case where the static path calls out a VPC, then both
            # the IDs and paths for both nodes used to make up the VPC must
            # be extracted from the static path.
            match = self.vpcport_desc_re.match(path)
            if match:
                pod_id, switch1, switch2, bundle = match.group(1, 2, 3, 4)
                for switch in (switch1, switch2):
                    nodes.append(switch)
                    node_paths.append(ACI_CHASSIS_DESCR_STRING % (pod_id,
                                                                  switch))
                return (True, pod_id, nodes, node_paths, bundle, None)
            else:
                LOG.error('Unsupported static path format: %s', path)
                return (False, None, None, None, None, None)

    def _ensure_svi_ips_for_static_ports(self, context, static_ports, network):
        for static_port in static_ports:
            if not static_port.encap:
                continue

            # Get the nodes from the static port's path.
            _, _, nodes, _, _, _ = self._get_topology_from_path(
                static_port.link.path)

            for node in nodes:
                # See if this node already has an IP for SVI for this
                # network. If not, allocate a port (and therefore IP)
                # for the SVI interface on this node.
                filters = {'network_id': [network['id']],
                           'name': ['apic-svi-port:node-%s' % node]}
                svi_ports = self.plugin.get_ports(context, filters)
                if svi_ports:
                    # We have some SVI ports already for the corresponding
                    # node - if the relevant subnets are not present,
                    # update and get out.
                    svi_subnets = [s['subnet_id']
                        for s in svi_ports[0]['fixed_ips']]
                    new_subnets = list(set(network['subnets']) - set(
                        svi_subnets))
                    if new_subnets:
                        fixed_ips = svi_ports[0].setdefault('fixed_ips', [])
                        for s in new_subnets:
                            fixed_ips.append({'subnet_id': s})
                        port_info = {
                            'port': {'fixed_ips': fixed_ips}}
                        self.plugin.update_port(context, svi_ports[0]['id'],
                            port_info)
                    continue
                # We don't have an IP for this node, so create a port.
                #
                # REVISIT: The node identifier should be in device_id
                # rather than name, but changing this would require a
                # data migration.
                attrs = {'device_id': '',
                         'device_owner': aim_cst.DEVICE_OWNER_SVI_PORT,
                         'tenant_id': network['tenant_id'],
                         'name': 'apic-svi-port:node-%s' % node,
                         'network_id': network['id'],
                         'mac_address': n_constants.ATTR_NOT_SPECIFIED,
                         'fixed_ips': [{'subnet_id': subnet}
                            for subnet in network['subnets']],
                         'admin_state_up': False}
                try:
                    port = self.plugin.create_port(context, {'port': attrs})
                    LOG.info("Allocated IP %(ip)s for node %(node)s on SVI "
                             "network %(net)s",
                             {'ip': port['fixed_ips'][0]['ip_address'],
                              'node': node, 'net': network['id']})
                except Exception as e:
                    LOG.warning("Failed to create SVI port %(port)s: %(ex)s",
                            {'port': attrs, 'ex': e})

    def _update_static_path_for_svi(self, session, plugin_context, network,
                                    l3out, static_port, remove=False,
                                    del_subnet=None):
        """Configure static path for an SVI on a L3 Out

        Add, update, or delete a single static path on an SVI. Adds
        and updates are handled by providing a StaticPort with a valid
        encap, as well as link that contains the path. For deletion,
        the remove flag should be set to True.
        """
        if not static_port.encap:
            return

        path = static_port.link.path
        is_vpc, _, nodes, node_paths, _, _ = self._get_topology_from_path(path)

        # ACI requires all router IDs to be unique within a VRF,
        # so if we're creating new nodes, then we need to allocate
        # new IDs for each one.
        aim_ctx = aim_context.AimContext(db_session=session)
        if not remove and not del_subnet and path:
            for node_path in node_paths:
                # REVISIT: We should check to see if this node is
                # already present under the node profile.
                apic_router_id = self._allocate_apic_router_ids(aim_ctx,
                                                                l3out,
                                                                node_path)
                aim_l3out_node = aim_resource.L3OutNode(
                    tenant_name=l3out.tenant_name, l3out_name=l3out.name,
                    node_profile_name=L3OUT_NODE_PROFILE_NAME,
                    node_path=node_path, router_id=apic_router_id,
                    router_id_loopback=False)
                self.aim.create(aim_ctx, aim_l3out_node, overwrite=True)

            if not network['subnets']:
                return

            query = BAKERY(lambda s: s.query(
                models_v2.Subnet))
            query += lambda q: q.filter(
                models_v2.Subnet.network_id == sa.bindparam('net_id'),
                models_v2.Subnet.ip_version == sa.bindparam('ip_vers'))
            subnets_dict = {}
            for ip_vers in [4, 6]:
                subnet = query(session).params(
                    net_id=network['id'], ip_vers=ip_vers).one_or_none()
                if subnet is not None:
                    subnets_dict[ip_vers] = {
                        'subnet': subnet,
                        'mask': str(netaddr.IPNetwork(
                            subnet['cidr']).prefixlen),
                        'primary_ips': []}
            for node in nodes:
                # Get the IP of the SVI port for this node on this network.
                #
                # REVISIT: The node identifier should be in device_id
                # rather than name, but changing this would require a
                # data migration.
                filters = {'network_id': [network['id']],
                           'name': ['apic-svi-port:node-%s' % node]}
                svi_ports = self.plugin.get_ports(plugin_context, filters)
                if svi_ports and svi_ports[0]['fixed_ips']:
                    for fixed_ip in svi_ports[0]['fixed_ips']:
                        ip_vers = netaddr.IPAddress(
                            fixed_ip['ip_address']).version
                        if ip_vers not in subnets_dict:
                            continue

                        subnets_dict[ip_vers]['primary_ips'].append(
                            fixed_ip['ip_address'] + '/' + (
                                subnets_dict[ip_vers]['mask']))
                else:
                    # No SVI port was found. This may mean
                    # _ensure_svi_ips_for_static_ports could not
                    # create one, possibly due to the subnet being
                    # exhausted, which would result in a separate
                    # warning being logged. It is also possible,
                    # though very unlikely, that relevant AIM HostLink
                    # state has changed since the call to
                    # _ensure_svi_ips_for_static_ports that would have
                    # allocated the SVI port. If this happens due to a
                    # rapid series of topology RPCs, the port should
                    # be found when processing the final RPC in the
                    # series.
                    LOG.warning("SVI port not found for node %(node)s on SVI "
                                "network %(net)s",
                                {'node': node, 'net': network['id']})
                    return

            # We only need one interface profile, even if it's a VPC.
            network_db = self.plugin._get_network(plugin_context,
                network['id'])

            for ip_vers, subnet_dict in list(subnets_dict.items()):
                secondary_ip = subnet_dict['subnet']['gateway_ip'] + '/' + (
                    subnet_dict['mask'])
                aim_l3out_if = aim_resource.L3OutInterface(
                    tenant_name=l3out.tenant_name,
                    l3out_name=l3out.name,
                    node_profile_name=L3OUT_NODE_PROFILE_NAME,
                    interface_profile_name=(L3OUT_IF_PROFILE_NAME
                        if ip_vers == 4 else L3OUT_IF_PROFILE_NAME6),
                    interface_path=path, encap=static_port.encap,
                    host=static_port.link.host_name,
                    primary_addr_a=subnet_dict['primary_ips'][0],
                    secondary_addr_a_list=[{'addr': secondary_ip}],
                    primary_addr_b=(subnet_dict['primary_ips'][1]
                        if is_vpc else ''),
                    secondary_addr_b_list=[{'addr':
                                            secondary_ip}] if is_vpc else [])
                self.aim.create(aim_ctx, aim_l3out_if, overwrite=True)

                if (network_db.aim_extension_mapping.bgp_enable and
                        network_db.aim_extension_mapping.bgp_type == (
                            'default_export')):
                    aim_bgp_peer_prefix = aim_resource.L3OutInterfaceBgpPeerP(
                        tenant_name=l3out.tenant_name,
                        l3out_name=l3out.name,
                        node_profile_name=L3OUT_NODE_PROFILE_NAME,
                        interface_profile_name=(L3OUT_IF_PROFILE_NAME
                        if ip_vers == 4 else L3OUT_IF_PROFILE_NAME6),
                        interface_path=path,
                        addr=subnet_dict['subnet']['cidr'],
                        asn=network_db.aim_extension_mapping.bgp_asn)
                    self.aim.create(aim_ctx, aim_bgp_peer_prefix,
                        overwrite=True)

        if remove or del_subnet:
            if del_subnet:
                # This subnet is not present on this host, Find the AF,
                # to map the IProfile to remove.
                query = BAKERY(lambda s: s.query(
                    models_v2.Subnet))
                query += lambda q: q.filter(
                    models_v2.Subnet.id == sa.bindparam('subnet_id'))
                subnet = query(session).params(
                    subnet_id=del_subnet[0]).one_or_none()
                if subnet.ip_version == 6:
                    if_profiles = [L3OUT_IF_PROFILE_NAME6]
                else:
                    if_profiles = [L3OUT_IF_PROFILE_NAME]
            else:
                # Last port being removed so remove all IProfiles.
                if_profiles = [L3OUT_IF_PROFILE_NAME, L3OUT_IF_PROFILE_NAME6]
            # REVISIT: Should we also delete the node profiles if there aren't
            # any more instances on this host?
            for if_profile in if_profiles:
                aim_l3out_if = aim_resource.L3OutInterface(
                    tenant_name=l3out.tenant_name,
                    l3out_name=l3out.name,
                    node_profile_name=L3OUT_NODE_PROFILE_NAME,
                    interface_profile_name=if_profile,
                    interface_path=path)
                if self.aim.get(aim_ctx, aim_l3out_if):
                    self.aim.delete(aim_ctx, aim_l3out_if, cascade=True)

    def _update_static_path_for_network(self, session, network,
                                        static_port, remove=False):
        """Configure static path on an EPG.

        Add, update, or delete a single static path on an EPG. Adds
        and updates are handled by providing a StaticPort with a valid
        encap, as well as link that contains the path. For deletion,
        the remove flag should be set to True.
        """
        if not static_port.encap:
            return

        epg = self.get_epg_for_network(session, network)
        if not epg:
            LOG.info('Network %s does not map to any EPG', network['id'])
            return

        aim_ctx = aim_context.AimContext(db_session=session)
        epg = self.aim.get(aim_ctx, epg)
        # Static paths configured on an EPG can be uniquely
        # identified by their path attribute.
        to_remove = [static_port.link.path]
        if to_remove:
            epg.static_paths = [p for p in epg.static_paths
                                if p.get('path') not in to_remove]
        if not remove and static_port:
            static_info = {'path': static_port.link.path,
                           'encap': static_port.encap,
                           'mode': static_port.mode}
            if static_port.link.host_name:
                static_info['host'] = static_port.link.host_name
            epg.static_paths.append(static_info)
        LOG.debug('Setting static paths for EPG %s to %s',
                  epg, epg.static_paths)
        self.aim.update(aim_ctx, epg, static_paths=epg.static_paths)

    def _get_static_ports(self, plugin_context, host, segment,
                          port_context=None):
        """Get StaticPort objects for ACI.

        :param plugin_context : plugin context
        :param host : host ID for the static port
        :param segment : bound segment of this host
        :param port_context : port context instance
        :returns: List of zero or more static port objects

        This method should be called when a neutron port requires
        static port configuration state for an interface profile in
        a L3 Out policy or for an Endpoint Group. There are two
        sources of this information:
           o from binding:profile when the port has a vnic_type
             of "baremetal"
           o from the HostLink entries in the AIM database
        The information is only available on bound ports, as the
        encapsulation information must also be available. The method
        should only be called by code that has an active DB transaction,
        as it makes use of the session from the plugin_context.
        """
        encap = self._segment_to_vlan_encap(segment)
        if not encap:
            return []
        if port_context and self._is_baremetal_vnic_type(port_context.current):
            # Check if there's any topology information available
            topology = self._get_baremetal_topology(plugin_context,
                                                    port_context.current)
            if not any(topology):
                topology = self._get_baremetal_topology(plugin_context,
                                                        port_context.original)
                if not any(topology):
                    LOG.warning("Invalid topology: port %(port)s does not "
                                "contain required topology information in the "
                                "binding:profile's local_link_information "
                                "array.", {'port': port_context.current['id']})
                    return []
            # The local_link_information should be populated, and
            # will have the static path.
            static_path, _, _, _ = topology

            hlink = aim_infra.HostLink(host_name='',
                                       interface_name='', path=static_path)
            if (port_context.current['device_owner'] ==
                    trunk_consts.TRUNK_SUBPORT_OWNER):
                return [StaticPort(hlink, encap, 'regular')]
            else:
                return [StaticPort(hlink, encap, 'untagged')]
        else:
            # If it's not baremetal, return qualifying entries from the
            # host links table in AIM, with the host links by segments
            # filtering applied
            session = plugin_context.session
            aim_ctx = aim_context.AimContext(db_session=session)
            host_links = self.aim.find(aim_ctx,
                                       aim_infra.HostLink, host_name=host)
            return [StaticPort(host_link, encap, 'regular') for host_link in
                    self._filter_host_links_by_segment(session,
                                                       segment, host_links)]

    def _get_parent_port_for_subport(self, plugin_context, subport_id):
        trunk = self._get_trunk_for_subport(plugin_context, subport_id)
        return self.plugin.get_port(plugin_context, trunk['port_id'])

    @db_api.retry_if_session_inactive()
    def _safe_update_trunk_status(self, context, trunk_id, status):
        self.trunk_plugin.update_trunk(context, trunk_id,
                                       {'trunk': {'status': status}})

    def _handle_subport_binding(self, context, port_id, trunk_id, host_id,
                                owner, binding_profile=None):
        """Bind the given trunk subport to the given host.

           :param context: The context to use for the operation
           :param port_id: The UUID of the port to be bound
           :param trunk_id: The trunk ID that the given port belongs to
           :param host_id: The host to bind the given port to
           :param owner: The value for the device_owner of the port
           :param binding_profile: If not none, value for binding:profile
        """
        port_info = {'port': {portbindings.HOST_ID: host_id,
                              portbindings.PROFILE: binding_profile
                              if binding_profile and host_id else '',
                              'device_owner': owner}}
        port = self.plugin.update_port(context, port_id, port_info)
        vif_type = port.get(portbindings.VIF_TYPE)
        if vif_type == portbindings.VIF_TYPE_BINDING_FAILED:
            raise trunk_exc.SubPortBindingError(port_id=port_id,
                                                trunk_id=trunk_id)
        return port

    # Set up listener for adding or removing subports from a trunk.
    @registry.receives(resources.SUBPORTS,
                       [events.AFTER_CREATE, events.AFTER_DELETE])
    def _after_subport_event(self, resource, event, trunk_plugin, payload):
        context = payload.context
        subports = payload.metadata['subports']
        first_subport_id = subports[0].port_id
        # This is only needed for baremetal VNIC types, as they don't
        # have agents to perform port binding.
        subport_db = self.plugin._get_port(context, first_subport_id)
        if (not subport_db.port_bindings or
            subport_db.port_bindings[0].vnic_type !=
            portbindings.VNIC_BAREMETAL):
            return
        if event == events.AFTER_DELETE:
            parent_port = None
            host_id = ''
            owner = ''
        else:
            parent_port = self._get_parent_port_for_subport(
                context, first_subport_id)
            host_id = parent_port['binding:host_id']
            owner = trunk_consts.TRUNK_SUBPORT_OWNER
        subport_ids = [subport.port_id for subport in subports]
        profile = parent_port[portbindings.PROFILE] if parent_port else None
        self._update_trunk_status_and_subports(context, payload.resource_id,
                                               host_id, subport_ids, owner,
                                               binding_profile=profile)

    def _update_trunk_status_and_subports(self, context, trunk_id,
                                          trunk_host, subport_ids, owner,
                                          binding_profile=None):
        # Set to BUILD status to indicate there's a change.
        self._safe_update_trunk_status(
            context, trunk_id, trunk_consts.TRUNK_BUILD_STATUS)
        updated_ports = []
        op = 'bind' if trunk_host else 'unbind'
        for subport_id in subport_ids:
            try:
                updated_port = self._handle_subport_binding(
                    context, subport_id, trunk_id, trunk_host, owner,
                    binding_profile=binding_profile)
                updated_ports.append(updated_port)
            except trunk_exc.SubPortBindingError as e:
                LOG.error("Failed to %(op)s subport: %(err)s",
                          {'op': op, 'err': e})
            except Exception as e:
                msg = ("Failed to %(op)s subport port %(port)s on trunk "
                       "%(trunk)s: %(exc)s")
                LOG.error(msg, {'op': op, 'port': subport_id,
                                'trunk': trunk_id, 'exc': e})

        self._update_trunk_status(context, trunk_id, trunk_host,
                                  subport_ids, updated_ports)

    def _update_trunk_status(self, plugin_context, trunk_id,
                             trunk_host, subport_ids, updated_ports):
        """Update the trunk port status after updating subports.

           The trunk's status depends on the state of the parent port,
           whether subports are present, the status of the subports,
           and what the operation was (bind or unbind).

           :param plugin_context: The plugin's DB context
           :param trunk_id: The trunk ID that the given port belongs to
           :param trunk_host: The value provided for bind/unbind
           :param subport_ids: The list of subport IDs being bound/unbound
           :param updated_ports: The result of the port bind/unbind
        """
        trunk = self.trunk_plugin.get_trunk(plugin_context, trunk_id)
        parent_port = self.plugin.get_port(plugin_context, trunk['port_id'])
        # Get any other ports that belong to the trunk.
        filters = {'id': [subport['port_id']
                          for subport in trunk['sub_ports']
                          if subport['port_id'] not in subport_ids]}
        subports = self.plugin.get_ports(plugin_context, filters)
        # If we were binding, then we need to include the list of
        # subports being added.
        if trunk_host:
            for subport_id in subport_ids:
                for port in updated_ports:
                    if port['id'] == subport_id:
                        subports.append(port)
                        break
                else:
                    # Operation didn't complete, so add an
                    # entry reflecting the failure.
                    subports.append(
                        {'id': subport_id,
                         portbindings.VIF_TYPE:
                             portbindings.VIF_TYPE_UNBOUND})

        subports_bound_state = set([self._is_port_bound(subport)
                                   for subport in subports])
        if self._is_port_bound(parent_port):
            if all(subports_bound_state):
                status = trunk_consts.TRUNK_ACTIVE_STATUS
            elif True not in subports_bound_state:
                status = trunk_consts.TRUNK_ERROR_STATUS
            elif any(subports_bound_state):
                status = trunk_consts.TRUNK_DEGRADED_STATUS
        else:
            status = trunk_consts.TRUNK_DOWN_STATUS
        self._safe_update_trunk_status(
            plugin_context, trunk_id, status)

    def _get_subport_segmentation_id(self, context):
        subport_id = context.current['id']
        subport_db = self.plugin._get_port(context._plugin_context, subport_id)
        return (subport_db.sub_port.segmentation_id if subport_db.sub_port
                else None)

    def _get_trunk_for_subport(self, plugin_context, subport_id):
        subport_db = self.plugin._get_port(plugin_context, subport_id)
        return self.trunk_plugin.get_trunk(plugin_context,
                                           subport_db.sub_port.trunk_id)

    def _get_baremetal_topology(self, plugin_context, port):
        """Return topology information for a port of vnic_type baremetal.

        :param port : Port instance
        :returns: Tuple of topology information

        Get the topology information relevant to a port that has a
        vnic_type of baremetal. Topology is stored in the binding:profile
        member of the port object, using the local_link_infomration list.

        If there is more than one entry in the local_link_information list,
        then the port corresponds to a VPC. In this case, properties that
        should be the same for both entries are checked (e.g. static_path).
        Some properties, such as port_id and switch_id, are allowed to be
        different (and in the case of VPC should be different).

        Currently returns a tuple with the format:

            (static_path, interfaces, physical_network, physical_domain)

        where:
            static_path: DN to use for static path
            interfaces: list of tuples, where each tuple has a string
                        for the leaf interface name and a string for the
                        leaf interface's MAC address
            physical_network: physical network that the interfaces belong to
            physical_domain: physical domain that the interfaces belong to

        If the topology information is invalid, a tuple of None values
        is  returned instead.
        """
        interfaces = []
        static_path = None
        physical_domain = None
        physical_network = None
        lli_list = port.get(portbindings.PROFILE, {}).get(LL_INFO, [])
        for lli_idx in range(len(lli_list)):
            # 2 entries is VPC, one is single link. Others are
            # invalid.
            if lli_idx > 1:
                LOG.error("Invalid topology: port %(port)s has more than "
                          "two elements in the binding:profile's "
                          "local_link_information array.",
                          {'port': port['id']})
                return (None, None, None, None)
            lli = lli_list[lli_idx]
            mac = lli.get('switch_id')
            interface = lli.get('port_id')
            switch_info = lli.get('switch_info', '')
            # switch_info must be a string of a comma-separated
            # key-value pairs in order to be valid.
            info_dict = {}
            for kv_pair in switch_info.split(","):
                if ":" in kv_pair:
                    key, value = kv_pair.split(':', 1)
                    info_dict[key] = value
            if info_dict.get('apic_dn'):
                dn = info_dict['apic_dn']
                # If it's a VPC, the static paths should match.
                if static_path and dn != static_path:
                    LOG.error("Invalid topology: port %(port)s has "
                              "inconsistently configured switch_info inside "
                              "the binding:profile's link_local_information "
                              "elements [apic_dn's: %(dn1)s:%(dn2)s]. The "
                              "switch_info field must be identical for all "
                              "ports used within a portgroup for a baremetal "
                              "VNIC.", {'port': port['id'],
                               'dn1': static_path, 'dn2': dn})
                    return (None, None, None, None)
                static_path = dn
                if mac or interface:
                    interfaces.append((interface, mac))
            if info_dict.get(api.PHYSICAL_NETWORK):
                baremetal_physnet = info_dict[api.PHYSICAL_NETWORK]
                # If it's a VPC, physical_networks should match.
                if physical_network and baremetal_physnet != physical_network:
                    LOG.error("Invalid topology: port %(port)s has "
                              "inconsistently configured switch_info inside "
                              "the binding:profile's link_local_information "
                              "elements [physical_network: %(pn1)s:%(pn2)s]. "
                              "The switch_info field must be identical for "
                              "all ports used within a portgroup for a "
                              "baremetal VNIC.",
                              {'port': port['id'],
                               'pn1': physical_network,
                               'pn2': baremetal_physnet})
                    return (None, None, None, None)
                physical_network = baremetal_physnet
            if info_dict.get('physical_domain'):
                pd = info_dict['physical_domain']
                # If it's a VPC, physical_domains should match.
                if physical_domain and pd != physical_domain:
                    LOG.error("Invalid topology: port %(port)s has "
                              "inconsistently configured switch_info inside "
                              "the binding:profile's link_local_information "
                              "elements [physical_domain: %(pd1)s:%(pd2)s]. "
                              "The switch_info field must be identical for "
                              "all ports used within a portgroup for a "
                              "baremetal VNIC.",
                              {'port': port['id'],
                               'pd1': physical_domain,
                               'pn2': pd})
                    return (None, None, None, None)
                physical_domain = pd

        # We at least need the static path and physical_network
        if not static_path or not physical_network:
            return (None, None, None, None)

        return (static_path, interfaces, physical_network, physical_domain)

    def _update_static_path(self, port_context, host=None, segment=None,
                            remove=False):
        host = host or port_context.host
        segment = segment or port_context.bottom_bound_segment
        session = port_context._plugin_context.session

        if not segment:
            LOG.debug('Port %s is not bound to any segment',
                      port_context.current['id'])
            return

        del_subnet = []
        if port_context.current and port_context.original:
            # We are in a port update.
            # If a subnet is deleted on this bound port, we need
            # evaluate if we need to remove the corresponding
            # IProfile.
            curr = [s['subnet_id']
                for s in port_context.current['fixed_ips']]
            orig = [s['subnet_id']
                for s in port_context.original['fixed_ips']]
            del_subnet = list(set(orig) - set(curr))

        if remove or del_subnet:
            # check if there are any other ports from this network on the host
            query = BAKERY(lambda s: s.query(
                models.PortBindingLevel))
            query += lambda q: q.filter_by(
                host=sa.bindparam('host'),
                segment_id=sa.bindparam('segment_id'))
            query += lambda q: q.filter(
                models.PortBindingLevel.port_id != sa.bindparam('port_id'))
            exist = query(session).params(
                host=host,
                segment_id=segment['id'],
                port_id=port_context.current['id']).all()

            if remove and exist:
                # We are removing a bound port, but there is at least
                # one other port on this host so don't remove the
                # IProfile.
                return

            if del_subnet and exist:
                # Subnet is being removed from the port, if there is
                # at least one other port with the subnet on the host -
                # Leave the IProfile for this AF intact.
                host_ports = [p['port_id'] for p in exist]
                query = BAKERY(lambda s: s.query(
                    models_v2.Port))
                query += lambda q: q.filter(
                    models_v2.Port.id.in_(sa.bindparam(
                        'host_ports', expanding=True)))
                query += lambda q: q.filter(
                    models_v2.Port.fixed_ips.any(
                        models_v2.IPAllocation.subnet_id.in_(
                            sa.bindparam('del_subnet', expanding=True))))
                if query(session).params(host_ports=host_ports,
                    del_subnet=del_subnet).all():
                    return

        static_ports = self._get_static_ports(port_context._plugin_context,
                                              host, segment,
                                              port_context=port_context)
        for static_port in static_ports:
            if self._is_svi(port_context.network.current):
                l3out, _, _ = self._get_aim_external_objects(
                    port_context.network.current)
                self._update_static_path_for_svi(
                    session, port_context._plugin_context,
                    port_context.network.current, l3out,
                    static_port, remove=remove, del_subnet=del_subnet)
            else:
                self._update_static_path_for_network(
                    session, port_context.network.current,
                    static_port, remove=remove)

    def _release_dynamic_segment(self, port_context, use_original=False):
        top = (port_context.original_top_bound_segment if use_original
               else port_context.top_bound_segment)
        btm = (port_context.original_bottom_bound_segment if use_original
               else port_context.bottom_bound_segment)
        if (top and btm and top != btm and
            top[api.NETWORK_TYPE] in SUPPORTED_HPB_SEGMENT_TYPES and
            self._is_supported_non_opflex_type(btm[api.NETWORK_TYPE])):

            # if there are no other ports bound to segment, release the segment
            query = BAKERY(lambda s: s.query(
                models.PortBindingLevel))
            query += lambda q: q.filter_by(
                segment_id=sa.bindparam('segment_id'))
            query += lambda q: q.filter(
                models.PortBindingLevel.port_id != sa.bindparam('port_id'))
            ports = query(port_context._plugin_context.session).params(
                segment_id=btm[api.ID],
                port_id=port_context.current['id']).first()

            if not ports:
                LOG.info('Releasing dynamic-segment %(s)s for port %(p)s',
                         {'s': btm, 'p': port_context.current['id']})
                port_context.release_dynamic_segment(btm[api.ID])

    # public interface for aim_mapping GBP policy driver
    def associate_domain(self, port_context):
        if self._is_port_bound(port_context.current):
            if self._use_static_path(port_context.bottom_bound_segment):
                self._associate_domain(port_context, is_vmm=False)
            elif (port_context.bottom_bound_segment and
                  self._is_opflex_type(
                        port_context.bottom_bound_segment[api.NETWORK_TYPE])):
                self._associate_domain(port_context, is_vmm=True)

    def _skip_domain_processing(self, port_context):
        ext_net = port_context.network.current
        # skip domain processing if it's not managed by us, or
        # for external networks with NAT (FIPs or SNAT),
        if not ext_net:
            return True
        if ext_net[external_net.EXTERNAL] is True:
            _, _, ns = self._get_aim_nat_strategy(ext_net)
            if not isinstance(ns, nat_strategy.NoNatStrategy):
                return True
        return False

    def _associate_domain(self, port_context, is_vmm=True):
        if self._is_svi(port_context.network.current):
            return
        port = port_context.current
        session = port_context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        if self._skip_domain_processing(port_context):
            return
        ptg = None
        # TODO(kentwu): remove this coupling with policy driver if possible
        if self.gbp_driver:
            ptg, pt = self.gbp_driver._port_id_to_ptg(
                port_context._plugin_context, port['id'])
        if ptg:
            epg = self.gbp_driver._aim_endpoint_group(session, ptg)
        else:
            mapping = self._get_network_mapping(session, port['network_id'])
            epg = self._get_network_epg(mapping)
        aim_epg = self.aim.get(aim_ctx, epg)
        # Verify that the aim_epg is present before proceeding further.
        if not aim_epg:
            return
        host_id = port[portbindings.HOST_ID]
        aim_hd_mappings = (self.aim.find(aim_ctx,
                                         aim_infra.HostDomainMappingV2,
                                         host_name=host_id) or
                           self.aim.find(aim_ctx,
                                         aim_infra.HostDomainMappingV2,
                                         host_name=DEFAULT_HOST_DOMAIN))
        domains = []
        try:
            if is_vmm:
                # Get all the openstack VMM domains. We either
                # get domains from a lookup of the HostDomainMappingV2
                # table, or we get all the applicable VMM domains
                # found in AIM. We then apply these to the EPG.
                if aim_hd_mappings:
                    domains = [{'type': mapping.domain_type,
                                'name': mapping.domain_name}
                               for mapping in aim_hd_mappings
                               if mapping.domain_type in ['OpenStack']]
                if not domains:
                    vmms, phys = self.get_aim_domains(aim_ctx)
                    self.aim.update(aim_ctx, epg,
                                    vmm_domains=vmms)
                else:
                    vmms = aim_epg.vmm_domains[:]
                    for domain in domains:
                        if domain not in aim_epg.vmm_domains:
                            aim_epg.vmm_domains.append(domain)
                    if vmms != aim_epg.vmm_domains:
                        vmms = aim_epg.vmm_domains
                        self.aim.update(aim_ctx, epg, vmm_domains=vmms)
            else:
                # For baremetal VNIC types, there may be additional topology
                # information in the binding:profile from Ironic. This may
                # include the PhysDom name in ACI, which can be used to
                # disambiguate interfaces on the same host through matching
                # by PhysDom.
                if self._is_baremetal_vnic_type(port):
                    _, _, _, physdom = self._get_baremetal_topology(
                        port_context._plugin_context, port)
                    if physdom:
                        aim_hd_mappings = [aim_infra.HostDomainMappingV2(
                                domain_type='PhysDom', domain_name=physdom,
                                host_name=host_id)]
                # Get all the Physical domains. We either get domains
                # from a lookup of the HostDomainMappingV2
                # table, or we get all the applicable Physical
                # domains found in AIM. We then apply these to the EPG.
                if aim_hd_mappings:
                    domains = [{'name': mapping.domain_name}
                               for mapping in aim_hd_mappings
                               if mapping.domain_type in ['PhysDom']]
                if not domains:
                    vmms, phys = self.get_aim_domains(aim_ctx)
                    self.aim.update(aim_ctx, epg,
                                    physical_domains=phys)
                else:
                    phys = aim_epg.physical_domains[:]
                    for domain in domains:
                        if domain not in aim_epg.physical_domains:
                            aim_epg.physical_domains.append(domain)
                    if phys != aim_epg.physical_domains:
                        phys = aim_epg.physical_domains
                        self.aim.update(aim_ctx, epg,
                                        physical_domains=phys)
        # this could be caused by concurrent transactions
        except db_exc.DBDuplicateEntry as e:
            LOG.debug(e)

    # public interface for aim_mapping GBP policy driver also
    def disassociate_domain(self, port_context, use_original=False):
        if self._is_svi(port_context.network.current):
            return

        btm = (port_context.original_bottom_bound_segment if use_original
               else port_context.bottom_bound_segment)
        if not btm:
            return
        port = port_context.current
        if (self._is_opflex_type(btm[api.NETWORK_TYPE]) or
                self._is_supported_non_opflex_type(btm[api.NETWORK_TYPE])):
            if self._skip_domain_processing(port_context):
                return
            host_id = (port_context.original_host if use_original
                       else port_context.host)
            session = port_context._plugin_context.session
            aim_ctx = aim_context.AimContext(session)
            aim_hd_mappings = self.aim.find(aim_ctx,
                                            aim_infra.HostDomainMappingV2,
                                            host_name=host_id)
            # For baremetal VNIC types, there may be additional topology
            # information in the binding:profile from Ironic. This may
            # include the PhysDom name in ACI, which can be used to
            # disambiguate interfaces on the same host through matching
            # by PhysDom.
            if self._is_baremetal_vnic_type(port):
                _, _, _, physdom = self._get_baremetal_topology(
                    port_context._plugin_context, port)
                if physdom:
                    aim_hd_mappings = [aim_infra.HostDomainMappingV2(
                            domain_type='PhysDom', domain_name=physdom,
                            host_name=host_id)]
            if not aim_hd_mappings:
                return

            if self._is_opflex_type(btm[api.NETWORK_TYPE]):
                domain_type = 'OpenStack'
            else:
                domain_type = 'PhysDom'

            domains = []
            hd_mappings = []
            for mapping in aim_hd_mappings:
                d_type = mapping.domain_type
                if d_type == domain_type and mapping.domain_name:
                    domains.append(mapping.domain_name)
                    hd_mappings.extend(self.aim.find(aim_ctx,
                        aim_infra.HostDomainMappingV2,
                        domain_name=mapping.domain_name,
                        domain_type=d_type))
            if not domains:
                return
            hosts = [x.host_name
                     for x in hd_mappings
                     if x.host_name != DEFAULT_HOST_DOMAIN]
            ptg = None
            if self.gbp_driver:
                ptg, pt = self.gbp_driver._port_id_to_ptg(
                    port_context._plugin_context, port['id'])

            def _bound_port_query(session, port, hosts=None):
                query = BAKERY(lambda s: s.query(
                    models.PortBindingLevel))
                query += lambda q: q.join(
                    models_v2.Port, models_v2.Port.id ==
                    models.PortBindingLevel.port_id)
                if hosts:
                    query += lambda q: q.filter(
                        models.PortBindingLevel.host.in_(
                            sa.bindparam('hosts', expanding=True)))
                query += lambda q: q.filter(
                    models.PortBindingLevel.port_id != sa.bindparam('port_id'))
                ports = query(session).params(
                    hosts=hosts,
                    port_id=port['id'])
                return ports

            if ptg:
                # if there are no other ports under this PTG bound to those
                # hosts under this vmm, release the domain
                bound_ports = _bound_port_query(session, port, hosts=hosts)
                bound_ports = [x['port_id'] for x in bound_ports]
                ptg_ports = self.gbp_driver.get_ptg_port_ids(
                    port_context._plugin_context, ptg)
                ports = set(bound_ports).intersection(ptg_ports)
                if ports:
                    return
                epg = self.gbp_driver._aim_endpoint_group(session, ptg)
            else:
                # if there are no other ports under this network bound to those
                # hosts under this vmm, release the domain
                ports = _bound_port_query(session, port, hosts=hosts)
                if ports.first():
                    return
                mapping = self._get_network_mapping(
                    session, port['network_id'])
                epg = self._get_network_epg(mapping)
            aim_epg = self.aim.get(aim_ctx, epg)
            try:
                if self._is_opflex_type(btm[api.NETWORK_TYPE]):
                    vmms = aim_epg.vmm_domains[:]
                    for domain in domains:
                        mapping = {'type': domain_type,
                                   'name': domain}
                        if mapping in aim_epg.vmm_domains:
                            aim_epg.vmm_domains.remove(mapping)
                    if vmms != aim_epg.vmm_domains:
                        vmms = aim_epg.vmm_domains
                        self.aim.update(aim_ctx, epg,
                                        vmm_domains=vmms)
                else:
                    phys = aim_epg.physical_domains[:]
                    for domain in domains:
                        mapping = {'name': domain}
                        if mapping in aim_epg.physical_domains:
                            aim_epg.physical_domains.remove(mapping)
                    if phys != aim_epg.physical_domains:
                        phys = aim_epg.physical_domains
                        self.aim.update(aim_ctx, epg,
                                        physical_domains=phys)
            # this could be caused by concurrent transactions
            except db_exc.DBDuplicateEntry as e:
                LOG.debug(e)
            LOG.info('Releasing domain %(d)s for port %(p)s',
                     {'d': domain, 'p': port['id']})

    def _get_non_opflex_segments_on_host(self, context, host):
        session = context.session

        query = BAKERY(lambda s: s.query(
            segments_model.NetworkSegment))
        query += lambda q: q.join(
            models.PortBindingLevel,
            models.PortBindingLevel.segment_id ==
            segments_model.NetworkSegment.id)
        query += lambda q: q.filter(
            models.PortBindingLevel.host == sa.bindparam('host'))
        segments = query(session).params(
            host=host).all()

        net_ids = set([])
        result = []
        for seg in segments:
            if (self._is_supported_non_opflex_type(seg[api.NETWORK_TYPE]) and
                    seg.network_id not in net_ids):
                net = self.plugin.get_network(context, seg.network_id)
                result.append((net, segments_db._make_segment_dict(seg)))
                net_ids.add(seg.network_id)
        return result

    def _notify_existing_vm_ports(self, plugin_context, ext_net_id):
        session = plugin_context.session
        router_ids = self._get_router_ids_from_exteral_net(
                                        session, ext_net_id)
        sub_ids = self._get_router_interface_subnets(
                                        session, router_ids)
        affected_port_ids = self._get_compute_dhcp_ports_in_subnets(
                                        session, sub_ids)
        self._add_postcommit_port_notifications(
            plugin_context, affected_port_ids)

    def _get_router_ids_from_exteral_net(self, session, network_id):
        query = BAKERY(lambda s: s.query(
            l3_db.RouterPort.router_id))
        query += lambda q: q.join(
            models_v2.Port,
            l3_db.RouterPort.port_id == models_v2.Port.id)
        query += lambda q: q.filter(
            l3_db.RouterPort.port_type == n_constants.DEVICE_OWNER_ROUTER_GW)
        query += lambda q: q.filter(
            models_v2.Port.network_id == sa.bindparam('network_id'))
        router_ids = query(session).params(
            network_id=network_id)

        return [r[0] for r in router_ids]

    def _get_router_interface_subnets(self, session, router_ids):
        if not router_ids:
            return []

        query = BAKERY(lambda s: s.query(
            models_v2.IPAllocation.subnet_id))
        query += lambda q: q.join(
            l3_db.RouterPort,
            l3_db.RouterPort.port_id == models_v2.IPAllocation.port_id)
        query += lambda q: q.filter(
            l3_db.RouterPort.router_id.in_(
                sa.bindparam('router_ids', expanding=True)))
        query += lambda q: q.distinct()
        subnet_ids = query(session).params(
            router_ids=router_ids)

        return [s[0] for s in subnet_ids]

    def _get_compute_dhcp_ports_in_subnets(self, session, subnet_ids):
        if not subnet_ids:
            return []

        query = BAKERY(lambda s: s.query(
            models_v2.IPAllocation.port_id))
        query += lambda q: q.join(
            models_v2.Port,
            models_v2.Port.id == models_v2.IPAllocation.port_id)
        query += lambda q: q.filter(
            models_v2.IPAllocation.subnet_id.in_(
                sa.bindparam('subnet_ids', expanding=True)))
        query += lambda q: q.filter(
            sa.or_(models_v2.Port.device_owner.startswith(
                   n_constants.DEVICE_OWNER_COMPUTE_PREFIX),
                   models_v2.Port.device_owner ==
                   n_constants.DEVICE_OWNER_DHCP))
        port_ids = query(session).params(
            subnet_ids=subnet_ids).all()

        return [p[0] for p in port_ids]

    def _get_non_router_ports_in_networks(self, session, network_ids):
        if not network_ids:
            return []

        query = BAKERY(lambda s: s.query(
            models_v2.Port.id))
        query += lambda q: q.filter(
            models_v2.Port.network_id.in_(
                sa.bindparam('network_ids', expanding=True)))
        query += lambda q: q.filter(
            models_v2.Port.device_owner !=
            n_constants.DEVICE_OWNER_ROUTER_INTF)
        port_ids = query(session).params(
            network_ids=list(network_ids)).all()

        return [p[0] for p in port_ids]

    def _get_svi_default_external_epg(self, network):
        if not network.get(cisco_apic.SVI):
            return None
        ext_net_dn = network.get(cisco_apic.DIST_NAMES, {}).get(
            cisco_apic.EXTERNAL_NETWORK)
        return aim_resource.ExternalNetwork.from_dn(ext_net_dn)

    def _get_svi_net_l3out(self, network):
        aim_ext_net = self._get_svi_default_external_epg(network)
        if not aim_ext_net:
            return None
        return aim_resource.L3Outside(
            tenant_name=aim_ext_net.tenant_name, name=aim_ext_net.l3out_name)

    # Called by sfc_driver.
    def _get_bd_by_network_id(self, session, network_id):
        net_mapping = self._get_network_mapping(session, network_id)
        return self._get_network_bd(net_mapping)

    # Called by sfc_driver and its unit tests.
    def _get_epg_by_network_id(self, session, network_id):
        net_mapping = self._get_network_mapping(session, network_id)
        return self._get_network_epg(net_mapping)

    # Called by sfc_driver.
    def _get_vrf_by_network(self, session, network):
        vrf_dn = network.get(cisco_apic.DIST_NAMES, {}).get(cisco_apic.VRF)
        if vrf_dn:
            return aim_resource.VRF.from_dn(vrf_dn)
        # Pre-existing EXT NET.
        l3out = self._get_svi_net_l3out(network)
        if l3out:
            aim_ctx = aim_context.AimContext(db_session=session)
            l3out = self.aim.get(aim_ctx, l3out)
            # TODO(ivar): VRF could be in tenant common, there's no way of
            # knowing it until we put the VRF in the mapping.
            return aim_resource.VRF(tenant_name=l3out.tenant_name,
                                    name=l3out.vrf_name)
        net_mapping = self._get_network_mapping(session, network['id'])
        return self._get_network_vrf(net_mapping)

    # Called by sfc_driver.
    def _get_port_static_path_info(self, plugin_context, port):
        port_id = port['id']
        path = encap = host = None
        if self._is_port_bound(port):
            port_context = self.make_port_context(plugin_context, port_id)
            host = port_context.host
            segment = port_context.bottom_bound_segment
            static_ports = self._get_static_ports(plugin_context,
                                                  host, segment)
            if not static_ports:
                LOG.warning("No host link information found for host %s ",
                            host)
                return None, None, None
            # REVISIT(ivar): We should return a list for all available host
            # links.
            path = static_ports[0].link.path
            encap = static_ports[0].encap
            host = static_ports[0].link.host_name
        return path, encap, host

    # Called by sfc_driver.
    def _get_port_unique_domain(self, plugin_context, port):
        """Get port domain

        Returns a unique domain (either virtual or physical) in which the
        specific endpoint is placed. If the domain cannot be uniquely
        identified returns None

        :param plugin_context:
        :param port:
        :return:
        """
        session = plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        if self._is_port_bound(port):
            host_id = port[portbindings.HOST_ID]
            dom_mappings = (self.aim.find(aim_ctx,
                                          aim_infra.HostDomainMappingV2,
                                          host_name=host_id) or
                            self.aim.find(aim_ctx,
                                          aim_infra.HostDomainMappingV2,
                                          host_name=DEFAULT_HOST_DOMAIN))

            if not dom_mappings:
                # If there's no direct mapping, get all the existing domains in
                # AIM.
                vmms, phys = self.get_aim_domains(aim_ctx)
                # Since supporting workflows with VMM domains is WIP, this is
                # left as is for now even if it not yet relevant.
                for vmm in vmms:
                    dom_mappings.append(
                        aim_infra.HostDomainMappingV2(
                            domain_type=vmm['type'], domain_name=vmm['name'],
                            host_name=DEFAULT_HOST_DOMAIN))
                for phy in phys:
                    dom_mappings.append(
                        aim_infra.HostDomainMappingV2(
                            domain_type='PhysDom', domain_name=phy['name'],
                            host_name=DEFAULT_HOST_DOMAIN))
            if not dom_mappings:
                return None, None

            port_context = self.make_port_context(plugin_context, port['id'])
            if self._use_static_path(port_context.bottom_bound_segment):
                phys = [{'domain_name': x.domain_name,
                         'domain_type': x.domain_type}
                        for x in dom_mappings
                        if x.domain_type == 'PhysDom']
                # Ensure that we only have one PhysDom
                if len(phys) == 1:
                    return phys[0]['domain_type'], phys[0]['domain_name']
            # We do not support the workflow on VMM Domains yet - WIP.
        return None, None

    # Similar to ML2's get_bound_port_context, but does not try to
    # bind port if unbound. Called internally and by GBP policy
    # driver.
    #
    # REVISIT: Callers often only need the bottom bound segment and
    # maybe the host, so consider a simpler alternative.
    def make_port_context(self, plugin_context, port_id):
        # REVISIT: Use CONTEXT_READER once upstream ML2 get_network no
        # longer uses a write transaction. Or call get_network outside
        # of a transaction.
        with db_api.CONTEXT_WRITER.using(plugin_context):
            port_db = self.plugin._get_port(plugin_context, port_id)
            port = self.plugin._make_port_dict(port_db)
            network = self.plugin.get_network(
                plugin_context, port_db.network_id)
            host = (port_db.port_bindings[0].host if port_db.port_bindings
                    else None)
            levels = (n_db.get_binding_level_objs(plugin_context,
                port_id, host) if host else None)
            return ml2_context.PortContext(
                self.plugin, plugin_context, port, network,
                port_db.port_bindings[0] if port_db.port_bindings else None,
                levels)

    def _add_network_mapping_and_notify(self, context, network_id, bd, epg,
                                        vrf):
        with db_api.CONTEXT_WRITER.using(context) as session:
            self._add_network_mapping(session, network_id, bd, epg, vrf)
            registry.publish(aim_cst.GBP_NETWORK_VRF, events.PRECOMMIT_UPDATE,
                             self,
                             payload=events.DBEventPayload(
                                 context,
                                 metadata={
                                     'network_id': network_id}))

    def _set_network_epg_and_notify(self, context, mapping, epg):
        with db_api.CONTEXT_WRITER.using(context):
            self._set_network_epg(mapping, epg)
            registry.publish(aim_cst.GBP_NETWORK_VRF, events.PRECOMMIT_UPDATE,
                             self,
                             payload=events.DBEventPayload(
                                 context,
                                 metadata={
                                     'network_id': mapping.network_id}))

    def _set_network_vrf_and_notify(self, context, mapping, vrf):
        with db_api.CONTEXT_WRITER.using(context):
            self._set_network_vrf(mapping, vrf)
            registry.publish(aim_cst.GBP_NETWORK_VRF, events.PRECOMMIT_UPDATE,
                             self,
                             payload=events.DBEventPayload(
                                 context,
                                 metadata={
                                     'network_id': mapping.network_id}))

    def validate_aim_mapping(self, mgr):
        mgr.register_aim_resource_class(aim_infra.HostDomainMappingV2)
        mgr.register_aim_resource_class(aim_resource.ApplicationProfile)
        mgr.register_aim_resource_class(aim_resource.BridgeDomain)
        mgr.register_aim_resource_class(aim_resource.Contract)
        mgr.register_aim_resource_class(aim_resource.ContractSubject)
        mgr.register_aim_resource_class(aim_resource.EndpointGroup)
        mgr.register_aim_resource_class(aim_resource.ExternalNetwork)
        mgr.register_aim_resource_class(
            aim_resource.ExternalNetworkProvidedContract)
        mgr.register_aim_resource_class(
            aim_resource.ExternalNetworkConsumedContract)
        mgr.register_aim_resource_class(aim_resource.ExternalSubnet)
        mgr.register_aim_resource_class(aim_resource.Filter)
        mgr.register_aim_resource_class(aim_resource.FilterEntry)
        mgr.register_aim_resource_class(aim_resource.L3Outside)
        mgr.register_aim_resource_class(aim_resource.PhysicalDomain)
        mgr.register_aim_resource_class(aim_resource.SecurityGroup)
        mgr.register_aim_resource_class(aim_resource.SecurityGroupRule)
        mgr.register_aim_resource_class(aim_resource.SecurityGroupSubject)
        mgr.register_aim_resource_class(aim_resource.Subnet)
        mgr.register_aim_resource_class(aim_resource.EPGSubnet)
        mgr.register_aim_resource_class(aim_resource.Tenant)
        mgr.register_aim_resource_class(aim_resource.VMMDomain)
        mgr.register_aim_resource_class(aim_resource.VRF)
        mgr.register_aim_resource_class(aim_resource.SpanVsourceGroup)
        mgr.register_aim_resource_class(aim_resource.SpanVdestGroup)
        mgr.register_aim_resource_class(aim_resource.SpanVsource)
        mgr.register_aim_resource_class(aim_resource.SpanVdest)
        mgr.register_aim_resource_class(aim_resource.SpanVepgSummary)
        mgr.register_aim_resource_class(aim_resource.SpanSpanlbl)
        mgr.register_aim_resource_class(aim_resource.InfraAccBundleGroup)

        # Copy common Tenant from actual to expected AIM store.
        for tenant in mgr.aim_mgr.find(
            mgr.actual_aim_ctx,
            aim_resource.Tenant,
            name=COMMON_TENANT_NAME):
            mgr.aim_mgr.create(mgr.expected_aim_ctx, tenant)

        # Copy AIM resources that are managed via aimctl from actual
        # to expected AIM stores.
        for resource_class in [aim_infra.HostDomainMappingV2,
                               aim_resource.PhysicalDomain,
                               aim_resource.VMMDomain]:
            for resource in mgr.actual_aim_resources(resource_class):
                mgr.aim_mgr.create(mgr.expected_aim_ctx, resource)

        # Copy pre-existing AIM resources for external networking from
        # actual to expected AIM stores.
        for resource_class in [aim_resource.ExternalNetwork,
                               aim_resource.ExternalSubnet,
                               aim_resource.L3Outside,
                               aim_resource.VRF]:
            for resource in mgr.actual_aim_resources(resource_class):
                if (resource.monitored and
                    mgr._should_validate_tenant(resource.tenant_name)):
                    mgr.aim_mgr.create(mgr.expected_aim_ctx, resource)

        # Register DB tables to be validated.
        mgr.register_db_instance_class(
            aim_lib_model.CloneL3Out, ['tenant_name', 'name'])
        mgr.register_db_instance_class(
            db.AddressScopeMapping, ['scope_id'])
        mgr.register_db_instance_class(
            db.NetworkMapping, ['network_id'])

        # Determine expected AIM resources and DB records for each
        # Neutron resource type. We stash a set identifying the
        # projects that have been processed so far in the validation
        # manager since this will be needed for both Neutron and GBP
        # resources.
        mgr._expected_projects = set()

        self._validate_static_resources(mgr)
        self._validate_address_scopes(mgr)
        self._validate_routers(mgr)
        self._validate_networks(mgr)
        self._validate_security_groups(mgr)
        self._validate_ports(mgr)
        self._validate_subnetpools(mgr)
        self._validate_floatingips(mgr)
        self._validate_port_bindings(mgr)

    # Note: The queries below are executed only once per run of the
    # validation CLI tool, but are baked in order to speed up unit
    # test execution, where they are called repeatedly.

    def _validate_static_resources(self, mgr):
        if not mgr._should_validate_tenant(COMMON_TENANT_NAME):
            return
        self._ensure_common_tenant(mgr.expected_aim_ctx)
        self._ensure_unrouted_vrf(mgr.expected_aim_ctx)
        self._ensure_any_filter(mgr.expected_aim_ctx)
        self._setup_default_arp_dhcp_security_group_rules(
            mgr.expected_aim_ctx)

    def _validate_address_scopes(self, mgr):
        if (not mgr._should_validate_neutron_resource("address_scope") and
            not mgr._should_validate_neutron_resource("network")):
            return
        owned_scopes_by_vrf = defaultdict(list)

        scopes_db = None
        query = BAKERY(lambda s: s.query(
            as_db.AddressScope))
        if mgr.tenant_scope:
            query += lambda q: q.filter(
                as_db.AddressScope.project_id.in_(
                    sa.bindparam('project_ids', expanding=True)))
            scopes_db = query(mgr.actual_session).params(
                    project_ids=list(mgr.tenant_ids))
        else:
            scopes_db = query(mgr.actual_session)
        for scope_db in scopes_db:
            self._expect_project(mgr, scope_db.project_id)
            mapping = scope_db.aim_mapping
            if mapping:
                mgr.expect_db_instance(mapping)
            else:
                vrf = self._map_address_scope(mgr.expected_session, scope_db)
                mapping = self._add_address_scope_mapping(
                    mgr.expected_session, scope_db.id, vrf, update_scope=False)
            vrf = self._get_address_scope_vrf(mapping)
            vrf.monitored = not mapping.vrf_owned
            vrf.display_name = (
                aim_utils.sanitize_display_name(scope_db.name)
                if mapping.vrf_owned else "")
            vrf.policy_enforcement_pref = 'enforced'
            mgr.expect_aim_resource(vrf, replace=True)
            if mapping.vrf_owned:
                scopes = owned_scopes_by_vrf[tuple(vrf.identity)]
                scopes.append(scope_db)
                # REVISIT: Fail if multiple scopes for same address family?
                if len(scopes) > 1:
                    scopes = sorted(scopes, key=lambda scope: scope.ip_version)
                    self._update_vrf_display_name(
                        mgr.expected_aim_ctx, vrf, scopes)

    def _validate_routers(self, mgr):
        if not mgr._should_validate_neutron_resource("router"):
            return
        router_dbs = {}
        ext_net_routers = defaultdict(list)

        query = BAKERY(lambda s: s.query(
            l3_db.Router))
        if mgr.tenant_scope:
            query += lambda q: q.filter(
                l3_db.Router.project_id.in_(
                    sa.bindparam('project_ids', expanding=True)))
            rtr_dbs = query(mgr.actual_session).params(
                project_ids=list(mgr.tenant_ids))
        else:
            rtr_dbs = query(mgr.actual_session)
        for router_db in rtr_dbs:
            self._expect_project(mgr, router_db.project_id)
            router_dbs[router_db.id] = router_db
            if router_db.gw_port_id:
                ext_net_routers[router_db.gw_port.network_id].append(
                    router_db.id)

            contract, subject = self._map_router(
                mgr.expected_session, router_db)
            dname = aim_utils.sanitize_display_name(router_db.name)

            contract.scope = "context"
            contract.display_name = dname
            contract.monitored = False
            mgr.expect_aim_resource(contract)

            subject.in_filters = []
            subject.out_filters = []
            subject.bi_filters = [self._any_filter_name]
            subject.service_graph_name = ''
            subject.in_service_graph_name = ''
            subject.out_service_graph_name = ''
            subject.display_name = dname
            subject.monitored = False
            mgr.expect_aim_resource(subject)

        return router_dbs, ext_net_routers

    def _get_routers(self, mgr):
        router_dbs = {}
        ext_net_routers = defaultdict(list)

        query = BAKERY(lambda s: s.query(
            l3_db.Router))
        for router_db in query(mgr.actual_session):
            router_dbs[router_db.id] = router_db
            if router_db.gw_port_id:
                ext_net_routers[router_db.gw_port.network_id].append(
                    router_db.id)
        return router_dbs, ext_net_routers

    def _validate_networks(self, mgr):
        if not mgr._should_validate_neutron_resource("network"):
            return
        router_dbs, ext_net_routers = self._get_routers(mgr)
        net_dbs = {}

        query = BAKERY(lambda s: s.query(
            models_v2.Network))
        query += lambda q: q.options(
            orm.joinedload('segments'))
        net_dbs = {net_db.id: net_db
            for net_db in query(mgr.actual_session)}

        router_ext_prov, router_ext_cons = self._get_router_ext_contracts(mgr)
        routed_nets = self._get_router_interface_info(mgr)
        network_vrfs, router_vrfs = self._determine_vrfs(
            mgr, net_dbs, routed_nets)
        self._validate_routed_vrfs(mgr, routed_nets, network_vrfs)

        for net_db in list(net_dbs.values()):
            if not net_db.aim_extension_mapping:
                self._missing_network_extension_mapping(mgr, net_db)
            self._expect_project(mgr, net_db.project_id)

            for subnet_db in net_db.subnets:
                if not subnet_db.aim_extension_mapping:
                    self._missing_subnet_extension_mapping(mgr, subnet_db)
                self._expect_project(mgr, subnet_db.project_id)

            for segment_db in net_db.segments:
                # REVISIT: Consider validating that physical_network
                # and segmentation_id values make sense for the
                # network_type, and possibly validate that there are
                # no conflicting segment allocations.
                if (segment_db.network_type not in
                    self.plugin.type_manager.drivers):
                    # REVISIT: For migration from non-APIC backends,
                    # change type to 'opflex'?
                    mgr.validation_failed(
                        "network %(net_id)s segment %(segment_id)s type "
                        "%(type)s is invalid" % {
                            'net_id': segment_db.network_id,
                            'segment_id': segment_db.id,
                            'type': segment_db.network_type})

            bd = None
            epg = None
            vrf = None
            if net_db.external:
                bd, epg, vrf = self._validate_external_network(
                    mgr, net_db, ext_net_routers, router_dbs, router_vrfs,
                    router_ext_prov, router_ext_cons)
            elif self._is_svi_db(net_db):
                mgr.validation_failed(
                    "SVI network validation not yet implemented")
            else:
                bd, epg, vrf = self._validate_normal_network(
                    mgr, net_db, network_vrfs, router_dbs, routed_nets)

            # Copy binding-related attributes from actual EPG to
            # expected EPG.
            #
            # REVISIT: Should compute expected values, but current
            # domain and static_path code needs significant
            # refactoring to enable re-use. The resulting static_paths
            # also may not be deterministic, at least in the SVI BGP
            # case. We therefore may need to validate that the actual
            # values are sensible rather than computing the expected
            # values.
            if epg:
                actual_epg = mgr.actual_aim_resource(epg)
                if actual_epg:
                    expected_epg = mgr.expected_aim_resource(epg)
                    expected_epg.vmm_domains = actual_epg.vmm_domains
                    expected_epg.physical_domains = actual_epg.physical_domains
                    expected_epg.static_paths = actual_epg.static_paths
                    # REVISIT: Move to ValidationManager, just before
                    # comparing actual and expected resources?
                    expected_epg.openstack_vmm_domain_names = [
                        d['name'] for d in expected_epg.vmm_domains
                        if d['type'] == 'OpenStack']
                    expected_epg.physical_domain_names = [
                        d['name'] for d in expected_epg.physical_domains]
                    expected_epg.qos_name = actual_epg.qos_name
                else:
                    # REVISIT: Force rebinding of ports using this
                    # EPG?
                    pass

            # Expect NetworkMapping record if applicable. Note that
            # when validation of SVI networks is implemented, there
            # will also be an ext_net resource, and when this is used,
            # the bd and epg are not used.
            if bd and epg and vrf:
                self._add_network_mapping(
                    mgr.expected_session, net_db.id, bd, epg, vrf,
                    update_network=False)
            elif bd or epg or vrf:
                mgr.validation_failed(
                    "Missing resource(s) needed to create expected "
                    "NetworkMapping for network %s - bd: %s, epg: %s, "
                    "vrf: %s" % (net_db.id, bd, epg, vrf))

    def _get_router_ext_contracts(self, mgr):
        # Get external contracts for routers.
        router_ext_prov = defaultdict(set)
        router_ext_cons = defaultdict(set)

        query = BAKERY(lambda s: s.query(
            extension_db.RouterExtensionContractDb))
        for contract in query(mgr.actual_session):
            if contract.provides:
                router_ext_prov[contract.router_id].add(contract.contract_name)
            else:
                router_ext_cons[contract.router_id].add(contract.contract_name)

        return router_ext_prov, router_ext_cons

    def _get_router_interface_info(self, mgr):
        # Find details of all router interfaces for each routed network.
        routed_nets = defaultdict(list)

        query = BAKERY(lambda s: s.query(
            l3_db.RouterPort.router_id,
            models_v2.IPAllocation.ip_address,
            models_v2.Subnet,
            db.AddressScopeMapping))
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.port_id == l3_db.RouterPort.port_id)
        query += lambda q: q.join(
            models_v2.Subnet,
            models_v2.Subnet.id == models_v2.IPAllocation.subnet_id)
        query += lambda q: q.outerjoin(
            models_v2.SubnetPool,
            models_v2.SubnetPool.id == models_v2.Subnet.subnetpool_id)
        query += lambda q: q.outerjoin(
            db.AddressScopeMapping,
            db.AddressScopeMapping.scope_id ==
            models_v2.SubnetPool.address_scope_id)
        query += lambda q: q.filter(
            l3_db.RouterPort.port_type == n_constants.DEVICE_OWNER_ROUTER_INTF)
        for intf in query(mgr.actual_session):
            intf = InterfaceValidationInfo._make(intf)
            routed_nets[intf.subnet.network_id].append(intf)

        return routed_nets

    def _determine_vrfs(self, mgr, net_dbs, routed_nets):
        # Determine VRFs for all scoped routed networks, as well as
        # unscoped topology information.
        network_vrfs = {}
        router_vrfs = defaultdict(dict)
        unscoped_net_router_ids = {}
        unscoped_router_net_ids = defaultdict(set)
        unscoped_net_dbs = {}
        shared_unscoped_net_ids = []
        for intfs in list(routed_nets.values()):
            net_id = None
            v4_scope_mapping = None
            v6_scope_mapping = None
            router_ids = set()
            for intf in intfs:
                router_ids.add(intf.router_id)
                if not net_id:
                    net_id = intf.subnet.network_id
                if intf.scope_mapping:
                    if intf.subnet.ip_version == 4:
                        if (v4_scope_mapping and
                            v4_scope_mapping != intf.scope_mapping):
                            mgr.validation_failed(
                                "inconsistent IPv4 scopes for network %s" %
                                intfs)
                        else:
                            v4_scope_mapping = intf.scope_mapping
                    elif intf.subnet.ip_version == 6:
                        if (v6_scope_mapping and
                            v6_scope_mapping != intf.scope_mapping):
                            mgr.validation_failed(
                                "inconsistent IPv6 scopes for network %s" %
                                intfs)
                        else:
                            v6_scope_mapping = intf.scope_mapping
            # REVISIT: If there is a v6 scope and no v4 scope, but
            # there are unscoped v4 subnets, should the unscoped
            # topology's default VRF be used instead? Or should
            # validation fail?
            scope_mapping = v4_scope_mapping or v6_scope_mapping
            if scope_mapping:
                vrf = self._get_address_scope_vrf(scope_mapping)
                network_vrfs[net_id] = vrf
                for router_id in router_ids:
                    router_vrfs[router_id][tuple(vrf.identity)] = vrf
            else:
                unscoped_net_router_ids[net_id] = router_ids
                for router_id in router_ids:
                    unscoped_router_net_ids[router_id].add(net_id)
                net_db = net_dbs[net_id]
                unscoped_net_dbs[net_id] = net_db
                if self._network_shared(net_db):
                    shared_unscoped_net_ids.append(intf.subnet.network_id)

        default_vrfs = set()

        def use_default_vrf(net_db):
            vrf = self._map_default_vrf(mgr.expected_session, net_db)
            key = tuple(vrf.identity)
            if key not in default_vrfs:
                default_vrfs.add(key)
                vrf.display_name = 'DefaultRoutedVRF'
                vrf.policy_enforcement_pref = 'enforced'
                vrf.monitored = False
                mgr.expect_aim_resource(vrf)
            network_vrfs[net_db.id] = vrf
            return vrf

        def expand_shared_topology(net_id, vrf):
            for router_id in unscoped_net_router_ids[net_id]:
                router_vrfs[router_id][tuple(vrf.identity)] = vrf
                for net_id in unscoped_router_net_ids[router_id]:
                    if net_id not in network_vrfs:
                        network_vrfs[net_id] = vrf
                        expand_shared_topology(net_id, vrf)

        # Process shared unscoped topologies.
        for net_id in shared_unscoped_net_ids:
            if net_id not in network_vrfs:
                vrf = use_default_vrf(unscoped_net_dbs[net_id])
                expand_shared_topology(net_id, vrf)

        # Process remaining (unshared) unscoped networks.
        for net_db in list(unscoped_net_dbs.values()):
            if net_db.id not in network_vrfs:
                vrf = use_default_vrf(net_db)
                for router_id in unscoped_net_router_ids[net_db.id]:
                    router_vrfs[router_id][tuple(vrf.identity)] = vrf

        return network_vrfs, router_vrfs

    def _validate_routed_vrfs(self, mgr, routed_nets, network_vrfs):
        vrf_subnets = defaultdict(list)
        for net_id, intfs in list(routed_nets.items()):
            vrf = network_vrfs[net_id]
            vrf_subnets[tuple(vrf.identity)] += [
                (intf.subnet.id, netaddr.IPNetwork(intf.subnet.cidr))
                for intf in intfs]
        for vrf_id, subnets in list(vrf_subnets.items()):
            subnets.sort(key=lambda s: s[1])
            for (id1, cidr1), (id2, cidr2) in zip(subnets[:-1], subnets[1:]):
                if id2 != id1 and cidr2 in cidr1:
                    vrf = aim_resource.VRF(
                        tenant_name=vrf_id[0], name=vrf_id[1])
                    mgr.validation_failed(
                        "overlapping routed subnets %(id1)s (%(cidr1)s) "
                        "and %(id2)s (%(cidr2)s) mapped to %(vrf)s" %
                        {'id1': id1, 'cidr1': cidr1,
                         'id2': id2, 'cidr2': cidr2,
                         'vrf': vrf})

    def _missing_network_extension_mapping(self, mgr, net_db):
        # Note that this is intended primarily to handle migration to
        # apic_aim, where the previous plugin and/or drivers did not
        # populate apic_aim's extension data. Migration of external
        # networks is supported through configuration of ACI
        # ExternalNetwork DNs, but other apic_aim-specific features
        # such as SVI do not apply to these migration use cases. After
        # migration, other attributes can be changed via the REST API
        # if needed.

        if not mgr.should_repair(
                "network %s missing extension data" % net_db.id):
            return

        ext_net_dn = None
        if net_db.external:
            ext_net_dn = cfg.CONF.ml2_apic_aim.migrate_ext_net_dns.get(
                net_db.id)
            if not ext_net_dn:
                mgr.validation_failed(
                    "missing extension data for external network %s and no "
                    "external network DN configured" % net_db.id)
            try:
                ext_net = aim_resource.ExternalNetwork.from_dn(ext_net_dn)
                ext_net = mgr.aim_mgr.get(mgr.expected_aim_ctx, ext_net)
                if not ext_net:
                    mgr.validation_failed(
                        "missing extension data for external network %(net)s "
                        "and configured external network DN '%(dn)s' does not "
                        "exist" % {'net': net_db.id, 'dn': ext_net_dn})
                    ext_net_dn = None
            except aim_exceptions.InvalidDNForAciResource:
                mgr.validation_failed(
                    "missing extension data for external network %(net)s and "
                    "configured external network DN '%(dn)s' is invalid" %
                    {'net': net_db.id, 'dn': ext_net_dn})
                ext_net_dn = None
        res_dict = {
            cisco_apic.EXTERNAL_NETWORK: ext_net_dn,
            cisco_apic.SVI: False,
            cisco_apic.BGP: False,
            cisco_apic.BGP_TYPE: 'default_export',
            cisco_apic.BGP_ASN: 0,
            cisco_apic.POLICY_ENFORCEMENT_PREF: 'unenforced',
            cisco_apic.NESTED_DOMAIN_NAME: '',
            cisco_apic.NESTED_DOMAIN_TYPE: '',
            cisco_apic.NESTED_DOMAIN_INFRA_VLAN: None,
            cisco_apic.NESTED_DOMAIN_SERVICE_VLAN: None,
            cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN: None,
            cisco_apic.MULTI_EXT_NETS: False,
        }
        if net_db.aim_mapping and net_db.aim_mapping.get(cisco_apic.BD):
            res_dict.update({cisco_apic.BD: net_db.aim_mapping[cisco_apic.BD]})
        if net_db.external:
            # REVISIT: These are typical values, but the ability to
            # specify them per-network via config could be useful in
            # certain migration use cases. The apic:external_cidrs
            # attribute is mutable, so can be fixed manually after
            # migration. The apic:nat_type attribute is immutable, so
            # using other values requires deleting and re-creating the
            # external network.
            res_dict[cisco_apic.NAT_TYPE] = 'distributed'
            res_dict[cisco_apic.EXTERNAL_CIDRS] = [aim_cst.IPV4_ANY_CIDR]
        self.set_network_extn_db(mgr.actual_session, net_db.id, res_dict)

    def _missing_subnet_extension_mapping(self, mgr, subnet_db):
        # Note that this is intended primarily to handle migration to
        # apic_aim, where the previous plugin and/or drivers did not
        # populate apic_aim's extension data. After migration, the
        # SNAT_HOST_POOL (but not the ACTIVE_ACTIVE_AAP) attribute can be
        # changed via the REST API if needed.

        if not mgr.should_repair(
                "subnet %s missing extension data" % subnet_db.id):
            return

        res_dict = {
            cisco_apic.SNAT_HOST_POOL: False,
            cisco_apic.ACTIVE_ACTIVE_AAP: False,
            cisco_apic.EPG_SUBNET: False
        }
        self.set_subnet_extn_db(mgr.actual_session, subnet_db.id, res_dict)

    def _validate_normal_network(self, mgr, net_db, network_vrfs, router_dbs,
                                 routed_nets):
        routed_vrf = network_vrfs.get(net_db.id)
        vrf = routed_vrf or self._map_unrouted_vrf()
        # Check for preexisting BDs
        preexisting_bd_dn = (net_db.aim_extension_mapping.bridge_domain_dn if
                             net_db.aim_extension_mapping and
                             net_db.aim_extension_mapping.bridge_domain_dn
                             else None)
        bd, epg = self._map_network(mgr.expected_session, net_db, vrf,
                                    preexisting_bd_dn)

        router_contract_names = set()
        for intf in routed_nets.get(net_db.id, []):
            # REVISIT: Refactor to share code.
            gw_ip = intf.ip_address
            router_db = router_dbs[intf.router_id]
            dname = aim_utils.sanitize_display_name(
                router_db['name'] + '-' +
                (intf.subnet.name or intf.subnet.cidr))
            sn = self._map_subnet(intf.subnet, gw_ip, bd)
            sn.scope = 'public'
            sn.display_name = dname
            sn.monitored = False
            mgr.expect_aim_resource(sn)

            contract = self._map_router(
                mgr.expected_session, router_db, True)
            router_contract_names.add(contract.name)

        provided_contract_names = (
            router_contract_names |
            set([x.contract_name for x in
                 net_db.aim_extension_extra_contract_mapping
                 if x.provides]))
        consumed_contract_names = (
            router_contract_names |
            set([x.contract_name for x in
                 net_db.aim_extension_extra_contract_mapping
                 if not x.provides]))
        epg_contract_masters = [{key: x[key]
                                for key in ('app_profile_name', 'name')}
                                for x in
                                net_db.aim_extension_epg_contract_masters]
        policy_enforcement_pref = (
            net_db.aim_extension_mapping.policy_enforcement_pref if
            net_db.aim_extension_mapping
            else 'unenforced')

        # REVISIT: Refactor to share code.
        dname = aim_utils.sanitize_display_name(net_db.name)

        bd.display_name = dname
        bd.vrf_name = vrf.name
        bd.enable_arp_flood = True
        bd.enable_routing = len(router_contract_names) != 0
        bd.limit_ip_learn_to_subnets = True
        bd.ep_move_detect_mode = 'garp'
        bd.l3out_names = []
        bd.monitored = False
        mgr.expect_aim_resource(bd)

        epg.display_name = dname
        epg.bd_name = bd.name
        epg.policy_enforcement_pref = policy_enforcement_pref
        epg.provided_contract_names = provided_contract_names
        epg.consumed_contract_names = consumed_contract_names
        epg.openstack_vmm_domain_names = []
        epg.physical_domain_names = []
        epg.vmm_domains = []
        epg.physical_domains = []
        epg.static_paths = []
        epg.epg_contract_masters = epg_contract_masters
        epg.monitored = False
        qos_policy_binding = net_db.get('qos_policy_binding')
        epg.qos_name = (
            qos_policy_binding.policy_id if qos_policy_binding else None)
        mgr.expect_aim_resource(epg)

        return bd, epg, vrf

    def _validate_external_network(self, mgr, net_db, ext_net_routers,
                                   router_dbs, router_vrfs, router_ext_prov,
                                   router_ext_cons):
        l3out, ext_net, ns = self._get_aim_nat_strategy_db(
            mgr.actual_session, net_db)
        if not ext_net:
            return None, None, None

        # REVISIT: Avoid piecemeal queries against the actual DB
        # throughout this code.

        # Copy the external network's pre-existing resources, if they
        # are monitored, from the actual AIM store to the validation
        # AIM store, so that the NatStrategy behaves as expected
        # during validation. Make sure not to overwrite any
        # pre-existing resources that have already been copied.
        actual_l3out = mgr.aim_mgr.get(mgr.actual_aim_ctx, l3out)
        if actual_l3out and actual_l3out.monitored:
            if not mgr.aim_mgr.get(mgr.expected_aim_ctx, actual_l3out):
                mgr.aim_mgr.create(mgr.expected_aim_ctx, actual_l3out)
            ext_vrf = aim_resource.VRF(
                tenant_name=actual_l3out.tenant_name,
                name=actual_l3out.vrf_name)
            actual_ext_vrf = mgr.aim_mgr.get(mgr.actual_aim_ctx, ext_vrf)
            if not actual_ext_vrf:
                ext_vrf.tenant_name = 'common'
                actual_ext_vrf = mgr.aim_mgr.get(mgr.actual_aim_ctx, ext_vrf)
            if actual_ext_vrf and actual_ext_vrf.monitored:
                if not mgr.aim_mgr.get(mgr.expected_aim_ctx, actual_ext_vrf):
                    mgr.aim_mgr.create(mgr.expected_aim_ctx, actual_ext_vrf)
        actual_ext_net = mgr.aim_mgr.get(mgr.actual_aim_ctx, ext_net)
        if actual_ext_net and actual_ext_net.monitored:
            if not mgr.aim_mgr.get(mgr.expected_aim_ctx, actual_ext_net):
                mgr.aim_mgr.create(mgr.expected_aim_ctx, actual_ext_net)
            for actual_ext_sn in mgr.aim_mgr.find(
                    mgr.actual_aim_ctx, aim_resource.ExternalSubnet,
                    tenant_name=actual_ext_net.tenant_name,
                    l3out_name=actual_ext_net.l3out_name,
                    external_network_name=actual_ext_net.name,
                    monitored=True):
                if not mgr.aim_mgr.get(mgr.expected_aim_ctx, actual_ext_sn):
                    mgr.aim_mgr.create(mgr.expected_aim_ctx, actual_ext_sn)

        domains = self._get_vmm_domains(mgr.expected_aim_ctx, ns)
        ns.create_l3outside(
            mgr.expected_aim_ctx, l3out, vmm_domains=domains)
        ns.create_external_network(mgr.expected_aim_ctx, ext_net)

        # Get external CIDRs for all external networks that share this
        # APIC external network.
        cidrs = sorted(self.get_external_cidrs_by_ext_net_dn(
            mgr.actual_session, ext_net.dn, lock_update=False))
        ns.update_external_cidrs(mgr.expected_aim_ctx, ext_net, cidrs)

        # Get the L3Outside resources, and check that there is an
        # external VRF.
        bd = None
        epg = None
        vrf = None
        for resource in ns.get_l3outside_resources(
                mgr.expected_aim_ctx, l3out):
            if isinstance(resource, aim_resource.BridgeDomain):
                bd = resource
            elif isinstance(resource, aim_resource.EndpointGroup):
                epg = resource
            elif isinstance(resource, aim_resource.VRF):
                vrf = resource
        if not vrf:
            mgr.validation_failed(
                "missing external VRF for external network %s" % net_db.id)

        for subnet_db in net_db.subnets:
            if not subnet_db.aim_extension_mapping:
                self._missing_subnet_extension_mapping(mgr, subnet_db)
            if subnet_db.gateway_ip:
                if subnet_db.aim_extension_mapping.epg_subnet:
                    ns.create_epg_subnet(
                        mgr.expected_aim_ctx, l3out,
                        self._subnet_to_gw_ip_mask(subnet_db))
                else:
                    ns.create_subnet(
                        mgr.expected_aim_ctx, l3out,
                        self._subnet_to_gw_ip_mask(subnet_db))

        # REVISIT: Process each AIM ExternalNetwork rather than each
        # external Neutron network?
        eqv_net_ids = self.get_network_ids_by_ext_net_dn(
            mgr.actual_session, ext_net.dn, lock_update=False)
        router_ids = set()
        for eqv_net_id in eqv_net_ids:
            router_ids.update(ext_net_routers[eqv_net_id])
        vrf_routers = defaultdict(set)
        int_vrfs = {}
        for router_id in router_ids:
            for int_vrf in list(router_vrfs[router_id].values()):
                key = tuple(int_vrf.identity)
                vrf_routers[key].add(router_id)
                int_vrfs[key] = int_vrf

        for key, routers in list(vrf_routers.items()):
            prov = set()
            cons = set()
            for router_id in routers:
                contract = self._map_router(
                    mgr.expected_session, router_dbs[router_id], True)
                prov.add(aim_resource.ExternalNetworkProvidedContract(
                    tenant_name=ext_net.tenant_name,
                    l3out_name=ext_net.l3out_name,
                    ext_net_name=ext_net.name,
                    name=contract.name))
                cons.add(aim_resource.ExternalNetworkProvidedContract(
                    tenant_name=ext_net.tenant_name,
                    l3out_name=ext_net.l3out_name,
                    ext_net_name=ext_net.name,
                    name=contract.name))
                for p_con in router_ext_prov[router_id]:
                    prov.add(aim_resource.ExternalNetworkProvidedContract(
                        tenant_name=ext_net.tenant_name,
                        l3out_name=ext_net.l3out_name,
                        ext_net_name=ext_net.name,
                        name=p_con))
                for c_con in router_ext_cons[router_id]:
                    cons.add(aim_resource.ExternalNetworkConsumedContract(
                        tenant_name=ext_net.tenant_name,
                        l3out_name=ext_net.l3out_name,
                        ext_net_name=ext_net.name,
                        name=c_con))
            provided = sorted(prov, key=lambda x: x.name)
            consumed = sorted(cons, key=lambda x: x.name)
            int_vrf = int_vrfs[key]

            # Keep only the identity attributes of the VRF so that
            # calls to nat-library have consistent resource
            # values. This is mainly required to ease unit-test
            # verification. Note that this also effects validation
            # of the L3Outside's display_name.
            int_vrf = aim_resource.VRF(
                tenant_name=int_vrf.tenant_name, name=int_vrf.name)
            ns.connect_vrf(mgr.expected_aim_ctx, ext_net, int_vrf,
                provided_contracts=provided, consumed_contracts=consumed)

        return bd, epg, vrf

    def _validate_security_groups(self, mgr):
        if not mgr._should_validate_neutron_resource("security_group"):
            return
        sg_ips = defaultdict(set)

        query = BAKERY(lambda s: s.query(
            sg_models.SecurityGroupPortBinding.security_group_id,
            models_v2.IPAllocation.ip_address))
        query += lambda q: q.join(
            models_v2.IPAllocation,
            models_v2.IPAllocation.port_id ==
            sg_models.SecurityGroupPortBinding.port_id)
        for sg_id, ip in query(mgr.actual_session):
            sg_ips[sg_id].add(ip)

        # REVISIT: Loading of the SG rules was previously optimized
        # via options(orm.joinedload('rules')), but this broke when
        # upstream neutron changed the relationship from
        # lazy='subquery' to lazy='dynamic'. If there is any way to
        # override this dynamic loading with eager loading for a
        # specific query, we may want to do so.
        sg_dbs = None
        query = BAKERY(lambda s: s.query(
            sg_models.SecurityGroup))
        if mgr.tenant_scope:
            query += lambda q: q.filter(
                sg_models.SecurityGroup.tenant_id.in_(
                    sa.bindparam('project_ids', expanding=True)))
            sg_dbs = query(mgr.actual_session).params(
                    project_ids=list(mgr.tenant_ids))
        else:
            sg_dbs = query(mgr.actual_session)
        for sg_db in sg_dbs:
            # Ignore anonymous SGs, which seem to be a Neutron bug.
            if sg_db.tenant_id:
                tenant_name = self.name_mapper.project(
                    mgr.expected_session, sg_db.tenant_id)
                self._expect_project(mgr, sg_db.project_id)
                sg = aim_resource.SecurityGroup(
                    tenant_name=tenant_name, name=sg_db.id,
                    display_name=aim_utils.sanitize_display_name(sg_db.name))
                mgr.expect_aim_resource(sg)
                sg_subject = aim_resource.SecurityGroupSubject(
                    tenant_name=tenant_name, security_group_name=sg_db.id,
                    name='default')
                mgr.expect_aim_resource(sg_subject)
                for rule_db in sg_db.rules:
                    remote_ips = []
                    remote_group_id = ''
                    if rule_db.remote_group_id:
                        remote_group_id = rule_db.remote_group_id
                        ip_version = (4 if rule_db.ethertype == 'IPv4' else
                                      6 if rule_db.ethertype == 'IPv6' else
                                      0)
                        remote_ips = [
                            ip for ip in sg_ips[rule_db.remote_group_id]
                            if netaddr.IPAddress(ip).version == ip_version]
                    elif rule_db.remote_ip_prefix:
                        remote_ips = [rule_db.remote_ip_prefix]
                    sg_rule = aim_resource.SecurityGroupRule(
                        tenant_name=tenant_name,
                        security_group_name=rule_db.security_group_id,
                        security_group_subject_name='default',
                        name=rule_db.id,
                        direction=rule_db.direction,
                        ethertype=rule_db.ethertype.lower(),
                        ip_protocol=self.get_aim_protocol(rule_db.protocol),
                        remote_ips=remote_ips,
                        from_port=(rule_db.port_range_min
                                   if rule_db.port_range_min
                                   else 'unspecified'),
                        to_port=(rule_db.port_range_max
                                 if rule_db.port_range_max
                                 else 'unspecified'),
                        remote_group_id=remote_group_id)
                    mgr.expect_aim_resource(sg_rule)

    def _validate_ports(self, mgr):
        if not mgr._should_validate_neutron_resource("port"):
            return
        query = BAKERY(lambda s: s.query(
            models_v2.Port.project_id))
        query += lambda q: q.distinct()
        for project_id, in query(mgr.actual_session):
            tenant_name = self.name_mapper.project(
                mgr.expected_session, project_id)
            if not mgr._should_validate_tenant(tenant_name):
                continue
            self._expect_project(mgr, project_id)
        query = BAKERY(lambda s: s.query(
            models_v2.Port))
        for port_db in query(mgr.actual_session):
            # We can only validate AIM resources on bound ports.
            if port_db.aim_extension_erspan_configs and port_db.port_bindings:
                cep_dn = self._map_port(mgr.actual_session, port_db)
                resources = self._get_erspan_aim_resources_list(port_db,
                                                                cep_dn)
                self.update_summary_resource(mgr.actual_session, resources,
                        port_db['network_id'])
                # Copy the bundle group pre-existing resources, if they
                # are monitored, from the actual AIM store to the validation
                # AIM store, so that the resource behaves as expected
                # during validation. Make sure not to overwrite any
                # pre-existing resources that have already been copied.
                acc_name = self._get_acc_bundle_for_host(mgr.actual_aim_ctx,
                    port_db.port_bindings[0].host)
                if acc_name:
                    acc_bundle = aim_resource.InfraAccBundleGroup(
                        name=acc_name)
                    actual_bg = mgr.aim_mgr.get(mgr.actual_aim_ctx, acc_bundle)
                    if actual_bg and actual_bg.monitored:
                        if not mgr.aim_mgr.get(mgr.expected_aim_ctx,
                                               actual_bg):
                            mgr.aim_mgr.create(mgr.expected_aim_ctx,
                                               actual_bg)
                for resource in resources:
                    mgr.expect_aim_resource(resource)

    def _validate_subnetpools(self, mgr):
        if not mgr._should_validate_neutron_resource("subnet_pool"):
            return
        query = BAKERY(lambda s: s.query(
            models_v2.SubnetPool.project_id))
        query += lambda q: q.distinct()
        for project_id, in query(mgr.actual_session):
            tenant_name = self.name_mapper.project(
                mgr.expected_session, project_id)
            if not mgr._should_validate_tenant(tenant_name):
                continue
            self._expect_project(mgr, project_id)

    def _validate_floatingips(self, mgr):
        if not mgr._should_validate_neutron_resource("floatingip"):
            return
        query = BAKERY(lambda s: s.query(
            l3_db.FloatingIP.project_id))
        query += lambda q: q.distinct()
        for project_id, in query(mgr.actual_session):
            tenant_name = self.name_mapper.project(
                mgr.expected_session, project_id)
            if not mgr._should_validate_tenant(tenant_name):
                continue
            self._expect_project(mgr, project_id)

    def _validate_port_bindings(self, mgr):
        # REVISIT: Deal with distributed port bindings? Also, consider
        # moving this to the ML2Plus plugin or to a base validation
        # manager, as it is not specific to this mechanism driver.

        query = BAKERY(lambda s: s.query(
            models_v2.Port))
        query += lambda q: q.options(
            orm.joinedload('binding_levels'))
        for port in query(mgr.actual_session):
            binding = port.port_bindings[0] if port.port_bindings else None
            levels = port.binding_levels
            unbind = False
            # REVISIT: Validate that vif_type and vif_details are
            # correct when host is empty?
            for level in levels:
                if (level.driver not in
                    self.plugin.mechanism_manager.mech_drivers):
                    if mgr.should_repair(
                            "port %(id)s bound with invalid driver "
                            "%(driver)s" %
                            {'id': port.id, 'driver': level.driver},
                            "Unbinding"):
                        unbind = True
                elif (level.host != binding.host):
                    if mgr.should_repair(
                            "port %(id)s bound with invalid host "
                            "%(host)s" %
                            {'id': port.id, 'host': level.host},
                            "Unbinding"):
                        unbind = True
                elif (not level.segment_id):
                    if mgr.should_repair(
                            "port %s bound without valid segment" % port.id,
                            "Unbinding"):
                        unbind = True
            if unbind:
                binding.vif_type = portbindings.VIF_TYPE_UNBOUND
                binding.vif_details = ''
                for level in port.binding_levels:
                    mgr.actual_session.delete(level)

    def _expect_project(self, mgr, project_id):
        # REVISIT: Currently called for all Neutron and GBP resources
        # for which plugin create methods call _ensure_tenant. Remove
        # once per-project resources are managed more dynamically.
        if project_id and project_id not in mgr._expected_projects:
            mgr._expected_projects.add(project_id)
            tenant_name = self.name_mapper.project(
                mgr.expected_session, project_id)

            tenant = aim_resource.Tenant(name=tenant_name)
            project_details = self.project_details_cache.get_project_details(
                project_id)
            tenant.display_name = aim_utils.sanitize_display_name(
                project_details[0])
            tenant.descr = aim_utils.sanitize_description(project_details[1])
            tenant.monitored = False
            mgr.expect_aim_resource(tenant)

            ap = aim_resource.ApplicationProfile(
                tenant_name=tenant_name, name=self.ap_name)
            ap.display_name = aim_utils.sanitize_display_name(self.ap_name)
            ap.monitored = False
            mgr.expect_aim_resource(ap)

    def bind_unbound_ports(self, mgr):
        # REVISIT: Deal with distributed port bindings? Also, consider
        # moving this to the ML2Plus plugin or to a base validation
        # manager, as it is not specific to this mechanism driver.
        failure_count = 0
        failure_hosts = set()

        query = BAKERY(lambda s: s.query(
            models.PortBinding.port_id))
        query += lambda q: q.filter(
            models.PortBinding.host != '',
            models.PortBinding.vif_type == portbindings.VIF_TYPE_UNBOUND)
        for port_id, in query(mgr.actual_session):
            mgr.output("Attempting to bind port %s" % port_id)
            # REVISIT: Use the more efficient get_bound_port_contexts,
            # which is not available in stable/newton?
            pc = self.plugin.get_bound_port_context(
                mgr.actual_context, port_id)
            if (pc.vif_type == portbindings.VIF_TYPE_BINDING_FAILED or
                pc.vif_type == portbindings.VIF_TYPE_UNBOUND):
                mgr.bind_ports_failed(
                    "Unable to bind port %(port)s on host %(host)s" %
                    {'port': port_id, 'host': pc.host})
                failure_count += 1
                failure_hosts.add(pc.host)
        if failure_count:
            mgr.output(
                "Failed to bind %s ports on hosts %s. See log for details. "
                "Make sure L2 agents are alive, and re-run validation to try "
                "binding them again." % (failure_count, list(failure_hosts)))
        else:
            mgr.output("All ports are bound")

    # The sg_rule_protocol can be either protocol name , protocol number or
    # None.
    # If sg_rule_protocol is None, return 'unspecified' otherwise return
    # protocol number.
    def get_aim_protocol(self, sg_rule_protocol):
        if sg_rule_protocol:
            try:
                return n_constants.IP_PROTOCOL_MAP[sg_rule_protocol]
            except KeyError:
                return sg_rule_protocol
        return 'unspecified'
