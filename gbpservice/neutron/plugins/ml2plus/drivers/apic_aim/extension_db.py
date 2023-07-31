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

from neutron.db import models_v2
from neutron_lib.db import model_base
from oslo_log import log
import sqlalchemy as sa
from sqlalchemy.ext import baked
from sqlalchemy import orm
from sqlalchemy.sql.expression import true

from gbpservice.neutron.extensions import cisco_apic
from gbpservice.neutron.extensions import cisco_apic_l3

LOG = log.getLogger(__name__)

BAKERY = baked.bakery(_size_alert=lambda c: LOG.warning(
    "sqlalchemy baked query cache size exceeded in %s", __name__))


class PortExtensionErspanDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_port_erspan_configurations'

    port_id = sa.Column(
        sa.String(36), sa.ForeignKey('ports.id', ondelete="CASCADE"),
        primary_key=True)
    dest_ip = sa.Column(sa.String(64), primary_key=True)
    flow_id = sa.Column(sa.Integer, primary_key=True)
    direction = sa.Column(sa.Enum('in', 'out', 'both'),
                          default='both', primary_key=True)
    port = orm.relationship(models_v2.Port,
                            backref=orm.backref(
                                'aim_extension_erspan_configs',
                                uselist=True,
                                lazy='joined', cascade='delete'))


class NetworkExtensionDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_network_extensions'

    network_id = sa.Column(
        sa.String(36), sa.ForeignKey('networks.id', ondelete="CASCADE"),
        primary_key=True)
    external_network_dn = sa.Column(sa.String(1024))
    bridge_domain_dn = sa.Column(sa.String(1024))
    nat_type = sa.Column(sa.Enum('distributed', 'edge', ''))
    svi = sa.Column(sa.Boolean)
    bgp_enable = sa.Column(sa.Boolean, default=False, nullable=False)
    bgp_type = sa.Column(sa.Enum('default_export', ''),
                         default='default_export',
                         nullable=False)
    bgp_asn = sa.Column(sa.String(64), default='0', nullable=False)
    policy_enforcement_pref = sa.Column(sa.Enum('unenforced', 'enforced', ''),
                         default='unenforced',
                         nullable=False)

    network = orm.relationship(models_v2.Network,
                               backref=orm.backref(
                                   'aim_extension_mapping', lazy='joined',
                                   uselist=False, cascade='delete'))
    nested_domain_name = sa.Column(sa.String(1024), nullable=True)
    nested_domain_type = sa.Column(sa.String(1024), nullable=True)
    nested_domain_infra_vlan = sa.Column(sa.Integer, nullable=True)
    nested_domain_service_vlan = sa.Column(sa.Integer, nullable=True)
    nested_domain_node_network_vlan = sa.Column(sa.Integer, nullable=True)
    multi_ext_nets = sa.Column(sa.Boolean, default=False, nullable=False)


class NetworkExtensionCidrDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_network_external_cidrs'

    network_id = sa.Column(
        sa.String(36), sa.ForeignKey('networks.id', ondelete="CASCADE"),
        primary_key=True)
    cidr = sa.Column(sa.String(64), primary_key=True)
    network = orm.relationship(models_v2.Network,
                               backref=orm.backref(
                                   'aim_extension_cidr_mapping', lazy='joined',
                                   uselist=True, cascade='delete'))


class NetworkExtNestedDomainAllowedVlansDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_network_nested_domain_allowed_vlans'

    # There is a single pool of VLANs for an APIC
    vlan = sa.Column(sa.Integer(), primary_key=True)
    network_id = sa.Column(
        sa.String(36), sa.ForeignKey('networks.id', ondelete="CASCADE"))
    network = orm.relationship(models_v2.Network,
                               backref=orm.backref(
                                   'aim_extension_domain_mapping',
                                   uselist=True,
                                   lazy='joined', cascade='delete'))


class NetworkExtExtraContractDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_network_extra_contracts'

    network_id = sa.Column(
        sa.String(36), sa.ForeignKey('networks.id', ondelete="CASCADE"))
    contract_name = sa.Column(sa.String(64), primary_key=True)
    provides = sa.Column(sa.Boolean, primary_key=True)
    network = orm.relationship(models_v2.Network,
                               backref=orm.backref(
                                   'aim_extension_extra_contract_mapping',
                                   uselist=True,
                                   lazy='joined', cascade='delete'))


class NetworkExtEpgContractMasterDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_network_epg_contract_masters'

    network_id = sa.Column(
        sa.String(36), sa.ForeignKey('networks.id', ondelete="CASCADE"))
    app_profile_name = sa.Column(sa.String(64), primary_key=True)
    name = sa.Column(sa.String(64), primary_key=True)
    network = orm.relationship(models_v2.Network,
                               backref=orm.backref(
                                   'aim_extension_epg_contract_masters',
                                   uselist=True,
                                   lazy='joined', cascade='delete'))


class NetworkExtensionNoNatCidrsDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_network_no_nat_cidrs'

    network_id = sa.Column(
        sa.String(36), sa.ForeignKey('networks.id', ondelete="CASCADE"),
        primary_key=True)
    cidr = sa.Column(sa.String(64), primary_key=True)
    network = orm.relationship(models_v2.Network,
                               backref=orm.backref(
                                   'aim_extension_no_nat_cidrs_mapping',
                                   lazy='joined', uselist=True,
                                   cascade='delete'))


class SubnetExtensionDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_subnet_extensions'

    subnet_id = sa.Column(
        sa.String(36), sa.ForeignKey('subnets.id', ondelete="CASCADE"),
        primary_key=True)
    snat_host_pool = sa.Column(sa.Boolean)
    active_active_aap = sa.Column(sa.Boolean)
    snat_subnet_only = sa.Column(sa.Boolean)
    epg_subnet = sa.Column(sa.Boolean)
    subnet = orm.relationship(models_v2.Subnet,
                              backref=orm.backref(
                                  'aim_extension_mapping', lazy='joined',
                                  uselist=False, cascade='delete'))


class RouterExtensionContractDb(model_base.BASEV2):

    __tablename__ = 'apic_aim_router_external_contracts'

    router_id = sa.Column(
        sa.String(36), sa.ForeignKey('routers.id', ondelete="CASCADE"),
        primary_key=True)
    contract_name = sa.Column(sa.String(64), primary_key=True)
    provides = sa.Column(sa.Boolean, primary_key=True)


class ExtensionDbMixin(object):

    def _set_if_not_none(self, res_dict, res_attr, db_attr):
        if db_attr is not None:
            res_dict[res_attr] = db_attr

    def get_port_extn_db(self, session, port_id):
        return self.get_port_extn_db_bulk(session, [port_id]).get(
            port_id, {})

    def get_port_extn_db_bulk(self, session, port_ids):
        if not port_ids:
            return {}

        query = BAKERY(lambda s: s.query(
            PortExtensionErspanDb))
        query += lambda q: q.filter(
            PortExtensionErspanDb.port_id.in_(
                sa.bindparam('port_ids', expanding=True)))
        db_erspans = query(session).params(
            port_ids=port_ids).all()

        erspans_by_port_id = {}
        for db_erspan in db_erspans:
            erspans_by_port_id.setdefault(db_erspan.port_id, []).append(
                db_erspan)

        result = {}
        for db_obj in db_erspans:
            port_id = db_obj.port_id
            result.setdefault(port_id, self.make_port_extn_db_conf_dict(
                erspans_by_port_id.get(port_id, [])))
        return result

    def make_port_extn_db_conf_dict(self, db_erspans):
        port_res = {}
        db_obj = db_erspans
        if db_obj:
            def _db_to_dict(db_obj):
                ed = {cisco_apic.ERSPAN_DEST_IP: db_obj.dest_ip,
                      cisco_apic.ERSPAN_FLOW_ID: db_obj.flow_id,
                      cisco_apic.ERSPAN_DIRECTION: db_obj.direction}
                return ed
            port_res[cisco_apic.ERSPAN_CONFIG] = [_db_to_dict(e)
                                                  for e in db_erspans]
        return port_res

    def set_port_extn_db(self, session, port_id, res_dict):
        with session.begin(subtransactions=True):
            if cisco_apic.ERSPAN_CONFIG in res_dict:
                self._update_dict_attr(
                        session, PortExtensionErspanDb,
                        (cisco_apic.ERSPAN_DEST_IP,
                         cisco_apic.ERSPAN_FLOW_ID,
                         cisco_apic.ERSPAN_DIRECTION
                         ),
                        res_dict[cisco_apic.ERSPAN_CONFIG],
                        port_id=port_id)

    def get_network_extn_db(self, session, network_id):
        return self.get_network_extn_db_bulk(session, [network_id]).get(
            network_id, {})

    def get_network_extn_db_bulk(self, session, network_ids):
        if not network_ids:
            return {}

        query = BAKERY(lambda s: s.query(
            NetworkExtensionDb))
        query += lambda q: q.filter(
            NetworkExtensionDb.network_id.in_(
                sa.bindparam('network_ids', expanding=True)))
        db_objs = query(session).params(
            network_ids=network_ids).all()

        query = BAKERY(lambda s: s.query(
            NetworkExtensionCidrDb))
        query += lambda q: q.filter(
            NetworkExtensionCidrDb.network_id.in_(
                sa.bindparam('network_ids', expanding=True)))
        db_cidrs = query(session).params(
            network_ids=network_ids).all()

        query = BAKERY(lambda s: s.query(
            NetworkExtNestedDomainAllowedVlansDb))
        query += lambda q: q.filter(
            NetworkExtNestedDomainAllowedVlansDb.network_id.in_(
                sa.bindparam('network_ids', expanding=True)))
        db_vlans = query(session).params(
            network_ids=network_ids).all()

        query = BAKERY(lambda s: s.query(
            NetworkExtExtraContractDb))
        query += lambda q: q.filter(
            NetworkExtExtraContractDb.network_id.in_(
                sa.bindparam('network_ids', expanding=True)))
        db_contracts = query(session).params(
            network_ids=network_ids).all()

        query = BAKERY(lambda s: s.query(
            NetworkExtEpgContractMasterDb))
        query += lambda q: q.filter(
            NetworkExtEpgContractMasterDb.network_id.in_(
                sa.bindparam('network_ids', expanding=True)))
        db_masters = query(session).params(
            network_ids=network_ids).all()

        query = BAKERY(lambda s: s.query(
            NetworkExtensionNoNatCidrsDb))
        query += lambda q: q.filter(
            NetworkExtensionNoNatCidrsDb.network_id.in_(
                sa.bindparam('network_ids', expanding=True)))
        db_no_nat_cidrs = query(session).params(
            network_ids=network_ids).all()

        cidrs_by_net_id = {}
        vlans_by_net_id = {}
        contracts_by_net_id = {}
        masters_by_net_id = {}
        no_nat_cidrs_by_net_id = {}
        for db_cidr in db_cidrs:
            cidrs_by_net_id.setdefault(db_cidr.network_id, []).append(
                db_cidr)
        for db_vlan in db_vlans:
            vlans_by_net_id.setdefault(db_vlan.network_id, []).append(
                db_vlan)
        for db_contract in db_contracts:
            contracts_by_net_id.setdefault(db_contract.network_id, []).append(
                db_contract)
        for db_master in db_masters:
            masters_by_net_id.setdefault(db_master.network_id, []).append(
                db_master)
        for db_no_nat_cidr in db_no_nat_cidrs:
            no_nat_cidrs_by_net_id.setdefault(db_no_nat_cidr.network_id,
            []).append(db_no_nat_cidr)

        result = {}
        for db_obj in db_objs:
            net_id = db_obj.network_id
            result.setdefault(net_id, self.make_network_extn_db_conf_dict(
                db_obj, cidrs_by_net_id.get(net_id, []),
                vlans_by_net_id.get(net_id, []),
                contracts_by_net_id.get(net_id, []),
                masters_by_net_id.get(net_id, []),
                no_nat_cidrs_by_net_id.get(net_id, [])))
        return result

    def make_network_extn_db_conf_dict(self, ext_db, db_cidrs, db_vlans,
                                       db_contracts, db_masters,
                                       db_no_nat_cidrs):
        net_res = {}
        db_obj = ext_db
        if db_obj:
            self._set_if_not_none(net_res, cisco_apic.EXTERNAL_NETWORK,
                                  db_obj['external_network_dn'])
            self._set_if_not_none(net_res, cisco_apic.BD,
                                  db_obj['bridge_domain_dn'])
            self._set_if_not_none(net_res, cisco_apic.NAT_TYPE,
                                  db_obj['nat_type'])
            self._set_if_not_none(net_res, cisco_apic.SVI, db_obj['svi'])
            net_res[cisco_apic.BGP] = db_obj['bgp_enable']
            net_res[cisco_apic.BGP_TYPE] = db_obj['bgp_type']
            net_res[cisco_apic.BGP_ASN] = db_obj['bgp_asn']
            net_res[cisco_apic.NESTED_DOMAIN_NAME] = (
                    db_obj['nested_domain_name'])
            net_res[cisco_apic.NESTED_DOMAIN_TYPE] = (
                    db_obj['nested_domain_type'])
            net_res[cisco_apic.NESTED_DOMAIN_INFRA_VLAN] = (
                    db_obj['nested_domain_infra_vlan'])
            net_res[cisco_apic.NESTED_DOMAIN_SERVICE_VLAN] = (
                    db_obj['nested_domain_service_vlan'])
            net_res[cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN] = (
                    db_obj['nested_domain_node_network_vlan'])
            net_res[cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS] = [
                c.vlan for c in db_vlans]
            net_res[cisco_apic.EXTRA_PROVIDED_CONTRACTS] = [
                c.contract_name for c in db_contracts if c.provides]
            net_res[cisco_apic.EXTRA_CONSUMED_CONTRACTS] = [
                c.contract_name for c in db_contracts if not c.provides]
            net_res[cisco_apic.EPG_CONTRACT_MASTERS] = [
                {'app_profile_name': m.app_profile_name,
                 'name': m.name} for m in db_masters]
            net_res[cisco_apic.POLICY_ENFORCEMENT_PREF] = db_obj[
                'policy_enforcement_pref']
            net_res[cisco_apic.NO_NAT_CIDRS] = [
                c.cidr for c in db_no_nat_cidrs]
            net_res[cisco_apic.MULTI_EXT_NETS] = db_obj['multi_ext_nets']
        if net_res.get(cisco_apic.EXTERNAL_NETWORK):
            net_res[cisco_apic.EXTERNAL_CIDRS] = [c.cidr for c in db_cidrs]
        return net_res

    def set_network_extn_db(self, session, network_id, res_dict):
        with session.begin(subtransactions=True):
            query = BAKERY(lambda s: s.query(
                NetworkExtensionDb))
            query += lambda q: q.filter_by(
                network_id=sa.bindparam('network_id'))
            db_obj = query(session).params(
                network_id=network_id).first()

            db_obj = db_obj or NetworkExtensionDb(network_id=network_id)
            if cisco_apic.EXTERNAL_NETWORK in res_dict:
                db_obj['external_network_dn'] = (
                    res_dict[cisco_apic.EXTERNAL_NETWORK])
            if cisco_apic.BD in res_dict:
                db_obj['bridge_domain_dn'] = (
                    res_dict[cisco_apic.BD])
            if cisco_apic.NAT_TYPE in res_dict:
                db_obj['nat_type'] = res_dict[cisco_apic.NAT_TYPE]
            if cisco_apic.SVI in res_dict:
                db_obj['svi'] = res_dict[cisco_apic.SVI]
            if cisco_apic.BGP in res_dict:
                db_obj['bgp_enable'] = res_dict[cisco_apic.BGP]
            if cisco_apic.BGP_TYPE in res_dict:
                db_obj['bgp_type'] = res_dict[cisco_apic.BGP_TYPE]
            if cisco_apic.BGP_ASN in res_dict:
                db_obj['bgp_asn'] = res_dict[cisco_apic.BGP_ASN]
            if cisco_apic.NESTED_DOMAIN_NAME in res_dict:
                db_obj['nested_domain_name'] = res_dict[
                        cisco_apic.NESTED_DOMAIN_NAME]
            if cisco_apic.NESTED_DOMAIN_TYPE in res_dict:
                db_obj['nested_domain_type'] = res_dict[
                        cisco_apic.NESTED_DOMAIN_TYPE]
            if cisco_apic.NESTED_DOMAIN_INFRA_VLAN in res_dict:
                db_obj['nested_domain_infra_vlan'] = res_dict[
                        cisco_apic.NESTED_DOMAIN_INFRA_VLAN]
            if cisco_apic.NESTED_DOMAIN_SERVICE_VLAN in res_dict:
                db_obj['nested_domain_service_vlan'] = res_dict[
                        cisco_apic.NESTED_DOMAIN_SERVICE_VLAN]
            if cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN in res_dict:
                db_obj['nested_domain_node_network_vlan'] = res_dict[
                        cisco_apic.NESTED_DOMAIN_NODE_NETWORK_VLAN]
            if cisco_apic.POLICY_ENFORCEMENT_PREF in res_dict:
                db_obj['policy_enforcement_pref'] = res_dict[
                        cisco_apic.POLICY_ENFORCEMENT_PREF]
            if cisco_apic.MULTI_EXT_NETS in res_dict:
                db_obj['multi_ext_nets'] = res_dict[cisco_apic.MULTI_EXT_NETS]
            session.add(db_obj)

            if cisco_apic.EXTERNAL_CIDRS in res_dict:
                self._update_list_attr(session, NetworkExtensionCidrDb, 'cidr',
                                       res_dict[cisco_apic.EXTERNAL_CIDRS],
                                       network_id=network_id)

            if cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS in res_dict:
                self._update_list_attr(
                        session, NetworkExtNestedDomainAllowedVlansDb, 'vlan',
                        res_dict[cisco_apic.NESTED_DOMAIN_ALLOWED_VLANS],
                        network_id=network_id)

            if cisco_apic.EXTRA_PROVIDED_CONTRACTS in res_dict:
                self._update_list_attr(
                        session, NetworkExtExtraContractDb, 'contract_name',
                        res_dict[cisco_apic.EXTRA_PROVIDED_CONTRACTS],
                        network_id=network_id, provides=True)

            if cisco_apic.EXTRA_CONSUMED_CONTRACTS in res_dict:
                self._update_list_attr(
                        session, NetworkExtExtraContractDb, 'contract_name',
                        res_dict[cisco_apic.EXTRA_CONSUMED_CONTRACTS],
                        network_id=network_id, provides=False)

            if cisco_apic.EPG_CONTRACT_MASTERS in res_dict:
                self._update_dict_attr(
                        session, NetworkExtEpgContractMasterDb,
                        ('app_profile_name', 'name'),
                        res_dict[cisco_apic.EPG_CONTRACT_MASTERS],
                        network_id=network_id)

            if cisco_apic.NO_NAT_CIDRS in res_dict:
                self._update_list_attr(session, NetworkExtensionNoNatCidrsDb,
                                       'cidr',
                                       res_dict[cisco_apic.NO_NAT_CIDRS],
                                       network_id=network_id)

    def get_network_ids_by_ext_net_dn(self, session, dn, lock_update=False):
        query = BAKERY(lambda s: s.query(
            NetworkExtensionDb.network_id))
        query += lambda q: q.filter_by(
            external_network_dn=sa.bindparam('dn'))
        if lock_update:
            # REVISIT: Eliminate locking.
            query += lambda q: q.with_for_update()
        ids = query(session).params(dn=dn)

        return [i[0] for i in ids]

    def get_network_ids_by_l3out_dn(self, session, dn, lock_update=False):
        query = BAKERY(lambda s: s.query(
            NetworkExtensionDb.network_id))
        query += lambda q: q.filter(
            NetworkExtensionDb.external_network_dn.like(
                sa.bindparam('dn') + "/%"))
        if lock_update:
            # REVISIT: Eliminate locking.
            query += lambda q: q.with_for_update()
        ids = query(session).params(dn=dn)

        return [i[0] for i in ids]

    def get_network_ids_by_l3out_dn_filter_multi(self, session, dn,
                                              wanted_multi_val=False):
        query = BAKERY(lambda s: s.query(
            NetworkExtensionDb.network_id,
            NetworkExtensionDb.multi_ext_nets))
        query += lambda q: q.filter(
            NetworkExtensionDb.external_network_dn.like(
                sa.bindparam('dn') + "/%"))
        ids = query(session).params(dn=dn)

        return [i[0] for i in ids if i[1] is wanted_multi_val]

    def get_svi_network_ids_by_l3out_dn(self, session, dn, lock_update=False):
        query = BAKERY(lambda s: s.query(
            NetworkExtensionDb.network_id))
        query += lambda q: q.filter(
            NetworkExtensionDb.external_network_dn.like(
                sa.bindparam('dn') + "/%"),
            NetworkExtensionDb.svi == true())
        if lock_update:
            # REVISIT: Eliminate locking.
            query += lambda q: q.with_for_update()
        ids = query(session).params(dn=dn)

        return [i[0] for i in ids]

    def get_external_cidrs_by_ext_net_dn(self, session, dn, lock_update=False):
        ctab = NetworkExtensionCidrDb
        ntab = NetworkExtensionDb

        query = BAKERY(lambda s: s.query(
            ctab.cidr))
        query += lambda q: q.join(
            ntab,
            ntab.network_id == ctab.network_id)
        query += lambda q: q.filter(
            ntab.external_network_dn == sa.bindparam('dn'))
        query += lambda q: q.distinct()
        if lock_update:
            # REVISIT: Eliminate locking.
            query += lambda q: q.with_for_update()
        cidrs = query(session).params(dn=dn)

        return [c[0] for c in cidrs]

    def get_external_cidrs_by_net_id(self, session, nid):
        query = BAKERY(lambda s: s.query(
            NetworkExtensionCidrDb.cidr))
        query += lambda q: q.filter_by(
            network_id=sa.bindparam('nid'))
        cidrs = query(session).params(nid=nid)

        return [i[0] for i in cidrs]

    def get_subnet_extn_db(self, session, subnet_id):
        query = BAKERY(lambda s: s.query(
            SubnetExtensionDb))
        query += lambda q: q.filter_by(
            subnet_id=sa.bindparam('subnet_id'))
        db_obj = query(session).params(
            subnet_id=subnet_id).first()

        result = {}
        if db_obj:
            self._set_if_not_none(result, cisco_apic.SNAT_HOST_POOL,
                                  db_obj['snat_host_pool'])
            self._set_if_not_none(result, cisco_apic.ACTIVE_ACTIVE_AAP,
                                  db_obj['active_active_aap'])
            self._set_if_not_none(result, cisco_apic.SNAT_SUBNET_ONLY,
                                  db_obj['snat_subnet_only'])
            self._set_if_not_none(result, cisco_apic.EPG_SUBNET,
                                  db_obj['epg_subnet'])
        return result

    def set_subnet_extn_db(self, session, subnet_id, res_dict):
        query = BAKERY(lambda s: s.query(
            SubnetExtensionDb))
        query += lambda q: q.filter_by(
            subnet_id=sa.bindparam('subnet_id'))
        db_obj = query(session).params(
            subnet_id=subnet_id).first()

        db_obj = db_obj or SubnetExtensionDb(subnet_id=subnet_id)
        if cisco_apic.SNAT_HOST_POOL in res_dict:
            db_obj['snat_host_pool'] = res_dict[cisco_apic.SNAT_HOST_POOL]
        if cisco_apic.ACTIVE_ACTIVE_AAP in res_dict:
            db_obj['active_active_aap'] = res_dict[
                                            cisco_apic.ACTIVE_ACTIVE_AAP]
        if cisco_apic.SNAT_SUBNET_ONLY in res_dict:
            db_obj['snat_subnet_only'] = res_dict[
                                            cisco_apic.SNAT_SUBNET_ONLY]
        if cisco_apic.EPG_SUBNET in res_dict:
            db_obj['epg_subnet'] = res_dict[cisco_apic.EPG_SUBNET]
        session.add(db_obj)

    def get_router_extn_db(self, session, router_id):
        query = BAKERY(lambda s: s.query(
            RouterExtensionContractDb))
        query += lambda q: q.filter_by(
            router_id=sa.bindparam('router_id'))
        db_contracts = query(session).params(
            router_id=router_id).all()

        return {cisco_apic_l3.EXTERNAL_PROVIDED_CONTRACTS:
                [c['contract_name'] for c in db_contracts if c['provides']],
                cisco_apic_l3.EXTERNAL_CONSUMED_CONTRACTS:
                [c['contract_name'] for c in db_contracts
                 if not c['provides']]}

    def get_router_extn_db_bulk(self, session, router_ids):
        query = BAKERY(lambda s: s.query(
            RouterExtensionContractDb))
        query += lambda q: q.filter(
            RouterExtensionContractDb.router_id.in_(
                sa.bindparam('router_ids', expanding=True)))
        db_contracts = query(session).params(
            router_ids=router_ids).all()

        attr_dict = defaultdict(dict)
        for db_contract in db_contracts:
            router_id = db_contract['router_id']
            p_contracts = attr_dict[router_id].setdefault(
                cisco_apic_l3.EXTERNAL_PROVIDED_CONTRACTS, [])
            c_contracts = attr_dict[router_id].setdefault(
                cisco_apic_l3.EXTERNAL_CONSUMED_CONTRACTS, [])
            if db_contract['provides']:
                p_contracts.append(db_contract['contract_name'])
            else:
                c_contracts.append(db_contract['contract_name'])
        return attr_dict

    def get_network_ids_and_multi_by_l3out_dn(self, session, dn):
        query = BAKERY(lambda s: s.query(
            NetworkExtensionDb.network_id,
            NetworkExtensionDb.multi_ext_nets))
        query += lambda q: q.filter(
            NetworkExtensionDb.external_network_dn.like(
                sa.bindparam('dn') + "/%"))
        ids_and_multis = query(session).params(dn=dn)

        return [(i[0], i[1]) for i in ids_and_multis]

    def _update_list_attr(self, session, db_model, column,
                          new_values, **filters):
        if new_values is None:
            return

        # REVISIT: Can this query be baked?
        rows = session.query(db_model).filter_by(**filters).all()

        new_values = set(new_values)
        for r in rows:
            if r[column] in new_values:
                new_values.discard(r[column])
            else:
                session.delete(r)
        for v in new_values:
            attr = {column: v}
            attr.update(filters)
            db_obj = db_model(**attr)
            session.add(db_obj)

    def _update_dict_attr(self, session, db_model, keys,
                          new_values, **filters):
        if new_values is None:
            return

        # REVISIT: Can this query be baked?
        rows = session.query(db_model).filter_by(**filters).all()

        # remove duplicates, may change order
        new_values = [dict(t) for t in {tuple(d.items()) for d in new_values}]
        # Updates are deletions with additions, so to ensure that
        # the delete happens before a subsequent addtion, we create
        # a subtransaction
        with session.begin(subtransactions=True):
            for r in rows:
                curr_obj = {key: r[key] for key in keys}
                if curr_obj in new_values:
                    new_values.remove(curr_obj)
                else:
                    session.delete(r)
        for v in new_values:
            v.update(filters)
            db_obj = db_model(**v)
            session.add(db_obj)

    def set_router_extn_db(self, session, router_id, res_dict):
        with session.begin(subtransactions=True):
            if cisco_apic_l3.EXTERNAL_PROVIDED_CONTRACTS in res_dict:
                self._update_list_attr(session, RouterExtensionContractDb,
                   'contract_name',
                   res_dict[cisco_apic_l3.EXTERNAL_PROVIDED_CONTRACTS],
                   router_id=router_id, provides=True)
            if cisco_apic_l3.EXTERNAL_CONSUMED_CONTRACTS in res_dict:
                self._update_list_attr(session, RouterExtensionContractDb,
                    'contract_name',
                    res_dict[cisco_apic_l3.EXTERNAL_CONSUMED_CONTRACTS],
                    router_id=router_id, provides=False)
