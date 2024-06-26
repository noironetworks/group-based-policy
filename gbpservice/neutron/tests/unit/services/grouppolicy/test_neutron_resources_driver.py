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

from neutron_lib import context as nctx
from neutron_lib.plugins import constants as pconst
from neutron_lib.plugins import directory
import webob.exc

from gbpservice.neutron.services.grouppolicy import config
from gbpservice.neutron.tests.unit.services.grouppolicy import (
    test_grouppolicy_plugin as test_plugin)


ML2PLUS_PLUGIN = 'gbpservice.neutron.plugins.ml2plus.plugin.Ml2PlusPlugin'
CORE_PLUGIN = ('gbpservice.neutron.tests.unit.services.grouppolicy.'
               'test_resource_mapping.NoL3NatSGTestPlugin')


class CommonNeutronBaseTestCase(test_plugin.GroupPolicyPluginTestBase):

    def setUp(self, policy_drivers=None, core_plugin=None, l3_plugin=None,
              ml2_options=None, sc_plugin=None, qos_plugin=None,
              trunk_plugin=None):
        core_plugin = core_plugin or ML2PLUS_PLUGIN
        policy_drivers = policy_drivers or ['neutron_resources']
        config.cfg.CONF.set_override('policy_drivers',
                                     policy_drivers,
                                     group='group_policy')
        super(CommonNeutronBaseTestCase, self).setUp(core_plugin=core_plugin,
                                                     l3_plugin=l3_plugin,
                                                     ml2_options=ml2_options,
                                                     sc_plugin=sc_plugin,
                                                     qos_plugin=qos_plugin,
                                                     trunk_plugin=trunk_plugin)
        self._plugin = directory.get_plugin()
        self._plugin.remove_networks_from_down_agents = mock.Mock()
        self._plugin.is_agent_down = mock.Mock(return_value=False)
        self._context = nctx.get_admin_context()
        self._gbp_plugin = directory.get_plugin(pconst.GROUP_POLICY)
        self._l3_plugin = directory.get_plugin(pconst.L3)
        config.cfg.CONF.set_override('debug', True)

    def get_plugin_context(self):
        return self._plugin, self._context


class TestL2Policy(CommonNeutronBaseTestCase):

    def _test_l2_policy_lifecycle_implicit_l3p(self,
                                               shared=False):
        l2p = self.create_l2_policy(name="l2p1", shared=shared)
        l2p_id = l2p['l2_policy']['id']
        network_id = l2p['l2_policy']['network_id']
        l3p_id = l2p['l2_policy']['l3_policy_id']
        self.assertIsNotNone(network_id)
        self.assertIsNotNone(l3p_id)
        req = self.new_show_request('networks', network_id, fmt=self.fmt)
        res = self.deserialize(self.fmt, req.get_response(self.api))
        self.assertIsNotNone(res['network']['id'])
        self.show_l3_policy(l3p_id, expected_res_status=200)
        self.show_l2_policy(l2p_id, expected_res_status=200)
        self.update_l2_policy(l2p_id, expected_res_status=200,
                              name="new name")
        self.delete_l2_policy(l2p_id, expected_res_status=204)
        self.show_l2_policy(l2p_id, expected_res_status=404)
        req = self.new_show_request('networks', network_id, fmt=self.fmt)
        res = req.get_response(self.api)
        self.assertEqual(webob.exc.HTTPNotFound.code, res.status_int)
        self.show_l3_policy(l3p_id, expected_res_status=404)

    def test_unshared_l2_policy_lifecycle_implicit_l3p(self):
        self._test_l2_policy_lifecycle_implicit_l3p()

    def test_shared_l2_policy_lifecycle_implicit_l3p(self):
        self._test_l2_policy_lifecycle_implicit_l3p(shared=True)


class TestL2PolicyRollback(CommonNeutronBaseTestCase):

    def setUp(self, policy_drivers=None,
              core_plugin=None, ml2_options=None, sc_plugin=None):
        policy_drivers = policy_drivers or ['neutron_resources',
                                            'dummy']
        super(TestL2PolicyRollback, self).setUp(policy_drivers=policy_drivers,
                                                core_plugin=core_plugin,
                                                ml2_options=ml2_options,
                                                sc_plugin=sc_plugin)
        self.dummy_driver = directory.get_plugin(
            'GROUP_POLICY').policy_driver_manager.policy_drivers['dummy'].obj

    def test_l2_policy_create_fail(self):
        orig_func = self.dummy_driver.create_l2_policy_precommit
        self.dummy_driver.create_l2_policy_precommit = mock.Mock(
            side_effect=Exception)
        self.create_l2_policy(name="l2p1", expected_res_status=500)
        self.assertEqual([], self._plugin.get_networks(self._context))
        self.assertEqual([], self._gbp_plugin.get_l2_policies(self._context))
        self.assertEqual([], self._gbp_plugin.get_l3_policies(self._context))
        self.dummy_driver.create_l2_policy_precommit = orig_func

    def test_l2_policy_update_fail(self):
        orig_func = self.dummy_driver.update_l2_policy_precommit
        self.dummy_driver.update_l2_policy_precommit = mock.Mock(
            side_effect=Exception)
        l2p = self.create_l2_policy(name="l2p1")
        l2p_id = l2p['l2_policy']['id']
        self.update_l2_policy(l2p_id, expected_res_status=500,
                              name="new name")
        new_l2p = self.show_l2_policy(l2p_id, expected_res_status=200)
        self.assertEqual(l2p['l2_policy']['name'],
                         new_l2p['l2_policy']['name'])
        self.dummy_driver.update_l2_policy_precommit = orig_func

    def test_l2_policy_delete_fail(self):
        orig_func = self.dummy_driver.delete_l2_policy_precommit
        self.dummy_driver.delete_l2_policy_precommit = mock.Mock(
            side_effect=Exception)
        l2p = self.create_l2_policy(name="l2p1")
        l2p_id = l2p['l2_policy']['id']
        network_id = l2p['l2_policy']['network_id']
        l3p_id = l2p['l2_policy']['l3_policy_id']
        self.delete_l2_policy(l2p_id, expected_res_status=500)
        req = self.new_show_request('networks', network_id, fmt=self.fmt)
        res = self.deserialize(self.fmt, req.get_response(self.api))
        self.assertIsNotNone(res['network']['id'])
        self.show_l3_policy(l3p_id, expected_res_status=200)
        self.show_l2_policy(l2p_id, expected_res_status=200)
        self.dummy_driver.delete_l2_policy_precommit = orig_func
