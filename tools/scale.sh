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
CREATED_TRUNKS=()

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
    if [ "$PROJECT_ID" = "" ]; then
        openstack project create $PROJECT_NAME > /dev/null
        openstack role add _member_ --project ${PROJECT_NAME} --user admin > /dev/null
        PROJECT_ID=`openstack project list | grep " $PROJECT_NAME " | head -n 1 | get_field 1`
        die_if_not_set $LINENO PROJECT_ID "Failure retrieving PROJECT_ID for $PROJECT_NAME"
    fi
    echo "$PROJECT_ID"
}

function create_network {
    local PROJECT_ID=$1
    local NET_NAME=$2
    local EXTRA=$3
    local PROJECT_ID
    local NET_ID=$(neutron net-create --tenant-id $PROJECT_ID $NET_NAME $EXTRA| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO NET_ID "Failure creating NET_ID for $PROJECT_ID $NET_NAME $EXTRA"
    CREATED_NETWORKS+=(${NET_ID})
}

function create_subnet {
    local PROJECT_ID=$1
    local NET_ID=$2
    local GATEWAY=$3
    local CIDR=$4
    local EXTRA=$5
    local SUBNET_ID
    SUBNET_ID=$(openstack subnet create --ip-version 4 --project $PROJECT_ID --gateway $GATEWAY \
                --network $NET_ID --subnet-range $CIDR $EXTRA '' | grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO SUBNET_ID "Failure creating SUBNET_ID for $PROJECT_ID $NET_ID $CIDR"
    CREATED_SUBNETS+=(${SUBNET_ID})
}

function create_port {
    local PROJECT_ID=$1
    local NAME=$2
    local NET_ID=$3
    local EXTRA=$4
    local PORT_ID=$(openstack port create $NAME --project $PROJECT_ID --network $NET_ID $EXTRA | grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PORT_ID "Failure creating PORT_ID for $PROJECT_ID $NET_ID $EXTRA"
    CREATED_PORTS+=(${PORT_ID})
}

function create_trunk {
    local PROJECT_ID=$1
    local NAME=$2
    local PARENT_PORT_ID=$3
    local TRUNK_ID=$(openstack network trunk create --project $PROJECT_ID --parent-port $PARENT_PORT_ID $NAME | grep ' id ' | awk '{print $4}')
    die_if_not_set $LINENO TRUNK_ID "Failure creating TRUNK_ID for $PROJECT_ID"
    CREATED_TRUNKS+=(${TRUNK_ID})
}

function add_trunk_subport {
    local PROJECT_ID=$1
    local TRUNK_ID=$2
    local PORT_ID=$3
    local LOCAL_VLAN=$4
    openstack network trunk set --subport port=$PORT_ID,segmentation-type=vlan,segmentation-id=$LOCAL_VLAN $TRUNK_ID
}

function create_router {
    local PROJECT_ID=$1
    local NAME=$2
    local ROUTER_ID=$(openstack router create --project $PROJECT_ID $NAME| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO ROUTER_ID "Failure creating ROUTER_ID for $NAME"
    CREATED_ROUTERS+=(${ROUTER_ID})
}

function add_subnets_to_router {
    local PROJECT_ID=$1
    local ROUTER_ID=$2
    local SUB_IDS=${@:3}
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
    local FLOWC_ID=$(neutron --os-project-id $PROJECT flow-classifier-create --destination-ip-prefix $PROV_CIDR \
                     --source-ip-prefix $CONS_CIDR --l7-parameters logical_source_network=$CONS_NET_ID,logical_destination_network=$PROV_NET_ID \
                     flowc| grep ' id ' | awk '{print $4}')
    die_if_not_set $LINENO FLOWC_ID "Failure launching Flow Classifier"
    CREATED_FLOW_CLASSIFIERS+=(${FLOWC_ID})
    local PP_ID=$(neutron --os-project-id $PROJECT port-pair-create --ingress $LEFT_PORT_ID --egress $RIGHT_PORT_ID pp| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PP_ID "Failure launching Port Pair"
    CREATED_PORT_PAIRS+=(${PP_ID})
    local PPG_ID=$(neutron --os-project-id $PROJECT port-pair-group-create --port-pair $PP_ID ppg| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PPG_ID "Failure launching Port Pair Group"
    CREATED_PORT_PAIR_GROUPS+=(${PPG_ID})
    local PC_ID=$(neutron --os-project-id $PROJECT port-chain-create --flow-classifier $FLOWC_ID --port-pair-group $PPG_ID pc| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PC_ID "Failure launching Port Chain"
    CREATED_PORT_CHAINS+=(${PC_ID})
}

function create_port_pair {
    local PROJECT=$1
    local NAME=$2
    local LEFT_PORT_ID=$3
    local RIGHT_PORT_ID=$4
    local PP_ID=$(openstack --os-project-id $PROJECT sfc port pair create --ingress $LEFT_PORT_ID --egress $RIGHT_PORT_ID $NAME| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PP_ID "Failure launching Port Pair"
    CREATED_PORT_PAIRS+=(${PP_ID})
}

function create_port_pair_group {
    local PROJECT=$1
    local NAME=$2
    local PP_IDS=${@:3}
    PPS=""
    for pp_id in ${PP_IDS}; do
        PPS="$PPS --port-pair $pp_id"
    done
    local PPG_ID=$(openstack --os-project-id $PROJECT sfc port pair group create $PPS $NAME| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PPG_ID "Failure launching Port Pair Group"
    CREATED_PORT_PAIR_GROUPS+=(${PPG_ID})
}

function create_flow_classifier {
    local PROJECT=$1
    local NAME=$2
    local PROV_CIDR=$3
    local CONS_CIDR=$4
    local PROV_NET_ID=$5
    local CONS_NET_ID=$6
    local FLOWC_ID=$(openstack --os-project-id $PROJECT sfc flow classifier create --destination-ip-prefix $PROV_CIDR \
                     --source-ip-prefix $CONS_CIDR --l7-parameters logical_source_network=$CONS_NET_ID,logical_destination_network=$PROV_NET_ID \
                     $NAME| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO FLOWC_ID "Failure launching Flow Classifier"
    CREATED_FLOW_CLASSIFIERS+=(${FLOWC_ID})
}

function create_port_chain {
    local PROJECT=$1
    local NAME=$2
    local PPGS=$3
    local FLOWCS=$4
    local PAR_PPGS=""
    for ppg_id in ${PPGS//,/ }; do
        PAR_PPGS="$PAR_PPGS --port-pair-group $ppg_id"
    done
    local PAR_FLOWCS=""
    for flowc_id in ${FLOWCS//,/ }; do
        PAR_FLOWCS="$PAR_FLOWCS --flow-classifier $flowc_id"
    done
    local PC_ID=$(openstack --os-project-id $PROJECT sfc port chain create $PAR_FLOWCS $PAR_PPGS $NAME| grep ' id ' | awk '{print $4}' )
    die_if_not_set $LINENO PC_ID "Failure launching Port Chain"
    CREATED_PORT_CHAINS+=(${PC_ID})
}

function update_port_chain {
    local PROJECT=$1
    local CHAIN_ID=$2
    local PPGS=$3
    local FLOWCS=$4
    local PAR_PPGS=""
    for ppg_id in ${PPGS//,/ }; do
        PAR_PPGS="$PAR_PPGS --port-pair-group $ppg_id"
    done
    local PAR_FLOWCS=""
    for flowc_id in ${FLOWCS//,/ }; do
        PAR_FLOWCS="$PAR_FLOWCS --flow-classifier $flowc_id"
    done
    res=$(neutron port-chain-update --tenant-id $PROJECT $CHAIN_ID $PAR_FLOWCS $PAR_PPGS)
}

function create_vm {
    local PROJECT=$1
    local NAME=$2
    local PORT_IDS=$3
    local IMAGE_ID=$4
    local FLAVOR_ID=$5
    local NIC=""
    for PORT_ID in ${PORT_IDS//,/ };do
        NIC="$NIC --nic port-id=$PORT_ID"
    done
    local VM_UUID
    VM_UUID=`nova --os-project-id $PROJECT boot \
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

function deploy_sfc_topology_big {
    local PROJECT=$(get_project_id $1)
    create_router $PROJECT "ROUTER"; ROUTER_ID=${CREATED_ROUTERS[-1]}
    # SVI network in Starbucks
    create_network $PROJECT "TRANSIT" "--provider:network_type vlan --apic:svi True"; NET_ID=${CREATED_NETWORKS[-1]}
    # Starbucks network subnet
    create_subnet $PROJECT $NET_ID "192.168.0.1" "192.168.0.0/24" ""; SUB_ID=${CREATED_SUBNETS[-1]}

    local PROJECT_PORT_PAIR_GROUPS=""
    # Create Service Networks: Service 1
    for ((i=1; i<=3; i++)); do
        create_network $PROJECT "LEFT-${i}" ""; LEFT_ID=${CREATED_NETWORKS[-1]}
        create_subnet $PROJECT $LEFT_ID "1.1.${i}.1" "1.1.${i}.0/24" ""; LEFT_S_ID=${CREATED_SUBNETS[-1]}
        add_subnets_to_router $PROJECT $ROUTER_ID $LEFT_S_ID
        create_network $PROJECT "RIGHT-${i}" ""; RIGHT_ID=${CREATED_NETWORKS[-1]}
        create_subnet $PROJECT $RIGHT_ID "2.2.${i}.1" "2.2.${i}.0/24" ""; RIGHT_S_ID=${CREATED_SUBNETS[-1]}
        add_subnets_to_router $PROJECT $ROUTER_ID $RIGHT_S_ID
        create_port $PROJECT "SERVICE-${i}-INGRESS" $LEFT_ID "--device service${i}${PROJECT} --host overcloud-controller-0.localdomain"; SERVICE1_INGRESS_PORT_ID=${CREATED_PORTS[-1]}
        create_port $PROJECT "SERVICE-${i}-EGRESS" $RIGHT_ID "--device service${i}${PROJECT} --host overcloud-controller-0.localdomain"; SERVICE1_EGRESS_PORT_ID=${CREATED_PORTS[-1]}
        # Create Service one Device Cluster
        create_port_pair $PROJECT "SERVICE-${i}" $SERVICE1_INGRESS_PORT_ID $SERVICE1_EGRESS_PORT_ID; SERVICE1_PP=${CREATED_PORT_PAIRS[-1]}
        create_port_pair_group $PROJECT "CLUSTER-${i}" $SERVICE1_PP; PROJECT_PORT_PAIR_GROUPS="$PROJECT_PORT_PAIR_GROUPS,${CREATED_PORT_PAIR_GROUPS[-1]}"
    done

    local PROJECT_FLOW_CLASSIFIERS=""
    for ((i=1; i<=10; i++)); do
        # Create Flow Classifier for Site1
        create_flow_classifier $PROJECT "INTERNET-${i}" "0.0.0.0/0" "10.0.${i}.0/24" $NET_ID $NET_ID; PROJECT_FLOW_CLASSIFIERS="$PROJECT_FLOW_CLASSIFIERS,${CREATED_FLOW_CLASSIFIERS[-1]}"
    done
    # Create Port Chain
    create_port_chain $PROJECT "CHAIN" $PROJECT_PORT_PAIR_GROUPS $PROJECT_FLOW_CLASSIFIERS
}

function deploy_sfc_topology {
    local PROJECT=$(get_project_id $1)
    create_router $PROJECT "ROUTER"; ROUTER_ID=${CREATED_ROUTERS[-1]}
    # SVI network in Starbucks
    create_network $PROJECT "TRANSIT" "--provider:network_type vlan --apic:svi True"; NET_ID=${CREATED_NETWORKS[-1]}
    # Starbucks network subnet
    create_subnet $PROJECT $NET_ID "192.168.0.1" "192.168.0.0/24" ""; SUB_ID=${CREATED_SUBNETS[-1]}

    # Create Service Networks: Service 1
    create_network $PROJECT "LEFT-1" ""; LEFT_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT $LEFT_ID "1.1.0.1" "1.1.0.0/24" ""; LEFT_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $ROUTER_ID $LEFT_S_ID
    create_network $PROJECT "RIGHT-1" ""; RIGHT_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT $RIGHT_ID "2.2.0.1" "2.2.0.0/24" ""; RIGHT_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $ROUTER_ID $RIGHT_S_ID
    create_port $PROJECT "SERVICE1-INGRESS" $LEFT_ID "--device-id service${PROJECT} --host overcloud-controller-0.localdomain"; SERVICE1_INGRESS_PORT_ID=${CREATED_PORTS[-1]}
    create_port $PROJECT "SERVICE1-EGRESS" $RIGHT_ID "--device-id service${PROJECT} --host overcloud-controller-0.localdomain"; SERVICE1_EGRESS_PORT_ID=${CREATED_PORTS[-1]}
    # Create Service one Device Cluster
    create_port_pair $PROJECT "SERVICE" $SERVICE1_INGRESS_PORT_ID $SERVICE1_EGRESS_PORT_ID; SERVICE1_PP=${CREATED_PORT_PAIRS[-1]}
    create_port_pair_group $PROJECT "CLUSTER" $SERVICE1_PP; PPG1=${CREATED_PORT_PAIR_GROUPS[-1]}
    # Create Flow Classifier for Site1
    create_flow_classifier $PROJECT "INTERNET" "0.0.0.0/0" "10.0.1.0/24" $NET_ID $NET_ID; FLC=${CREATED_FLOW_CLASSIFIERS[-1]}
    # Create Port Chain
    create_port_chain $PROJECT "CHAIN" $PPG1 $FLC
}

function deploy_router_topology {
    local PROJECT=$(get_project_id $1)
    create_router $PROJECT "ROUTER"; PEETS_ROUTER_ID=${CREATED_ROUTERS[-1]}
    create_network $PROJECT "LEFT" ""; LEFT_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT $LEFT_ID "192.168.0.1" "192.168.0.0/24"; LEFT_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $PEETS_ROUTER_ID $LEFT_S_ID
    create_network $PROJECT "RIGHT" ""; RIGHT_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT $RIGHT_ID "192.168.1.1" "192.168.1.0/24"; RIGHT_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $PEETS_ROUTER_ID $RIGHT_S_ID
}

function deploy_router_topology_bad {
    local PROJECT=$(get_project_id $1)
    create_router $PROJECT "ROUTER"; PEETS_ROUTER_ID=${CREATED_ROUTERS[-1]}
    create_network $PROJECT "LEFT-${PROJECT}" ""; LEFT_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT "LEFT-${PROJECT}" "192.168.0.1" "192.168.0.0/24"; LEFT_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $PEETS_ROUTER_ID $LEFT_S_ID
    create_network $PROJECT "RIGHT-${PROJECT}" ""; RIGHT_ID=${CREATED_NETWORKS[-1]}
    create_subnet $PROJECT "RIGHT-${PROJECT}" "192.168.1.1" "192.168.1.0/24"; RIGHT_S_ID=${CREATED_SUBNETS[-1]}
    add_subnets_to_router $PROJECT $PEETS_ROUTER_ID $RIGHT_S_ID
}


function router {
    set -o xtrace
    local NUM=$1
    local BASE=${2-0}
    local i
    for ((i=1; i<=NUM; i++)); do
        local k=$((BASE+i))
        local PR="Scale-${k}"
        #deploy_router_topology $PR &
        #deploy_router_topology_bad $PR &
        deploy_sfc_topology_big $PR
        echo "Launched ${PR}"
    done
    wait
}

function neutron_list {
    local PROJECT_ID=$1
    local TYPE=$2
    local result
    result=$(neutron ${TYPE}-list --tenant-id $PROJECT_ID -c id | tail -n +4 | head -n -1 | tr -d ' |')
    echo "${result[@]}"
}

function neutron_delete {
    local TYPE=$1
    local RESOURCES=${@:2}
    for ID in ${RESOURCES}; do
        res=$(neutron ${TYPE}-delete "${ID}" || true)
    done
}

function openstack_list {
    local PROJECT=$1
    local TYPE=$2
    local result
    result=$(openstack --os-project-id ${PROJECT} ${TYPE} list -f value -c ID)
    echo "${result[@]}"
}

function openstack_delete {
    local TYPE=$1
    local RESOURCES=${@:2}
    for ID in ${RESOURCES}; do
        openstack ${TYPE} delete "${ID}";
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

function delete_trunks {
    local RESOURCES=("$@")
    openstack_delete "network trunk" "${RESOURCES[@]}"
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

function delete_all_trunks {
    local PROJECT=$1
    local list
    list=$(openstack_list ${PROJECT} "network trunk")
    delete_trunks "${list[@]}"
}

function erase_tenant {
    local PROJECT=$(get_project_id $1)
    delete_all_port_chains $PROJECT
    delete_all_port_pair_groups $PROJECT
    delete_all_port_pairs $PROJECT
    delete_all_flow_classifiers $PROJECT
    delete_all_vms $PROJECT
    delete_all_routers $PROJECT
    delete_all_trunks $PROJECT
    delete_all_ports $PROJECT
    delete_all_networks $PROJECT
}

function delete_all {
    set -o xtrace
    local NUM=${1}
    for ((i=1; i<=NUM; i++)); do
        local PROJECT="Scale-${i}"
        erase_tenant $PROJECT &
    done
    wait
    #set +o xtrace
    echo "SUCCESS"
}

function cleanup {
    if [ $# -lt 2 ] ; then
        echo "Cleanup accepts at least 1 argument"
        usage
        exit 1
    fi
    delete_all $*
}

function usage {
    echo "$0: [-h]"
    echo "  -h, --help                                 Display help message"
    echo "  -r, --router  NUMBER BASE                  Deploy router topology in NUMBER tenants"
    echo "  -c, --cleanup NUMBER                       Deletes *ALL* VMs and Networks from Project"

}

trap failed ERR
function failed {
    local r=$?
    set +o errtrace
    set +o xtrace
    set -e
    echo "Failed to execute"
    exit $r
}

function main {
    if [ $# -eq 0 ] ; then
        usage
    else

        while [ "$1" != "" ]; do
            case $1 in
                -h | --help )    usage
                                 exit
                                 ;;
                -r | --router )  router ${@:2}
                                 exit
                                 ;;
                -c | --cleanup ) delete_all ${@:2}
                                 exit
                                 ;;
                * )              usage
                                 exit 1
            esac
            shift
        done
    fi
}

main $*
