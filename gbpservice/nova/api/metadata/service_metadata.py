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

from nova.api.metadata import base
from nova import context
from nova import exception
from nova import objects
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)

SCREENED_VALUES = ['uuid', 'host', 'display_name', 'display_description',
                   'hostname']


class ServiceVendorData(base.VendorDataDriver):
    def __init__(self, *args, **kwargs):
        super(ServiceVendorData, self).__init__(*args, **kwargs)
        self._instance = kwargs['instance']

    def get(self):
        ctxt = context.get_admin_context()
        vms = objects.InstanceList.get_by_filters(
            ctxt, filters={'project_id': self._instance['project_id'],
                           'deleted': False})
        result = [{k: v for k, v in x.iteritems() if k in SCREENED_VALUES}
                  for x in vms]
        for item in result:
            try:
                net_info = objects.InstanceInfoCache.get_by_instance_uuid(
                    ctxt, item['uuid'])['network_info']
                item['fixed_ips'] = [
                    ips.get('address') for info in net_info for subnets in
                    info.get('network', {}).get('subnets', [])
                    for ips in subnets.get('ips', [])]
            except exception.InstanceInfoCacheNotFound:
                item['fixed_ips'] = []
        LOG.debug("My Vendor Metadata:\n%s" % str(result))
        return result