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

from unittest import mock

# from aim.api import infra as aim_infra
from aim.api import resource as aim_res
from gbpservice.neutron.tests.unit.plugins.ml2plus import (
    test_apic_aim as test_aim_md)
from gbpservice.neutron.tests.unit.services.grouppolicy import (
    test_aim_mapping_driver as test_aim_base)
from neutron.objects.qos import policy as policy_object
from neutron.objects.qos import rule as rule_object
from neutron_lib import context
from neutron_lib.plugins import directory
from oslo_log import log as logging
from oslo_utils import uuidutils

from gbpservice.neutron.services.grouppolicy import config

LOG = logging.getLogger(__name__)


class TestAIMQosBase(test_aim_base.AIMBaseTestCase):

    def setUp(self, *args, **kwargs):
        net_type = kwargs.pop('tenant_network_types', None) or ['vlan']
        ml2_options = {'mechanism_drivers': ['apic_aim', 'openvswitch'],
                       'extension_drivers': ['apic_aim', 'port_security',
                                             'dns', 'qos'],
                       'type_drivers': ['opflex', 'local', 'vlan'],
                       'tenant_network_types': net_type}
        config.cfg.CONF.set_override(
            'network_vlan_ranges', ['physnet1:100:200'], group='ml2_type_vlan')
        self._aim_mech_driver = None
        self._qos_driver = None
        super(TestAIMQosBase, self).setUp(
            *args, ml2_options=ml2_options, qos_plugin='qos', **kwargs)
        self._plugin = directory.get_plugin()
        self.plugin = self._plugin
        self._plugin.remove_networks_from_down_agents = mock.Mock()
        self._plugin.is_agent_down = mock.Mock(return_value=False)
        self._ctx = context.get_admin_context()
        self.ctxt = context.Context('fake_user', 'fake_tenant')
        self.admin_ctxt = context.get_admin_context()

        self.policy_data = {
            'policy': {'project_id': uuidutils.generate_uuid(),
                       'name': 'test-policy',
                       'description': 'Test policy description',
                       'shared': True,
                       'is_default': False}}

        self.rule_data = {
            'egress_bandwidth_limit_rule': {
                'id': uuidutils.generate_uuid(),
                'direction': 'egress',
                'max_kbps': 100,
                'max_burst_kbps': 150},
            'ingress_bandwidth_limit_rule': {
                'id': uuidutils.generate_uuid(),
                'direction': 'ingress',
                'max_kbps': 101,
                'max_burst_kbps': 1150},
            'dscp_marking_rule': {'id': uuidutils.generate_uuid(),
                                  'dscp_mark': 16}, }

        self.egress_rule = rule_object.QosBandwidthLimitRule(
            self.ctxt, **self.rule_data['egress_bandwidth_limit_rule'])

        self.ingress_rule = rule_object.QosBandwidthLimitRule(
            self.ctxt, **self.rule_data['ingress_bandwidth_limit_rule'])

        self.dscp_rule = rule_object.QosDscpMarkingRule(
            self.ctxt, **self.rule_data['dscp_marking_rule'])

    def tearDown(self):
        super(TestAIMQosBase, self).tearDown()

    @property
    def qos_plugin(self):
        if not self._qos_plugin:
            self._qos_plugin = directory.get_plugin('qos')
        return self._qos_plugin

    @property
    def qos_driver(self):
        if not self._qos_driver:
            self._qos_driver = self.aim_mech.qos_driver
        return self._qos_driver

    @property
    def aim_mech(self):
        if not self._aim_mech_driver:
            self._aim_mech_driver = (
                self._plugin.mechanism_manager.mech_drivers['apic_aim'].obj)
        return self._aim_mech_driver


class TestQosPolicy(TestAIMQosBase):

    def test_create_delete_update_policy(self):
        _policy = policy_object.QosPolicy(
            self.ctxt, **self.policy_data['policy'])
        _policy.create()
        self.qos_driver.create_policy_precommit(self.ctxt, _policy)
        tenant_name = 'prj_' + self.ctxt.tenant_id
        pol = aim_res.QosRequirement(name=_policy.id, tenant_name=tenant_name)
        pol = self.aim_mgr.get(self._aim_context, pol)
        self.assertIsNotNone(pol)

        # Test dscp rule
        setattr(_policy, "rules", [self.dscp_rule])
        self.qos_driver.update_policy_precommit(self.ctxt, _policy)
        pol = self.aim_mgr.get(self._aim_context, pol)
        self.assertIsNotNone(pol)
        self.assertEqual(int(pol.dscp),
                         self.rule_data['dscp_marking_rule']['dscp_mark'])

        # Test bw rules
        setattr(_policy, "rules", [self.egress_rule, self.ingress_rule])
        self.qos_driver.update_policy_precommit(self.ctxt, _policy)
        pol = self.aim_mgr.get(self._aim_context, pol)
        self.assertIsNotNone(pol)
        self.assertEqual(pol.ingress_dpp_pol, self.ingress_rule.id)
        self.assertEqual(pol.egress_dpp_pol, self.egress_rule.id)
        self.assertEqual(pol.dscp, '')
        egress_bw_rule = aim_res.QosDppPol(
            name=self.egress_rule.id, tenant_name=tenant_name)
        egress_bw_rule = self.aim_mgr.get(self._aim_context, egress_bw_rule)
        self.assertIsNotNone(egress_bw_rule)
        self.assertEqual(egress_bw_rule.burst,
                         str(self.egress_rule.max_burst_kbps))
        self.assertEqual(int(egress_bw_rule.rate), self.egress_rule.max_kbps)
        ingress_bw_rule = aim_res.QosDppPol(
            name=self.ingress_rule.id, tenant_name=tenant_name)
        ingress_bw_rule = self.aim_mgr.get(self._aim_context, ingress_bw_rule)
        self.assertIsNotNone(ingress_bw_rule)
        self.assertEqual(ingress_bw_rule.burst,
                         str(self.ingress_rule.max_burst_kbps))
        self.assertEqual(int(ingress_bw_rule.rate), self.ingress_rule.max_kbps)

        # Clean up the rules
        setattr(_policy, "rules", [])
        self.qos_driver.update_policy_precommit(self.ctxt, _policy)
        pol = self.aim_mgr.get(self._aim_context, pol)
        self.assertIsNone(pol.egress_dpp_pol)
        self.assertIsNone(pol.ingress_dpp_pol)
        egress_bw_rule = self.aim_mgr.get(self._aim_context, egress_bw_rule)
        self.assertIsNone(egress_bw_rule)
        ingress_bw_rule = self.aim_mgr.get(self._aim_context, ingress_bw_rule)
        self.assertIsNone(ingress_bw_rule)

        self.qos_driver.delete_policy_precommit(self.ctxt, _policy)
        pol = self.aim_mgr.get(self._aim_context, pol)
        self.assertIsNone(pol)

    def _make_qos_policy(self):
        qos_policy = policy_object.QosPolicy(
            self.admin_ctxt, **self.policy_data['policy'])
        qos_policy.create()
        return qos_policy

    def test_attach_detach_network_qos(self):
        net_qos_obj = self._make_qos_policy()
        net_qos_id = net_qos_obj.id if net_qos_obj else None
        network = self._make_network(self.fmt, 'net1', True,
                                     arg_list=tuple(list(['qos_policy_id'])),
                                     qos_policy_id=net_qos_id)
        epg = self.aim_mech._get_epg_by_network_id(self._ctx.session,
                                                   network['network']['id'])
        epg = self.aim_mgr.get(self._aim_context, epg)
        self.assertEqual(epg.qos_name, net_qos_id)

        data = {'network': {'qos_policy_id': None}}
        self._update('networks', network['network']['id'], data)
        epg = self.aim_mgr.get(self._aim_context, epg)
        self.assertIsNone(epg.qos_name)

    def test_port_qos_gbpDetailsForML2(self):
        self._register_agent('host1', test_aim_md.AGENT_CONF_OPFLEX)
        net = self._make_network(self.fmt, 'net1', True)
        self._make_subnet(self.fmt, net, '10.0.1.1', '10.0.1.0/24')
        qosPolicy = self._make_qos_policy()
        qosPort = self._make_port(self.fmt,
                                  net['network']['id'],
                                  None, arg_list=('qos_policy_id',),
                                  qos_policy_id=qosPolicy.id)
        self._bind_port_to_host(qosPort['port']['id'], 'host1')

        mapping = self.aim_mech.get_gbp_details(
            self._neutron_admin_context,
            device='tap%s' % qosPort['port']['id'],
            host='host1')
        self.assertEqual(qosPolicy.id, mapping['qos_policy']['name'])

    def _test_invalid_network_exception(self, kwargs):
        # Verify creating network with QoS fails
        net_qos_obj = self._make_qos_policy()
        net_qos_id = net_qos_obj.id if net_qos_obj else None
        kwargs['qos_policy_id'] = net_qos_id

        resp = self._create_network(
            self.fmt, 'net', True, arg_list=tuple(list(kwargs.keys())),
            **kwargs)
        result = self.deserialize(self.fmt, resp)
        self.assertEqual(
            'InvalidNetworkForQos',
            result['NeutronError']['type'])

    def test_external_network_exception(self):
        self._test_invalid_network_exception({'router:external': True})

    def test_svi_network_exception(self):
        self._test_invalid_network_exception({'apic:svi': True})
