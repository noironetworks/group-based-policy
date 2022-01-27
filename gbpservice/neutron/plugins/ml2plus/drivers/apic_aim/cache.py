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

from gbpclient.v2_0 import client as gbp_client
from keystoneclient import auth as ksc_auth
from keystoneclient import session as ksc_session
from keystoneclient.v3 import client as ksc_client
from neutron_lib.plugins import directory
from neutronclient.neutron.v2_0 import purge
from neutronclient.v2_0 import client as neutron_client
from oslo_config import cfg
from oslo_log import log as logging


LOG = logging.getLogger(__name__)

# REVISIT(rkukura): We use keystone to get the name of the keystone
# project owning each neutron resource, which by default, requires
# admin. If we keep this, we should probably move it to a separate
# config module. But we should also investigate whether admin is even
# needed, or if neutron's credentials could somehow be used.
AUTH_GROUP = 'apic_aim_auth'
ksc_session.Session.register_conf_options(cfg.CONF, AUTH_GROUP)
ksc_auth.register_conf_options(cfg.CONF, AUTH_GROUP)


class ProjectDetailsCache(object):
    """Cache of Keystone project ID to project details mappings."""

    def __init__(self):
        self.project_details = {}
        self.keystone = None
        self.neutron = None
        self._gbp_plugin = None
        self._gbp_driver = None
        self.gbp = None
        self.enable_neutronclient_internal_ep_interface = (
            cfg.CONF.ml2_apic_aim.enable_neutronclient_internal_ep_interface)

    def _get_keystone_client(self):
        # REVISIT: It seems load_from_conf_options() and
        # keystoneclient auth plugins have been deprecated, and we
        # should use keystoneauth instead.
        LOG.debug("Getting keystone client")
        auth = ksc_auth.load_from_conf_options(cfg.CONF, AUTH_GROUP)
        LOG.debug("Got auth: %s", auth)
        if not auth:
            LOG.warning('No auth_plugin configured in %s',
                        AUTH_GROUP)
        session = ksc_session.Session.load_from_conf_options(
            cfg.CONF, AUTH_GROUP, auth=auth)
        LOG.debug("Got session: %s", session)
        self.keystone = ksc_client.Client(session=session)
        LOG.debug("Got keystone client: %s", self.keystone)
        endpoint_type = 'publicURL'
        if self.enable_neutronclient_internal_ep_interface:
            endpoint_type = 'internalURL'
        if self.gbp_driver:
            self.gbp = gbp_client.Client(session=session,
                                     endpoint_type=endpoint_type)
            LOG.debug("Got gbp client: %s", self.gbp)
        else:
            self.neutron = neutron_client.Client(session=session,
                                     endpoint_type=endpoint_type)

    @property
    def gbp_plugin(self):
        if not self._gbp_plugin:
            self._gbp_plugin = directory.get_plugin("GROUP_POLICY")
        return self._gbp_plugin

    @property
    def gbp_driver(self):
        if not self._gbp_driver and self.gbp_plugin:
            self._gbp_driver = (self.gbp_plugin.policy_driver_manager.
                                policy_drivers['aim_mapping'].obj)
        return self._gbp_driver

    def ensure_project(self, project_id):
        """Ensure cache contains mapping for project.

        :param project_id: ID of the project

        Ensure that the cache contains a mapping for the project
        identified by project_id. If it is not, Keystone will be
        queried for the current list of projects, and any new mappings
        will be added to the cache. This method should never be called
        inside a transaction with a project_id not already in the
        cache.
        """
        if project_id and project_id not in self.project_details:
            self.load_projects()

    def load_projects(self):
        if self.keystone is None:
            self._get_keystone_client()
        LOG.debug("Calling project API")
        projects = self.keystone.projects.list()
        LOG.debug("Received projects: %s", projects)
        for project in projects:
            self.project_details[project.id] = (project.name,
                project.description)

    def get_project_details(self, project_id):
        """Get name and descr of project from cache.

        :param project_id: ID of the project

        If the cache contains project_id, a tuple with
        project name and description is returned
        else a tuple (None,None) is returned
        """
        if self.project_details.get(project_id):
            return self.project_details[project_id]
        return ('', '')

    def update_project_details(self, project_id):
        """Update project name and description from keystone.

        :param project_id: ID of the project

        Get the name and description of the project identified by
        project_id from the keystone. If the value in cache doesn't
        match values in keystone, update the cache and return 1,
        to indicate that cache has been updated
        """
        if self.keystone is None:
            self._get_keystone_client()
        if self.keystone:
            project = self.keystone.projects.get(project_id)
            if project:
                prj_details = self.get_project_details(project_id)
                if (prj_details[0] != project.name or
                    prj_details[1] != project.description):
                    self.project_details[project_id] = (
                        project.name, project.description)
                    LOG.debug("Project updated %s ",
                              str(self.project_details[project_id]))
                    return self.project_details[project_id]
        return None

    def purge_gbp(self, project_id):
        class TempArg(object):
            pass

        if self.gbp is None:
            self._get_keystone_client()
        LOG.debug("Calling purge() API")
        temp_arg = TempArg()
        temp_arg.tenant = project_id
        if self.gbp:
            self.gbp.purge(temp_arg)
        else:
            neutron_purge = PurgeAPI(None, None, self.neutron)
            neutron_purge.take_action(temp_arg)


class PurgeAPI(purge.Purge):
    def __init__(self, app, app_args, neutron_client):
        self.neutron_client = neutron_client
        super(PurgeAPI, self).__init__(app, app_args)

    def get_client(self):
        return self.neutron_client
