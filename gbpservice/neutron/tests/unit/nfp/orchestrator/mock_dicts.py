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

"""This module has the input data for heat_driver UTs."""


class DummyDictionaries(object):
    """Implements the input data for heat_driver UTs.

    This class holds the input data that are required in
    testing the heat_driver test cases.
    """

    DEFAULT_LBV2_CONFIG = {
        "heat_template_version": "2015-10-15",
        "description": "Configuration for Haproxy Neutron LB V2 service",
        "parameters": {
            "lb_port": {
                "type": "number",
                "default": 80,
                "description": "Port used by the load balancer"
            },
            "app_port": {
                "type": "number",
                "default": 80,
                "description": "Port used by the servers"
            },
            "Subnet": {
                "type": "string",
                "description": "Subnet on which the LB will be located"
            },
            "vip_ip": {
                "type": "string",
                "description": "VIP IP Address"
            },
            "service_chain_metadata": {
                "type": "string",
                "description": "sc metadata"
            }
        },
        "resources": {
            "monitor": {
                "type": "OS::Neutron::LBaaS::HealthMonitor",
                "properties": {
                    "delay": 3,
                    "type": "HTTP",
                    "timeout": 3,
                    "max_retries": 3,
                    "pool": {
                        "get_resource": "pool"
                    }
                }
            },
            "pool": {
                "type": "OS::Neutron::LBaaS::Pool",
                "properties": {
                    "lb_algorithm": "ROUND_ROBIN",
                    "protocol": "HTTP",
                    "listener": {
                        "get_resource": "listener"
                    }
                }
            },
            "listener": {
                "type": "OS::Neutron::LBaaS::Listener",
                "properties": {
                    "loadbalancer": {
                        "get_resource": "loadbalancer"
                    },
                    "protocol": "HTTP",
                    "protocol_port": {
                        "get_param": "lb_port"
                    }
                }
            },
            "loadbalancer": {
                "type": "OS::Neutron::LBaaS::LoadBalancer",
                "properties": {
                    "vip_subnet": {
                        "get_param": "Subnet"
                    },
                    "provider": "loadbalancerv2",
                    "vip_address": {
                        "get_param": "vip_ip"
                    },
                    "description": {
                        "get_param": "service_chain_metadata"
                    }
                }
            }
        }
    }

    DEFAULT_FW_CONFIG = {
        'heat_template_version': '2013-05-23',
        'description': 'Template to deploy firewall',
        'resources': {
            'sc_firewall_rule3': {
                'type': 'OS::Neutron::FirewallRule',
                'properties': {
                    'action': 'allow',
                    'destination_port': '82',
                    'protocol': 'tcp', 'name': 'Rule_3'
                }
            },
            'sc_firewall_rule2': {
                'type': 'OS::Neutron::FirewallRule',
                'properties': {
                    'action': 'allow',
                    'destination_port': '81',
                    'protocol': 'tcp', 'name': 'Rule_2'
                }
            },
            'sc_firewall_rule1': {
                'type': 'OS::Neutron::FirewallRule',
                'properties': {
                    'action': 'allow',
                    'destination_port': '80',
                    'protocol': 'tcp',
                    'name': 'Rule_1'
                }
            },
            'sc_firewall_rule0': {
                'type': 'OS::Neutron::FirewallRule',
                'properties': {
                    'action': 'allow',
                    'destination_port': '22',
                    'protocol': 'tcp', 'name': 'Rule_0'
                }
            },
            'sc_firewall_rule4': {
                'type': 'OS::Neutron::FirewallRule',
                'properties': {
                    'action': 'allow',
                    'protocol': 'icmp',
                    'name': 'Rule_4'
                }
            },
            'sc_firewall_policy': {
                'type': 'OS::Neutron::FirewallPolicy',
                'properties': {
                    'name': '',
                    'firewall_rules': [
                         {'get_resource': 'sc_firewall_rule0'},
                         {'get_resource': 'sc_firewall_rule1'},
                         {'get_resource': 'sc_firewall_rule2'},
                         {'get_resource': 'sc_firewall_rule3'},
                         {'get_resource': 'sc_firewall_rule4'}]
                }
            },
            'sc_firewall': {
                'type': 'OS::Neutron::Firewall',
                'properties': {
                    'firewall_policy_id': {
                         'get_resource': 'sc_firewall_policy'
                    },
                    'name': 'serviceVM_infra_FW',
                    'description': {'insert_type': 'east_west'}
                }
            }
        }
    }

    DEFAULT_VPN_CONFIG = {
        'resources': {
            'IKEPolicy': {
                'type': 'OS::Neutron::IKEPolicy',
                'properties': {
                    'name': 'IKEPolicy',
                    'auth_algorithm': 'sha1',
                    'encryption_algorithm': '3des',
                    'pfs': 'group5',
                    'lifetime': {
                        'units': 'seconds',
                        'value': 3600
                    },
                    'ike_version': 'v1',
                    'phase1_negotiation_mode': 'main'
                }
            },
            'VPNService': {
                'type': 'OS::Neutron::VPNService',
                'properties': {
                    'router_id': {
                        'get_param': 'RouterId'
                    },
                    'subnet_id': {
                        'get_param': 'Subnet'
                    },
                    'admin_state_up': 'true',
                    'description': {
                        'get_param': 'ServiceDescription'
                    },
                    'name': 'VPNService'
                }
            },
            'site_to_site_connection1': {
                'type': 'OS::Neutron::IPsecSiteConnection',
                'properties': {
                    'psk': 'secret',
                    'initiator': 'bi-directional',
                    'name': 'site_to_site_connection1',
                    'admin_state_up': 'true',
                    'description':
                        'fip=1.103.1.20;tunnel_local_cidr=11.0.1.0/24;\
                        user_access_ip=1.103.2.20;fixed_ip=192.168.0.3;\
                        standby_fip=1.103.1.21;service_vendor=vyos;\
                        stitching_cidr=192.168.0.0/28;\
                        stitching_gateway=192.168.0.1;mgmt_gw_ip=120.0.0.1',
                    'peer_cidrs': ['11.0.0.0/24'],
                    'mtu': 1500,
                    'ikepolicy_id': {
                        'get_resource': 'IKEPolicy'
                    },
                    'dpd': {
                        'interval': 30,
                        'actions': 'hold',
                        'timeout': 120
                    },
                    'vpnservice_id': {
                        'get_resource': 'VPNService'
                    },
                    'peer_address': '1.103.2.88',
                    'peer_id': '1.103.2.88',
                    'ipsecpolicy_id': {
                        'get_resource': 'IPsecPolicy'
                    }
                }
            },
            'IPsecPolicy': {
                'type': 'OS::Neutron::IPsecPolicy',
                'properties': {
                    'name': 'IPsecPolicy',
                    'transform_protocol': 'esp',
                    'auth_algorithm': 'sha1',
                    'encapsulation_mode': 'tunnel',
                    'encryption_algorithm': '3des',
                    'pfs': 'group5',
                    'lifetime': {
                        'units': 'seconds',
                        'value': 3600
                    }
                }
            }
        }
    }

    appended_sc_firewall_policy = {
        'type': 'OS::Neutron::FirewallPolicy',
        'properties': {
            'name': '',
            'firewall_rules': [
                {
                    'get_resource': 'sc_firewall_rule0'
                },
                {'get_resource': 'sc_firewall_rule1'},
                {'get_resource': 'sc_firewall_rule2'},
                {'get_resource': 'sc_firewall_rule3'},
                {'get_resource': 'sc_firewall_rule4'},
                {'get_resource': 'node_driver_rule_2b86019a-45f7-44_1'},
                {'get_resource': 'node_driver_rule_2b86019a-45f7-44_2'},
                {'get_resource': 'node_driver_rule_2b86019a-45f7-44_3'},
                {'get_resource': 'node_driver_rule_2b86019a-45f7-44_4'},
                {'get_resource': 'node_driver_rule_2b86019a-45f7-44_5'},
            ]
        }
    }

    updated_sc_firewall_policy = {
        'type': 'OS::Neutron::FirewallPolicy',
        'properties': {
            'name': '-fw_redirect',
            'firewall_rules': [
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_1'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_2'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_3'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_4'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_5'},
            ]
        }
    }

    updated_template_sc_firewall_policy = {
        'type': 'OS::Neutron::FirewallPolicy',
        'properties': {
            'name': '',
            'firewall_rules': [
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_1'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_2'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_3'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_4'},
                {'get_resource': 'node_driver_rule_af6a8a58-1e25-49_5'},
            ]
        }
    }

    policy_targets = {
        'policy_targets': [
            {'name': 'provider_0132c_00b93',
             'port_id': 'dde7d849-4c7c-4b48-8c21-f3f52c646fbe',
             'id': "dde7d849-4c7c-4b48-8c21-f3f52c646fbf",
             'policy_target_group_id': "dde7d849-4c7c-4b48-8c21-f3f52c646fbg"}]
    }

    policy_target = {
        'policy_target': {
            'name': 'service_target_provider_0132c_00b93'
        }
    }

    port_info = {
        'port': {
            'status': 'ACTIVE',
            'binding:host_id': 'LibertyCompute',
            'name': '',
            'allowed_address_pairs': [],
            'admin_state_up': True,
            'network_id': '2286b432-a443-4cd3-be49-e354f531abe3',
            'dns_name': '',
            'extra_dhcp_opts': [],
            'mac_address': 'fa:16:3e:43:34:33',
            'dns_assignment': [
                {'hostname': 'host-42-0-0-13',
                 'ip_address': '42.0.0.13',
                 'fqdn': 'host-42-0-0-13.openstacklocal.'
                 }],
            'binding:vif_details': {
                'port_filter': True,
                'ovs_hybrid_plug': True
            },
            'binding:vif_type': 'ovs',
            'device_owner': 'compute:nova',
            'tenant_id': 'f6b09b7a590642d8ac6de73df0ab0686',
            'binding:profile': {},
            'binding:vnic_type': 'normal',
            'fixed_ips': [
                {'subnet_id': 'b31cdafe-bdf3-4c19-b768-34d623d77d6c',
                 'ip_address': '42.0.0.13'}],
            'id': 'dde7d849-4c7c-4b48-8c21-f3f52c646fbe',
            'security_groups': ['ad3b95a4-b5ce-4a95-9add-6ef2ee797e72'],
            'device_id': '36e9a6d9-ea04-4627-93c5-6f708368c070'
        }
    }
    provider_ptg = {
        'shared': False,
        'subnets': ['a2702d68-6deb-425c-a266-e27b349e00ce'],
        'proxy_group_id': None,
        'description': '',
        'consumed_policy_rule_sets': [],
        'network_service_policy_id': '0cdf2cba-90f8-44da-84a5-876e582f6e35',
        'tenant_id': '8ae6701128994ab281dde6b92207bb19',
        'service_management': False,
        'provided_policy_rule_sets': ['7d4b1ef2-eb80-415d-ad13-abf0ea0c52f3'],
        'policy_targets': [
            {'name': 'provider_0132c_00b93',
             'port_id': 'dde7d849-4c7c-4b48-8c21-f3f52c646fbe'}],
        'proxy_type': None,
        'proxied_group_id': None,
        'l2_policy_id': '120aa972-1b58-418d-aa5b-1d2f96612c49',
        'id': 'af6a8a58-1e25-49c4-97a3-d5f50b3aa04b',
        'name': 'fw_redirect'
    }

    consumer_ptg = {
        'shared': False,
        'subnets': ['a2702d68-6deb-425c-a266-e27b349e00ce'],
        'proxy_group_id': None,
        'description': '',
        'consumed_policy_rule_sets': ['7d4b1ef2-eb80-415d-ad13-abf0ea0c52f3'],
        'network_service_policy_id': '0cdf2cba-90f8-44da-84a5-876e582f6e35',
        'tenant_id': '8ae6701128994ab281dde6b92207bb19',
        'service_management': False,
        'provided_policy_rule_sets': [],
        'policy_targets': [
            {'name': 'provider_0132c_00b93',
             'port_id': 'dde7d849-4c7c-4b48-8c21-f3f52c646fbe'}],
        'proxy_type': None,
        'proxied_group_id': None,
        'l2_policy_id': '120aa972-1b58-418d-aa5b-1d2f96612c49',
        'id': 'af6a8a58-1e25-49c4-97a3-d5f50b3aa04b',
        'name': 'fw_redirect'
    }

    l3_policies = {
        'l3_policies': [
            {'tenant_id': '8ae6701128994ab281dde6b92207bb19',
             'name': 'remote-vpn-client-pool-cidr-l3policy'}]
    }

    policy_rule_sets = {
        'policy_rule_sets': [
            {'id': '7d4b1ef2-eb80-415d-ad13-abf0ea0c52f3',
             'name': 'fw_redirect',
             'policy_rules': ['493788ad-2b9a-47b1-b04d-9096d4057fb5'],
             'tenant_id': '8ae6701128994ab281dde6b92207bb19',
             'shared': False,
             'consuming_policy_target_groups':
             ['af6a8a58-1e25-49c4-97a3-d5f50b3aa04b'],
             'consuming_external_policies': None}]
    }

    policy_rules = {
        'policy_rules': [
            {'id': '493788ad-2b9a-47b1-b04d-9096d4057fb5',
             'name': 'fw_redirect',
             'policy_actions': ['0bab5fa6-4f89-4e15-8363-dacc7d825466'],
             'policy_classifier_id': '8e5fc80f-7544-484c-82d0-2a5794c10664',
             'tenant_id': '8ae6701128994ab281dde6b92207bb19',
             'shared': False}]
    }

    policy_actions = {
        'policy_actions': [
            {'id': '0bab5fa6-4f89-4e15-8363-dacc7d825466',
             'name': 'fw_redirect',
             'action_value': '1e83b288-4b56-4851-83e2-69c4365aa8e5',
             'action_type': 'redirect',
             'tenant_id': '8ae6701128994ab281dde6b92207bb19',
             'shared': False}]
    }

    policy_target_groups = {
        'policy_target_groups': [
            {'shared': False,
             'subnets': ['a2702d68-6deb-425c-a266-e27b349e00ce'],
             'proxy_group_id': None,
             'description': '',
             'consumed_policy_rule_sets': [],
             'network_service_policy_id':
             '0cdf2cba-90f8-44da-84a5-876e582f6e35',
             'tenant_id': '8ae6701128994ab281dde6b92207bb19',
             'service_management': False,
             'provided_policy_rule_sets':
                 ['7d4b1ef2-eb80-415d-ad13-abf0ea0c52f3'],
             'policy_targets': [
                 {'name': 'provider_0132c_00b93',
                  'port_id': 'dde7d849-4c7c-4b48-8c21-f3f52c646fbe'}],
             'proxy_type': None,
             'proxied_group_id': None,
             'l2_policy_id': '120aa972-1b58-418d-aa5b-1d2f96612c49',
             'id': 'af6a8a58-1e25-49c4-97a3-d5f50b3aa04b',
             'name': 'fw_redirect'}]
    }

    subnet_info = {
        'subnet': {
            'name': 'lb-subnet',
            'enable_dhcp': True,
            'network_id': '2286b432-a443-4cd3-be49-e354f531abe3',
            'tenant_id': 'f6b09b7a590642d8ac6de73df0ab0686',
            'dns_nameservers': [],
            'ipv6_ra_mode': None,
            'allocation_pools': [{
                'start': '42.0.0.2', 'end': '42.0.0.254'}],
            'gateway_ip': '42.0.0.1',
            'ipv6_address_mode': None,
            'ip_version': 4,
            'host_routes': [],
            'cidr': '42.0.0.0/24',
            'id': 'b31cdafe-bdf3-4c19-b768-34d623d77d6c',
            'subnetpool_id': None
        }
    }

    subnets_info = {
        'subnets': [
            {'name': 'lb-subnet',
             'enable_dhcp': True,
             'network_id': '2286b432-a443-4cd3-be49-e354f531abe3',
             'tenant_id': 'f6b09b7a590642d8ac6de73df0ab0686',
             'dns_nameservers': [],
             'ipv6_ra_mode': None,
             'allocation_pools': [{
                 'start': '42.0.0.2', 'end': '42.0.0.254'}],
             'gateway_ip': '42.0.0.1',
             'ipv6_address_mode': None,
             'ip_version': 4,
             'host_routes': [],
             'cidr': '42.0.0.0/24',
             'id': 'b31cdafe-bdf3-4c19-b768-34d623d77d6c',
             'subnetpool_id': None}]
    }

    external_policies = {'external_policies': {}}

    fw_template_properties = {
        'fw_rule_keys': ['sc_firewall_rule3', 'sc_firewall_rule2',
                         'sc_firewall_rule1', 'sc_firewall_rule0',
                         'sc_firewall_rule4'],
        'name': '2b8',
        'properties_key': 'properties',
        'resources_key': 'resources',
        'is_template_aws_version': False,
        'fw_policy_key': 'sc_firewall_policy'
    }

    fw_scn_config = "{\"heat_template_version\": \"2013-05-23\",\
        \"description\": \"Template to deploy firewall\", \"resources\":\
        {\"sc_firewall_rule3\": {\"type\": \"OS::Neutron::FirewallRule\",\
        \"properties\": {\"action\": \"allow\", \"destination_port\": \"82\",\
        \"protocol\": \"tcp\", \"name\": \"Rule_3\"}}, \"sc_firewall_rule2\":\
        {\"type\": \"OS::Neutron::FirewallRule\", \"properties\": {\"action\":\
        \"allow\", \"destination_port\": \"81\", \"protocol\": \"tcp\",\
        \"name\": \"Rule_2\"}}, \"sc_firewall_rule1\": {\"type\":\
        \"OS::Neutron::FirewallRule\", \"properties\": {\"action\": \"allow\",\
        \"destination_port\": \"80\", \"protocol\": \"tcp\", \"name\":\
        \"Rule_1\"}}, \"sc_firewall_rule0\": {\"type\":\
        \"OS::Neutron::FirewallRule\", \"properties\": {\"action\": \"allow\",\
        \"destination_port\": \"22\", \"protocol\": \"tcp\", \"name\":\
        \"Rule_0\"}}, \"sc_firewall_rule4\": {\"type\":\
        \"OS::Neutron::FirewallRule\", \"properties\": {\"action\": \"allow\",\
        \"protocol\": \"icmp\", \"name\": \"Rule_4\"}}, \"sc_firewall_policy\"\
        :{\"type\": \"OS::Neutron::FirewallPolicy\", \"properties\": {\"name\"\
        :\"\", \"firewall_rules\": [{\"get_resource\": \"sc_firewall_rule0\"},\
        {\"get_resource\": \"sc_firewall_rule1\"}, {\"get_resource\":\
        \"sc_firewall_rule2\"}, {\"get_resource\": \"sc_firewall_rule3\"},\
        {\"get_resource\": \"sc_firewall_rule4\"}]}}, \"sc_firewall\":\
        {\"type\": \"OS::Neutron::Firewall\", \"properties\":\
        {\"firewall_policy_id\": {\"get_resource\": \"sc_firewall_policy\"},\
        \"description\": \"{\'insert_type\': \'east_west\',\
        \'vm_management_ip\': u'192.168.20.138', \'provider_ptg_info\':\
        [\'fa:16:3e:28:7d:b2\']}\", \"name\": \"serviceVM_infra_FW\"}}}}"

    lbv2_scn_config = "{\"heat_template_version\": \"2015-10-15\",\
        \"description\": \"Configuration for Haproxy Neutron LB V2 service\",\
        \"parameters\": {\"Subnet\": {\"type\": \"string\", \"description\":\
        \"Subnet on which the load balancer will be located\"}, \
        \"service_chain_metadata\": {\"type\": \"string\", \"description\":\
        \"sc metadata\"}, \"vip_ip\": {\"type\": \"string\", \"description\":\
        \"VIP IP Address\"}, \"app_port\": {\"default\": 80, \"type\":\
        \"number\", \"description\": \"Port used by the servers\"}, \
        \"lb_port\": {\"default\": 80, \"type\": \"number\", \"description\":\
        \"Port used by the load balancer\"}}, \"resources\": {\"listener\":\
        {\"type\": \"OS::Neutron::LBaaS::Listener\", \"properties\":\
        {\"protocol_port\": {\"get_param\": \"lb_port\"}, \"protocol\":\
        \"HTTP\", \"loadbalancer\": {\"get_resource\": \"loadbalancer\"}}},\
        \"loadbalancer\": {\"type\": \"OS::Neutron::LBaaS::LoadBalancer\",\
        \"properties\": {\"vip_subnet\": {\"get_param\": \"Subnet\"},\
        \"vip_address\": {\"get_param\": \"vip_ip\"}, \"description\":\
        {\"get_param\": \"service_chain_metadata\"}, \"provider\":\
        \"loadbalancerv2\"}}, \"monitor\": {\"type\":\
        \"OS::Neutron::LBaaS::HealthMonitor\", \"properties\": {\"delay\": 3,\
        \"max_retries\": 3, \"type\": \"HTTP\", \"pool\": {\"get_resource\":\
        \"pool\"}, \"timeout\": 3}}, \"pool\": {\"type\": \
        \"OS::Neutron::LBaaS::Pool\", \"properties\":\
        {\"lb_algorithm\": \"ROUND_ROBIN\", \"listener\": {\"get_resource\":\
        \"listener\"}, \"protocol\": \"HTTP\"}}}}"

    vpn_scn_config = "{\"description\":\"Createsnewvpnservice-ike+ipsec+\
        vpnservice+site-siteconnection(s)\", \"heat_template_version\
        \":\"2013-05-23\", \"parameters\":{\"RouterId\":{\"description\
        \":\"RouterID\", \"type\":\"string\"}, \"ServiceDescription\":{\
        \"description\":\"fip;tunnel_local-cidr\", \"type\":\"string\"}, \
        \"Subnet\":{\"description\":\"Subnetidonwhichvpnserviceislaunched\
        \", \"type\":\"string\"}}, \"resources\":{\"IKEPolicy\":{\"properties\
        \":{\"auth_algorithm\":\"sha1\", \"encryption_algorithm\":\"3des\", \
        \"ike_version\":\"v1\", \"lifetime\":{\"units\":\"seconds\", \"value\
        \":3600}, \"name\":\"IKEPolicy\", \"pfs\":\"group5\", \
        \"phase1_negotiation_mode\":\"main\"}, \"type\":\
        \"OS::Neutron::IKEPolicy\"}, \"IPsecPolicy\":{\"properties\":{\
        \"auth_algorithm\":\"sha1\", \"encapsulation_mode\":\"tunnel\", \
        \"encryption_algorithm\":\"3des\", \"lifetime\":{\"units\":\"seconds\
        \", \"value\":3600}, \"name\":\"IPsecPolicy\", \"pfs\":\"group5\", \
        \"transform_protocol\":\"esp\"}, \"type\":\"OS::Neutron::IPsecPolicy\
        \"}, \"VPNService\":{\"properties\":{\"admin_state_up\":\"true\", \
        \"description\":{\"get_param\":\"ServiceDescription\"}, \"name\":\
        \"VPNService\", \"router_id\":{\"get_param\":\"RouterId\"}, \
        \"subnet_id\":{\"get_param\":\"Subnet\"}}, \"type\":\
        \"OS::Neutron::VPNService\"}, \"site_to_site_connection1\
        \":{\"properties\":{\"admin_state_up\":\"true\", \"dpd\":{\"actions\
        \":\"hold\", \"interval\":30, \"timeout\":120}, \"ikepolicy_id\":{\
        \"get_resource\":\"IKEPolicy\"}, \"initiator\":\"bi-directional\", \
        \"ipsecpolicy_id\":{\"get_resource\":\"IPsecPolicy\"}, \"mtu\":1500, \
        \"name\":\"site_to_site_connection1\", \"peer_address\":\
        \"192.168.102.117\", \"peer_cidrs\":[\"11.0.0.0/24\"], \"peer_id\":\
        \"11.0.0.3\", \"psk\":\"secret\", \"vpnservice_id\":{\"get_resource\
        \":\"VPNService\"}}, \"type\":\"OS::Neutron::IPsecSiteConnection\"}}}"

    service_profile = {
        'service_flavor': 'vyos',
        'service_type': 'FIREWALL'
    }

    vpn_service_profile = {
        'service_flavor': 'vyos',
        'service_type': 'VPN'
    }

    lbv2_service_profile = {
        'service_flavor': 'haproxy',
        'service_type': 'LOADBALANCERV2'
    }

    fw_service_chain_node = {
        'id': '012345678919',
        'name': 'scn_fw',
        'config': fw_scn_config
    }

    vpn_service_chain_node = {
        'id': '012345678919',
        'name': 'scn_vpn',
        'config': vpn_scn_config
    }

    lbv2_service_chain_node = {
        'id': '012345678919',
        'name': 'scn_lb',
        'config': lbv2_scn_config
    }

    service_chain_instance = {
        'id': '7834569034456677',
        'name': 'sci_fw'
    }

    consumer_port = {
        'fixed_ips': [{
            'ip_address': '11.0.3.4',
            'subnet_id': '9876256378888333'
        }],
        'id': 'af6a8a58-1e25-49c4-97a3-d5f50b3aa04b'
    }

    network_function_details = {
        'network_function': {
            'status': 'ACTIVE',
            'description': '\nuser_access_ip=\'192.168.203.12\';'
                           'fixed_ip=\'11.0.3.4\';'
                           'tunnel_local_cidr=\'11.0.3.0/24\'',
            'config_policy_id': '57d6b523-ae89-41cd-9b63-9bfb054a20b6',
            'tenant_id': 'ee27b1d0d7f04ac390ee7ec4b2fd5b13',
            'network_function_instances': [
                '4693118c-149a-46e7-b92c-cc729b536a2e'],
            'service_chain_id': '507988d2-4b46-4df4-99d2-746676500872',
            'service_id': '1200332d-b432-403b-8350-89b782256be5',
            'service_profile_id': 'ab3b704b-a7d9-4c55-ab43-57ed5e29867d',
            'id': '5ad7439b-7259-47cd-be88-36f641e0b5c8',
            'name':
            'LOADBALANCERV2.haproxy.507988d2-4b46-4df4-99d2-746676500872'
        },
        'network_function_instance': {
            'status': 'ACTIVE',
            'name':
            'LOADBALANCERV2.haproxy.507988d2-4b46-4df4-99d2-7466765002',
            'network_function_device_id': '3c3e502a-256e-4597-91a9-71902380c0',
            'tenant_id': 'ee27b1d0d7f04ac390ee7ec4b2fd5b13',
            'ha_state': None,
            'network_function_id': '5ad7439b-7259-47cd-be88-36f641e0b5c8',
            'port_info': ['8cdcc00b-b791-4039-a5b4-e4d8b3d59e9f'],
            'id': '4693118c-149a-46e7-b92c-cc729b536a2e',
            'description': None
        },
        'network_function_device': {
            'status': 'ACTIVE',
            'monitoring_port_network': None,
            'monitoring_port_id': None,
            'mgmt_ip_address': '11.0.0.27',
            'description': '',
            'service_vendor': None,
            'tenant_id': 'ee27b1d0d7f04ac390ee7ec4b2fd5b13',
            'max_interfaces': 8,
            'mgmt_port_id': '4497a287-d947-4845-af29-a9d6ad6515e9',
            'reference_count': 1,
            'interfaces_in_use': 2,
            'id': '3c3e502a-256e-4597-91a9-719023808ec0',
            'name': 'LOADBALANCERV2.haproxy.507988d2-4b46-4df4-99d2-7466765008'
        }
    }

    _service_details = {
        'consuming_external_policies': [{
            'status': None,
            'consumed_policy_rule_sets': (
                                ['46de9c30-3f87-4fb7-8e56-5e60827e1e8f']),
            'external_segments': ['7648db78-f0e4-403d-91d4-c6d80963d56c'],
            'description': '',
            'tenant_id': '793827b52b3348929e97b23081dfac27',
            'provided_policy_rule_sets': [],
            'shared': False,
            'status_details': None,
            'id': 'aa06bb8b-1250-40e0-a1d0-e25a713cc978',
            'name': 'vpn-consumer'}],
        'service_vendor': 'vyos',
        'image_name': 'vyos',
        'network_mode': 'gbp',
        'consuming_ptgs_details': [],
        'device_type': 'nova',
        'service_type': 'VPN'
    }

    _subnet = {
        'shared': False,
        'description': None,
        'enable_dhcp': True,
        'network_id': '1e8612e0-8099-4577-ac27-97e7db6f5841',
        'tenant_id': '793827b52b3348929e97b23081dfac27',
        'created_at': '2016-07-26T17:05:11',
        'dns_nameservers': [],
        'updated_at': '2016-07-26T17:05:21',
        'ipv6_ra_mode': None,
        'allocation_pools': [{
                        'start': '192.168.0.2',
                        'end': '192.168.0.14'}],
        'gateway_ip': '192.168.0.1',
        'ipv6_address_mode': None,
        'ip_version': 4,
        'host_routes': [{
                    'nexthop': '192.168.0.3',
                    'destination': '12.0.0.0/24'}],
        'cidr': '192.168.0.0/28',
        'id': 'bab31ffb-07e1-42e9-a2b0-776efbf10f4a',
        'subnetpool_id': None,
        'name': 'ptg_tscp_1_vpn-provider'
    }

    service_details = {
        'service_details': _service_details,
        'provider_subnet': _subnet,
        'consumer_subnet': _subnet,
    }
    fip = [{'floating_ip_address': '192.168.102.118',
            'port_id': 'af6a8a58-1e25-49c4-97a3-d5f50b3aa04b'}]
    mgmt_ip = '11.3.4.5'
    l2p = {
           'l3_policy_id': '760d1763-9111-410a-a03e-61623afd7b25'
    }

    l3p = {
        'routers': ['64803e64-7db7-4050-a343-cbafbd2d356a']
           }
    services_nsp = [{'id': '479982d1-7947-478f-bf6c-dc234f38677d'}]
    stitching_pts = [{
            'policy_target_group_id': '6fa92b57-69ee-4143-9cf9-fcef0d067e65'
                }]
