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

from gbpservice.neutron.plugins.ml2plus import extension_overrides


# Monkeypatch Neutron to allow overriding its own extension
# descriptors. Note that extension descriptor classes cannot be
# monkeypatched directly because they are loaded explicitly by file
# name and then used immediately.
_real_get_extensions_path = extensions.get_extensions_path


def get_extensions_path(service_plugins=None):
    path = _real_get_extensions_path(service_plugins)
    return extension_overrides.__path__[0] + ':' + path


extensions.get_extensions_path = get_extensions_path


from gbpservice.common import utils as gbp_utils


from neutron_lib import context as nlib_ctx


orig_get_admin_context = nlib_ctx.get_admin_context


def new_get_admin_context():
    current_context = gbp_utils.get_current_context()
    if not current_context:
        return orig_get_admin_context()
    else:
        return current_context.elevated()


nlib_ctx.get_admin_context = new_get_admin_context


from oslo_db.sqlalchemy import exc_filters


exc_filters.LOG.exception = exc_filters.LOG.debug
