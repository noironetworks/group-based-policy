#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.

import collections
import subprocess

from oslo_log import log as logging
from tempest import config
from tempest.lib.common.utils import data_utils
from tempest.scenario import manager
from tempest import test

CONF = config.CONF
LOG = logging.getLogger(__name__)
Floating_IP_tuple = collections.namedtuple('Floating_IP_tuple',
                                           ['floating_ip', 'server'])
MAX_RETRY = 50


class TestServerEp(manager.NetworkScenarioTest):
    def setUp(self):
        super(TestServerEp, self).setUp()
        self.keypairs = {}
        self.servers = []
        self.deleted_rule = False
        self.track_success = False

    def ping_remote(self, should_connect, should_check_floating_ip_status):
        floating_ip, server = self.floating_ip_tuple
        ip_address = floating_ip['floating_ip_address']
        private_key = None
        floatingip_status = 'DOWN'

        if should_connect:
            private_key = self._get_server_key(server)
            if private_key is not None:
                floatingip_status = 'ACTIVE'

        # Check FloatingIP Status before initiating a connection
        if should_check_floating_ip_status:
            self.check_floating_ip_status(floating_ip, floatingip_status)
        LOG.debug('Starting Ping to Connect')
        self._track_connectivity(ip_address)
        return self.track_success

    def _track_connectivity(self, floating_ip):
        ping_command = "ping -O " + floating_ip
        process = subprocess.Popen(
            ping_command, shell=True, stdout=subprocess.PIPE, close_fds=True)
        LOG.debug('Tracking Connectivity')
        error_msg = "no answer yet for icmp_seq"
        success_msg = "bytes from " + floating_ip
        retry = MAX_RETRY

        # To skip first line of ping command
        output = process.stdout.readline()

        while process.poll() is None and retry > 0:
            output = process.stdout.readline()
            output = output.strip()
            if not output:
                LOG.error('Ping failed - Nothing to show')
                break
            if error_msg in output and self.deleted_rule is True:
                self.track_success = True
                LOG.debug("Rules Deleted Successfully")
                break
            elif success_msg in output and self.deleted_rule is True:
                LOG.debug("Waiting for rule to be deleted in fabric")
                retry -= 1
                if retry == 0:
                    LOG.error("Error - Still Pinging even after "
                              "deleting the security group rule")
                    break
            elif success_msg in output and self.deleted_rule is False:
                LOG.debug("Waiting for rules to be "
                          "deleted from security group")
                self.deleted_rule = self._delete_security_group_rule(
                                    self.servers)
                retry -= 1
                if retry == 0:
                    LOG.error("Security rule was not deleted in time")
                    break
            else:
                LOG.debug("Waiting for Server to get Active")
                retry -= 1
                if retry == 0:
                    LOG.error("Error - %s", (output,))
                    break

        LOG.debug("Closing Subprocess")
        process.stdout.close()
        process.kill()

    def _delete_security_group_rule(self, servers):
        for server in servers:
            sg = server['security_groups']
            LOG.debug("Security group is %s ", (sg,))
            rule_list_body = (
                self.security_group_rules_client.list_security_group_rules())
            for rule in rule_list_body['security_group_rules']:
                if ("icmp" == rule["protocol"] and
                        rule["direction"] == "ingress"):
                    LOG.debug("The rule is %s ", (rule,))
                    (self.security_group_rules_client.
                     delete_security_group_rule(rule["id"]))

        return True

    def _setup_network_and_servers(self, **kwargs):
        boot_with_port = kwargs.pop('boot_with_port', False)
        self.security_group = self._create_security_group(
                              tenant_id=self.tenant_id)
        security_groups = [{'name': self.security_group['name']}]
        self.network, self.subnet, self.router = self.create_networks(**kwargs)
        self.check_networks()

        self.ports = []
        self.port_id = None
        if boot_with_port:
            # create a port on the network and boot with that
            self.port_id = self._create_port(self.network['id'])['id']
            self.ports.append({'port': self.port_id})

        name = data_utils.rand_name('server-smoke')
        server = self._create_server(name, self.network,
                 security_groups, self.port_id)

        floating_ip = self.create_floating_ip(server)
        self.floating_ip_tuple = Floating_IP_tuple(floating_ip, server)

    def check_networks(self):
        """Checks that we see the newly created network/subnet/router

        via checking the result of list_[networks,routers,subnets]
        """

        seen_nets = self._list_networks()
        seen_names = [n['name'] for n in seen_nets]
        seen_ids = [n['id'] for n in seen_nets]
        self.assertIn(self.network['name'], seen_names)
        self.assertIn(self.network['id'], seen_ids)

        if self.subnet:
            seen_subnets = self._list_subnets()
            seen_net_ids = [n['network_id'] for n in seen_subnets]
            seen_subnet_ids = [n['id'] for n in seen_subnets]
            self.assertIn(self.network['id'], seen_net_ids)
            self.assertIn(self.subnet['id'], seen_subnet_ids)

        if self.router:
            seen_routers = self._list_routers()
            seen_router_ids = [n['id'] for n in seen_routers]
            seen_router_names = [n['name'] for n in seen_routers]
            self.assertIn(self.router['name'],
                          seen_router_names)
            self.assertIn(self.router['id'],
                          seen_router_ids)

    def _create_server(self, name, network, security_groups, port_id=None):
        keypair = self.create_keypair()
        self.keypairs[keypair['name']] = keypair
        network = {'uuid': network['id']}
        if port_id is not None:
            network['port'] = port_id

        server = self.create_server(
            name=name,
            networks=[network],
            key_name=keypair['name'],
            security_groups=security_groups,
            wait_until='ACTIVE')
        self.servers.append(server)
        return server

    def _get_server_key(self, server):
        return self.keypairs[server['key_name']]['private_key']

    @test.attr(type='smoke')
    @test.services('compute', 'network')
    def test_server_ep(self):
        self._setup_network_and_servers()
        self.assertTrue(self.ping_remote(True, True))
        LOG.debug("Testing Done")
