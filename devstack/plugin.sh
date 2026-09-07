GBP="Group-Based Policy"

function gbp_configure_nova {
    iniset $NOVA_CONF neutron allow_duplicate_networks "True"
}

function gbp_configure_heat {
    local HEAT_PLUGINS_DIR="/opt/stack/gbpautomation/gbpautomation/heat"
    iniset $HEAT_CONF DEFAULT plugin_dirs "$HEAT_PLUGINS_DIR"
}

function gbp_configure_neutron {
    iniset $NEUTRON_CONF group_policy policy_drivers "implicit_policy,resource_mapping,chain_mapping"
    iniset $NEUTRON_CONF group_policy extension_drivers "proxy_group"
    iniset $NEUTRON_CONF servicechain servicechain_drivers "simplechain_driver"
    iniset $NEUTRON_CONF node_composition_plugin node_plumber "stitching_plumber"
    iniset $NEUTRON_CONF node_composition_plugin node_drivers "heat_node_driver"
    iniset $NEUTRON_CONF quotas default_quota "-1"
    iniset $NEUTRON_CONF quotas quota_network "-1"
    iniset $NEUTRON_CONF quotas quota_subnet "-1"
    iniset $NEUTRON_CONF quotas quota_port "-1"
    iniset $NEUTRON_CONF quotas quota_security_group "-1"
    iniset $NEUTRON_CONF quotas quota_security_group_rule "-1"
    iniset $NEUTRON_CONF quotas quota_router "-1"
    iniset $NEUTRON_CONF quotas quota_floatingip "-1"
    iniset $NEUTRON_CONF agent extensions "qos"
}

# Process contract
if is_service_enabled group-policy; then
    if [[ "$1" == "stack" && "$2" == "pre-install" ]]; then
        echo_summary "Preparing $GBP"
    elif [[ "$1" == "stack" && "$2" == "install" ]]; then
        echo_summary "Installing $GBP"
        [[ $ENABLE_APIC_AIM = True || $ENABLE_APIC_AIM_GATE = True ]] && install_apic_aim
        if [[ $ENABLE_NSX_POLICY = True ]]; then
            echo_summary "Installing NSX Policy requirements"
            prepare_nsx_policy
        fi

    elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
        echo_summary "Configuring $GBP"
        # REVIST Ideally, we should be configuring nova, heat and UI as well for GBP in the
        # GBP devstack gate job. However, contrary to the documentation, this block
        # of code is not being invoked by devstack after the nova, heat and
        # dashboard config files have been created. Once this is sorted out, the
        # ENABLE_GBP_GATE variable can be eliminated.
        [[ $ENABLE_GBP_GATE = False && $ENABLE_APIC_AIM_GATE = False ]] && gbp_configure_nova
        [[ $ENABLE_GBP_GATE = False && $ENABLE_APIC_AIM_GATE = False ]] && gbp_configure_heat
        gbp_configure_neutron

        if [[ $ENABLE_NSX_POLICY = True ]]; then
            echo_summary "Configuring NSX"
            nsx_configure_neutron
        fi
        # REVISIT move installs to install phase?
        install_gbpclient
        install_gbpservice
        init_gbpservice
        [[ $ENABLE_GBP_GATE = False && $ENABLE_APIC_AIM_GATE = False ]] && install_gbpheat
        [[ $ENABLE_GBP_GATE = False && $ENABLE_APIC_AIM_GATE = False ]] && install_gbpui
        [[ $ENABLE_APIC_AIM = True || $ENABLE_APIC_AIM_GATE = True ]] && configure_apic_aim
        [[ $ENABLE_GBP_GATE = False && $ENABLE_APIC_AIM_GATE = False ]] && stop_apache_server
        [[ $ENABLE_GBP_GATE = False && $ENABLE_APIC_AIM_GATE = False ]] && start_apache_server
    fi

    if [[ "$1" == "unstack" ]]; then
        echo_summary "Removing $GBP"
    fi

    if [[ "$1" == "clean" ]]; then
        echo_summary "Cleaning $GBP"
    fi
fi
