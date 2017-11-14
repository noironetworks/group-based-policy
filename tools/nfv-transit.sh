#!/usr/bin/env bash

# This script exits on an error so that errors don't compound and you see
# only the first error that occurred.

set -o errtrace

export ACTIVE_TIMEOUT=${ACTIVE_TIMEOUT:-60}

CREATED_NETWORKS=()
CREATED_SUBNETS=()
CREATED_PORTS=()
CREATED_VMS=()
CREATED_PORT_CHAINS=()
CREATED_PORT_PAIR_GROUPS=()
CREATED_PORT_PAIRS=()
CREATED_FLOW_CLASSIFIERS=()
CREATED_ROUTERS=()

function is_set {
    local var=\$"$1"
    eval "[ -n \"$var\" ]" # For ex.: sh -c "[ -n \"$var\" ]" would be better, but several exercises depends on this
}

function err {
    local exitcode=$?
    local xtrace
    xtrace=$(set +o | grep xtrace)
    set +o xtrace
    local msg="[ERROR] ${BASH_SOURCE[2]}:$1 $2"
    echo $msg 1>&2;
    if [[ -n ${LOGDIR} ]]; then
        echo $msg >> "${LOGDIR}/error.log"
    fi
    $xtrace
    return $exitcode
}

function backtrace {
    local level=$1
    local deep
    deep=$((${#BASH_SOURCE[@]} - 1))
    echo "[Call Trace]"
    while [ $level -le $deep ]; do
        echo "${BASH_SOURCE[$deep]}:${BASH_LINENO[$deep-1]}:${FUNCNAME[$deep-1]}"
        deep=$((deep - 1))
    done
}

function die {
    local exitcode=$?
    set +o xtrace
    local line=$1; shift
    if [ $exitcode == 0 ]; then
        exitcode=1
    fi
    backtrace 2
    err $line "$*"
    # Give buffers a second to flush
    sleep 1
    exit $exitcode
}

function die_if_not_set {
    local exitcode=$?
    local xtrace
    xtrace=$(set +o | grep xtrace)
    set +o xtrace
    local line=$1; shift
    local evar=$1; shift
    if ! is_set $evar || [ $exitcode != 0 ]; then
        die $line "$*"
    fi
    $xtrace
}

function get_project_id {
    local PROJECT_NAME=$1
    local PROJECT_ID
    PROJECT_ID=`openstack project list | grep " $PROJECT_NAME " | head -n 1 | get_field 1`
    die_if_not_set $LINENO PROJECT_ID "Failure retrieving PROJECT_ID for $PROJECT_NAME"
    echo "$PROJECT_ID"
}

function create_network {
    local PROJECT=$1
    local NET_NAME=$2
    local EXTRA=$3
    local PROJECT_ID
    PROJECT_ID=$(get_project_id $PROJECT)
    local NET_ID=$(neutron net-create --tenant-id $PROJECT_ID $NET_NAME $EXTRA| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO NET_ID "Failure creating NET_ID for $PROJECT_ID $NET_NAME $EXTRA"
    CREATED_NETWORKS+=(${NET_ID})
}

function create_subnet {
    local PROJECT=$1
    local NET_ID=$2
    local GATEWAY=$3
    local CIDR=$4
    local EXTRA=$5
    PROJECT_ID=$(get_project_id $PROJECT)
    local SUBNET_ID
    SUBNET_ID=$(openstack subnet create --ip-version 4 --project $PROJECT_ID --gateway $GATEWAY \
                --network $NET_ID --subnet-range $CIDR $EXTRA '' | grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO SUBNET_ID "Failure creating SUBNET_ID for $PROJECT_ID $NET_ID $CIDR"
    CREATED_SUBNETS+=(${SUBNET_ID})
}

function create_port {
    local PROJECT=$1
    local NET_ID=$2
    local EXTRA=$3
    PROJECT_ID=$(get_project_id $PROJECT)
    local PORT_ID=$(openstack port create --project $PROJECT_ID --network $NET_ID $EXTRA ''| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PORT_ID "Failure creating PORT_ID for $PROJECT_ID $NET_ID $EXTRA"
    CREATED_PORTS+=(${PORT_ID})
}

function create_router {
    local PROJECT=$1
    local NAME=$2
    PROJECT_ID=$(get_project_id $PROJECT)
    local ROUTER_ID=$(openstack router create --project $PROJECT_ID $NAME| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO ROUTER_ID "Failure creating ROUTER_ID for $PROJECT $NAME"
    CREATED_ROUTERS+=(${ROUTER_ID})
}

function add_subnets_to_router {
    local PROJECT=$1
    local ROUTER_ID=$2
    local SUB_IDS=${@:3}
    PROJECT_ID=$(get_project_id $PROJECT)
    for sub_id in ${SUB_IDS}; do
        openstack router add subnet $ROUTER_ID ${sub_id}
    done
}

function get_network_id {
    local NETWORK_NAME="$1"
    local NETWORK_ID
    NETWORK_ID=`openstack network show -f value -c id $NETWORK_NAME`
    echo $NETWORK_ID
}

function confirm_server_active {
    local VM_UUID=$1
    if ! timeout $ACTIVE_TIMEOUT sh -c "while ! nova show $VM_UUID | grep status | grep -q ACTIVE; do sleep 1; done"; then
        echo "server '$VM_UUID' did not become active!"
        false
    fi
}

function create_service_chain {
    local PROJECT=$1
    local PROV_CIDR=$2
    local CONS_CIDR=$3
    local PROV_NET_ID=$4
    local CONS_NET_ID=$5
    local LEFT_PORT_ID=$6
    local RIGHT_PORT_ID=$7
    local FLOWC_ID=$(neutron --os-project-name $PROJECT flow-classifier-create --destination-ip-prefix $PROV_CIDR \
                     --source-ip-prefix $CONS_CIDR --l7-parameters logical_source_network=$CONS_NET_ID,logical_destination_network=$PROV_NET_ID \
                     flowc| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO FLOWC_ID "Failure launching Flow Classifier"
    CREATED_FLOW_CLASSIFIERS+=(${FLOWC_ID})
    local PP_ID=$(neutron --os-project-name $PROJECT port-pair-create --ingress $LEFT_PORT_ID --egress $RIGHT_PORT_ID pp| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PP_ID "Failure launching Port Pair"
    CREATED_PORT_PAIRS+=(${PP_ID})
    local PPG_ID=$(neutron --os-project-name $PROJECT port-pair-group-create --port-pair $PP_ID ppg| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PPG_ID "Failure launching Port Pair Group"
    CREATED_PORT_PAIR_GROUPS+=(${PPG_ID})
    local PC_ID=$(neutron --os-project-name $PROJECT port-chain-create --flow-classifier $FLOWC_ID --port-pair-group $PPG_ID pc| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PC_ID "Failure launching Port Chain"
    CREATED_PORT_CHAINS+=(${PC_ID})
}

function create_port_pair {
    local PROJECT=$1
    local LEFT_PORT_ID=$2
    local RIGHT_PORT_ID=$3
    local PP_ID=$(neutron --os-project-name $PROJECT port-pair-create --ingress $LEFT_PORT_ID --egress $RIGHT_PORT_ID pp| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PP_ID "Failure launching Port Pair"
    CREATED_PORT_PAIRS+=(${PP_ID})
}

function create_port_pair_group {
    local PROJECT=$1
    local PP_IDS=${@:2}
    PPS=""
    for pp_id in ${PP_IDS}; do
        PPS="$PPS --port-pair $pp_id"
    done
    local PPG_ID=$(neutron --os-project-name $PROJECT port-pair-group-create $PPS ppg| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PPG_ID "Failure launching Port Pair Group"
    CREATED_PORT_PAIR_GROUPS+=(${PPG_ID})
}

function create_flow_classifier {
    local PROJECT=$1
    local PROV_CIDR=$2
    local CONS_CIDR=$3
    local PROV_NET_ID=$4
    local CONS_NET_ID=$5
    local FLOWC_ID=$(neutron --os-project-name $PROJECT flow-classifier-create --destination-ip-prefix $PROV_CIDR \
                     --source-ip-prefix $CONS_CIDR --l7-parameters logical_source_network=$CONS_NET_ID,logical_destination_network=$PROV_NET_ID \
                     flowc| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO FLOWC_ID "Failure launching Flow Classifier"
    CREATED_FLOW_CLASSIFIERS+=(${FLOWC_ID})
}

function create_port_chain_with_all {
    local PROJECT=$1
    local PPGS=""
    for ppg_id in ${CREATED_PORT_PAIR_GROUPS[@]}; do
        PPGS="$PPGS --port-pair-group $ppg_id"
    done
    local FLOWCS=""
    for flowc_id in ${CREATED_FLOW_CLASSIFIERS[@]}; do
        FLOWCS="$FLOWCS --flow-classifier $flowc_id"
    done
    local PC_ID=$(neutron --os-project-name $PROJECT port-chain-create $FLOWCS $PPGS pc| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PC_ID "Failure launching Port Chain"
    CREATED_PORT_CHAINS+=(${PC_ID})
}

function create_vm {
    local PROJECT=$1
    local NET_IDS=$2
    local PORT_IDS=$3
    local IMAGE_ID=$4
    local FLAVOR_ID=$5
    local NAME=$6
    local NIC=""
    for NET_ID in ${NET_IDS//,/ };do
        NIC="$NIC --nic net-id=$NET_ID"
    done
    for PORT_ID in ${PORT_IDS//,/ };do
        NIC="$NIC --nic port-id=$PORT_ID"
    done
    local VM_UUID
    VM_UUID=`nova --os-project-name $PROJECT boot \
        --flavor $FLAVOR_ID \
        --image $IMAGE_ID \
        $NIC \
        $NAME| grep ' id ' | cut -d"|" -f3 | sed 's/ //g'`
    die_if_not_set $LINENO VM_UUID "Failure launching $NAME"
    CREATED_VMS+=(${VM_UUID})
    confirm_server_active $VM_UUID
}

function get_field {
    local data field
    while read data; do
        if [ "$1" -lt 0 ]; then
            field="(\$(NF$1))"
        else
            field="\$$(($1 + 1))"
        fi
        echo "$data" | awk -F'[ \t]*\\|[ \t]*' "{print $field}"
    done
}

function get_image_id {
    local IMAGE_NAME=$1
    IMAGE_ID=$(openstack image list | egrep " $IMAGE_NAME " | get_field 1)
    die_if_not_set $LINENO IMAGE_ID "Failure retrieving IMAGE_ID"
    echo "$IMAGE_ID"
}

function test_north_south {
    set -o xtrace
    PROJECT=$1
    local MULTI_NIC_IMG_NAME=$2
    local TRAFFIC_IMG_NAME=$3
    local FLAVOR_NAME=$4
    local NODES=$5
    local CONSUMERS=$6
    # Create left and right networks
    # Create routers to set VRF
    create_router $PROJECT "LEFT-CONS-ROUTER"
    LEFT_ROUTER_ID=${CREATED_ROUTERS[-1]}
    create_router $PROJECT "RIGHT-PROV-ROUTER"
    RIGHT_ROUTER_ID=${CREATED_ROUTERS[-1]}
    for ((i=1; i<=NODES; i++)); do
        create_network $PROJECT "left-${i}" ""
        LEFT_ID=${CREATED_NETWORKS[-1]}
        create_subnet $PROJECT $LEFT_ID "1.1.${i}.1" "1.1.${i}.0/24" "--host-route destination=172.168.0.0/16,gateway=1.1.${i}.1"
        LEFT_S_ID=${CREATED_SUBNETS[-1]}
        add_subnets_to_router $PROJECT $LEFT_ROUTER_ID $LEFT_S_ID
        create_network $PROJECT "right-${i}" ""
        RIGHT_ID=${CREATED_NETWORKS[-1]}
        create_subnet $PROJECT $RIGHT_ID "2.2.${i}.1" "2.2.${i}.0/24" "--host-route destination=173.168.0.0/16,gateway=2.2.${i}.1"
        RIGHT_S_ID=${CREATED_SUBNETS[-1]}
        add_subnets_to_router $PROJECT $RIGHT_ROUTER_ID $RIGHT_S_ID
        # Create service VM
        create_port $PROJECT $LEFT_ID "--no-security-group --disable-port-security"
        LEFT_PORT_ID=${CREATED_PORTS[-1]}
        create_port $PROJECT $RIGHT_ID "--no-security-group --disable-port-security"
        RIGHT_PORT_ID=${CREATED_PORTS[-1]}
        create_vm $PROJECT "" "$LEFT_PORT_ID,$RIGHT_PORT_ID" $MULTI_NIC_IMG_NAME $FLAVOR_NAME "service-${i}"
        create_port_pair $PROJECT $LEFT_PORT_ID $RIGHT_PORT_ID
        create_port_pair_group $PROJECT ${CREATED_PORT_PAIRS[-1]}
    done
    # Create SVI Provider network
    create_network $PROJECT "provider" "--provider:network_type vlan --apic:svi True"
    PROV_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT $PROV_ID "192.168.1.1" "192.168.1.0/24" "--host-route destination=172.168.0.0/24,gateway=192.168.1.1"
    PROV_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $RIGHT_ROUTER_ID $PROV_S_ID
    create_network $PROJECT "prov-back1" ""
    PROV_BACK_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT $PROV_BACK_ID "172.168.1.1" "172.168.1.0/24"
    PROV_BACK_S_ID=${CREATED_SUBNETS[-1]}
    # Create Provider side routing VM (NAT)
    create_port $PROJECT $PROV_ID "--no-security-group --disable-port-security"
    PROV_PORT_ID=${CREATED_PORTS[-1]}
    create_port $PROJECT $PROV_BACK_ID "--no-security-group --disable-port-security --fixed-ip subnet=$PROV_BACK_S_ID,ip-address=172.168.1.1"
    PROV_BACK_PORT_ID=${CREATED_PORTS[-1]}
    create_vm $PROJECT "" "$PROV_PORT_ID,$PROV_BACK_PORT_ID" $MULTI_NIC_IMG_NAME $FLAVOR_NAME "prov-router"

    # Create SVI Consumer network
    create_vm $PROJECT "$PROV_BACK_ID" "" $TRAFFIC_IMG_NAME $FLAVOR_NAME "provider"
    create_network $PROJECT "consumer" "--provider:network_type vlan --apic:svi True"
    CONS_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT $CONS_ID "192.168.0.1" "192.168.0.0/24" "--host-route destination=172.168.1.0/24,gateway=192.168.0.1"
    CONS_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $LEFT_ROUTER_ID $CONS_S_ID

    for ((i=1; i<=CONSUMERS; i++)); do
        # Create backend networks. These networks simulate the customer sites. Create more than one on the consumer side to test multi-site and multi-classifier chains.
        create_network $PROJECT "cons-back${i}" ""
        CONS_BACK_ID=${CREATED_NETWORKS[-1]}
        create_subnet $PROJECT $CONS_BACK_ID "173.168.${i}.1" "173.168.${i}.0/24"
        CONS_BACK_S_ID=${CREATED_SUBNETS[-1]}
        create_flow_classifier $PROJECT "172.168.1.0/24" "173.168.${i}.0/24" $PROV_ID $CONS_ID
        # Create Consumer side routing VM (BRAS)
        create_port $PROJECT $CONS_ID "--no-security-group --disable-port-security"
        CONS_PORT_ID=${CREATED_PORTS[-1]}
        create_port $PROJECT $CONS_BACK_ID "--no-security-group --disable-port-security --fixed-ip subnet=$CONS_BACK_S_ID,ip-address=173.168.${i}.1"
        CONS_BACK_PORT_ID=${CREATED_PORTS[-1]}
        create_vm $PROJECT "" "$CONS_PORT_ID,$CONS_BACK_PORT_ID" $MULTI_NIC_IMG_NAME $FLAVOR_NAME "cons-router-${i}"
        # Create Consumer Back VM
        create_vm $PROJECT "$CONS_BACK_ID" "" $TRAFFIC_IMG_NAME $FLAVOR_NAME "consumer-${i}"
    done
    # Create Service Chain
    create_port_chain_with_all $PROJECT
    set +o xtrace
    echo "SUCCESS"
}

function neutron_list {
    local PROJECT=$1
    local TYPE=$2
    PROJECT_ID=$(get_project_id $PROJECT)
    local result
    result=$(neutron ${TYPE}-list --tenant-id $PROJECT_ID -c id | tail -n +4 | head -n -1 | tr -d ' |')
    echo "${result[@]}"
}

function neutron_delete {
    local TYPE=$1
    local RESOURCES=${@:2}
    for ID in ${RESOURCES}; do
        neutron ${TYPE}-delete "${ID}";
    done
}

function openstack_list {
    local PROJECT=$1
    local TYPE=$2
    local result
    result=$(openstack --os-project-name ${PROJECT} ${TYPE} list -f value -c ID)
    echo "${result[@]}"
}

function openstack_delete {
    local TYPE=$1
    local RESOURCES=${@:2}
    for ID in ${RESOURCES}; do
        openstack --os-project-name ${PROJECT} ${TYPE} delete "${ID}";
    done
}

function delete_ports {
    local RESOURCES=("$@")
    neutron_delete "port" "${RESOURCES[@]}"
}

function delete_networks {
    local RESOURCES=("$@")
    neutron_delete "net" "${RESOURCES[@]}"
}

function delete_routers {
    local RESOURCES=${@}
    # Router interfaces have to be deleted first
    for router_id in ${RESOURCES}; do
        port_list=$(neutron router-port-list -c id ${router_id} | tail -n +4 | head -n -1 | tr -d ' |')
        for port_id in ${port_list}; do
            openstack router remove port ${router_id} ${port_id};
        done;
    done;
    neutron_delete "router" "${RESOURCES[@]}"
}

function delete_port_chains {
    local RESOURCES=("$@")
    neutron_delete "port-chain" "${RESOURCES[@]}"
}

function delete_port_pair_groups {
    local RESOURCES=("$@")
    neutron_delete "port-pair-group" "${RESOURCES[@]}"
}

function delete_port_pairs {
    local RESOURCES=("$@")
    neutron_delete "port-pair" "${RESOURCES[@]}"
}

function delete_flow_classifiers {
    local RESOURCES=("$@")
    neutron_delete "flow-classifier" "${RESOURCES[@]}"
}

function delete_vms {
    local RESOURCES=("$@")
    openstack_delete "server" "${RESOURCES[@]}"
}

function delete_all_ports {
    local PROJECT=$1
    local list
    list=$(neutron_list ${PROJECT} "port")
    delete_ports "${list[@]}"
}

function delete_all_networks {
    local PROJECT=$1
    local list
    list=$(neutron_list ${PROJECT} "net")
    delete_networks "${list[@]}"
}

function delete_all_routers {
    local PROJECT=$1
    local list
    list=$(neutron_list ${PROJECT} "router")
    delete_routers "${list[@]}"
}

function delete_all_port_chains {
    local PROJECT=$1
    local list
    list=$(neutron_list ${PROJECT} "port-chain")
    delete_port_chains "${list[@]}"
}

function delete_all_port_pair_groups {
    local PROJECT=$1
    local list
    list=$(neutron_list ${PROJECT} "port-pair-group")
    delete_port_pair_groups "${list[@]}"
}

function delete_all_port_pairs {
    local PROJECT=$1
    local list
    list=$(neutron_list ${PROJECT} "port-pair")
    delete_port_pairs "${list[@]}"
}

function delete_all_flow_classifiers {
    local PROJECT=$1
    local list
    list=$(neutron_list ${PROJECT} "flow-classifier")
    delete_flow_classifiers "${list[@]}"
}

function delete_all_vms {
    local PROJECT=$1
    local list
    list=$(openstack_list ${PROJECT} "server")
    delete_vms "${list[@]}"
}

function usage {
    echo "$0: [-h]"
    echo "  -h, --help                                                                                                 Display help message"
    echo "  -t, --transit PROJECT_NAME MULTI_NIC_IMG_NAME TRAFFIC_IMG_NAME FLAVOR_NAME NODE_NUM CONSUMER_NUM           Deploy transit traffic test"
    echo "  -c, --cleanup PROJECT_NAME                                                                                 Deletes *ALL* VMs and Networks from Project"

}

function delete_all {
    set -o xtrace
    local PROJECT=$1
    delete_all_port_chains $PROJECT
    delete_all_port_pair_groups $PROJECT
    delete_all_port_pairs $PROJECT
    delete_all_flow_classifiers $PROJECT
    delete_all_vms $PROJECT
    delete_all_routers $PROJECT
    delete_all_ports $PROJECT
    delete_all_networks $PROJECT
    set +o xtrace
    echo "SUCCESS"
}

function delete_created {
    set -o xtrace
    delete_port_chains "${CREATED_PORT_CHAINS[@]}"
    delete_port_pair_groups "${CREATED_PORT_PAIR_GROUPS[@]}"
    delete_port_pairs "${CREATED_PORT_PAIRS[@]}"
    delete_flow_classifiers "${CREATED_FLOW_CLASSIFIERS[@]}"
    delete_vms "${CREATED_VMS[@]}"
    delete_routers "${CREATED_ROUTERS[@]}"
    delete_ports "${CREATED_PORTS[@]}"
    delete_networks "${CREATED_NETWORKS[@]}"
    set +o xtrace
}

function transit {
    if [ $# -lt 6 ] ; then
        echo "Transit accepts exactly 4 arguments"
        usage
        exit 1
    fi
    test_north_south $*
}

function cleanup {
    if [ $# -ne 2 ] ; then
        echo "Cleanup accepts exactly 1 argument"
        usage
        exit 1
    fi
    delete_all $*
}

function main {

    if [ $# -eq 0 ] ; then
        usage
    else

        while [ "$1" != "" ]; do
            case $1 in
                -h | --help )   usage
                                exit
                                ;;
                -t | --transit ) transit ${@:2}
                                exit
                                ;;
                -c | --cleanup ) delete_all ${@:2}
                                exit
                                ;;
                * )             usage
                                exit 1
            esac
            shift
        done
    fi
}

trap failed ERR
function failed {
    local r=$?
    set +o errtrace
    set +o xtrace
    set -e
    echo "Failed to execute"
    echo "Starting cleanup..."
    delete_created
    echo "Finished cleanup"
    exit $r
}

main $*
