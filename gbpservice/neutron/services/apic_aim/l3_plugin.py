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

from neutron.api import extensions
from neutron.common import utils as n_utils
from neutron.db import dns_db
from neutron.db import extraroute_db
from neutron.db import l3_gwmode_db
from neutron.db.models import l3 as l3_db
from neutron.quota import resource_registry
from neutron_lib.api.definitions import l3 as l3_def
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as lib_constants
from neutron_lib.db import model_query
from neutron_lib.db import resource_extend
from neutron_lib.db import utils as db_utils
from neutron_lib import exceptions
from neutron_lib.plugins import constants
from neutron_lib.plugins import directory
from neutron_lib.services.qos import constants as qos_consts
from oslo_log import log as logging
from oslo_utils import excutils

from gbpservice._i18n import _
from gbpservice.neutron.db import api as db_api
from gbpservice.neutron import extensions as extensions_pkg
from gbpservice.neutron.extensions import cisco_apic_l3 as l3_ext
from gbpservice.neutron.plugins.ml2plus import driver_api as api_plus
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    exceptions as aim_exceptions)
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    extension_db as extn_db)
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    mechanism_driver as md)


LOG = logging.getLogger(__name__)

EXTERNAL_GW_INFO = l3_def.EXTERNAL_GW_INFO


@resource_extend.has_resource_extenders
class ApicL3Plugin(extraroute_db.ExtraRoute_db_mixin,
                   l3_gwmode_db.L3_NAT_db_mixin,
                   extn_db.ExtensionDbMixin,
                   dns_db.DNSDbMixin):

    supported_extension_aliases = ["router", "ext-gw-mode", "extraroute",
                                   "cisco-apic-l3", "dns-integration"]

    @staticmethod
    def get_plugin_type():
        return constants.L3

    @staticmethod
    def get_plugin_description():
        return _("L3 Router Service Plugin using the APIC via AIM")

    @resource_registry.tracked_resources(router=l3_db.Router,
                                         floatingip=l3_db.FloatingIP)
    def __init__(self):
        LOG.info("APIC AIM L3 Plugin __init__")
        extensions.append_api_extensions_path(extensions_pkg.__path__)
        self._mechanism_driver = None
        super(ApicL3Plugin, self).__init__()

    @property
    def _md(self):
        if not self._mechanism_driver:
            # REVISIT(rkukura): It might be safer to search the MDs by
            # class rather than index by name, or to use a class
            # variable to find the instance.
            mech_mgr = self._core_plugin.mechanism_manager
            self._mechanism_driver = mech_mgr.mech_drivers['apic_aim'].obj
        return self._mechanism_driver

    @staticmethod
    @resource_extend.extends([l3_def.ROUTERS])
    def _extend_router_dict_apic(router_res, router_db):
        if router_res.get(api_plus.BULK_EXTENDED):
            return

        LOG.debug("APIC AIM L3 Plugin extending router dict: %s", router_res)
        plugin = directory.get_plugin(constants.L3)
        session = db_api.get_session_from_obj(router_db)
        if not session:
            session = db_api.get_writer_session()
        try:
            plugin._md.extend_router_dict(session, router_db, router_res)
            plugin._include_router_extn_attr(session, router_res)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("APIC AIM extend_router_dict failed")

    @staticmethod
    @resource_extend.extends([l3_def.ROUTERS + '_BULK'])
    def _extend_router_dict_bulk_apic(routers, _):
        LOG.debug("APIC AIM L3 Plugin bulk extending router dict: %s",
                  routers)
        if not routers:
            return
        router_db = routers[0]
        plugin = directory.get_plugin(constants.L3)
        session = db_api.get_session_from_obj(router_db)
        if not session:
            session = db_api.get_writer_session()
        try:
            plugin._md.extend_router_dict_bulk(session, routers)
            plugin._include_router_extn_attr_bulk(session, routers)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("APIC AIM _extend_router_dict_bulk failed")

    def _make_routers_dict(self, routers, fields=None):
        results = []
        for router in routers:
            res = self._make_router_dict(router, fields,
                                         process_extensions=False)
            res[api_plus.BULK_EXTENDED] = True
            # If fields is not None, then the dictionary returned
            # above contains only the fields specified. Here we add
            # the gateway, since the subsequent call to apply_funcs
            # raises an exception due to this missing field when
            # extending the dictionary with the gateway (note: we
            # also are keeping this call consistent with upstream code).
            if router['gw_port_id']:
                ext_gw_info = {
                    'network_id': router.gw_port['network_id'],
                    'external_fixed_ips': [
                        {'subnet_id': ip["subnet_id"],
                         'ip_address': ip["ip_address"]}
                        for ip in router.gw_port['fixed_ips']]}
            else:
                ext_gw_info = None
            res.update({
                EXTERNAL_GW_INFO: ext_gw_info,
                'gw_port_id': router['gw_port_id'],
            })
            resource_extend.apply_funcs(l3_def.ROUTERS, res, router)
            res.pop(api_plus.BULK_EXTENDED, None)
            results.append(res)

        resource_extend.apply_funcs(l3_def.ROUTERS + '_BULK',
                                    results, None)
        return results

    def validate_snat_extension(self, context, gw_info):
        for fixed in gw_info['external_fixed_ips']:
            sub = None
            try:
                sub = self._core_plugin.get_subnet(context,
                                                   fixed['subnet_id'])
            except exceptions.SubnetNotFound:
                pass

            if (sub and sub['network_id'] != gw_info['network_id']):
                raise exceptions.SubnetNotFound(subnet_id=fixed['subnet_id'])

            # subnet should be used for snat ip only
            if (sub and sub['apic:snat_subnet_only'] is True):
                raise (aim_exceptions.SnatPoolCannotBeUsedForGatewayIp())

    # Overwrite the upstream implementation to take advantage
    # of the bulk extension support.
    @db_api.retry_if_session_inactive()
    def get_routers(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        marker_obj = db_utils.get_marker_obj(self, context, 'router',
                                             limit, marker)
        routers_db = model_query.get_collection(context, l3_db.Router,
                                                dict_func=None,
                                                filters=filters,
                                                fields=fields,
                                                sorts=sorts,
                                                limit=limit,
                                                marker_obj=marker_obj,
                                                page_reverse=page_reverse)
        return self._make_routers_dict(routers_db, fields)

    def _check_snat_subnets(self, context, gw_info, router):
        self._core_plugin._get_network(context, gw_info['network_id'])
        if ('external_fixed_ips' in gw_info):
            # Check to make sure they haven't picked a subnet
            # that has an extension set to disallow it.
            self.validate_snat_extension(context, gw_info)
        else:
            # No subnet set, so look for at most one subnet in
            # each address family to include in the request,
            # skipping any subnets with the SNAT only extension
            # set to True.
            subnets_in_nw = self._core_plugin.get_subnets_by_network(context,
                gw_info['network_id'])
            subs = {}
            for ext_sn in subnets_in_nw:
                if (ext_sn['apic:snat_subnet_only'] is False and
                        not subs.get(ext_sn['ip_version'])):
                    subs[ext_sn['ip_version']] = {'subnet_id': ext_sn['id']}
            gw_info.update({'external_fixed_ips': list(subs.values())})
            router['router'].update({EXTERNAL_GW_INFO: gw_info})

    # REVISIT: Eliminate this wrapper?
    @n_utils.transaction_guard
    def create_router(self, context, router):
        LOG.debug("APIC AIM L3 Plugin creating router: %s", router)
        # REVISIT: Do this in MD by registering for
        # ROUTER.BEFORE_CREATE event.
        self._md.ensure_tenant(context, router['router']['tenant_id'])

        current = router['router']
        gw_info = (current[EXTERNAL_GW_INFO] if EXTERNAL_GW_INFO in current
                   else lib_constants.ATTR_NOT_SPECIFIED)
        if (gw_info and gw_info != lib_constants.ATTR_NOT_SPECIFIED and
                'network_id' in gw_info):
            self._check_snat_subnets(context, gw_info, router)
        return super(ApicL3Plugin, self).create_router(context, router)

    # REVISIT: Eliminate this wrapper?
    @n_utils.transaction_guard
    def update_router(self, context, id, router):
        original = self.get_router(context, id)
        current = router['router']
        original_gw_info = (original[EXTERNAL_GW_INFO]
                            if EXTERNAL_GW_INFO in original
                            else lib_constants.ATTR_NOT_SPECIFIED)
        current_gw_info = (current[EXTERNAL_GW_INFO]
                           if EXTERNAL_GW_INFO in current
                           else lib_constants.ATTR_NOT_SPECIFIED)

        LOG.debug("APIC AIM L3 Plugin updating router %(id)s with: %(router)s",
                  {'id': id, 'router': router})

        # If the gateway is being set, do additional validation for
        # the SNAT only subnets extension.
        if ((current_gw_info and
             current_gw_info != lib_constants.ATTR_NOT_SPECIFIED) and
            (not original_gw_info or
             original_gw_info == lib_constants.ATTR_NOT_SPECIFIED) and (
             'network_id' in current_gw_info)):
            self._check_snat_subnets(context, current_gw_info, router)
        return super(ApicL3Plugin, self).update_router(context, id, router)

    # REVISIT: Eliminate this wrapper?
    @n_utils.transaction_guard
    def delete_router(self, context, id):
        LOG.debug("APIC AIM L3 Plugin deleting router: %s", id)
        super(ApicL3Plugin, self).delete_router(context, id)

    def _include_router_extn_attr(self, session, router):
        attr = self.get_router_extn_db(session, router['id'])
        router.update(attr)

    def _include_router_extn_attr_bulk(self, session, routers):
        router_ids = [router['id'] for router in routers]
        attr_dict = self.get_router_extn_db_bulk(session, router_ids)
        for router in routers:
            router.update(attr_dict[router['id']] if router['id'] in attr_dict
                          else {l3_ext.EXTERNAL_PROVIDED_CONTRACTS: [],
                                l3_ext.EXTERNAL_CONSUMED_CONTRACTS: []})

    @n_utils.transaction_guard
    def add_router_interface(self, context, router_id, interface_info):
        LOG.debug("APIC AIM L3 Plugin adding interface %(interface)s "
                  "to router %(router)s",
                  {'interface': interface_info, 'router': router_id})
        # REVISIT: Remove override flag when no longer needed for GBP.
        context.override_network_routing_topology_validation = (
            interface_info.get(
                l3_ext.OVERRIDE_NETWORK_ROUTING_TOPOLOGY_VALIDATION))
        # REVISIT: The REST API layer unwraps these exceptions, such
        # as MechanismDriverError raised by ML2, returning the
        # inner_exceptions to the client.  But many UTs call these L3
        # API methods directly, and need the proper exception to be
        # raised. Changing the UTs to unwrap raised exceptions
        # themselves would eliminate the need to do it here.
        try:
            info = super(ApicL3Plugin, self).add_router_interface(
                context, router_id, interface_info)
        except exceptions.MultipleExceptions as e:
            inner = e.inner_exceptions
            raise inner[0] if inner else e
        finally:
            del context.override_network_routing_topology_validation
        # REVISIT: This could be moved to create_port_postcommit and
        # update_port_postcommit.
        self._core_plugin.update_port(
            context, info['port_id'],
            {'port': {portbindings.HOST_ID: md.FABRIC_HOST_ID}})
        return info

    @n_utils.transaction_guard
    def remove_router_interface(self, context, router_id, interface_info):
        LOG.debug("APIC AIM L3 Plugin removing interface %(interface)s "
                  "from router %(router)s",
                  {'interface': interface_info, 'router': router_id})
        # REVISIT: The REST API layer unwraps these exceptions, such
        # as MechanismDriverError raised by ML2, returning the
        # inner_exceptions to the client.  But many UTs call these L3
        # API methods directly, and need the proper exception to be
        # raised. Changing the UTs to unwrap raised exceptions
        # themselves would eliminate the need to do it here.
        try:
            info = super(ApicL3Plugin, self).remove_router_interface(
                context, router_id, interface_info)
        except exceptions.MultipleExceptions as e:
            inner = e.inner_exceptions
            raise inner[0] if inner else e
        return info

    @n_utils.transaction_guard
    def create_floatingip(self, context, floatingip):
        if floatingip['floatingip'].get(qos_consts.QOS_POLICY_ID):
            raise aim_exceptions.InvalidPolicyTargetForQos()
        fip = floatingip['floatingip']
        # REVISIT: This ensure_tenant call probably isn't needed, as
        # FIPs don't map directly to any AIM resources. If it is
        # needed, it could me moved to the FLOATING_IP.BEFORE_CREATE
        # callback in rocky and newer.
        self._md.ensure_tenant(context, fip['tenant_id'])
        with db_api.CONTEXT_READER.using(context):
            # Verify that subnet is not a SNAT host-pool. This could
            # be done from a FLOATING_IP.PRECOMMIT_CREATE callback,
            # but that callback is made after a FIP port has been
            # allocated from the subnet. An exception would cause that
            # port to be deleted, but we are better off not trying to
            # allocate from the SNAT subnet in the first place.
            self._md.check_floatingip_external_address(context, fip)
        if fip.get('subnet_id') or fip.get('floating_ip_address'):
            result = super(ApicL3Plugin, self).create_floatingip(
                context, floatingip)
        else:
            # Iterate over non SNAT host-pool subnets and try to
            # allocate an address.
            with db_api.CONTEXT_READER.using(context):
                other_subs = self._md.get_subnets_for_fip(context, fip)
            result = None
            for ext_sn in other_subs:
                fip['subnet_id'] = ext_sn
                try:
                    result = (super(ApicL3Plugin, self).create_floatingip(
                        context, floatingip))
                    break
                except exceptions.IpAddressGenerationFailure:
                    LOG.info('No more floating IP addresses available '
                             'in subnet %s', ext_sn)
            if not result:
                raise exceptions.IpAddressGenerationFailure(
                    net_id=fip['floating_network_id'])
        # REVISIT: Replace with FLOATING_IP.AFTER_UPDATE callback,
        # which is called after creation as well, in ocata and newer
        # (called inside transaction in newton)?
        self._md.create_floatingip(context, result)
        # REVISIT: Consider using FLOATING_IP.PRECOMMIT_UPDATE
        # callback, which is called after creation as well, in queens
        # and newer, or maybe just calling update_floatingip_status
        # from the MD's create_floatingip method.
        with db_api.CONTEXT_WRITER.using(context):
            self.update_floatingip_status(
                context, result['id'], result['status'])
        return result

    @n_utils.transaction_guard
    def update_floatingip(self, context, id, floatingip):
        if floatingip['floatingip'].get(qos_consts.QOS_POLICY_ID):
            raise aim_exceptions.InvalidPolicyTargetForQos()
        old_fip = self.get_floatingip(context, id)
        result = super(ApicL3Plugin, self).update_floatingip(
            context, id, floatingip)
        # REVISIT: Replace with FLOATING_IP.AFTER_UPDATE callback in
        # ocata and newer (called inside transaction in newton)?
        self._md.update_floatingip(context, old_fip, result)
        # REVISIT: Consider using FLOATING_IP.PRECOMMIT_UPDATE
        # callback in queens and newer, or maybe just calling
        # update_floatingip_status from the MD's update_floatingip
        # method.
        if old_fip['status'] != result['status']:
            with db_api.CONTEXT_WRITER.using(context):
                self.update_floatingip_status(
                    context, result['id'], result['status'])
        return result

    @n_utils.transaction_guard
    def delete_floatingip(self, context, id):
        old_fip = self.get_floatingip(context, id)
        super(ApicL3Plugin, self).delete_floatingip(context, id)
        # REVISIT: Replace with FLOATING_IP.AFTER_DELETE callback in
        # queens and newer?
        self._md.delete_floatingip(context, old_fip)
