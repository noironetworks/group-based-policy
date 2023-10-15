# Copyright (c) 2018 Cisco Systems Inc.
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

import sys

from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import (
    mechanism_driver as md)
from oslo_config import cfg
from neutron.common import config
from neutron import manager


def main():
    config.init(sys.argv[1:])

    # Enable logging but prevent output to stderr.
    cfg.CONF.use_stderr = False
    config.setup_logging()

    if not cfg.CONF.config_file:
        sys.exit(_("ERROR: Unable to find configuration file via the default"
                   " search paths (~/.neutron/, ~/, /etc/neutron/, /etc/) and"
                   " the '--config-file' option!"))

    manager.init()

    mech = md.ApicMechanismDriver()
    result = mech.normalize_hpp()

    if not result:
        sys.exit(_("ERROR: APIC version doesn't support"
                   " normalization of remote ips"))
    return 0
