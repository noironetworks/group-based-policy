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

import threading

from eventlet import greenpool
from neutron_lib.db import model_base
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import constants as pconst
from oslo_config import cfg
from oslo_log import log as logging
import sqlalchemy as sa

from gbpservice._i18n import _
from gbpservice.neutron.services.servicechain.plugins.ncp import (
    exceptions as exc)
from gbpservice.nfp.common import constants as nfp_constants


NFP_NODE_DRIVER_OPTS = [
    cfg.BoolOpt('is_service_admin_owned',
                help=_("Parameter to indicate whether the Service VM has to "
                       "be owned by the Admin"),
                default=False),
    cfg.IntOpt('service_create_timeout',
               default=nfp_constants.SERVICE_CREATE_TIMEOUT,
               help=_("Seconds to wait for service creation "
                      "to complete")),
    cfg.IntOpt('service_delete_timeout',
               default=nfp_constants.SERVICE_DELETE_TIMEOUT,
               help=_("Seconds to wait for service deletion "
                      "to complete")),
]
# REVISIT(ashu): Can we use is_service_admin_owned config from RMD
cfg.CONF.register_opts(NFP_NODE_DRIVER_OPTS, "nfp_node_driver")


LOG = logging.getLogger(__name__)

# REVISIT: L2 insertion not supported
GATEWAY_PLUMBER_TYPE = [pconst.FIREWALL, pconst.VPN]
nfp_context_store = threading.local()


class InvalidServiceType(exc.NodeCompositionPluginBadRequest):
    message = _("The NFP Node driver only supports the services "
                "VPN, Firewall and LB in a Service Chain")


class ServiceProfileRequired(exc.NodeCompositionPluginBadRequest):
    message = _("A Service profile is required in Service node")


class NodeVendorMismatch(exc.NodeCompositionPluginBadRequest):
    message = _("The NFP Node driver only handles nodes which have service "
                "profile with vendor name %(vendor)s")


class DuplicateServiceTypeInChain(exc.NodeCompositionPluginBadRequest):
    message = _("The NFP Node driver does not support duplicate "
                "service types in same chain")


class RequiredProfileAttributesNotSet(exc.NodeCompositionPluginBadRequest):
    message = _("The required attributes in service profile are not present")


class InvalidNodeOrderInChain(exc.NodeCompositionPluginBadRequest):
    message = _("The NFP Node driver does not support the order "
                "of nodes defined in the current service chain spec, "
                "order should be : %(node_order)s")


class UnSupportedServiceProfile(exc.NodeCompositionPluginBadRequest):
    message = _("The NFP Node driver does not support this service "
                "profile with service type %(service_type)s and vendor "
                "%(vendor)s")


class UnSupportedInsertionMode(exc.NodeCompositionPluginBadRequest):
    message = _("The NFP Node driver supports only L3 Insertion "
                "mode")


class ServiceInfoNotAvailableOnUpdate(n_exc.NeutronException):
    message = _("Service information is not available with Service Manager "
                "on node update")


class VipNspNotSetonProvider(n_exc.NeutronException):
    message = _("Network Service policy for VIP IP address is not configured "
                "on the Providing Group")


class NodeInstanceDeleteFailed(n_exc.NeutronException):
    message = _("Node instance delete failed in NFP Node driver")


class NodeInstanceCreateFailed(n_exc.NeutronException):
    message = _("Node instance create failed in NFP Node driver")


class NodeInstanceUpdateFailed(n_exc.NeutronException):
    message = _("Node instance update failed in NFP Node driver")


class OperationNotSupported(exc.NodeCompositionPluginBadRequest):
    message = _("The NFP Node driver doesn't support operation, "
                "if instance status is in BUILD state.")


class ServiceNodeInstanceNetworkFunctionMapping(model_base.BASEV2):
    """ServiceChainInstance to NFP network function mapping."""

    __tablename__ = 'ncp_node_instance_network_function_mappings'
    sc_instance_id = sa.Column(sa.String(36),
                               nullable=False, primary_key=True)
    sc_node_id = sa.Column(sa.String(36),
                           nullable=False, primary_key=True)
    network_function_id = sa.Column(sa.String(36),
                                    nullable=True)
    status = sa.Column(sa.String(20), nullable=True)
    status_details = sa.Column(sa.String(4096), nullable=True)


class NFPContext(object):

    @staticmethod
    def store_nfp_context(sc_instance_id, **context):
        if not hasattr(nfp_context_store, 'context'):
            nfp_context_store.context = {}

        # Considering each store request comes with one entry
        if not nfp_context_store.context.get(sc_instance_id):
            NFPContext._initialise_attr(sc_instance_id)
        nfp_context_store.context[sc_instance_id].update(context)

    @staticmethod
    def clear_nfp_context(sc_instance_id):
        if not hasattr(nfp_context_store, 'context'):
            return
        if nfp_context_store.context.get(sc_instance_id):
            del nfp_context_store.context[sc_instance_id]

    @staticmethod
    def get_nfp_context(sc_instance_id):
        if not hasattr(nfp_context_store, 'context'):
            return {}
        if nfp_context_store.context.get(sc_instance_id):
            return nfp_context_store.context[sc_instance_id]
        return {}

    @staticmethod
    def _initialise_attr(sc_instance_id):
        context = {'thread_pool': greenpool.GreenPool(10),
                   'active_threads': [],
                   'sc_node_count': 0,
                   'sc_gateway_type_nodes': [],
                   'network_functions': [],
                   'update': False}
        if nfp_context_store.context:
            nfp_context_store.context.update({sc_instance_id: context})
        else:
            nfp_context_store.context = {sc_instance_id: context}
