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

from neutron_lib import exceptions

from gbpservice._i18n import _


class InternalError(exceptions.NeutronException):
    message = _("Internal mechanism driver error - %(details)s.")

    def __init__(self, **kwargs):
        kwargs.setdefault('details', _("See error log for details"))
        super(InternalError, self).__init__(**kwargs)


class UnsupportedRoutingTopology(exceptions.BadRequest):
    message = _("All router interfaces for a network must share either the "
                "same router or the same subnet.")


class UnscopedSharedNetworkProjectConflict(exceptions.BadRequest):
    message = _("Shared network %(net1)s from project %(proj1)s and shared "
                "network %(net2)s from project %(proj2)s cannot be combined "
                "in the same topology.")


class NonIsomorphicNetworkRoutingUnsupported(exceptions.BadRequest):
    message = _("All router interfaces for a network must utilize the same "
                "VRF.")


class ScopeUpdateNotSupported(exceptions.BadRequest):
    message = _("Updating the address_scope of a subnetpool that is "
                "associated with routers is not currently supported.")


class SnatPortsInUse(exceptions.SubnetInUse):
    def __init__(self, **kwargs):
        kwargs['reason'] = _('Subnet has SNAT IP addresses allocated')
        super(SnatPortsInUse, self).__init__(**kwargs)


class SnatPoolCannotBeUsedForFloatingIp(exceptions.InvalidInput):
    message = _("Floating IP cannot be allocated in SNAT host pool subnet.")


class PreExistingSVICannotBeConnectedToRouter(exceptions.BadRequest):
    message = _("A SVI network with pre-existing l3out is not allowed to "
                "be connected to a router.")


class PreExistingSVICannotUseSameL3out(exceptions.BadRequest):
    message = _("Can not create a SVI network with pre-existing l3out "
                "if that l3out has been used by another SVI network.")


class OnlyOneSubnetPerAddressFamilyInSVINetwork(exceptions.BadRequest):
    message = _("Only one subnet per address family is allowed in "
                "SVI network.")


class ExternalSubnetOverlapInL3Out(exceptions.BadRequest):
    message = _("External subnet CIDR %(cidr)s overlaps with existing "
                "subnets in APIC L3Outside %(l3out)s.")


class ExhaustedApicRouterIdPool(exceptions.IpAddressGenerationFailure):
    message = _("All the IPs in the APIC router ID pool %(pool)s "
                "have been taken.")


class ExternalSubnetNotAllowed(exceptions.BadRequest):
    message = _("Connecting port or subnet which is on external network "
                "%(network_id)s as a router interface is not allowed. "
                "External networks can only be used as router gateways.")


class SubnetOverlapInRoutedVRF(exceptions.BadRequest):
    message = _("Subnets %(id1)s (%(cidr1)s) and %(id2)s (%(cidr2)s) mapped "
                "to %(vrf)s overlap.")


class ActiveActiveAAPSubnetConnectedToRouter(exceptions.BadRequest):
    message = _("Subnet %(subnet_id)s can not be connected to a router "
                "because its an active active AAP subnet.")


class AAPNotAllowedOnDifferentActiveActiveAAPSubnet(exceptions.BadRequest):
    message = _("Allowed address pair can not be added to this port "
                "because its subnets %(subnet_ids)s active active AAP mode is "
                "different than other port's subnets %(other_subnet_ids)s.")


class InvalidNetworkForExtraContracts(exceptions.BadRequest):
    message = _("Cannot specify apic:extra_provided_contracts or "
                "apic:extra_consumed_consumed contracts for an external or "
                "SVI network.")


class InvalidNetworkForEpgContractMaster(exceptions.BadRequest):
    message = _("Cannot specify apic:epg_contract_masters for "
                "an external or SVI network.")


class InvalidNetworkForPolicyEnforcementPref(exceptions.BadRequest):
    message = _("apic:policy_enforcement_pref cannot be 'enforced' for "
                "SVI network.")


class InvalidNetworkForQos(exceptions.BadRequest):
    message = _("Cannot specify qos policy for "
                "an external or SVI network.")


class InvalidPolicyTargetForQos(exceptions.BadRequest):
    message = _("Apic_aim does not support QoS policy for Floating IP.")


class InvalidPreexistingBdForNetwork(exceptions.BadRequest):
    message = _("The Bridge Domain specified in apic:distinguished_names "
                "either does not exist in ACI or belongs to another network "
                "in this OpenStack instance.")


class InvalidPortForErspanSession(exceptions.BadRequest):
    message = _("AIM ERSPAN extensions are only supported on ports with a "
                "device owner of 'compute:'")


class InvalidFabricPathForErspanSession(exceptions.BadRequest):
    message = _("AIM ERSPAN extensions are only supported for ports on hosts "
                "that are connected to the fabric via virtual port channels "
                "(VPCs) or port channels (PCs).")


class InvalidNetworkForErspanSession(exceptions.BadRequest):
    message = _("AIM ERSPAN extensions are not supported on ports on SVI "
                "type networks.")


class SnatPoolCannotBeUsedForGatewayIp(exceptions.BadRequest):
    message = _("Snat only subnet cannot be used to assign network gateway.")


class MultiExtNetworkMixing(exceptions.BadRequest):
    message = _("All external networks associated with a l3out must "
                "use the same apic:multi_ext_nets setting. ")
