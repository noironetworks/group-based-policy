# Copyright (c) 2017 Cisco Systems Inc.
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

from contextlib import contextmanager
import copy

from aim.aim_lib.db import model as aim_lib_model
from aim import aim_store
from aim.api import resource as aim_resource
from aim import context as aim_context
from neutron_lib import context
from neutron_lib.plugins import directory
from oslo_log import log

from gbpservice.neutron.db import api as db_api
from gbpservice.neutron.plugins.ml2plus.drivers.apic_aim import db
from gbpservice.neutron.services.grouppolicy import (
    group_policy_driver_api as api)

LOG = log.getLogger(__name__)

COMMON_TENANT_NAME = 'common'


class InternalValidationError(Exception):
    pass


class RollbackTransaction(Exception):
    pass


class IncorrectResourceError(Exception):
    def __init(self, message):
        self.message = message
        super().__init__(self.message)


class IncorrectTenantError(Exception):
    def __init(self, message):
        self.message = message
        super().__init__(self.message)


class ValidationManager(object):

    def __init__(self):
        # REVISIT: Defer until after validating config? Or pass in PD
        # & MD?
        self.core_plugin = directory.get_plugin()
        self.md = self.core_plugin.mechanism_manager.mech_drivers[
            'apic_aim'].obj
        self.pd = self.md.gbp_driver
        self.sfcd = None
        sfc_plugin = directory.get_plugin('sfc')
        if sfc_plugin:
            driver = sfc_plugin.driver_manager.drivers.get('aim')
            if driver:
                self.sfcd = driver.obj

    def validate(self, repair=False, resources=None, tenants=None):
        self.output("Validating deployment, repair: %s" % repair)
        self.result = api.VALIDATION_PASSED
        self.repair = repair
        self.neutron_resources = resources if resources else []
        self.tenants = set(tenants) if tenants else set()
        self.resource_scope = True if self.neutron_resources else False
        self.tenant_scope = True if self.tenants else False

        self.neutron_to_aim_mapping = {
            "router": [
                aim_resource.Contract,
                aim_resource.ContractSubject,
                aim_resource.Tenant,
                aim_resource.ApplicationProfile],
            "security_group": [
                aim_resource.SecurityGroup,
                aim_resource.SecurityGroupRule,
                aim_resource.SecurityGroupSubject,
                aim_resource.Tenant,
                aim_resource.ApplicationProfile],
            "network": [
                aim_resource.BridgeDomain,
                aim_resource.ApplicationProfile,
                aim_resource.EndpointGroup,
                aim_resource.ExternalNetwork,
                aim_resource.ExternalSubnet,
                db.NetworkMapping,
                db.AddressScopeMapping,
                aim_resource.L3Outside,
                aim_resource.Subnet,
                aim_resource.EPGSubnet,
                aim_resource.VRF,
                aim_resource.Tenant,
                aim_resource.ApplicationProfile,
                aim_lib_model.CloneL3Out],
            "port": [
                aim_resource.InfraAccBundleGroup,
                aim_resource.SpanVsourceGroup,
                aim_resource.SpanVdestGroup,
                aim_resource.SpanVsource,
                aim_resource.SpanVdest,
                aim_resource.SpanVepgSummary,
                aim_resource.SpanSpanlbl,
                aim_resource.Tenant,
                aim_resource.ApplicationProfile],
            "subnetpool": [
                aim_resource.Tenant,
                aim_resource.ApplicationProfile],
            "floatingip": [
                aim_resource.Tenant,
                aim_resource.ApplicationProfile],
            "address_scope": [
                aim_resource.VRF,
                db.AddressScopeMapping,
                aim_resource.Tenant,
                aim_resource.ApplicationProfile],
                                   }

        # REVISIT: Validate configuration.
        # Load project names from Keystone.
        self.md.project_details_cache.load_projects()

        # Start transaction.
        #
        # REVISIT: Set session's isolation level to serializable?
        self.actual_context = context.get_admin_context()
        try:
            with db_api.CONTEXT_WRITER.using(
                    self.actual_context) as session:
                self.actual_session = session
                self.aim_mgr = self.md.aim
                self.actual_aim_ctx = aim_context.AimContext(session)
                self.expected_session = ValidationSession(self)
                self.expected_aim_ctx = aim_context.AimContext(
                    None, ValidationAimStore(self))

                self.validate_scope_arguments()

                # Validate & repair GBP->Neutron mappings.
                if self.pd:
                    self.pd.validate_neutron_mapping(self)

                # Start with no expected or actual AIM resources or DB
                # records.
                self._expected_aim_resources = {}
                self._actual_aim_resources = {}
                self._expected_db_instances = {}
                self._db_instance_primary_keys = {}

                # Validate Neutron->AIM mapping, getting expected AIM
                # resources and DB records.
                self.md.validate_aim_mapping(self)

                # Validate GBP->AIM mapping, getting expected AIM
                # resources and DB records.
                if self.pd:
                    self.pd.validate_aim_mapping(self)

                # Validate SFC->AIM mapping, getting expected AIM
                # resources and DB records.
                if self.sfcd:
                    self.sfcd.validate_aim_mapping(self)

                # Validate that actual AIM resources match expected
                # AIM resources.
                self._validate_aim_resources()

                # Validate that actual DB instances match expected DB
                # instances.
                self._validate_db_instances()

                # Commit or rollback transaction.
                if self.result is api.VALIDATION_REPAIRED:
                    self.output("Committing repairs")
                else:
                    if (self.repair and
                        self.result is api.VALIDATION_FAILED_UNREPAIRABLE):
                        self.output("Rolling back attempted repairs")
                    raise RollbackTransaction()
        except RollbackTransaction:
            pass
        except Exception as exc:
            self.output("Validation failed with exception: %s - see log for "
                        "details" % exc)
            LOG.exception(exc)
            return api.VALIDATION_FAILED_WITH_EXCEPTION

        # Bind unbound ports outside transaction.
        if (self.repair and
            self.result is not api.VALIDATION_FAILED_UNREPAIRABLE):
            self.output("Binding unbound ports")
            self.md.bind_unbound_ports(self)

        self.output("Validation result: %s" % self.result)
        return self.result

    def output(self, msg):
        LOG.info(msg)
        print(msg)

    def validate_scope_arguments(self):
        if self.neutron_resources:
            for resource in self.neutron_resources:
                if resource not in list(self.neutron_to_aim_mapping.keys()):
                    err_msg = ("Incorrect resource in the argument: " +
                            str(self.neutron_resources))
                    raise IncorrectResourceError(err_msg)

        self.tenant_ids = set()
        aim_tenant_list = set()

        project_dict = self.md.project_details_cache.project_details
        for project_id in list(project_dict.keys()):
            tenant_name = project_dict[project_id][0]
            if tenant_name in self.tenants:
                self.tenant_ids.add(project_id)
                tenant_aname = self.md.name_mapper.project(self.actual_session,
                    project_id)
                aim_tenant_list.add(tenant_aname)
                self.tenants.remove(tenant_name)

        if len(self.tenants):
            err_msg = ("Incorrect tenant in the argument: " +
                    str(self.tenants))
            raise IncorrectTenantError(err_msg)

        self.tenants = aim_tenant_list

        self.aim_resources = set()
        for resource in self.neutron_resources:
            self.aim_resources.update(
                    self.neutron_to_aim_mapping[resource])

    def register_aim_resource_class(self, resource_class):
        if resource_class not in self._expected_aim_resources:
            self._expected_aim_resources[resource_class] = {}
            self._actual_aim_resources[resource_class] = {
                tuple(resource.identity): resource
                for resource in self.aim_mgr.find(
                    self.actual_aim_ctx, resource_class)
                if self._should_register_resource(resource)}

    def expect_aim_resource(self, resource, replace=False, remove=False):
        expected_resources = self._expected_aim_resources[resource.__class__]
        key = tuple(resource.identity)
        if remove:
            del expected_resources[key]
            return
        elif not replace and key in expected_resources:
            self.output("resource %s already expected" % resource)
            raise InternalValidationError()
        for attr_name, attr_type in list(resource.other_attributes.items()):
            attr_type_type = attr_type['type']
            if attr_type_type == 'string':
                value = getattr(resource, attr_name)
                setattr(resource, attr_name,
                        str(value) if value else value)
            elif (attr_type_type == 'array' and
                attr_type['items']['type'] == 'string'):
                # REVISIT: May also need to dedup arrays of types
                # other than string.
                value = list(set(getattr(resource, attr_name)))
                setattr(resource, attr_name, value)
        expected_resources[key] = resource

    def expected_aim_resource(self, resource):
        expected_resources = self._expected_aim_resources[resource.__class__]
        key = tuple(resource.identity)
        return expected_resources.get(key)

    def expected_aim_resources(self, resource_class):
        return list(self._expected_aim_resources[resource_class].values())

    def actual_aim_resource(self, resource):
        actual_resources = self._actual_aim_resources[resource.__class__]
        key = tuple(resource.identity)
        return actual_resources.get(key)

    def actual_aim_resources(self, resource_class):
        return list(self._actual_aim_resources[resource_class].values())

    def register_db_instance_class(self, instance_class, primary_keys):
        if self.aim_resources and instance_class not in self.aim_resources:
            return
        self._expected_db_instances.setdefault(instance_class, {})
        self._db_instance_primary_keys[instance_class] = primary_keys

    def expect_db_instance(self, instance):
        instance_class = instance.__class__
        expected_instances = self._expected_db_instances[instance_class]
        primary_keys = self._db_instance_primary_keys[instance_class]
        key = tuple([getattr(instance, k) for k in primary_keys])
        expected_instances[key] = instance

    def query_db_instances(self, entities, args, filters):
        assert 1 == len(entities)
        assert 0 == len(args)
        instance_class = entities[0]
        expected_instances = self._expected_db_instances[instance_class]
        primary_keys = self._db_instance_primary_keys[instance_class]
        if filters:
            if (set(filters.keys()) == set(primary_keys)):
                key = tuple([filters[k] for k in primary_keys])
                instance = expected_instances.get(key)
                return [instance] if instance else []
            else:
                return [i for i in list(expected_instances.values())
                        if all([getattr(i, k) == v for k, v in
                                list(filters.items())])]
        else:
            return list(expected_instances.values())

    def should_repair(self, problem, action='Repairing'):
        if self.result is not api.VALIDATION_FAILED_UNREPAIRABLE:
            self.output("%s %s" % (action, problem))
            self.result = (api.VALIDATION_REPAIRED if self.repair
                           else api.VALIDATION_FAILED_REPAIRABLE)
            return True

    def validation_failed(self, reason):
        # REVISIT: Do we need drivers to be able to specify repairable
        # vs unrepairable? If so, add a keyword arg and make sure
        # VALIDATION_FAILED_REPAIRABLE does not overwrite
        # VALIDATION_FAILED_UNREPAIRABLE.
        self.output("Failed UNREPAIRABLE due to %s" % reason)
        self.result = api.VALIDATION_FAILED_UNREPAIRABLE

    def bind_ports_failed(self, message):
        self.output(message)
        self.result = api.VALIDATION_FAILED_BINDING_PORTS

    def _validate_aim_resources(self):
        for resource_class in list(self._expected_aim_resources.keys()):
            self._validate_aim_resource_class(resource_class)

    def _should_validate_neutron_resource(self, resource):
        if self.neutron_resources:
            return True if resource in self.neutron_resources else False
        else:
            return True

    def _should_handle_missing_resource(self, resource):
        if self.tenant_scope:
            if resource.__class__ == aim_resource.Tenant:
                if resource.name == COMMON_TENANT_NAME:
                    return True
                return True if resource.name in self.tenants else False

            if not hasattr(resource, 'tenant_name'):
                return True
            if not resource.tenant_name:
                return True
            if resource.tenant_name == COMMON_TENANT_NAME:
                return True
            return True if resource.tenant_name in self.tenants else False
        return True

    def _validate_aim_resource_class(self, resource_class):
        expected_resources = self._expected_aim_resources[resource_class]
        for actual_resource in self.actual_aim_resources(resource_class):
            key = tuple(actual_resource.identity)
            expected_resource = expected_resources.pop(key, None)
            self._validate_actual_aim_resource(
                actual_resource, expected_resource)

        for expected_resource in list(expected_resources.values()):
            if self._should_handle_missing_resource(expected_resource):
                self._handle_missing_aim_resource(expected_resource)

    def _should_validate_unexpected_resource(self, resource):
        resource_class = resource.__class__
        if self.tenant_scope or self.resource_scope:
            if (resource_class == aim_resource.Tenant or
                resource_class == aim_resource.ApplicationProfile):
                return False
            if (resource_class == aim_resource.VRF and
                resource.name == "DefaultVRF"):
                return False
            if not hasattr(resource, 'tenant_name'):
                return False
            if not resource.tenant_name:
                return True
            if resource.tenant_name == COMMON_TENANT_NAME:
                return False
        if self.tenant_scope:
            return True if resource.tenant_name in self.tenants else False
        return True

    def _should_validate_unexpected_db_instance(self, instance):
        instance_class = instance.__class__
        if self.tenant_scope:
            instance_tenant = None
            if instance_class == aim_lib_model.CloneL3Out:
                instance_tenant = instance.tenant_name
            elif instance_class == db.AddressScopeMapping:
                instance_tenant = instance.vrf_tenant_name
            elif instance_class == db.NetworkMapping:
                if instance.l3out_tenant_name:
                    instance_tenant = instance.l3out_tenant_name
                else:
                    instance_tenant = instance.epg_tenant_name
            if instance_tenant:
                return True if instance_tenant in self.tenants else False
            else:
                return True
        return True

    def _validate_actual_aim_resource(self, actual_resource,
                                      expected_resource):
        if not expected_resource:
            # Some infra resources do not have the monitored
            # attribute, but are treated as if they are monitored.
            # During resource scoped run of validation routine
            # it will report tenant and ap of some other resource
            # as unexpected, although they may be expected for other
            # resource. We can only know for sure from  unscoped validationn
            # or from tenant scoped validation of reported tenant/ap.
            # Therefore ignore them during recource scoped run.
            if (not self._should_validate_unexpected_resource(
                actual_resource)):
                return
            if not getattr(actual_resource, 'monitored', True):
                self._handle_unexpected_aim_resource(actual_resource)
        else:
            # Update both monitored and unmonitored
            # resources. Monitored resources should have started out
            # as copies of actual resources, but may have had changes
            # made, such as additional contracts provided or consumed.
            if not expected_resource.user_equal(actual_resource):
                self._handle_incorrect_aim_resource(
                    expected_resource, actual_resource)

    def _should_validate_tenant(self, tenant):
        if tenant == COMMON_TENANT_NAME:
            return True
        if self.tenants:
            return True if tenant in self.tenants else False
        return True

    def _should_register_resource(self, resource):
        resource_class = resource.__class__
        if (resource_class == aim_resource.Tenant and
            self.tenants and resource.name != COMMON_TENANT_NAME and
            resource.name not in self.tenants):
            return False
        if (not hasattr(resource, 'tenant_name') or
            resource.tenant_name == COMMON_TENANT_NAME):
            return True
        if (self.aim_resources and
            resource_class not in self.aim_resources):
            return False
        if self.tenants:
            return True if resource.tenant_name in self.tenants else False
        return True

    def _handle_unexpected_aim_resource(self, actual_resource):
        if self.should_repair(
                "unexpected %(type)s: %(actual)r" %
                {'type': actual_resource._aci_mo_name,
                 'actual': actual_resource},
                "Deleting"):
            self.aim_mgr.delete(self.actual_aim_ctx, actual_resource)

    def _handle_incorrect_aim_resource(self, expected_resource,
                                       actual_resource):
        if self.should_repair(
                "incorrect %(type)s: %(actual)r which should be: "
                "%(expected)r" %
                {'type': expected_resource._aci_mo_name,
                 'actual': actual_resource,
                 'expected': expected_resource}):
            self.aim_mgr.create(
                self.actual_aim_ctx, expected_resource, overwrite=True)

    def _handle_missing_aim_resource(self, expected_resource):
        if self.should_repair(
                "missing %(type)s: %(expected)r" %
                {'type': expected_resource._aci_mo_name,
                 'expected': expected_resource}):
            self.aim_mgr.create(self.actual_aim_ctx, expected_resource)

    def _validate_db_instances(self):
        for db_class in list(self._expected_db_instances.keys()):
            self._validate_db_instance_class(db_class)

    def _validate_db_instance_class(self, db_class):
        expected_instances = self._expected_db_instances[db_class]
        actual_instances = self.actual_session.query(db_class).all()

        for actual_instance in actual_instances:
            self._validate_actual_db_instance(
                actual_instance, expected_instances)

        for expected_instance in list(expected_instances.values()):
            self._handle_missing_db_instance(expected_instance)

    def _validate_actual_db_instance(self, actual_instance,
                                     expected_instances):
        primary_keys = self._db_instance_primary_keys[
            actual_instance.__class__]
        key = tuple([getattr(actual_instance, k) for k in primary_keys])
        expected_instance = expected_instances.pop(key, None)
        if not expected_instance:
            if (not self._should_validate_unexpected_db_instance(
                actual_instance)):
                return
            self._handle_unexpected_db_instance(actual_instance)
        else:
            if not self._is_db_instance_correct(
                    expected_instance, actual_instance):
                self._handle_incorrect_db_instance(
                    expected_instance, actual_instance)

    def _is_db_instance_correct(self, expected_instance, actual_instance):
        expected_values = expected_instance.__dict__
        actual_values = actual_instance.__dict__
        return all([v == actual_values[k]
                    for k, v in list(expected_values.items())
                    if not k.startswith('_')])

    def _handle_unexpected_db_instance(self, actual_instance):
        if self.should_repair(
                "unexpected %(type)s record: %(actual)s" %
                {'type': actual_instance.__tablename__,
                 'actual': actual_instance.__dict__},
                "Deleting"):
            self.actual_session.delete(actual_instance)

    def _handle_incorrect_db_instance(self, expected_instance,
                                      actual_instance):
        if self.should_repair(
                "incorrect %(type)s record: %(actual)s which should be: "
                "%(expected)s" %
                {'type': expected_instance.__tablename__,
                 'actual': actual_instance.__dict__,
                 'expected': expected_instance.__dict__}):
            self.actual_session.merge(expected_instance)

    def _handle_missing_db_instance(self, expected_instance):
        if self.should_repair(
                "missing %(type)s record: %(expected)s" %
                {'type': expected_instance.__tablename__,
                 'expected': expected_instance.__dict__}):
            self.actual_session.add(expected_instance)


class ValidationAimStore(aim_store.AimStore):

    def __init__(self, validation_mgr):
        self._mgr = validation_mgr
        self.db_session = validation_mgr.expected_session

    def add(self, db_obj):
        self._mgr.expect_aim_resource(db_obj, True)

    def delete(self, db_obj):
        self._mgr.expect_aim_resource(db_obj, remove=True)

    def query(self, db_obj_type, resource_class, in_=None, notin_=None,
              order_by=None, lock_update=False, **filters):
        assert in_ is None
        assert notin_ is None
        assert order_by is None
        if filters:
            if (set(filters.keys()) ==
                set(resource_class.identity_attributes.keys())):
                identity = resource_class(**filters)
                resource = self._mgr.expected_aim_resource(identity)
                return [resource] if resource else []
            else:
                return [r for r in
                        self._mgr.expected_aim_resources(resource_class)
                        if all([getattr(r, k) == v for k, v in
                                list(filters.items())])]
        else:
            return self._mgr.expected_aim_resources(resource_class)

    def count(self, db_obj_type, resource_class, in_=None, notin_=None,
              **filters):
        assert False

    def delete_all(self, db_obj_type, resource_class, in_=None, notin_=None,
                   **filters):
        assert False

    def from_attr(self, db_obj, resource_class, attribute_dict):
        for k, v in list(attribute_dict.items()):
            setattr(db_obj, k, v)

    def to_attr(self, resource_class, db_obj):
        assert False

    def make_resource(self, cls, db_obj, include_aim_id=False):
        return copy.deepcopy(db_obj)

    def make_db_obj(self, resource):
        result = copy.deepcopy(resource)
        if isinstance(result, aim_resource.EndpointGroup):
            # Since aim.db.models.EndpointGroup.to_attr() maintains
            # openstack_vmm_domain_names for backward compatibility,
            # we do so here.
            result.openstack_vmm_domain_names = [d['name'] for d in
                                                 result.vmm_domains
                                                 if d['type'] == 'OpenStack']
        return result


@contextmanager
def _begin():
    yield


class ValidationSession(object):
    # This is a very minimal implementation of a sqlalchemy DB session
    # (and query), providing only the functionality needed to simulate
    # and validate DB usage buried within library code that cannot be
    # otherwise validated. If more functionality is needed, consider
    # using a sqlite-backed sqlalchemy session instead.

    def __init__(self, validation_mgr):
        self._mgr = validation_mgr

    def begin(self, subtransactions=False, nested=False):
        return _begin()

    def add(self, instance):
        self._mgr.expect_db_instance(instance)

    def query(self, *entities, **kwargs):
        return ValidationQuery(self._mgr, entities, kwargs)


class ValidationQuery(object):

    def __init__(self, validation_mgr, entities, args):
        self._mgr = validation_mgr
        self._entities = entities
        self._args = args
        self._filters = {}

    def filter_by(self, **kwargs):
        self._filters.update(kwargs)
        return self

    def all(self):
        return self._mgr.query_db_instances(
            self._entities, self._args, self._filters)
