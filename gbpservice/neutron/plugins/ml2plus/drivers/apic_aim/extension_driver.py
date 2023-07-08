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

from aim.api import resource as aim_res
from aim import exceptions as aim_exc
from neutron.api import extensions
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import directory
from oslo_log import log
from oslo_utils import excutils

from gbpservice.neutron.db import api as db_api
from gbpservice.neutron import extensions as extensions_pkg
from gbpservice.neutron.extensions import cisco_apic
from gbpservice.neutron.plugins.ml2plus import driver_api as api_plus
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    extension_db as extn_db)
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import db

LOG = log.getLogger(__name__)


class ApicExtensionDriver(api_plus.ExtensionDriver,
                          db.DbMixin,
                          extn_db.ExtensionDbMixin):

    def __init__(self):
        LOG.info("APIC AIM ED __init__")
        self._mechanism_driver = None

    def initialize(self):
        LOG.info("APIC AIM ED initializing")
        extensions.append_api_extensions_path(extensions_pkg.__path__)

    @property
    def _md(self):
        if not self._mechanism_driver:
            # REVISIT(rkukura): It might be safer to search the MDs by
            # class rather than index by name, or to use a class
            # variable to find the instance.
            plugin = directory.get_plugin()
            mech_mgr = plugin.mechanism_manager
            self._mechanism_driver = mech_mgr.mech_drivers['apic_aim'].obj
        return self._mechanism_driver

    @property
    def extension_alias(self):
        return "cisco-apic"

    def extend_port_dict(self, session, base_model, result):
        try:
            self._md.extend_port_dict(session, base_model, result)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                if db_api.is_retriable(e):
                    LOG.debug("APIC AIM extend_port_dict got retriable "
                              "exception: %s", type(e))
                else:
                    LOG.exception("APIC AIM extend_port_dict failed")

    def extend_port_dict_bulk(self, session, results):
        try:
            self._md.extend_port_dict_bulk(session, results)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                if db_api.is_retriable(e):
                    LOG.debug("APIC AIM extend_port_dict_bulk got retriable "
                              "exception: %s", type(e))
                else:
                    LOG.exception("APIC AIM extend_port_dict_bulk failed")

    def process_create_port(self, plugin_context, data, result):
        res_dict = {cisco_apic.ERSPAN_CONFIG:
                    data.get(cisco_apic.ERSPAN_CONFIG, [])}
        self.set_port_extn_db(plugin_context.session, result['id'],
                              res_dict)
        result.update(res_dict)

    def process_update_port(self, plugin_context, data, result):
        if cisco_apic.ERSPAN_CONFIG not in data:
            return
        res_dict = {cisco_apic.ERSPAN_CONFIG: data[cisco_apic.ERSPAN_CONFIG]}
        self.set_port_extn_db(plugin_context.session, result['id'],
                              res_dict)
        result.update(res_dict)

    def extend_network_dict(self, session, base_model, result):
        try:
            self._md.extend_network_dict(session, base_model, result)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                if db_api.is_retriable(e):
                    LOG.debug("APIC AIM extend_network_dict got retriable "
                              "exception: %s", type(e))
                else:
                    LOG.exception("APIC AIM extend_network_dict failed")

    def extend_network_dict_bulk(self, session, results):
        try:
            self._md.extend_network_dict_bulk(session, results)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                if db_api.is_retriable(e):
                    LOG.debug("APIC AIM extend_network_dict got retriable "
                              "exception: %s", type(e))
                else:
                    LOG.exception("APIC AIM extend_network_dict failed")

    def validate_bgp_params(self, data, result=None):
        if result:
            is_svi = result.get(cisco_apic.SVI)
        else:
            is_svi = data.get(cisco_apic.SVI, False)
        is_bgp_enabled = data.get(cisco_apic.BGP, False)
        bgp_type = data.get(cisco_apic.BGP_TYPE, "default_export")
        asn = data.get(cisco_apic.BGP_ASN, "0")
        if not is_svi and (is_bgp_enabled or (bgp_type != "default_export") or
                           (asn != "0")):
            raise n_exc.InvalidInput(error_message="Network has to be created"
                                     " as svi type(--apic:svi True) to enable"
                                     " BGP or to set BGP parameters")

    def process_create_network(self, plugin_context, data, result):
        is_svi = data.get(cisco_apic.SVI, False)
        is_bgp_enabled = data.get(cisco_apic.BGP, False)
        bgp_type = data.get(cisco_apic.BGP_TYPE, "default_export")
        asn = data.get(cisco_apic.BGP_ASN, "0")
        use_multi_ext_nets = data.get(cisco_apic.MULTI_EXT_NETS, False)
        self.validate_bgp_params(data)
        res_dict = {cisco_apic.SVI: is_svi,
                    cisco_apic.BGP: is_bgp_enabled,
                    cisco_apic.BGP_TYPE: bgp_type,
                    cisco_apic.BGP_ASN: asn,
                    cisco_apic.NESTED_DOMAIN_NAME:
                    data.get(cisco_apic.NESTED_DOMAIN_NAME),
                    cisco_apic.NESTED_DOMAIN_TYPE:
                    data.get(cisco_apic.NESTED_DOMAIN_TYPE),
                    cisco_apic.NESTED_DOMAIN_INFRA_VLAN:
                    data.get(cisco_apic.NESTED_DOMAIN_INFRA_VLAN),
                    cisco_apic.NESTED_DOMAIN_SERVICE_VLAN:
                    data.get(cisco_apic.NESTED_DOMAIN_SERVICE_VLAN),
                    cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN:
                    data.get(cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN),
                    cisco_apic.EXTRA_PROVIDED_CONTRACTS:
                    data.get(cisco_apic.EXTRA_PROVIDED_CONTRACTS),
                    cisco_apic.EXTRA_CONSUMED_CONTRACTS:
                    data.get(cisco_apic.EXTRA_CONSUMED_CONTRACTS),
                    cisco_apic.EPG_CONTRACT_MASTERS:
                    data.get(cisco_apic.EPG_CONTRACT_MASTERS),
                    cisco_apic.POLICY_ENFORCEMENT_PREF:
                    data.get(cisco_apic.POLICY_ENFORCEMENT_PREF, "unenforced"),
                    cisco_apic.NO_NAT_CIDRS:
                    data.get(cisco_apic.NO_NAT_CIDRS),
                    cisco_apic.MULTI_EXT_NETS: use_multi_ext_nets,
                    }
        if cisco_apic.VLANS_LIST in (data.get(
                cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS) or {}):
            res_dict.update({cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS:
                data.get(cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS)[
                    cisco_apic.VLANS_LIST]})
        self.set_network_extn_db(plugin_context.session, result['id'],
                                 res_dict)
        result.update(res_dict)

        if (data.get(cisco_apic.DIST_NAMES) and
            data[cisco_apic.DIST_NAMES].get(cisco_apic.EXTERNAL_NETWORK)):
            dn = data[cisco_apic.DIST_NAMES][cisco_apic.EXTERNAL_NETWORK]
            try:
                aim_res.ExternalNetwork.from_dn(dn)
            except aim_exc.InvalidDNForAciResource:
                raise n_exc.InvalidInput(
                    error_message=('%s is not valid ExternalNetwork DN' % dn))
            if is_svi:
                res_dict = {cisco_apic.EXTERNAL_NETWORK: dn}
            else:
                res_dict = {cisco_apic.EXTERNAL_NETWORK: dn,
                            cisco_apic.NAT_TYPE:
                            data.get(cisco_apic.NAT_TYPE, 'distributed'),
                            cisco_apic.EXTERNAL_CIDRS:
                            data.get(
                                cisco_apic.EXTERNAL_CIDRS, ['0.0.0.0/0'])}
            self.set_network_extn_db(plugin_context.session, result['id'],
                                     res_dict)
            result.setdefault(cisco_apic.DIST_NAMES, {})[
                    cisco_apic.EXTERNAL_NETWORK] = res_dict.pop(
                        cisco_apic.EXTERNAL_NETWORK)
            result.update(res_dict)
        if (data.get(cisco_apic.DIST_NAMES) and
            data[cisco_apic.DIST_NAMES].get(cisco_apic.BD)):
            dn = data[cisco_apic.DIST_NAMES][cisco_apic.BD]
            try:
                aim_res.BridgeDomain.from_dn(dn)
            except aim_exc.InvalidDNForAciResource:
                raise n_exc.InvalidInput(
                    error_message=('%s is not valid BridgeDomain DN' % dn))
            res_dict = {cisco_apic.BD: dn}
            self.set_network_extn_db(plugin_context.session, result['id'],
                                     res_dict)
            result.setdefault(cisco_apic.DIST_NAMES, {})[
                    cisco_apic.BD] = res_dict.pop(
                        cisco_apic.BD)
            result.update(res_dict)

    def process_update_network(self, plugin_context, data, result):
        # Extension attributes that could be updated.
        update_attrs = [
                cisco_apic.EXTERNAL_CIDRS, cisco_apic.BGP, cisco_apic.BGP_TYPE,
                cisco_apic.BGP_ASN,
                cisco_apic.NESTED_DOMAIN_NAME, cisco_apic.NESTED_DOMAIN_TYPE,
                cisco_apic.NESTED_DOMAIN_INFRA_VLAN,
                cisco_apic.NESTED_DOMAIN_SERVICE_VLAN,
                cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN,
                cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS,
                cisco_apic.EXTRA_PROVIDED_CONTRACTS,
                cisco_apic.EXTRA_CONSUMED_CONTRACTS,
                cisco_apic.EPG_CONTRACT_MASTERS,
                cisco_apic.POLICY_ENFORCEMENT_PREF,
                cisco_apic.NO_NAT_CIDRS,
                cisco_apic.MULTI_EXT_NETS]
        if not (set(update_attrs) & set(data.keys())):
            return

        res_dict = {}
        if result.get(cisco_apic.DIST_NAMES, {}).get(
            cisco_apic.EXTERNAL_NETWORK):
            if cisco_apic.EXTERNAL_CIDRS in data:
                res_dict.update({cisco_apic.EXTERNAL_CIDRS:
                    data[cisco_apic.EXTERNAL_CIDRS]})
        self.validate_bgp_params(data, result)

        ext_keys = [cisco_apic.BGP, cisco_apic.BGP_TYPE, cisco_apic.BGP_ASN,
                cisco_apic.NESTED_DOMAIN_NAME, cisco_apic.NESTED_DOMAIN_TYPE,
                cisco_apic.NESTED_DOMAIN_INFRA_VLAN,
                cisco_apic.NESTED_DOMAIN_SERVICE_VLAN,
                cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN,
                cisco_apic.EXTRA_PROVIDED_CONTRACTS,
                cisco_apic.EXTRA_CONSUMED_CONTRACTS,
                cisco_apic.EPG_CONTRACT_MASTERS,
                cisco_apic.POLICY_ENFORCEMENT_PREF,
                cisco_apic.NO_NAT_CIDRS,
                cisco_apic.MULTI_EXT_NETS]
        for e_k in ext_keys:
            if e_k in data:
                res_dict.update({e_k: data[e_k]})

        if cisco_apic.VLANS_LIST in (data.get(
                cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS) or {}):
            res_dict.update({cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS:
                data.get(cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS)[
                    cisco_apic.VLANS_LIST]})

        if res_dict:
            self.set_network_extn_db(plugin_context.session, result['id'],
                                     res_dict)
            result.update(res_dict)

    def extend_subnet_dict(self, session, base_model, result):
        try:
            self._md.extend_subnet_dict(session, base_model, result)
            res_dict = self.get_subnet_extn_db(session, result['id'])
            result[cisco_apic.SNAT_HOST_POOL] = (
                res_dict.get(cisco_apic.SNAT_HOST_POOL, False))
            result[cisco_apic.ACTIVE_ACTIVE_AAP] = (
                res_dict.get(cisco_apic.ACTIVE_ACTIVE_AAP, False))
            result[cisco_apic.SNAT_SUBNET_ONLY] = (
                res_dict.get(cisco_apic.SNAT_SUBNET_ONLY, False))
            result[cisco_apic.EPG_SUBNET] = (
                res_dict.get(cisco_apic.EPG_SUBNET, False))
        except Exception as e:
            with excutils.save_and_reraise_exception():
                if db_api.is_retriable(e):
                    LOG.debug("APIC AIM extend_subnet_dict got retriable "
                              "exception: %s", type(e))
                else:
                    LOG.exception("APIC AIM extend_subnet_dict failed")

    def extend_subnet_dict_bulk(self, session, results):
        try:
            self._md.extend_subnet_dict_bulk(session, results)
            for result, subnet_db in results:
                res_dict = self.get_subnet_extn_db(session, subnet_db['id'])
                result[cisco_apic.SNAT_HOST_POOL] = (
                    res_dict.get(cisco_apic.SNAT_HOST_POOL, False))
                result[cisco_apic.ACTIVE_ACTIVE_AAP] = (
                    res_dict.get(cisco_apic.ACTIVE_ACTIVE_AAP, False))
                result[cisco_apic.SNAT_SUBNET_ONLY] = (
                    res_dict.get(cisco_apic.SNAT_SUBNET_ONLY, False))
                result[cisco_apic.EPG_SUBNET] = (
                    res_dict.get(cisco_apic.EPG_SUBNET, False))
        except Exception as e:
            with excutils.save_and_reraise_exception():
                if db_api.is_retriable(e):
                    LOG.debug("APIC AIM extend_subnet_dict_bulk got retriable "
                              "exception: %s", type(e))
                else:
                    LOG.exception("APIC AIM extend_subnet_dict_bulk failed")

    def process_create_subnet(self, plugin_context, data, result):
        res_dict = {cisco_apic.SNAT_HOST_POOL:
                    data.get(cisco_apic.SNAT_HOST_POOL, False),
                    cisco_apic.ACTIVE_ACTIVE_AAP:
                    data.get(cisco_apic.ACTIVE_ACTIVE_AAP, False),
                    cisco_apic.SNAT_SUBNET_ONLY:
                    data.get(cisco_apic.SNAT_SUBNET_ONLY, False),
                    cisco_apic.EPG_SUBNET:
                    data.get(cisco_apic.EPG_SUBNET, False)}
        self.set_subnet_extn_db(plugin_context.session, result['id'],
                                res_dict)
        result.update(res_dict)

    def process_update_subnet(self, plugin_context, data, result):
        if (cisco_apic.SNAT_HOST_POOL not in data and
                cisco_apic.SNAT_SUBNET_ONLY not in data):
            return

        res_dict = {}
        if cisco_apic.SNAT_HOST_POOL in data:
            res_dict.update({cisco_apic.SNAT_HOST_POOL:
                             data[cisco_apic.SNAT_HOST_POOL]})

        if cisco_apic.SNAT_SUBNET_ONLY in data:
            res_dict.update({cisco_apic.SNAT_SUBNET_ONLY:
                             data[cisco_apic.SNAT_SUBNET_ONLY]})

        self.set_subnet_extn_db(plugin_context.session, result['id'],
                                res_dict)
        result.update(res_dict)

    def extend_address_scope_dict(self, session, base_model, result):
        try:
            self._md.extend_address_scope_dict(session, base_model, result)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                if db_api.is_retriable(e):
                    LOG.debug("APIC AIM extend_address_scope_dict got "
                              "retriable exception: %s", type(e))
                else:
                    LOG.exception("APIC AIM extend_address_scope_dict failed")

    def process_create_address_scope(self, plugin_context, data, result):
        if (data.get(cisco_apic.DIST_NAMES) and
            data[cisco_apic.DIST_NAMES].get(cisco_apic.VRF)):
            dn = data[cisco_apic.DIST_NAMES][cisco_apic.VRF]
            try:
                vrf = aim_res.VRF.from_dn(dn)
            except aim_exc.InvalidDNForAciResource:
                raise n_exc.InvalidInput(
                    error_message=('%s is not valid VRF DN' % dn))

            # Check if another address scope already maps to this VRF.
            session = plugin_context.session
            mappings = self._get_address_scope_mappings_for_vrf(session, vrf)
            vrf_owned = False
            for mapping in mappings:
                if mapping.address_scope.ip_version == data['ip_version']:
                    raise n_exc.InvalidInput(
                        error_message=(
                            'VRF %s is already in use by address-scope %s' %
                            (dn, mapping.scope_id)))
                vrf_owned = mapping.vrf_owned

            self._add_address_scope_mapping(
                session, result['id'], vrf, vrf_owned)
