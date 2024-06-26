# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from neutron.extensions import securitygroup as ext_sg
from neutron.notifiers import nova
from neutron import quota
from neutron_lib import exceptions as n_exc
from neutron_lib.exceptions import address_scope as as_exc
from neutron_lib.exceptions import l3
from neutron_lib.plugins import constants as pconst
from neutron_lib.plugins import directory
from oslo_log import log as logging
from oslo_utils import excutils

from gbpservice.neutron.extensions import group_policy as gp_ext
from gbpservice.neutron.services.grouppolicy.common import exceptions as exc

LOG = logging.getLogger(__name__)


class LocalAPI(object):
    """API for interacting with the neutron Plugins directly."""

    @property
    def _nova_notifier(self):
        return nova.Notifier()

    @property
    def _core_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        return directory.get_plugin()

    @property
    def _l3_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        l3_plugin = directory.get_plugin(pconst.L3)
        if not l3_plugin:
            LOG.error("No L3 router service plugin found.")
            raise exc.GroupPolicyDeploymentError()
        return l3_plugin

    @property
    def _qos_plugin(self):
        # Probably as well:
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        qos_plugin = directory.get_plugin(pconst.QOS)
        if not qos_plugin:
            LOG.error("No QoS service plugin found.")
            raise exc.GroupPolicyDeploymentError()
        return qos_plugin

    @property
    def _group_policy_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        group_policy_plugin = directory.get_plugin(pconst.GROUP_POLICY)
        if not group_policy_plugin:
            LOG.error("No GroupPolicy service plugin found.")
            raise exc.GroupPolicyDeploymentError()
        return group_policy_plugin

    @property
    def _trunk_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        return directory.get_plugin('trunk')

    def _create_resource(self, plugin, context, resource, attrs,
                         do_notify=True):
        # REVISIT(rkukura): Do create.start notification?
        # REVISIT(rkukura): Check authorization?
        reservation = None
        if plugin in [self._group_policy_plugin]:
            reservation = quota.QUOTAS.make_reservation(
                context, context.tenant_id, {resource: 1}, plugin)
        action = 'create_' + resource
        obj_creator = getattr(plugin, action)
        try:
            obj = obj_creator(context, {resource: attrs})
        except Exception:
            # In case of failure the plugin will always raise an
            # exception. Cancel the reservation
            with excutils.save_and_reraise_exception():
                if reservation:
                    quota.QUOTAS.cancel_reservation(
                        context, reservation.reservation_id)
        if reservation:
            quota.QUOTAS.commit_reservation(
                context, reservation.reservation_id)
            # At this point the implicit resource creation is successfull,
            # so we should be calling:
            # resource_registry.set_resources_dirty(context)
            # to appropriately notify the quota engine. However, the above
            # call begins a new transaction and we want to avoid that.
            # Moreover, it can be safely assumed that any implicit resource
            # creation via this local_api is always in response to an
            # explicit resource creation request, and hence the above
            # method will be invoked in the API layer.
        return obj

    def _create_resource_qos(self, plugin, context, resource,
                             param, attrs):
        action = 'create_' + resource
        obj_creator = getattr(plugin, action)
        if resource == "policy_bandwidth_limit_rule":
            resource = resource[7:]  # in the body, policy_ should be removed
        obj = obj_creator(context, param, {resource: attrs})
        return obj

    def _update_resource(self, plugin, context, resource, resource_id, attrs,
                         do_notify=True):
        # REVISIT(rkukura): Check authorization?
        action = 'update_' + resource
        obj_updater = getattr(plugin, action)
        obj = obj_updater(context, resource_id, {resource: attrs})
        return obj

    def _delete_resource(self, plugin, context, resource, resource_id,
                         do_notify=True):
        # REVISIT(rkukura): Check authorization?
        action = 'delete_' + resource
        obj_deleter = getattr(plugin, action)
        obj_deleter(context, resource_id)

    def _delete_resource_qos(self, plugin, context, resource,
                             resource_id, second_id):
        action = 'delete_' + resource
        obj_deleter = getattr(plugin, action)
        obj_deleter(context, resource_id, second_id)

    def _get_resource(self, plugin, context, resource, resource_id):
        obj_getter = getattr(plugin, 'get_' + resource)
        obj = obj_getter(context, resource_id)
        if 'standard_attr_id' in obj:
            del obj['standard_attr_id']
        return obj

    def _get_resources(self, plugin, context, resource_plural, filters=None):
        obj_getter = getattr(plugin, 'get_' + resource_plural)
        obj = obj_getter(context, filters)
        if 'standard_attr_id' in obj:
            del obj['standard_attr_id']
        return obj

    # The following methods perform the necessary subset of
    # functionality from neutron.api.v2.base.Controller.
    #
    # REVISIT(rkukura): Can we just use the WSGI Controller?  Using
    # neutronclient is also a possibility, but presents significant
    # issues to unit testing as well as overhead and failure modes.

    def _get_port(self, plugin_context, port_id):
        return self._get_resource(self._core_plugin, plugin_context, 'port',
                                  port_id)

    def _get_ports(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._core_plugin, plugin_context, 'ports',
                                   filters)

    def _create_port(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context, 'port',
                                     attrs)

    def _update_port(self, plugin_context, port_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context, 'port',
                                     port_id, attrs)

    def _delete_port(self, plugin_context, port_id):
        try:
            self._delete_resource(self._core_plugin,
                                  plugin_context, 'port', port_id)
        except n_exc.PortNotFound:
            LOG.warning('Port %s already deleted', port_id)

    def _get_subnet(self, plugin_context, subnet_id):
        return self._get_resource(self._core_plugin, plugin_context, 'subnet',
                                  subnet_id)

    def _get_subnets(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._core_plugin, plugin_context,
                                   'subnets', filters)

    def _create_subnet(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'subnet', attrs)

    def _update_subnet(self, plugin_context, subnet_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'subnet', subnet_id, attrs)

    def _delete_subnet(self, plugin_context, subnet_id):
        try:
            self._delete_resource(self._core_plugin, plugin_context, 'subnet',
                                  subnet_id)
        except n_exc.SubnetNotFound:
            LOG.warning('Subnet %s already deleted', subnet_id)

    def _get_network(self, plugin_context, network_id):
        return self._get_resource(self._core_plugin, plugin_context, 'network',
                                  network_id)

    def _get_networks(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(
            self._core_plugin, plugin_context, 'networks', filters)

    def _create_network(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'network', attrs, True)

    def _update_network(self, plugin_context, network_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'network', network_id, attrs)

    def _delete_network(self, plugin_context, network_id):
        try:
            self._delete_resource(self._core_plugin, plugin_context,
                                  'network', network_id)
        except n_exc.NetworkNotFound:
            LOG.warning('Network %s already deleted', network_id)

    def _get_router(self, plugin_context, router_id):
        return self._get_resource(self._l3_plugin, plugin_context, 'router',
                                  router_id)

    def _get_routers(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._l3_plugin, plugin_context, 'routers',
                                   filters)

    def _create_router(self, plugin_context, attrs):
        return self._create_resource(self._l3_plugin, plugin_context, 'router',
                                     attrs)

    def _update_router(self, plugin_context, router_id, attrs):
        return self._update_resource(self._l3_plugin, plugin_context, 'router',
                                     router_id, attrs)

    def _add_router_interface(self, plugin_context, router_id, interface_info):
        self._l3_plugin.add_router_interface(plugin_context,
                                             router_id, interface_info)

    def _remove_router_interface(self, plugin_context, router_id,
                                 interface_info):
        # To detach Router interface either port ID or Subnet ID is mandatory
        try:
            self._l3_plugin.remove_router_interface(plugin_context, router_id,
                                                    interface_info)
        except l3.RouterInterfaceNotFoundForSubnet:
            LOG.warning('Router interface already deleted for subnet %s',
                        interface_info)
            return

    def _add_router_gw_interface(self, plugin_context, router_id, gw_info):
        return self._l3_plugin.update_router(
            plugin_context, router_id,
            {'router': {'external_gateway_info': gw_info}})

    def _remove_router_gw_interface(self, plugin_context, router_id,
                                    interface_info):
        self._l3_plugin.update_router(
            plugin_context, router_id,
            {'router': {'external_gateway_info': None}})

    def _delete_router(self, plugin_context, router_id):
        try:
            self._delete_resource(self._l3_plugin, plugin_context, 'router',
                                  router_id)
        except l3.RouterNotFound:
            LOG.warning('Router %s already deleted', router_id)

    def _get_sg(self, plugin_context, sg_id):
        return self._get_resource(
            self._core_plugin, plugin_context, 'security_group', sg_id)

    def _get_sgs(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(
            self._core_plugin, plugin_context, 'security_groups', filters)

    def _create_sg(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'security_group', attrs)

    def _update_sg(self, plugin_context, sg_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'security_group', sg_id, attrs)

    def _delete_sg(self, plugin_context, sg_id):
        try:
            self._delete_resource(self._core_plugin, plugin_context,
                                  'security_group', sg_id)
        except ext_sg.SecurityGroupNotFound:
            LOG.warning('Security Group %s already deleted', sg_id)

    def _get_sg_rule(self, plugin_context, sg_rule_id):
        return self._get_resource(
            self._core_plugin, plugin_context, 'security_group_rule',
            sg_rule_id)

    def _get_sg_rules(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(
            self._core_plugin, plugin_context, 'security_group_rules', filters)

    def _create_sg_rule(self, plugin_context, attrs):
        try:
            return self._create_resource(self._core_plugin, plugin_context,
                                         'security_group_rule', attrs)
        except ext_sg.SecurityGroupRuleExists as ex:
            LOG.warning('Security Group already exists %s', ex.message)
            return

    def _update_sg_rule(self, plugin_context, sg_rule_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'security_group_rule', sg_rule_id,
                                     attrs)

    def _delete_sg_rule(self, plugin_context, sg_rule_id):
        try:
            self._delete_resource(self._core_plugin, plugin_context,
                                  'security_group_rule', sg_rule_id)
        except ext_sg.SecurityGroupRuleNotFound:
            LOG.warning('Security Group Rule %s already deleted',
                        sg_rule_id)

    def _get_fip(self, plugin_context, fip_id):
        return self._get_resource(
            self._l3_plugin, plugin_context, 'floatingip', fip_id)

    def _get_fips(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(
            self._l3_plugin, plugin_context, 'floatingips', filters)

    def _create_fip(self, plugin_context, attrs):
        return self._create_resource(self._l3_plugin, plugin_context,
                                     'floatingip', attrs)

    def _update_fip(self, plugin_context, fip_id, attrs):
        return self._update_resource(self._l3_plugin, plugin_context,
                                     'floatingip', fip_id, attrs)

    def _delete_fip(self, plugin_context, fip_id):
        try:
            self._delete_resource(self._l3_plugin, plugin_context,
                                  'floatingip', fip_id)
        except l3.FloatingIPNotFound:
            LOG.warning('Floating IP %s Already deleted', fip_id)

    def _get_address_scope(self, plugin_context, address_scope_id):
        return self._get_resource(self._core_plugin, plugin_context,
                                  'address_scope', address_scope_id)

    def _get_address_scopes(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._core_plugin, plugin_context,
                                   'address_scopes', filters)

    def _create_address_scope(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'address_scope', attrs)

    def _update_address_scope(self, plugin_context, address_scope_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'address_scope', address_scope_id, attrs)

    def _delete_address_scope(self, plugin_context, address_scope_id):
        try:
            self._delete_resource(self._core_plugin, plugin_context,
                                  'address_scope', address_scope_id)
        except as_exc.AddressScopeNotFound:
            LOG.warning('Address Scope %s already deleted',
                        address_scope_id)

    def _get_subnetpool(self, plugin_context, subnetpool_id):
        return self._get_resource(self._core_plugin, plugin_context,
                                  'subnetpool', subnetpool_id)

    def _get_subnetpools(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._core_plugin, plugin_context,
                                   'subnetpools', filters)

    def _create_subnetpool(self, plugin_context, attrs):
        return self._create_resource(self._core_plugin, plugin_context,
                                     'subnetpool', attrs)

    def _update_subnetpool(self, plugin_context, subnetpool_id, attrs):
        return self._update_resource(self._core_plugin, plugin_context,
                                     'subnetpool', subnetpool_id, attrs)

    def _delete_subnetpool(self, plugin_context, subnetpool_id):
        try:
            self._delete_resource(self._core_plugin, plugin_context,
                                  'subnetpool', subnetpool_id)
        except n_exc.SubnetpoolNotFound:
            LOG.warning('Subnetpool %s already deleted', subnetpool_id)

    def _get_l2_policy(self, plugin_context, l2p_id):
        return self._get_resource(self._group_policy_plugin, plugin_context,
                                  'l2_policy', l2p_id)

    def _get_qos_policy(self, plugin_context, qos_policy_id):
        return self._get_resource(self._qos_plugin, plugin_context,
                                  'policy', qos_policy_id)

    def _create_qos_policy(self, plugin_context, attrs):
        return self._create_resource(self._qos_plugin, plugin_context,
                                     'policy', attrs)

    def _delete_qos_policy(self, plugin_context, qos_policy_id):
        try:
            self._delete_resource(self._qos_plugin,
                                  plugin_context, 'policy', qos_policy_id)
        except n_exc.QosPolicyNotFound:
            LOG.warning('QoS Policy %s already deleted', qos_policy_id)

    def _get_qos_rules(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._qos_plugin, plugin_context,
                                   'policy_bandwidth_limit_rules', filters)

    def _create_qos_rule(self, plugin_context, qos_policy_id, attrs):
        return self._create_resource_qos(self._qos_plugin,
                                         plugin_context,
                                         'policy_bandwidth_limit_rule',
                                         qos_policy_id, attrs)

    def _delete_qos_rule(self, plugin_context, rule_id, qos_policy_id):
        try:
            self._delete_resource_qos(self._qos_plugin,
                                      plugin_context,
                                      'policy_bandwidth_limit_rule',
                                      rule_id, qos_policy_id)
        except n_exc.QosRuleNotFound:
            LOG.warning('QoS Rule %s already deleted', rule_id)

    def _get_l2_policies(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._group_policy_plugin, plugin_context,
                                   'l2_policies', filters)

    def _create_l2_policy(self, plugin_context, attrs):
        return self._create_resource(self._group_policy_plugin, plugin_context,
                                     'l2_policy', attrs, False)

    def _update_l2_policy(self, plugin_context, l2p_id, attrs):
        return self._update_resource(self._group_policy_plugin, plugin_context,
                                     'l2_policy', l2p_id, attrs, False)

    def _delete_l2_policy(self, plugin_context, l2p_id):
        try:
            self._delete_resource(self._group_policy_plugin,
                                  plugin_context, 'l2_policy', l2p_id, False)
        except gp_ext.L2PolicyNotFound:
            LOG.warning('L2 Policy %s already deleted', l2p_id)

    def _get_l3_policy(self, plugin_context, l3p_id):
        return self._get_resource(self._group_policy_plugin, plugin_context,
                                  'l3_policy', l3p_id)

    def _get_l3_policies(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._group_policy_plugin, plugin_context,
                                   'l3_policies', filters)

    def _create_l3_policy(self, plugin_context, attrs):
        return self._create_resource(self._group_policy_plugin, plugin_context,
                                     'l3_policy', attrs, False)

    def _update_l3_policy(self, plugin_context, l3p_id, attrs):
        return self._update_resource(self._group_policy_plugin, plugin_context,
                                     'l3_policy', l3p_id, attrs, False)

    def _delete_l3_policy(self, plugin_context, l3p_id):
        try:
            self._delete_resource(self._group_policy_plugin,
                                  plugin_context, 'l3_policy', l3p_id, False)
        except gp_ext.L3PolicyNotFound:
            LOG.warning('L3 Policy %s already deleted', l3p_id)

    def _get_external_segment(self, plugin_context, es_id):
        return self._get_resource(self._group_policy_plugin, plugin_context,
                                  'external_segment', es_id)

    def _get_external_segments(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._group_policy_plugin, plugin_context,
                                   'external_segments', filters)

    def _create_external_segment(self, plugin_context, attrs):
        return self._create_resource(self._group_policy_plugin, plugin_context,
                                     'external_segment', attrs, False)

    def _update_external_segment(self, plugin_context, es_id, attrs):
        return self._update_resource(self._group_policy_plugin, plugin_context,
                                     'external_segment', es_id, attrs, False)

    def _delete_external_segment(self, plugin_context, es_id):
        try:
            self._delete_resource(self._group_policy_plugin, plugin_context,
                                  'external_segment', es_id, False)
        except gp_ext.ExternalSegmentNotFound:
            LOG.warning('External Segment %s already deleted', es_id)

    def _get_external_policy(self, plugin_context, ep_id):
        return self._get_resource(self._group_policy_plugin, plugin_context,
                                  'external_policy', ep_id)

    def _get_external_policies(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._group_policy_plugin, plugin_context,
                                   'external_policies', filters)

    def _create_external_policy(self, plugin_context, attrs):
        return self._create_resource(self._group_policy_plugin, plugin_context,
                                     'external_policy', attrs, False)

    def _update_external_policy(self, plugin_context, ep_id, attrs):
        return self._update_resource(self._group_policy_plugin, plugin_context,
                                     'external_policy', ep_id, attrs, False)

    def _delete_external_policy(self, plugin_context, ep_id):
        try:
            self._delete_resource(self._group_policy_plugin, plugin_context,
                                  'external_policy', ep_id, False)
        except gp_ext.ExternalPolicyNotFound:
            LOG.warning('External Policy %s already deleted', ep_id)

    def _get_policy_rule_set(self, plugin_context, prs_id):
        return self._get_resource(self._group_policy_plugin, plugin_context,
                                  'policy_rule_set', prs_id)

    def _get_policy_rule_sets(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._group_policy_plugin, plugin_context,
                                   'policy_rule_sets', filters)

    def _create_policy_rule_set(self, plugin_context, attrs):
        return self._create_resource(self._group_policy_plugin, plugin_context,
                                     'policy_rule_set', attrs, False)

    def _update_policy_rule_set(self, plugin_context, prs_id, attrs):
        return self._update_resource(self._group_policy_plugin, plugin_context,
                                     'policy_rule_set', prs_id, attrs, False)

    def _delete_policy_rule_set(self, plugin_context, prs_id):
        try:
            self._delete_resource(self._group_policy_plugin, plugin_context,
                                  'policy_rule_set', prs_id, False)
        except gp_ext.PolicyRuleSetNotFound:
            LOG.warning('Policy Rule Set %s already deleted', prs_id)

    def _get_policy_target(self, plugin_context, pt_id):
        return self._get_resource(self._group_policy_plugin, plugin_context,
                                  'policy_target', pt_id)

    def _get_policy_targets(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._group_policy_plugin, plugin_context,
                                   'policy_targets', filters)

    def _create_policy_target(self, plugin_context, attrs):
        return self._create_resource(self._group_policy_plugin, plugin_context,
                                     'policy_target', attrs, False)

    def _update_policy_target(self, plugin_context, pt_id, attrs):
        return self._update_resource(self._group_policy_plugin, plugin_context,
                                     'policy_target', pt_id, attrs, False)

    def _delete_policy_target(self, plugin_context, pt_id):
        try:
            self._delete_resource(self._group_policy_plugin, plugin_context,
                                  'policy_target', pt_id, False)
        except gp_ext.PolicyTargetNotFound:
            LOG.warning('Policy Rule Set %s already deleted', pt_id)

    def _get_policy_target_group(self, plugin_context, ptg_id):
        return self._get_resource(self._group_policy_plugin, plugin_context,
                                  'policy_target_group', ptg_id)

    def _get_policy_target_groups(self, plugin_context, filters=None):
        filters = filters or {}
        return self._get_resources(self._group_policy_plugin, plugin_context,
                                   'policy_target_groups', filters)

    def _create_policy_target_group(self, plugin_context, attrs):
        return self._create_resource(self._group_policy_plugin, plugin_context,
                                     'policy_target_group', attrs, False)

    def _update_policy_target_group(self, plugin_context, ptg_id, attrs):
        return self._update_resource(self._group_policy_plugin, plugin_context,
                                     'policy_target_group', ptg_id, attrs,
                                     False)
