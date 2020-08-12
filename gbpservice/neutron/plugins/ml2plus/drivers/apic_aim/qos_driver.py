# Copyright (c) 2020 Cisco Systems Inc.
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

from neutron_lib.api.definitions import portbindings
from neutron_lib import constants
from neutron_lib.db import constants as db_consts
from neutron_lib.services.qos import base
from neutron_lib.services.qos import constants as qos_consts
from oslo_config import cfg
from oslo_log import log as logging

LOG = logging.getLogger(__name__)

SUPPORTED_RULES = {
    qos_consts.RULE_TYPE_BANDWIDTH_LIMIT: {
        qos_consts.MAX_KBPS: {
            'type:range': [0, db_consts.DB_INTEGER_MAX_VALUE]},
        qos_consts.MAX_BURST: {
            'type:range': [0, db_consts.DB_INTEGER_MAX_VALUE]},
        qos_consts.DIRECTION: {
            'type:values': constants.VALID_DIRECTIONS},
    },
    qos_consts.RULE_TYPE_DSCP_MARKING: {
        qos_consts.DSCP_MARK: {'type:values': constants.VALID_DSCP_MARKS},
    }
}

VIF_TYPES = [portbindings.VIF_TYPE_OVS, portbindings.VIF_TYPE_VHOST_USER]
VNIC_TYPES = [portbindings.VNIC_NORMAL]

DRIVER = None


class ACIQosDriver(base.DriverBase):

    @classmethod
    def create(cls, plugin_driver):
        obj = ACIQosDriver(name='ACIQosDriver',
                           vif_types=VIF_TYPES,
                           vnic_types=VNIC_TYPES,
                           supported_rules=SUPPORTED_RULES,
                           requires_rpc_notifications=False)
        obj._driver = plugin_driver
        return obj

    @property
    def is_loaded(self):
        return 'qos' in cfg.CONF.ml2.extension_drivers

    def create_policy_precommit(self, context, policy):
        self._driver.create_qos_policy_precommit(context, policy)

    def update_policy_precommit(self, context, policy):
        self._driver.update_qos_policy_precommit(context, policy)

    def delete_policy_precommit(self, context, policy):
        self._driver.delete_qos_policy_precommit(context, policy)


def register(plugin_driver):
    """Register the driver."""
    global DRIVER
    if not DRIVER:
        DRIVER = ACIQosDriver.create(plugin_driver)
    LOG.debug('ACI QoS driver registered')
    return DRIVER
