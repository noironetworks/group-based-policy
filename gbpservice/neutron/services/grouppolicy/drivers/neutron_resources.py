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

from neutron_lib.plugins import directory
from oslo_log import helpers as log

from gbpservice.neutron.db import api as db_api
from gbpservice.neutron.services.grouppolicy.common import exceptions as exc
from gbpservice.neutron.services.grouppolicy.drivers import (
    implicit_policy as ipd)
from gbpservice.neutron.services.grouppolicy.drivers import (
    resource_mapping as rmd)


class CommonNeutronBase(ipd.ImplicitPolicyBase, rmd.OwnedResourcesOperations,
                        rmd.ImplicitResourceOperations):
    """Neutron Resources' Orchestration driver.

    This driver realizes GBP's network semantics by orchestrating
    the necessary Neutron resources.
    """

    @log.log_method_call
    def initialize(self):
        # REVISIT: Check if this is still required
        self._cached_agent_notifier = None
        self._gbp_plugin = None
        super(CommonNeutronBase, self).initialize()

    @property
    def gbp_plugin(self):
        if not self._gbp_plugin:
            self._gbp_plugin = directory.get_plugin("GROUP_POLICY")
        return self._gbp_plugin

    @log.log_method_call
    def create_l2_policy_postcommit(self, context):
        if not context.current['l3_policy_id']:
            self._use_implicit_l3_policy(context)
        with db_api.CONTEXT_READER.using(context._plugin_context):
            l3p_db = context._plugin._get_l3_policy(
                context._plugin_context, context.current['l3_policy_id'])
        if not context.current['network_id']:
            self._use_implicit_network(
                context, address_scope_v4=l3p_db['address_scope_v4_id'],
                address_scope_v6=l3p_db['address_scope_v6_id'])

    @log.log_method_call
    def update_l2_policy_precommit(self, context):
        if (context.current['inject_default_route'] !=
            context.original['inject_default_route']):
            raise exc.UnsettingInjectDefaultRouteOfL2PolicyNotSupported()
        if (context.current['l3_policy_id'] !=
            context.original['l3_policy_id']):
            raise exc.L3PolicyUpdateOfL2PolicyNotSupported()

    @log.log_method_call
    def delete_l2_policy_postcommit(self, context):
        network_id = context.current['network_id']
        if network_id:
            self._cleanup_network(context._plugin_context, network_id)
        l3p_id = context.current['l3_policy_id']
        if l3p_id:
            self._cleanup_l3_policy(context, l3p_id)

    def _port_id_to_pt(self, plugin_context, port_id):
        pts = self.gbp_plugin.get_policy_targets(
            plugin_context, {'port_id': [port_id]})
        if pts:
            return pts[0]

    def _pt_to_ptg(self, plugin_context, pt):
        if pt:
            return self.gbp_plugin.get_policy_target_group(
                plugin_context, pt['policy_target_group_id'])
        return None

    def _port_id_to_ptg(self, plugin_context, port_id):
        pt = self._port_id_to_pt(plugin_context, port_id)
        return self._pt_to_ptg(plugin_context, pt), pt

    def _network_id_to_l2p(self, context, network_id):
        l2ps = self.gbp_plugin.get_l2_policies(
            context, filters={'network_id': [network_id]})
        for l2p in l2ps:
            if l2p['network_id'] == network_id:
                return l2p
