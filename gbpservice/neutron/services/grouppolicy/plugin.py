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

import netaddr
import six

from neutron.common import utils as n_utils
from neutron.quota import resource_registry
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants
from neutron_lib import context as n_ctx
from neutron_lib.plugins import constants as pconst
from neutron_lib.plugins import directory
from oslo_log import helpers as log
from oslo_log import log as logging
from oslo_utils import excutils

from gbpservice.common import utils as gbp_utils
from gbpservice.neutron.db import api as db_api
from gbpservice.neutron.db.grouppolicy import group_policy_db as gpdb
from gbpservice.neutron.db.grouppolicy import group_policy_mapping_db
from gbpservice.neutron import extensions as gbp_extensions
from gbpservice.neutron.extensions import group_policy as gpex
from gbpservice.neutron.services.grouppolicy import (
    extension_manager as ext_manager)
from gbpservice.neutron.services.grouppolicy import (
    group_policy_context as p_context)
from gbpservice.neutron.services.grouppolicy import (
    policy_driver_manager as manager)
from gbpservice.neutron.services.grouppolicy.common import constants as gp_cts
from gbpservice.neutron.services.grouppolicy.common import exceptions as gp_exc
from gbpservice.neutron.services.grouppolicy.common import utils


LOG = logging.getLogger(__name__)
STATUS = 'status'
STATUS_DETAILS = 'status_details'
STATUS_SET = set([STATUS, STATUS_DETAILS])


class GroupPolicyPlugin(group_policy_mapping_db.GroupPolicyMappingDbPlugin):

    """Implementation of the Group Policy Model Plugin.

    This class manages the workflow of Group Policy request/response.
    Most DB related works are implemented in class
    db_group_policy_mapping.GroupPolicyMappingDbMixin.
    """
    _supported_extension_aliases = ["group-policy", "group-policy-mapping"]
    path_prefix = gp_cts.GBP_PREFIXES[pconst.GROUP_POLICY]

    @property
    def supported_extension_aliases(self):
        if not hasattr(self, '_aliases'):
            aliases = self._supported_extension_aliases[:]
            aliases += self.extension_manager.extension_aliases()
            self._aliases = aliases
        return self._aliases

    def start_rpc_listeners(self):
        return self.policy_driver_manager.start_rpc_listeners()

    def validate_state(self, repair, resources, tenants):
        return self.policy_driver_manager.validate_state(repair,
            resources, tenants)

    @property
    def servicechain_plugin(self):
        # REVISIT(rkukura): Need initialization method after all
        # plugins are loaded to grab and store plugin.
        servicechain_plugin = directory.get_plugin(pconst.SERVICECHAIN)
        if not servicechain_plugin:
            LOG.error("No Servicechain service plugin found.")
            raise gp_exc.GroupPolicyDeploymentError()
        return servicechain_plugin

    # Shared attribute validation rules:
    # - A shared resource cannot use/link a non-shared resource
    # - A shared resource cannot be reverted to non-shared if used/linked by
    # other shared resources, or by any resource owned by any other tenant

    # In the usage graph, specify which resource has to be checked to validate
    # sharing policy conformity:
    # usage_graph = {<to_check>: {<attribute>: <type>}, ...}
    # <attribute> is the field on the <to_check> dictionary that can be used
    # to retrieve the UUID/s of the specific object <type>

    usage_graph = {'l3_policy': {'external_segments':
                                 'external_segment'},
                   'l2_policy': {'l3_policy_id': 'l3_policy'},
                   'policy_target_group': {
                       'network_service_policy_id': 'network_service_policy',
                       'l2_policy_id': 'l2_policy',
                       'provided_policy_rule_sets': 'policy_rule_set',
                       'consumed_policy_rule_sets': 'policy_rule_set'},
                   'network_service_policy': {},
                   'policy_rule': {
                       'policy_classifier_id': 'policy_classifier',
                       'policy_actions': 'policy_action'},
                   'policy_action': {},
                   'policy_classifier': {},
                   'policy_rule_set': {
                       'parent_id': 'policy_rule_set',
                       'policy_rules': 'policy_rule'},
                   'external_segment': {},
                   'external_policy': {
                       'external_segments': 'external_segment',
                       'provided_policy_rule_sets': 'policy_rule_set',
                       'consumed_policy_rule_sets': 'policy_rule_set'},
                   'nat_pool': {'external_segment_id':
                                'external_segment'},
                   'policy_target': {'policy_target_group_id':
                                     'policy_target_group'},
                   'application_policy_group': {}
                   }

    @staticmethod
    def _validate_shared_create(self, context, obj, identity):
        # REVISIT(ivar): only validate new references
        links = self.usage_graph.get(identity, {})
        for attr in links:
            ids = obj[attr]
            if ids:
                if isinstance(ids, six.string_types):
                    ids = [ids]
                ref_type = links[attr]
                linked_objects = getattr(
                    self, 'get_%s' % gbp_extensions.get_plural(ref_type))(
                        context, filters={'id': ids})
                link_ids = set()
                for linked in linked_objects:
                    link_ids.add(linked['id'])
                    GroupPolicyPlugin._verify_sharing_consistency(
                        obj, linked, identity, ref_type, context.is_admin)
                # Check for missing references
                missing = set(ids) - link_ids
                if missing:
                    raise gpex.GbpResourceNotFound(identity=ref_type,
                                                   id=str(missing))

    @staticmethod
    def _validate_shared_update(self, context, original, updated, identity):
        # Need admin context to check sharing constraints

        # Even though the shared attribute may not be changed, the objects
        # it is referring to might. For this reson we run the reference
        # validation every time a shared resource is updated
        # TODO(ivar): run only when relevant updates happen
        self._validate_shared_create(self, context, updated, identity)
        if updated.get('shared') != original.get('shared'):
            context = context.elevated()
            getattr(self, '_validate_%s_unshare' % identity)(context, updated)

    @staticmethod
    def _check_shared_or_different_tenant(context, obj, method, attr,
                                          value=None):
        tenant_id = obj['tenant_id']
        refs = method(context, filters={attr: value or [obj['id']]})
        for ref in refs:
            if ref.get('shared') or tenant_id != ref['tenant_id']:
                raise gp_exc.InvalidSharedAttributeUpdate(id=obj['id'],
                                                          rid=ref['id'])

    def _validate_l3_policy_unshare(self, context, obj):
        self._check_shared_or_different_tenant(
            context, obj, self.get_l2_policies, 'l3_policy_id')

    def _validate_l2_policy_unshare(self, context, obj):
        self._check_shared_or_different_tenant(
            context, obj, self.get_policy_target_groups, 'l2_policy_id')

    def _validate_policy_target_group_unshare(self, context, obj):
        self._check_shared_or_different_tenant(
            context, obj, self.get_policy_targets, 'policy_target_group_id')

    def _validate_network_service_policy_unshare(self, context, obj):
        self._check_shared_or_different_tenant(
            context, obj, self.get_policy_target_groups,
            'network_service_policy_id')

    def _validate_policy_rule_set_unshare(self, context, obj):
        self._check_shared_or_different_tenant(
            context, obj, self.get_policy_target_groups, 'id',
            obj['providing_policy_target_groups'] +
            obj['consuming_policy_target_groups'])
        self._check_shared_or_different_tenant(
            context, obj, self.get_external_policies, 'id',
            obj['providing_external_policies'] +
            obj['consuming_external_policies'])

    def _validate_policy_classifier_unshare(self, context, obj):
        self._check_shared_or_different_tenant(
            context, obj, self.get_policy_rules, 'policy_classifier_id')

    def _validate_policy_rule_unshare(self, context, obj):
        c_ids = self._get_policy_rule_policy_rule_sets(context, obj['id'])
        self._check_shared_or_different_tenant(
            context, obj, self.get_policy_rule_sets, 'id', c_ids)

    def _validate_policy_action_unshare(self, context, obj):
        r_ids = self._get_policy_action_rules(context, obj['id'])
        self._check_shared_or_different_tenant(
            context, obj, self.get_policy_rules, 'id', r_ids)

    def _validate_external_segment_unshare(self, context, obj):
        self._check_shared_or_different_tenant(
            context, obj, self.get_l3_policies, 'id', obj['l3_policies'])
        self._check_shared_or_different_tenant(
            context, obj, self.get_external_policies, 'id',
            obj['external_policies'])
        self._check_shared_or_different_tenant(
            context, obj, self.get_nat_pools, 'external_segment_id')

    def _validate_external_policy_unshare(self, context, obj):
        pass

    def _validate_nat_pool_unshare(self, context, obj):
        pass

    def _validate_routes(self, context, current, original=None):
        if original:
            added = (set((x['destination'], x['nexthop']) for x in
                         current['external_routes']) -
                     set((x['destination'], x['nexthop']) for x in
                         original['external_routes']))
        else:
            added = set((x['destination'], x['nexthop']) for x in
                        current['external_routes'])
        if added:
            # Verify new ones don't overlap with the existing L3P
            added_dest = set(x[0] for x in added)
            # Remove default routes
            added_dest.discard('0.0.0.0/0')
            added_dest.discard('::/0')
            added_ipset = netaddr.IPSet(added_dest)
            if current['l3_policies']:
                l3ps = self.get_l3_policies(
                    context, filters={'id': current['l3_policies']})
                for l3p in l3ps:
                    ip_pool_list = utils.convert_ip_pool_string_to_list(
                        l3p['ip_pool'])
                    if netaddr.IPSet(ip_pool_list) & added_ipset:
                        raise gp_exc.ExternalRouteOverlapsWithL3PIpPool(
                            destination=added_dest, l3p_id=l3p['id'],
                            es_id=current['id'])
                    es_list = [current]
                    es_list.extend(self.get_external_segments(
                        context.elevated(),
                        filters={'id': [e for e in l3p['external_segments']
                                        if e != current['id']]}))
                    self._validate_identical_external_routes(es_list)
            # Verify NH in ES pool
            added_nexthop = netaddr.IPSet(x[1] for x in added if x[1])
            es_subnet = netaddr.IPSet([current['cidr']])
            if added_nexthop & es_subnet != added_nexthop:
                raise gp_exc.ExternalRouteNextHopNotInExternalSegment(
                    cidr=current['cidr'])

    def _validate_l3p_es(self, context, current, original=None):
        if original:
            added = (set(current['external_segments'].keys()) -
                     set(original['external_segments'].keys()))
        else:
            added = set(current['external_segments'].keys())
        if added:
            es_list = self.get_external_segments(context,
                                                 filters={'id': added})
            ip_pool_list = utils.convert_ip_pool_string_to_list(
                current['ip_pool'])
            l3p_ipset = netaddr.IPSet(ip_pool_list)
            for es in es_list:
                # Verify no route overlap
                dest_set = set(x['destination'] for x in
                               es['external_routes'])
                dest_set.discard('0.0.0.0/0')
                dest_set.discard('::/0')
                if l3p_ipset & netaddr.IPSet(dest_set):
                    raise gp_exc.ExternalRouteOverlapsWithL3PIpPool(
                        destination=dest_set, l3p_id=current['id'],
                        es_id=es['id'])
                # Verify segment CIDR doesn't overlap with L3P's
                cidr = es['cidr']
                if es['subnet_id']:
                    core_plugin = directory.get_plugin()
                    cidr = core_plugin.get_subnet(context,
                        es['subnet_id'])['cidr']
                if l3p_ipset & netaddr.IPSet([cidr]):
                    raise gp_exc.ExternalSegmentSubnetOverlapsWithL3PIpPool(
                        subnet=cidr, l3p_id=current['id'],
                        es_id=current['id'])
                # Verify allocated address correctly in subnet
                for addr in current['external_segments'][es['id']]:
                    if addr != gpdb.ADDRESS_NOT_SPECIFIED:
                        if addr not in netaddr.IPNetwork(cidr):
                            raise gp_exc.InvalidL3PExternalIPAddress(
                                ip=addr, es_id=es['id'], l3p_id=current['id'],
                                es_cidr=cidr)
            es_list_all = self.get_external_segments(
                context.elevated(),
                filters={'id': list(current['external_segments'].keys())})
            self._validate_identical_external_routes(es_list_all)

    def _validate_identical_external_routes(self, es_list):
        if len(es_list) < 2:
            return
        route_dict = {netaddr.IPNetwork(route['destination']).cidr: es
                      for es in es_list[1:]
                      for route in es['external_routes']}
        for route in es_list[0]['external_routes']:
            cidr = netaddr.IPNetwork(route['destination']).cidr
            if cidr in route_dict:
                raise gp_exc.IdenticalExternalRoute(
                    es1=es_list[0]['id'], es2=route_dict[cidr]['id'],
                    cidr=cidr)

    def _validate_action_value(self, context, action):
        if action.get('action_type') == gp_cts.GP_ACTION_REDIRECT:
            if action.get('action_value'):
                # Verify sc spec existence and visibility
                spec = self.servicechain_plugin.get_servicechain_spec(
                    context, action['action_value'])
                GroupPolicyPlugin._verify_sharing_consistency(
                    action, spec, 'policy_action', 'servicechain_spec',
                    context.is_admin)

    @staticmethod
    def _verify_sharing_consistency(primary, reference, primary_type,
                                    reference_type, is_admin):
        if not reference.get('shared'):
            if primary.get('shared'):
                raise gp_exc.SharedResourceReferenceError(
                    res_type=primary_type, res_id=primary['id'],
                    ref_type=reference_type, ref_id=reference['id'])
            if not is_admin:
                if primary.get('tenant_id') != reference.get('tenant_id'):
                    raise gp_exc.InvalidCrossTenantReference(
                        res_type=primary_type, res_id=primary['id'],
                        ref_type=reference_type, ref_id=reference['id'])

    def _get_status_from_drivers(self, context, context_name, resource_name,
                                 resource_id, resource):
        status = resource['status']
        status_details = resource['status_details']
        policy_context = getattr(p_context, context_name)(
            self, context, resource, resource)
        getattr(self.policy_driver_manager,
                "get_" + resource_name + "_status")(policy_context)
        _resource = getattr(policy_context, "_" + resource_name)
        updated_status = _resource['status']
        updated_status_details = _resource['status_details']
        if status != updated_status or (
                    status_details != updated_status_details):
            new_status = {resource_name: {'status': updated_status,
                                          'status_details':
                                          updated_status_details}}
            with db_api.CONTEXT_WRITER.using(context):
                getattr(super(GroupPolicyPlugin, self),
                        "update_" + resource_name)(
                            context, _resource['id'], new_status)
            resource['status'] = updated_status
            resource['status_details'] = updated_status_details
        return resource

    def _get_resource(self, context, resource_name, resource_id,
                      gbp_context_name, fields=None):
        # The following is a writer because we do DB write for status
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            get_method = "".join(['get_', resource_name])
            result = getattr(super(GroupPolicyPlugin, self), get_method)(
                context, resource_id, None)
            extend_resources_method = "".join(['extend_', resource_name,
                                               '_dict'])
            getattr(self.extension_manager, extend_resources_method)(
                session, result)

            # Invoke drivers only if status attributes are requested
            if not fields or STATUS_SET.intersection(set(fields)):
                result = self._get_status_from_drivers(
                    context, gbp_context_name, resource_name, resource_id,
                    result)
            return db_api.resource_fields(result, fields)

    def _get_resources(self, context, resource_name, gbp_context_name,
                       filters=None, fields=None, sorts=None, limit=None,
                       marker=None, page_reverse=False):
        # The following is a writer because we do DB write for status
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            resource_plural = gbp_utils.get_resource_plural(resource_name)
            get_resources_method = "".join(['get_', resource_plural])
            results = getattr(super(GroupPolicyPlugin, self),
                              get_resources_method)(
                context, filters, None, sorts, limit, marker, page_reverse)
            filtered_results = []
            for result in results:
                extend_resources_method = "".join(['extend_', resource_name,
                                                   '_dict'])
                getattr(self.extension_manager, extend_resources_method)(
                    session, result)
                filtered = self._filter_extended_result(result, filters)
                if filtered:
                    filtered_results.append(filtered)

        new_filtered_results = []
        # Invoke drivers only if status attributes are requested
        if not fields or STATUS_SET.intersection(set(fields)):
            for result in filtered_results:
                result = self._get_status_from_drivers(
                    context, gbp_context_name, resource_name, result['id'],
                    result)
                new_filtered_results.append(result)
        new_filtered_results = new_filtered_results or filtered_results
        return [db_api.resource_fields(nfresult, fields) for nfresult in
                new_filtered_results]

    @resource_registry.tracked_resources(
        l3_policy=group_policy_mapping_db.L3PolicyMapping,
        l2_policy=group_policy_mapping_db.L2PolicyMapping,
        policy_target=group_policy_mapping_db.PolicyTargetMapping,
        policy_target_group=group_policy_mapping_db.PolicyTargetGroupMapping,
        application_policy_group=gpdb.ApplicationPolicyGroup,
        policy_classifier=gpdb.PolicyClassifier,
        policy_action=gpdb.PolicyAction,
        policy_rule=gpdb.PolicyRule,
        policy_rule_set=gpdb.PolicyRuleSet,
        external_policy=gpdb.ExternalPolicy,
        external_segment=group_policy_mapping_db.ExternalSegmentMapping,
        nat_pool=group_policy_mapping_db.NATPoolMapping,
        network_service_policy=gpdb.NetworkServicePolicy)
    def __init__(self):
        self.extension_manager = ext_manager.ExtensionManager()
        self.policy_driver_manager = manager.PolicyDriverManager()
        super(GroupPolicyPlugin, self).__init__()
        self.extension_manager.initialize()
        self.policy_driver_manager.initialize()

    def _filter_extended_result(self, result, filters):
        filters = filters or {}
        for field in filters:
            # Ignore unknown fields
            if field in result:
                if result[field] not in filters[field]:
                    break
        else:
            return result

    def _add_fixed_ips_to_port_attributes(self, policy_target):
        if 'fixed_ips' in policy_target['policy_target'] and (
                policy_target['policy_target']['fixed_ips'] is not (
                    constants.ATTR_NOT_SPECIFIED)):
            port_attributes = {'fixed_ips': policy_target[
                'policy_target']['fixed_ips']}
            policy_target['policy_target'].update(
                {'port_attributes': port_attributes})

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_policy_target(self, context, policy_target):
        self._ensure_tenant(context, policy_target['policy_target'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            self._add_fixed_ips_to_port_attributes(policy_target)
            result = super(GroupPolicyPlugin,
                           self).create_policy_target(context, policy_target)
            self.extension_manager.process_create_policy_target(
                session, policy_target, result)
            self._validate_shared_create(
                self, context, result, 'policy_target')
            policy_context = p_context.PolicyTargetContext(self, context,
                                                           result)
            self.policy_driver_manager.create_policy_target_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_policy_target_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("create_policy_target_postcommit "
                              "failed, deleting policy_target %s",
                              result['id'])
                self.delete_policy_target(context, result['id'])

        return self.get_policy_target(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_policy_target(self, context, policy_target_id, policy_target):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            self._add_fixed_ips_to_port_attributes(policy_target)
            original_policy_target = self.get_policy_target(context,
                                                            policy_target_id)
            updated_policy_target = super(
                GroupPolicyPlugin, self).update_policy_target(
                    context, policy_target_id, policy_target)
            self.extension_manager.process_update_policy_target(
                session, policy_target, updated_policy_target)
            self._validate_shared_update(self, context, original_policy_target,
                                         updated_policy_target,
                                         'policy_target')
            policy_context = p_context.PolicyTargetContext(
                self, context, updated_policy_target,
                original_policy_target=original_policy_target)
            self.policy_driver_manager.update_policy_target_precommit(
                policy_context)

        self.policy_driver_manager.update_policy_target_postcommit(
            policy_context)
        return self.get_policy_target(context, policy_target_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_policy_target(self, context, policy_target_id):
        with db_api.CONTEXT_WRITER.using(context):
            policy_target = self.get_policy_target(context, policy_target_id)
            policy_context = p_context.PolicyTargetContext(
                self, context, policy_target)
            self.policy_driver_manager.delete_policy_target_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_policy_target(
                context, policy_target_id)

        try:
            self.policy_driver_manager.delete_policy_target_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_policy_target_postcommit failed "
                          "for policy_target %s",
                          policy_target_id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_target(self, context, policy_target_id, fields=None):
        return self._get_resource(context, 'policy_target', policy_target_id,
                                  'PolicyTargetContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_targets(self, context, filters=None, fields=None,
                           sorts=None, limit=None, marker=None,
                           page_reverse=False):
        return self._get_resources(
            context, 'policy_target', 'PolicyTargetContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_policy_target_group(self, context, policy_target_group):
        self._ensure_tenant(context,
                            policy_target_group['policy_target_group'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_policy_target_group(
                               context, policy_target_group)
            self.extension_manager.process_create_policy_target_group(
                session, policy_target_group, result)
            self._validate_shared_create(self, context, result,
                                         'policy_target_group')
            policy_context = p_context.PolicyTargetGroupContext(
                self, context, result)
            self.policy_driver_manager.create_policy_target_group_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_policy_target_group_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("create_policy_target_group_postcommit "
                              "failed, deleting policy_target_group %s",
                              result['id'])
                self.delete_policy_target_group(context, result['id'])

        return self.get_policy_target_group(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_policy_target_group(self, context, policy_target_group_id,
                                   policy_target_group):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_policy_target_group = self.get_policy_target_group(
                    context, policy_target_group_id)
            updated_policy_target_group = super(
                GroupPolicyPlugin, self).update_policy_target_group(
                    context, policy_target_group_id, policy_target_group)
            # REVISIT(rkukura): We could potentially allow updates to
            # l2_policy_id when no policy targets exist. This would
            # involve removing each old subnet from the l3_policy's
            # router, deleting each old subnet, creating a new subnet on
            # the new l2_policy's network, and adding that subnet to the
            # l3_policy's router in postcommit. Its also possible that new
            # subnet[s] would be provided explicitly as part of the
            # update.
            old_l2p = original_policy_target_group['l2_policy_id']
            new_l2p = updated_policy_target_group['l2_policy_id']
            if old_l2p and old_l2p != new_l2p:
                raise gp_exc.L2PolicyUpdateOfPolicyTargetGroupNotSupported()

            self.extension_manager.process_update_policy_target_group(
                session, policy_target_group, updated_policy_target_group)
            self._validate_shared_update(
                self, context, original_policy_target_group,
                updated_policy_target_group, 'policy_target_group')
            policy_context = p_context.PolicyTargetGroupContext(
                self, context, updated_policy_target_group,
                original_policy_target_group=original_policy_target_group)
            self.policy_driver_manager.update_policy_target_group_precommit(
                policy_context)

        self.policy_driver_manager.update_policy_target_group_postcommit(
            policy_context)

        return self.get_policy_target_group(context, policy_target_group_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_policy_target_group(self, context, policy_target_group_id):
        with db_api.CONTEXT_WRITER.using(context):
            policy_target_group = self.get_policy_target_group(
                context, policy_target_group_id)
            pt_ids = policy_target_group['policy_targets']
            for pt in self.get_policy_targets(context.elevated(),
                                              {'id': pt_ids}):
                if pt['port_id'] and self._is_port_bound(pt['port_id']):
                    raise gp_exc.PolicyTargetGroupInUse(
                        policy_target_group=policy_target_group_id)
            policy_context = p_context.PolicyTargetGroupContext(
                self, context, policy_target_group)
            self.policy_driver_manager.delete_policy_target_group_precommit(
                policy_context)

        # Disassociate all the PRSs first, this will trigger service chains
        # deletion.
        self.update_policy_target_group(
            context, policy_target_group_id,
            {'policy_target_group': {'provided_policy_rule_sets': {},
                                     'consumed_policy_rule_sets': {}}})
        policy_context.current['provided_policy_rule_sets'] = []
        policy_context.current['consumed_policy_rule_sets'] = []

        # Proxy PTGs must be deleted before the group itself.
        if policy_target_group.get('proxy_group_id'):
            try:
                self.delete_policy_target_group(
                    context, policy_target_group['proxy_group_id'])
            except gpex.PolicyTargetGroupNotFound:
                LOG.warning('PTG %s already deleted',
                            policy_target_group['proxy_group_id'])

        # Delete the PTG's PTs, which should not be in-use.
        for pt in self.get_policy_targets(context, {'id': pt_ids}):
            self.delete_policy_target(context, pt['id'])

        # REVISIT: The DB layer method called here manages its own
        # transaction, but ideally would be called within the same
        # transaction as the precommit calls above. Maybe the cleanup
        # of PRSs, proxy PTGs and PTs, which much be done outside any
        # transaction, could be moved before the initial transaction,
        # possibly within a loop to ensure no PTs concurrently become
        # in-use before proceding with the PTG deletion transaction.
        super(GroupPolicyPlugin, self).delete_policy_target_group(
            context, policy_target_group_id)

        try:
            self.policy_driver_manager.delete_policy_target_group_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_policy_target_group_postcommit failed "
                          "for policy_target_group %s",
                          policy_target_group_id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_target_group(self, context, policy_target_group_id,
                                fields=None):
        return self._get_resource(context, 'policy_target_group',
                                  policy_target_group_id,
                                  'PolicyTargetGroupContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_target_groups(self, context, filters=None, fields=None,
                                 sorts=None, limit=None, marker=None,
                                 page_reverse=False):
        return self._get_resources(
            context, 'policy_target_group', 'PolicyTargetGroupContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_application_policy_group(self, context,
                                        application_policy_group):
        self._ensure_tenant(
            context, application_policy_group['application_policy_group'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            pdm = self.policy_driver_manager
            result = super(GroupPolicyPlugin,
                           self).create_application_policy_group(
                               context, application_policy_group)
            self.extension_manager.process_create_application_policy_group(
                session, application_policy_group, result)
            self._validate_shared_create(self, context, result,
                                         'application_policy_group')
            policy_context = p_context.ApplicationPolicyGroupContext(
                self, context, result)
            pdm.create_application_policy_group_precommit(policy_context)

        try:
            pdm.create_application_policy_group_postcommit(policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("create_application_policy_group_postcommit "
                              "failed, deleting APG %s",
                              result['id'])
                self.delete_application_policy_group(context, result['id'])

        return self.get_application_policy_group(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_application_policy_group(self, context,
                                        application_policy_group_id,
                                        application_policy_group):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            pdm = self.policy_driver_manager
            original_application_policy_group = (
                self.get_application_policy_group(
                    context, application_policy_group_id))
            updated_application_policy_group = super(
                GroupPolicyPlugin, self).update_application_policy_group(
                    context, application_policy_group_id,
                    application_policy_group)

            self.extension_manager.process_update_application_policy_group(
                session, application_policy_group,
                updated_application_policy_group)
            self._validate_shared_update(
                self, context, original_application_policy_group,
                updated_application_policy_group, 'application_policy_group')
            policy_context = p_context.ApplicationPolicyGroupContext(
                self, context, updated_application_policy_group,
                original_application_policy_group=(
                    original_application_policy_group))
            pdm.update_application_policy_group_precommit(policy_context)

        pdm.update_application_policy_group_postcommit(policy_context)

        return self.get_application_policy_group(context,
                                                 application_policy_group_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_application_policy_group(self, context,
                                        application_policy_group_id):
        with db_api.CONTEXT_WRITER.using(context):
            pdm = self.policy_driver_manager
            application_policy_group = self.get_application_policy_group(
                context, application_policy_group_id)
            policy_context = p_context.ApplicationPolicyGroupContext(
                self, context, application_policy_group)
            pdm.delete_application_policy_group_precommit(policy_context)

            super(GroupPolicyPlugin, self).delete_application_policy_group(
                context, application_policy_group_id)

        try:
            pdm.delete_application_policy_group_postcommit(policy_context)
        except Exception:
            LOG.exception("delete_application_policy_group_postcommit "
                          "failed for application_policy_group %s",
                          application_policy_group_id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_application_policy_group(self, context,
                                     application_policy_group_id, fields=None):
        return self._get_resource(context, 'application_policy_group',
                                  application_policy_group_id,
                                  'ApplicationPolicyGroupContext',
                                  fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_application_policy_groups(self, context, filters=None, fields=None,
                                      sorts=None, limit=None, marker=None,
                                      page_reverse=False):
        return self._get_resources(
            context, 'application_policy_group',
            'ApplicationPolicyGroupContext', filters=filters, fields=fields,
            sorts=sorts, limit=limit, marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_l2_policy(self, context, l2_policy):
        self._ensure_tenant(context, l2_policy['l2_policy'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_l2_policy(context, l2_policy)
            self.extension_manager.process_create_l2_policy(
                session, l2_policy, result)
            self._validate_shared_create(self, context, result, 'l2_policy')
            policy_context = p_context.L2PolicyContext(self, context, result)
            self.policy_driver_manager.create_l2_policy_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_l2_policy_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("create_l2_policy_postcommit "
                              "failed, deleting l2_policy %s",
                              result['id'])
                self.delete_l2_policy(context, result['id'])

        return self.get_l2_policy(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_l2_policy(self, context, l2_policy_id, l2_policy):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_l2_policy = self.get_l2_policy(context, l2_policy_id)
            updated_l2_policy = super(GroupPolicyPlugin,
                                      self).update_l2_policy(
                                          context, l2_policy_id, l2_policy)
            self.extension_manager.process_update_l2_policy(
                session, l2_policy, updated_l2_policy)
            self._validate_shared_update(self, context, original_l2_policy,
                                         updated_l2_policy, 'l2_policy')
            policy_context = p_context.L2PolicyContext(
                self, context, updated_l2_policy,
                original_l2_policy=original_l2_policy)
            self.policy_driver_manager.update_l2_policy_precommit(
                policy_context)

        self.policy_driver_manager.update_l2_policy_postcommit(
            policy_context)

        return self.get_l2_policy(context, l2_policy_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_l2_policy(self, context, l2_policy_id):
        with db_api.CONTEXT_WRITER.using(context):
            l2_policy = self.get_l2_policy(context, l2_policy_id)
            policy_context = p_context.L2PolicyContext(self, context,
                                                       l2_policy)
            self.policy_driver_manager.delete_l2_policy_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_l2_policy(context,
                                                            l2_policy_id)

        try:
            self.policy_driver_manager.delete_l2_policy_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_l2_policy_postcommit failed "
                          "for l2_policy %s", l2_policy_id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_l2_policy(self, context, l2_policy_id, fields=None):
        return self._get_resource(context, 'l2_policy',
                                  l2_policy_id,
                                  'L2PolicyContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_l2_policies(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        return self._get_resources(
            context, 'l2_policy', 'L2PolicyContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_network_service_policy(self, context, network_service_policy):
        self._ensure_tenant(
            context, network_service_policy['network_service_policy'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_network_service_policy(
                               context, network_service_policy)
            self.extension_manager.process_create_network_service_policy(
                session, network_service_policy, result)
            self._validate_shared_create(self, context, result,
                                         'network_service_policy')
            policy_context = p_context.NetworkServicePolicyContext(
                self, context, result)
            pdm = self.policy_driver_manager
            pdm.create_network_service_policy_precommit(
                policy_context)

        try:
            pdm.create_network_service_policy_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(
                    "create_network_service_policy_postcommit "
                    "failed, deleting network_service_policy %s",
                    result['id'])
                self.delete_network_service_policy(context, result['id'])

        return self.get_network_service_policy(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_network_service_policy(self, context, network_service_policy_id,
                                      network_service_policy):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_network_service_policy = super(
                GroupPolicyPlugin, self).get_network_service_policy(
                    context, network_service_policy_id)
            updated_network_service_policy = super(
                GroupPolicyPlugin, self).update_network_service_policy(
                    context, network_service_policy_id, network_service_policy)
            self.extension_manager.process_update_network_service_policy(
                session, network_service_policy,
                updated_network_service_policy)
            self._validate_shared_update(
                self, context, original_network_service_policy,
                updated_network_service_policy, 'network_service_policy')
            policy_context = p_context.NetworkServicePolicyContext(
                self, context, updated_network_service_policy,
                original_network_service_policy=(
                    original_network_service_policy))
            self.policy_driver_manager.update_network_service_policy_precommit(
                policy_context)

        self.policy_driver_manager.update_network_service_policy_postcommit(
            policy_context)
        return self.get_network_service_policy(context,
                                               network_service_policy_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_network_service_policy(
        self, context, network_service_policy_id):
        with db_api.CONTEXT_WRITER.using(context):
            network_service_policy = self.get_network_service_policy(
                context, network_service_policy_id)
            policy_context = p_context.NetworkServicePolicyContext(
                self, context, network_service_policy)
            self.policy_driver_manager.delete_network_service_policy_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_network_service_policy(
                context, network_service_policy_id)

        try:
            pdm = self.policy_driver_manager
            pdm.delete_network_service_policy_postcommit(policy_context)
        except Exception:
            LOG.exception(
                "delete_network_service_policy_postcommit failed "
                "for network_service_policy %s", network_service_policy_id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_network_service_policy(self, context, network_service_policy_id,
                                   fields=None):
        return self._get_resource(context, 'network_service_policy',
                                  network_service_policy_id,
                                  'NetworkServicePolicyContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_network_service_policies(self, context, filters=None, fields=None,
                                     sorts=None, limit=None, marker=None,
                                     page_reverse=False):
        return self._get_resources(
            context, 'network_service_policy', 'NetworkServicePolicyContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_l3_policy(self, context, l3_policy):
        self._ensure_tenant(context, l3_policy['l3_policy'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_l3_policy(context, l3_policy)
            self.extension_manager.process_create_l3_policy(
                session, l3_policy, result)
            self._validate_shared_create(self, context, result, 'l3_policy')
            self._validate_l3p_es(context, result)
            policy_context = p_context.L3PolicyContext(self, context,
                                                       result)
            self.policy_driver_manager.create_l3_policy_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_l3_policy_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("create_l3_policy_postcommit "
                              "failed, deleting l3_policy %s",
                              result['id'])
                self.delete_l3_policy(context, result['id'])

        return self.get_l3_policy(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_l3_policy(self, context, l3_policy_id, l3_policy):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_l3_policy = self.get_l3_policy(context, l3_policy_id)
            updated_l3_policy = super(
                GroupPolicyPlugin, self).update_l3_policy(
                    context, l3_policy_id, l3_policy)
            self.extension_manager.process_update_l3_policy(
                session, l3_policy, updated_l3_policy)
            self._validate_shared_update(self, context, original_l3_policy,
                                         updated_l3_policy, 'l3_policy')
            self._validate_l3p_es(context, updated_l3_policy,
                                  original_l3_policy)
            policy_context = p_context.L3PolicyContext(
                self, context, updated_l3_policy,
                original_l3_policy=original_l3_policy)
            self.policy_driver_manager.update_l3_policy_precommit(
                policy_context)

        self.policy_driver_manager.update_l3_policy_postcommit(
            policy_context)
        return self.get_l3_policy(context, l3_policy_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_l3_policy(self, context, l3_policy_id, check_unused=False):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            if (check_unused and
                (session.query(group_policy_mapping_db.L2PolicyMapping).
                 filter_by(l3_policy_id=l3_policy_id).count())):
                return False
            l3_policy = self.get_l3_policy(context, l3_policy_id)
            policy_context = p_context.L3PolicyContext(self, context,
                                                       l3_policy)
            self.policy_driver_manager.delete_l3_policy_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_l3_policy(context,
                                                            l3_policy_id)

        try:
            self.policy_driver_manager.delete_l3_policy_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_l3_policy_postcommit failed "
                          "for l3_policy %s", l3_policy_id)
        return True

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_l3_policy(self, context, l3_policy_id, fields=None):
        return self._get_resource(context, 'l3_policy',
                                  l3_policy_id,
                                  'L3PolicyContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_l3_policies(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        return self._get_resources(
            context, 'l3_policy', 'L3PolicyContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_policy_classifier(self, context, policy_classifier):
        self._ensure_tenant(context, policy_classifier['policy_classifier'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(
                GroupPolicyPlugin, self).create_policy_classifier(
                    context, policy_classifier)
            self.extension_manager.process_create_policy_classifier(
                session, policy_classifier, result)
            self._validate_shared_create(
                self, context, result, 'policy_classifier')
            policy_context = p_context.PolicyClassifierContext(self, context,
                                                               result)
            self.policy_driver_manager.create_policy_classifier_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_policy_classifier_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(
                    "policy_driver_manager.create_policy_classifier_postcommit"
                    " failed, deleting policy_classifier %s", result['id'])
                self.delete_policy_classifier(context, result['id'])

        return self.get_policy_classifier(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_policy_classifier(self, context, id, policy_classifier):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_policy_classifier = super(
                GroupPolicyPlugin, self).get_policy_classifier(context, id)
            updated_policy_classifier = super(
                GroupPolicyPlugin, self).update_policy_classifier(
                    context, id, policy_classifier)
            self.extension_manager.process_update_policy_classifier(
                session, policy_classifier, updated_policy_classifier)
            self._validate_shared_update(
                self, context, original_policy_classifier,
                updated_policy_classifier, 'policy_classifier')
            policy_context = p_context.PolicyClassifierContext(
                self, context, updated_policy_classifier,
                original_policy_classifier=original_policy_classifier)
            self.policy_driver_manager.update_policy_classifier_precommit(
                policy_context)

        self.policy_driver_manager.update_policy_classifier_postcommit(
            policy_context)
        return self.get_policy_classifier(context, id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_policy_classifier(self, context, id):
        with db_api.CONTEXT_WRITER.using(context):
            policy_classifier = self.get_policy_classifier(context, id)
            policy_context = p_context.PolicyClassifierContext(
                self, context, policy_classifier)
            self.policy_driver_manager.delete_policy_classifier_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_policy_classifier(
                context, id)

        try:
            self.policy_driver_manager.delete_policy_classifier_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_policy_classifier_postcommit failed "
                          "for policy_classifier %s", id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_classifier(self, context, policy_classifier_id,
                              fields=None):
        return self._get_resource(context, 'policy_classifier',
                                  policy_classifier_id,
                                  'PolicyClassifierContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_classifiers(self, context, filters=None, fields=None,
                               sorts=None, limit=None, marker=None,
                               page_reverse=False):
        return self._get_resources(
            context, 'policy_classifier', 'PolicyClassifierContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_policy_action(self, context, policy_action):
        self._ensure_tenant(context, policy_action['policy_action'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_policy_action(context, policy_action)
            self.extension_manager.process_create_policy_action(
                session, policy_action, result)
            self._validate_shared_create(self, context, result,
                                         'policy_action')
            self._validate_action_value(context, result)
            policy_context = p_context.PolicyActionContext(self, context,
                                                           result)
            self.policy_driver_manager.create_policy_action_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_policy_action_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(
                    "policy_driver_manager.create_policy_action_postcommit "
                    "failed, deleting policy_action %s", result['id'])
                self.delete_policy_action(context, result['id'])

        return self.get_policy_action(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_policy_action(self, context, id, policy_action):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_policy_action = super(
                GroupPolicyPlugin, self).get_policy_action(context, id)
            updated_policy_action = super(
                GroupPolicyPlugin, self).update_policy_action(context, id,
                                                              policy_action)
            self.extension_manager.process_update_policy_action(
                session, policy_action, updated_policy_action)
            self._validate_shared_update(self, context, original_policy_action,
                                         updated_policy_action,
                                         'policy_action')
            self._validate_action_value(context, updated_policy_action)
            policy_context = p_context.PolicyActionContext(
                self, context, updated_policy_action,
                original_policy_action=original_policy_action)
            self.policy_driver_manager.update_policy_action_precommit(
                policy_context)

        self.policy_driver_manager.update_policy_action_postcommit(
            policy_context)
        return self.get_policy_action(context, id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_policy_action(self, context, id):
        with db_api.CONTEXT_WRITER.using(context):
            policy_action = self.get_policy_action(context, id)
            policy_context = p_context.PolicyActionContext(self, context,
                                                           policy_action)
            self.policy_driver_manager.delete_policy_action_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_policy_action(context, id)

        try:
            self.policy_driver_manager.delete_policy_action_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_policy_action_postcommit failed "
                          "for policy_action %s", id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_action(self, context, policy_action_id, fields=None):
        return self._get_resource(context, 'policy_action',
                                  policy_action_id,
                                  'PolicyActionContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_actions(self, context, filters=None, fields=None,
                           sorts=None, limit=None, marker=None,
                           page_reverse=False):
        return self._get_resources(
            context, 'policy_action', 'PolicyActionContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_policy_rule(self, context, policy_rule):
        self._ensure_tenant(context, policy_rule['policy_rule'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(
                GroupPolicyPlugin, self).create_policy_rule(
                    context, policy_rule)
            self.extension_manager.process_create_policy_rule(
                session, policy_rule, result)
            self._validate_shared_create(self, context, result, 'policy_rule')
            policy_context = p_context.PolicyRuleContext(self, context,
                                                         result)
            self.policy_driver_manager.create_policy_rule_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_policy_rule_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(
                    "policy_driver_manager.create_policy_rule_postcommit"
                    " failed, deleting policy_rule %s", result['id'])
                self.delete_policy_rule(context, result['id'])

        return self.get_policy_rule(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_policy_rule(self, context, id, policy_rule):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_policy_rule = super(
                GroupPolicyPlugin, self).get_policy_rule(context, id)
            updated_policy_rule = super(
                GroupPolicyPlugin, self).update_policy_rule(
                    context, id, policy_rule)
            self.extension_manager.process_update_policy_rule(
                session, policy_rule, updated_policy_rule)
            self._validate_shared_update(self, context, original_policy_rule,
                                         updated_policy_rule, 'policy_rule')
            policy_context = p_context.PolicyRuleContext(
                self, context, updated_policy_rule,
                original_policy_rule=original_policy_rule)
            self.policy_driver_manager.update_policy_rule_precommit(
                policy_context)

        self.policy_driver_manager.update_policy_rule_postcommit(
            policy_context)
        return self.get_policy_rule(context, id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_policy_rule(self, context, id):
        with db_api.CONTEXT_WRITER.using(context):
            policy_rule = self.get_policy_rule(context, id)
            policy_context = p_context.PolicyRuleContext(self, context,
                                                         policy_rule)
            self.policy_driver_manager.delete_policy_rule_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_policy_rule(
                context, id)

        try:
            self.policy_driver_manager.delete_policy_rule_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_policy_rule_postcommit failed "
                          "for policy_rule %s", id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_rule(self, context, policy_rule_id, fields=None):
        return self._get_resource(context, 'policy_rule',
                                  policy_rule_id,
                                  'PolicyRuleContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_rules(self, context, filters=None, fields=None,
                         sorts=None, limit=None, marker=None,
                         page_reverse=False):
        return self._get_resources(
            context, 'policy_rule', 'PolicyRuleContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_policy_rule_set(self, context, policy_rule_set):
        self._ensure_tenant(context, policy_rule_set['policy_rule_set'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_policy_rule_set(
                               context, policy_rule_set)
            self.extension_manager.process_create_policy_rule_set(
                session, policy_rule_set, result)
            self._validate_shared_create(
                self, context, result, 'policy_rule_set')
            policy_context = p_context.PolicyRuleSetContext(
                self, context, result)
            self.policy_driver_manager.create_policy_rule_set_precommit(
                policy_context)

        try:
            self.policy_driver_manager.create_policy_rule_set_postcommit(
                policy_context)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(
                    "policy_driver_manager.create_policy_rule_set_postcommit "
                    "failed, deleting policy_rule_set %s", result['id'])
                self.delete_policy_rule_set(context, result['id'])

        return self.get_policy_rule_set(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_policy_rule_set(self, context, id, policy_rule_set):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_policy_rule_set = super(
                GroupPolicyPlugin, self).get_policy_rule_set(context, id)
            updated_policy_rule_set = super(
                GroupPolicyPlugin, self).update_policy_rule_set(
                    context, id, policy_rule_set)
            self.extension_manager.process_update_policy_rule_set(
                session, policy_rule_set, updated_policy_rule_set)
            self._validate_shared_update(
                self, context, original_policy_rule_set,
                updated_policy_rule_set, 'policy_rule_set')
            policy_context = p_context.PolicyRuleSetContext(
                self, context, updated_policy_rule_set,
                original_policy_rule_set=original_policy_rule_set)
            self.policy_driver_manager.update_policy_rule_set_precommit(
                policy_context)

        self.policy_driver_manager.update_policy_rule_set_postcommit(
            policy_context)
        return self.get_policy_rule_set(context, id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_policy_rule_set(self, context, id):
        with db_api.CONTEXT_WRITER.using(context):
            policy_rule_set = self.get_policy_rule_set(context, id)
            policy_context = p_context.PolicyRuleSetContext(
                self, context, policy_rule_set)
            self.policy_driver_manager.delete_policy_rule_set_precommit(
                policy_context)
            super(GroupPolicyPlugin, self).delete_policy_rule_set(context, id)

        try:
            self.policy_driver_manager.delete_policy_rule_set_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_policy_rule_set_postcommit failed "
                          "for policy_rule_set %s", id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_rule_set(self, context, policy_rule_set_id, fields=None):
        return self._get_resource(context, 'policy_rule_set',
                                  policy_rule_set_id,
                                  'PolicyRuleSetContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_policy_rule_sets(self, context, filters=None, fields=None,
                             sorts=None, limit=None, marker=None,
                             page_reverse=False):
        return self._get_resources(
            context, 'policy_rule_set', 'PolicyRuleSetContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_external_segment(self, context, external_segment):
        self._ensure_tenant(context, external_segment['external_segment'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_external_segment(context,
                                                         external_segment)
            self.extension_manager.process_create_external_segment(
                session, external_segment, result)
            self._validate_shared_create(self, context, result,
                                         'external_segment')
            policy_context = p_context.ExternalSegmentContext(
                self, context, result)
            (self.policy_driver_manager.
             create_external_segment_precommit(policy_context))
            # Validate the routes after the drivers had the chance to fill
            # the cidr field.
            self._validate_routes(context, result)

        try:
            (self.policy_driver_manager.
             create_external_segment_postcommit(policy_context))
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("create_external_segment_postcommit "
                              "failed, deleting external_segment "
                              "%s", result['id'])
                self.delete_external_segment(context, result['id'])

        return self.get_external_segment(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_external_segment(self, context, external_segment_id,
                                external_segment):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_external_segment = super(
                GroupPolicyPlugin, self).get_external_segment(
                    context, external_segment_id)
            updated_external_segment = super(
                GroupPolicyPlugin, self).update_external_segment(
                    context, external_segment_id,
                    external_segment)
            self.extension_manager.process_update_external_segment(
                session, external_segment, updated_external_segment)
            self._validate_shared_update(
                self, context, original_external_segment,
                updated_external_segment, 'external_segment')
            self._validate_routes(context, updated_external_segment,
                                  original_external_segment)
            # TODO(ivar): Validate Routes' GW in es subnet
            policy_context = p_context.ExternalSegmentContext(
                self, context, updated_external_segment,
                original_external_segment)
            (self.policy_driver_manager.
             update_external_segment_precommit(policy_context))

        self.policy_driver_manager.update_external_segment_postcommit(
            policy_context)
        return self.get_external_segment(context, external_segment_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_external_segment(self, context, external_segment_id):
        with db_api.CONTEXT_WRITER.using(context):
            es = self.get_external_segment(context, external_segment_id)
            if es['l3_policies'] or es['nat_pools'] or es['external_policies']:
                raise gpex.ExternalSegmentInUse(es_id=es['id'])
            policy_context = p_context.ExternalSegmentContext(
                self, context, es)
            (self.policy_driver_manager.
             delete_external_segment_precommit(policy_context))
            super(GroupPolicyPlugin, self).delete_external_segment(
                context, external_segment_id)

        try:
            (self.policy_driver_manager.
             delete_external_segment_postcommit(policy_context))
        except Exception:
            LOG.exception("delete_external_segment_postcommit failed "
                          "for external_segment %s",
                          external_segment_id)
        return True

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_external_segment(self, context, external_segment_id, fields=None):
        return self._get_resource(context, 'external_segment',
                                  external_segment_id,
                                  'ExternalSegmentContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_external_segments(self, context, filters=None, fields=None,
                              sorts=None, limit=None, marker=None,
                              page_reverse=False):
        return self._get_resources(
            context, 'external_segment', 'ExternalSegmentContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_external_policy(self, context, external_policy):
        self._ensure_tenant(context, external_policy['external_policy'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin,
                           self).create_external_policy(
                               context, external_policy)
            self.extension_manager.process_create_external_policy(
                session, external_policy, result)
            self._validate_shared_create(self, context, result,
                                         'external_policy')
            policy_context = p_context.ExternalPolicyContext(
                self, context, result)
            (self.policy_driver_manager.
             create_external_policy_precommit(policy_context))

        try:
            (self.policy_driver_manager.
             create_external_policy_postcommit(policy_context))
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("create_external_policy_postcommit "
                              "failed, deleting external_policy "
                              "%s", result['id'])
                self.delete_external_policy(context, result['id'])

        return self.get_external_policy(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_external_policy(self, context, external_policy_id,
                               external_policy):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_external_policy = super(
                GroupPolicyPlugin, self).get_external_policy(
                    context, external_policy_id)
            updated_external_policy = super(
                GroupPolicyPlugin, self).update_external_policy(
                    context, external_policy_id,
                    external_policy)
            self.extension_manager.process_update_external_policy(
                session, external_policy, updated_external_policy)
            self._validate_shared_update(
                self, context, original_external_policy,
                updated_external_policy, 'external_policy')
            policy_context = p_context.ExternalPolicyContext(
                self, context, updated_external_policy,
                original_external_policy)
            (self.policy_driver_manager.
             update_external_policy_precommit(policy_context))

        self.policy_driver_manager.update_external_policy_postcommit(
            policy_context)
        return self.get_external_policy(context, external_policy_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_external_policy(self, context, external_policy_id,
                               check_unused=False):
        with db_api.CONTEXT_WRITER.using(context):
            es = self.get_external_policy(context, external_policy_id)
            policy_context = p_context.ExternalPolicyContext(
                self, context, es)
            (self.policy_driver_manager.
             delete_external_policy_precommit(policy_context))
            super(GroupPolicyPlugin, self).delete_external_policy(
                context, external_policy_id)

        try:
            self.policy_driver_manager.delete_external_policy_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_external_policy_postcommit failed "
                          "for external_policy %s", external_policy_id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_external_policy(self, context, external_policy_id, fields=None):
        return self._get_resource(context, 'external_policy',
                                  external_policy_id,
                                  'ExternalPolicyContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_external_policies(self, context, filters=None, fields=None,
                              sorts=None, limit=None, marker=None,
                              page_reverse=False):
        return self._get_resources(
            context, 'external_policy', 'ExternalPolicyContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def create_nat_pool(self, context, nat_pool):
        self._ensure_tenant(context, nat_pool['nat_pool'])
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            result = super(GroupPolicyPlugin, self).create_nat_pool(
                context, nat_pool)
            self.extension_manager.process_create_nat_pool(session, nat_pool,
                                                           result)
            self._validate_shared_create(self, context, result, 'nat_pool')
            policy_context = p_context.NatPoolContext(self, context, result)
            (self.policy_driver_manager.
             create_nat_pool_precommit(policy_context))

        try:
            (self.policy_driver_manager.
             create_nat_pool_postcommit(policy_context))
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(
                    "create_nat_pool_postcommit failed, deleting "
                    "nat_pool %s", result['id'])
                self.delete_nat_pool(context, result['id'])

        return self.get_nat_pool(context, result['id'])

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def update_nat_pool(self, context, nat_pool_id, nat_pool):
        with db_api.CONTEXT_WRITER.using(context):
            session = context.session
            original_nat_pool = super(
                GroupPolicyPlugin, self).get_nat_pool(context, nat_pool_id)
            updated_nat_pool = super(
                GroupPolicyPlugin, self).update_nat_pool(context, nat_pool_id,
                                                         nat_pool)
            self.extension_manager.process_update_nat_pool(
                session, nat_pool, updated_nat_pool)
            self._validate_shared_update(self, context, original_nat_pool,
                                         updated_nat_pool, 'nat_pool')
            policy_context = p_context.NatPoolContext(
                self, context, updated_nat_pool, original_nat_pool)
            (self.policy_driver_manager.
             update_nat_pool_precommit(policy_context))

        self.policy_driver_manager.update_nat_pool_postcommit(policy_context)
        return self.get_nat_pool(context, nat_pool_id)

    @log.log_method_call
    @n_utils.transaction_guard
    @db_api.retry_if_session_inactive()
    def delete_nat_pool(self, context, nat_pool_id, check_unused=False):
        with db_api.CONTEXT_WRITER.using(context):
            es = self.get_nat_pool(context, nat_pool_id)
            policy_context = p_context.NatPoolContext(self, context, es)
            (self.policy_driver_manager.delete_nat_pool_precommit(
                policy_context))
            super(GroupPolicyPlugin, self).delete_nat_pool(context,
                                                           nat_pool_id)

        try:
            self.policy_driver_manager.delete_nat_pool_postcommit(
                policy_context)
        except Exception:
            LOG.exception("delete_nat_pool_postcommit failed "
                          "for nat_pool %s",
                          nat_pool_id)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_nat_pool(self, context, nat_pool_id, fields=None):
        return self._get_resource(context, 'nat_pool',
                                  nat_pool_id,
                                  'NatPoolContext', fields=fields)

    @log.log_method_call
    @db_api.retry_if_session_inactive()
    def get_nat_pools(self, context, filters=None, fields=None,
                      sorts=None, limit=None, marker=None,
                      page_reverse=False):
        return self._get_resources(
            context, 'nat_pool', 'NatPoolContext',
            filters=filters, fields=fields, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)

    def _is_port_bound(self, port_id):
        # REVISIT(ivar): This operation shouldn't be done within a DB lock
        # once we refactor the server.
        not_bound = [portbindings.VIF_TYPE_UNBOUND,
                     portbindings.VIF_TYPE_BINDING_FAILED]
        context = n_ctx.get_admin_context()
        port = directory.get_plugin().get_port(context, port_id)
        return (port.get('binding:vif_type') not in not_bound) and port.get(
            'binding:host_id') and (port['device_owner'] or port['device_id'])

    def _ensure_tenant(self, context, resource):
        # TODO(Sumit): This check is ideally not required, but a bunch of UTs
        # are not setup correctly to populate the tenant_id, hence we
        # temporarily need to perform this check. This will go with the fix
        # for the deprecated get_tenant_id_for_create method.
        if 'tenant_id' in resource:
            tenant_id = resource['tenant_id']
            self.policy_driver_manager.ensure_tenant(context, tenant_id)
