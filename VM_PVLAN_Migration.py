from pyvim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
from pyVmomi import vmodl
from pyvim.task import WaitForTask
import time
import getpass

# Color definition
BLINK = '\033[5m'
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
MAGENTA = '\033[95m'
CYAN = '\033[96m'
WHITE = '\033[97m'
RESET = '\033[0m' 


# Script function
print(f"{YELLOW}\n\nThis script is used to automate the migration of VMs to Private VLAN on the same VDS:")
print(f"{CYAN}Any selected VM will be parked in a Dummy VLAN, until the PVLAN construct is in place")
print("The VM(s) will then be moved either to the promiscuous or isolated VLAN (user decision)")
print("Ports connected to Standard Switches will be ignored")
print("There is a network outage of any selected VM")
print("There is no automated recovery process, once the script made any changes")
print(f"{YELLOW}\nMake sure you understand the risks before proceeding{RESET}")

# Disclaimer
print(f"{YELLOW}\n\nDISCLAIMER:")
print("###################################")
print("This script is provided 'as is' without any guarantees or warranty.")
print("The use of this script is at your own risk and you are fully responsible for any consequences resulting from its use.")
print("This script may affect the networking configuration of your VMs and cause downtime or loss of connectivity.")
print("Before running this script, please ensure you have a full understanding of it function.")
print("By proceeding with this script, you are acknowledging that you have read and understood this disclaimer.{RESET}")
print("###################################")

# Ask the user to accept the disclaimer
accept_disclaimer = input(f"{MAGENTA}\nDo you accept the disclaimer and acknowledge the risks? (yes/no): {RESET}").strip().lower()

if accept_disclaimer != 'yes':
    print(f"{RED}You did not accept the disclaimer. Exiting the script.{RESET}")
    exit()

# Replace these values with your vCenter details
host = input(f"{MAGENTA}Enter vCenter host: {RESET}")
user= input(f"{MAGENTA}Enter user name: {RESET}")
password = getpass.getpass()

# Amount of retries to move a VM from its original port-group to the dummy port-group
MAX_RETRIES = 3 

# Disabling SSL certificate verification if untrusted
confirm = input(f"{MAGENTA}\nIs a trusted certificate used on the vCenter? (yes/no): {RESET}").strip().lower()
if confirm == 'yes':
    print(f"   {GREEN}Continuing in verified TLS context{RESET}")
else:
    print(f"   {RED}Continuing in unverified TLS context{RESET}")
    ssl._create_default_https_context = ssl._create_unverified_context


# Connecting to the vCenter server
si = SmartConnect(host=host, user=user, pwd=password)
content = si.content

def get_all_vds_names(content):
    dv_switches = get_all_objects(content, [vim.DistributedVirtualSwitch])
    return [dvs.name for dvs in dv_switches]

def get_all_port_group_names(vds):
    return [pg.name for pg in vds.portgroup]

def get_all_objects(content, vimtypes):
    container = content.viewManager.CreateContainerView(content.rootFolder, vimtypes, True)
    objects = container.view
    container.Destroy()
    return objects

def list_vms_with_vnic_and_vlan(content, port_group_name):
    dv_switches = get_all_objects(content, [vim.DistributedVirtualSwitch])
    found_port_group = None
    print("\n")

    # Find the port group in all distributed virtual switches
    for dvs in dv_switches:
        for pg in dvs.portgroup:
            if pg.name == port_group_name:
                found_port_group = pg
                break
        if found_port_group:
            break

    if found_port_group is None:
        print(f"Port group {port_group_name} not found.")
        return

    # Retrieve VLAN ID
    vlan_id = found_port_group.config.defaultPortConfig.vlan.vlanId
    print(f"{CYAN}VLAN ID for port group {port_group_name}{RESET} --> {GREEN}{BLINK}{vlan_id}{RESET}")

    # Check all VMs connected to the found port group
    if not found_port_group.vm:
        print(f"No VMs found in port group {port_group_name}.")
    else:
        print(f"{CYAN}The following VM's are attached to {port_group_name}{RESET}")
        for vm in found_port_group.vm:
            print(f"{CYAN}VM Name: {vm.name}{RESET}")
            for device in vm.config.hardware.device:
                if isinstance(device, vim.vm.device.VirtualEthernetCard):
                    # Check if the Ethernet card is connected to a standard switch
                    if isinstance(device.backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                        print(f"{YELLOW}VM {vm.name} has a network interface connected to a Standard Switch. This adapter will not be touched. Interface: {RESET}{RED}{device.deviceInfo.label}{RESET}")
                    # Check if the Ethernet card is connected to the target port group
                    elif hasattr(device.backing, 'port') and device.backing.port.portgroupKey == found_port_group.key:
                        print(f"{CYAN}  vNIC Device: {device.deviceInfo.label} (MAC: {device.macAddress}){RESET}\n")
    print("\n")

def get_network_by_name(content, network_name):
    for datacenter in content.rootFolder.childEntity:
        for network in datacenter.network:
            if network.name == network_name:
                return network
    return None

def get_vlan_id(content, vds_name, port_group_name):
    vds = None
    dv_switches = get_all_objects(content, [vim.DistributedVirtualSwitch])
    for dvs in dv_switches:
        if dvs.name == vds_name:
            vds = dvs
            break

    if vds is None:
        print(f"{RED}Distributed Virtual Switch {vds_name} not found.{RESET}")
        return None

    port_group = None
    for pg in vds.portgroup:
        if pg.name == port_group_name:
            port_group = pg
            break

    if port_group is None:
        print(f"{RED}Port group {port_group_name} not found on VDS {vds_name}.{RESET}")
        return None

    # Retrieve VLAN ID
    vlan_id = port_group.config.defaultPortConfig.vlan.vlanId
    return vlan_id

def migrate_vms(content, vds_name, original_port_group_name, target_port_group_name, is_initial_migration=True):
    # Find the specified VDS
    vds = None
    dv_switches = get_all_objects(content, [vim.DistributedVirtualSwitch])
    for dvs in dv_switches:
        if dvs.name == vds_name:
            vds = dvs
            break

    if vds is None:
        print(f"Distributed Virtual Switch {vds_name} not found.")
        return

    original_network = None
    target_network = None

    # Search for the original and target port groups within the specified VDS
    for pg in vds.portgroup:
        if pg.name == original_port_group_name:
            original_network = pg
        if pg.name == target_port_group_name:
            target_network = pg

    if original_network is None or target_network is None:
        print("Original or target port group not found.")
        return

    vms = original_network.vm

    # Ask the user if they want to migrate all VMs at once
    migrate_all = input(f"{CYAN}Do you want to migrate VMs one by one or all at once? (single/all): {RESET}").strip().lower()

    if migrate_all == 'all':
        print(f"{CYAN}\nThe following VMs will be migrated:\n{RESET}")
        for vm in vms:
            print(f"{GREEN}{vm.name}{RESET}")
        confirm = input("Do you want to proceed? (yes/no): ").strip().lower()
        if confirm != 'yes':
            return

    for vm in vms:
        device_change = []
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                # Check if the Ethernet card is connected to a Standard Switch
                if isinstance(device.backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                    #print(f"{YELLOW}VM {vm.name} has a network interface connected to a Standard Switch. This adapter will not be touched. Interface: {RESET}{RED}{device.deviceInfo.label}{RESET}")#
                    continue

                # Check if the Ethernet card is connected to a distributed switch
                if isinstance(device.backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                    # Check if the Ethernet card is connected to the original network
                    if is_initial_migration and device.backing.port.portgroupKey != original_network.key:
                        continue
              
                # Create specification for device change
                nic_spec = vim.vm.device.VirtualDeviceSpec()
                nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
                nic_spec.device = device
                nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
                nic_spec.device.backing.port = vim.dvs.PortConnection()
                nic_spec.device.backing.port.portgroupKey = target_network.key
                nic_spec.device.backing.port.switchUuid = vds.uuid
                nic_spec.device.connectable = device.connectable  # Keep the same connectable settings
              
                # Add to device change list
                device_change.append(nic_spec)

        # Reconfigure VM only if there are device changes
        if device_change:
            # If the user chose to migrate all VMs at once, skip the confirmation
            if migrate_all == 'all' or input(f"Confirm reconfiguration of VM {vm.name}? ([yes]/no): ").strip().lower() in ['yes', '']:
                for _ in range(MAX_RETRIES):
                    try:
                        vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=device_change))
                        print(f"   {GREEN}Reconfigured VM {vm.name}{RESET}")
                        break
                    except Exception as e:
                        print(f"   {RED}Failed to reconfigure VM {vm.name}. Retrying...{RESET}")
                        time.sleep(RETRY_DELAY)
                else:
                    print(f"   {RED}Failed to reconfigure VM {vm.name} after {MAX_RETRIES} attempts.{RESET}")
            else:
                print(f"   {RED}Skipped reconfiguration of VM {vm.name}{RESET}")

    #print("\n")
    #print(f"Migrating VMs from port group {original_port_group_name} to {target_port_group_name} on VDS {vds_name}.")

def create_empty_port_group(content, vds):
    # Get all port group names in the chosen VDS
    port_group_names = get_all_port_group_names(vds)

    # Let the user choose a port group or create a new one
    print(f"{GREEN}\n\nPlease choose a dummy port group or type 'new' to create a new one:{RESET} ")
    for i, port_group_name in enumerate(port_group_names):
        print(f"{i+1}. {port_group_name}")
    port_group_choice = input(f"{MAGENTA}\nSelect an entry or type 'new': {RESET}")

    if port_group_choice.lower() == 'new':
        # Prompt the user to enter the name of the new port-group
        port_group_name = input(f"{CYAN}Please enter the name of the new port-group: {RESET}").strip() 

        # Prompt the user to enter the VLAN ID for the new port-group
        vlan_id = int(input(f"{CYAN}Please enter the VLAN ID for the new port-group: {RESET}").strip()) 

        # Create the new port-group
        portgroup_config_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        portgroup_config_spec.name = port_group_name
        portgroup_config_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
        portgroup_config_spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec()
        portgroup_config_spec.defaultPortConfig.vlan.vlanId = vlan_id
        portgroup_config_spec.type = "earlyBinding"
        task = vds.AddDVPortgroup_Task([portgroup_config_spec])
        WaitForTask(task)
        print(f"{GREEN}Port group {port_group_name} with VLAN ID {vlan_id} created. {RESET}")
    else:
        port_group_choice = int(port_group_choice) - 1
        port_group_name = port_group_names[port_group_choice]
        print(f"{GREEN}Using existing port group {port_group_name}.{RESET}")

    return port_group_name

def create_promiscuous_pvlan_map(vds, promiscuous_vlan):
    promiscuous_pvlan_map_entry = vim.dvs.VmwareDistributedVirtualSwitch.PvlanMapEntry()
    promiscuous_pvlan_map_entry.primaryVlanId = promiscuous_vlan
    promiscuous_pvlan_map_entry.secondaryVlanId = promiscuous_vlan
    promiscuous_pvlan_map_entry.pvlanType = 'promiscuous'

    promiscuous_pvlan_config_spec = vim.dvs.VmwareDistributedVirtualSwitch.PvlanConfigSpec()
    promiscuous_pvlan_config_spec.operation = 'add'
    promiscuous_pvlan_config_spec.pvlanEntry = promiscuous_pvlan_map_entry

    vds_config_spec = vim.dvs.VmwareDistributedVirtualSwitch.ConfigSpec()
    vds_config_spec.configVersion = vds.config.configVersion
    vds_config_spec.pvlanConfigSpec = [promiscuous_pvlan_config_spec]
    task = vds.ReconfigureDvs_Task(vds_config_spec)
    WaitForTask(task)

def create_isolated_pvlan_map(vds, promiscuous_vlan, isolated_vlan):
    isolated_pvlan_map_entry = vim.dvs.VmwareDistributedVirtualSwitch.PvlanMapEntry()
    isolated_pvlan_map_entry.primaryVlanId = promiscuous_vlan
    isolated_pvlan_map_entry.secondaryVlanId = isolated_vlan
    isolated_pvlan_map_entry.pvlanType = 'isolated'

    isolated_pvlan_config_spec = vim.dvs.VmwareDistributedVirtualSwitch.PvlanConfigSpec()
    isolated_pvlan_config_spec.operation = 'add'
    isolated_pvlan_config_spec.pvlanEntry = isolated_pvlan_map_entry

    vds_config_spec = vim.dvs.VmwareDistributedVirtualSwitch.ConfigSpec()
    vds_config_spec.configVersion = vds.config.configVersion
    vds_config_spec.pvlanConfigSpec = [isolated_pvlan_config_spec]
    task = vds.ReconfigureDvs_Task(vds_config_spec)
    WaitForTask(task)

def create_port_group_with_pvlan(content, vds_name, target_port_group_name, promiscuous_vlan, isolated_vlan):
    # Find the specified VDS
    vds = None
    dv_switches = get_all_objects(content, [vim.DistributedVirtualSwitch])

    for dvs in dv_switches:
        if dvs.name == vds_name:
            vds = dvs
            break

    if vds is None:
        print(f"{RED}Distributed Virtual Switch {vds_name} not found.{RESET}")
        return

    # Use custom Promiscuous VLAN ID if provided, else default to original VLAN ID + 1
    #promiscuous_vlan = custom_promiscuous_vlan_id if custom_promiscuous_vlan_id is not None else vlan_id + 1
    #isolated_vlan = vlan_id

    # Define the Promiscuous PVLAN map entry (Primary VLAN)
    create_promiscuous_pvlan_map(vds, promiscuous_vlan)
    create_isolated_pvlan_map(vds, promiscuous_vlan, isolated_vlan)

    # Create port group with isolated PVLAN
    isolated_portgroup_config_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
    isolated_portgroup_config_spec.name = port_group_name + "_isolated"
    isolated_portgroup_config_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
    isolated_portgroup_config_spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.PvlanSpec()
    isolated_portgroup_config_spec.defaultPortConfig.vlan.pvlanId = isolated_vlan
    isolated_portgroup_config_spec.type = "earlyBinding"

    # Create port group with promiscuous PVLAN
    promiscuous_portgroup_config_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
    promiscuous_portgroup_config_spec.name = port_group_name + "_promiscuous"
    promiscuous_portgroup_config_spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
    promiscuous_portgroup_config_spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.PvlanSpec()
    promiscuous_portgroup_config_spec.defaultPortConfig.vlan.pvlanId = promiscuous_vlan
    promiscuous_portgroup_config_spec.type = "earlyBinding"

    # Create both port groups on the VDS
    print(f"{GREEN}Port groups with PVLAN configuration created on VDS {vds_name}.{RESET}")
    task = vds.AddDVPortgroup_Task([isolated_portgroup_config_spec, promiscuous_portgroup_config_spec])
    WaitForTask(task)
    print(f"{GREEN}   Created {port_group_name}_promiscuous{RESET}") 
    print(f"{GREEN}   Created {port_group_name}_isolated{RESET}")
    print("\n")

def delete_port_group(content, vds_name, port_group_name):
    # Find the specified VDS
    print(f"{YELLOW}\nDeleting original Port group from VDS {RESET}")
    vds = None
    dv_switches = get_all_objects(content, [vim.DistributedVirtualSwitch])

    for dvs in dv_switches:
        if dvs.name == vds_name:
            vds = dvs
            break

    if vds is None:
        print(f"{RED}Distributed Virtual Switch {vds_name} not found.{RESET}")
        return

    # Find the specified port group
    port_group = None
    for pg in vds.portgroup:
        if pg.name == port_group_name:
            port_group = pg
            break

    if port_group is None:
        print(f"{GREEN}   Port group {port_group_name} successfully removed from VDS {vds_name}{RESET}")
        return

    # Delete the port group
    task = port_group.Destroy_Task()
    WaitForTask(task)  # Wait for the task to complete

    print(f"{GREEN}   Port group {port_group_name} deleted from VDS {vds_name}{RESET}")

print("\n")

# Get all VDS names
vds_names = get_all_vds_names(content)

# Let the user choose a VDS
print("Please choose a VDS:")
for i, vds_name in enumerate(vds_names):
    print(f"{i+1}. {vds_name}")
vds_choice = int(input(f"{MAGENTA}\nSelect an entry: {RESET}")) - 1
original_vds_name = vds_names[vds_choice]

# Retrieve the chosen VDS
vds = None
dv_switches = get_all_objects(content, [vim.DistributedVirtualSwitch])
for dvs in dv_switches:
    if dvs.name == original_vds_name:
        vds = dvs
        break

# Get all port group names in the chosen VDS
port_group_names = get_all_port_group_names(vds)

# Let the user choose a port group
print(f"\n\n{GREEN}Please choose the source port group{RESET}:")
for i, port_group_name in enumerate(port_group_names):
    print(f"{i+1}. {port_group_name}")
port_group_choice = int(input(f"\n{MAGENTA}Select an entry: {RESET}")) - 1
original_port_group_name = port_group_names[port_group_choice]

dummy_port_group_name = create_empty_port_group(content, vds)

list_vms_with_vnic_and_vlan(content, original_port_group_name)

# Determine the new port_group name
# This will default to the original port group name but two will be created
# one with _promiscuous and one with _isolated appended 
port_group_name_input = input(f"Please enter the new base name for the port group ({GREEN}{original_port_group_name}{RESET}): ")
    
if port_group_name_input.strip() == "":
    port_group_name = original_port_group_name
else:
    try:
        port_group_name = port_group_name_input
    except ValueError:
        print("Invalid name entered. Keeping existing port group name")
        port_group_name = port_group_name_input

print(f"{CYAN}  Script will create promiscuous Port Group name: {port_group_name}_promiscuous{RESET}")
print(f"{CYAN}  Script will create Isolated Port Group name:    {port_group_name}_isolated{RESET}")
print("\n")


#Get the original VLAN ID we are working with that was configured on the selected Port_Group.
vlan_id = get_vlan_id(content, original_vds_name, original_port_group_name)
if vlan_id is not None:
    print(f"The existing base VLAN ID for port group {original_port_group_name} is {GREEN}{vlan_id}{RESET}")
else:
    print(f"Failed to retrieve the VLAN ID for port group {original_port_group_name}.")

# Determine the promiscuous VLAN ID that will be used. 
# This will default to the original non PVLAN ID that was used for the existing Port_Group.
promiscuous_vlan_input = input(f"{CYAN}Please enter the promiscuous VLAN number ({GREEN}{vlan_id}{RESET}): {RESET}") 
if promiscuous_vlan_input.strip() == "":
    promiscuous_vlan_id = vlan_id
else:
    try:
        promiscuous_vlan_id = int(promiscuous_vlan_input)
    except ValueError:
        print("Invalid VLAN number entered. Using the default VLAN ID.")
        promiscuous_vlan_id = vlan_id
print(f"   {CYAN}Using Promiscuous VLAN ID: {promiscuous_vlan_id}{RESET}")
print("\n")


# Determine the isolated VLAN ID for workloads.
# If nothing is selected then the default will be the original VLAN ID + 1 
isolated_vlan_input = input(f"{CYAN}Please enter the Isolated VLAN number ({GREEN}{vlan_id + 1}{RESET}): {RESET}")
if isolated_vlan_input.strip() == "":
    isolated_vlan_id = vlan_id + 1 
else:
    try:
        isolated_vlan_id = int(isolated_vlan_input)
    except ValueError:
        print("Invalid VLAN number entered. Using the default VLAN ID.")
        isolated_vlan_id = vlan_id + 1

print(f"   {CYAN}Using Isolated VLAN ID: {isolated_vlan_id}{RESET}")

# Migrate VMs to Dummy Port Group
print(f"{YELLOW}\nMigrating VM NIC's from original Port-Group to {dummy_port_group_name}\n{RESET}")
migrate_vms(content, original_vds_name, original_port_group_name, dummy_port_group_name)

# Wait for a bit to ensure that all migrations are complete
time.sleep(5)


# Validate that no VMs are on the original port group
original_network = get_network_by_name(content, original_port_group_name)
if original_network and not original_network.vm:
    delete_port_group(content, original_vds_name, original_port_group_name)
else:
    print(f"Failed to migrate all VMs from port group {original_port_group_name}. Cannot delete.")

# Delete Original Port Group
delete_port_group(content, original_vds_name, original_port_group_name)

# Create New Port Group with PVLAN
create_port_group_with_pvlan(content, original_vds_name, port_group_name, promiscuous_vlan_id, isolated_vlan_id)

# Migrate VMs to the New Port Group
# Prompt the user to choose between promiscuous or isolated port group
migration_choice = input(f"{CYAN}Do you want to migrate all VMs to the 'promiscuous' or 'isolated' port group? Enter 'promiscuous' or 'p', 'isolated' or 'i': {RESET}")

# Map user's choice to the corresponding option
migration_choice_map = {
    'promiscuous': '_promiscuous',
    'p': '_promiscuous',
    'isolated': '_isolated',
    'i': '_isolated'
}

# Retrieve the final choice based on user's input
final_migration_choice = migration_choice_map.get(migration_choice.lower())

if final_migration_choice is None:
    print("Invalid choice. Please enter 'promiscuous' or 'p', 'isolated' or 'i'.")
else:
    final_target_port_group_name = port_group_name + final_migration_choice

    # Migrate VMs to the chosen port group
    print(f"{GREEN}Migrating VM NIC's from {dummy_port_group_name} to {final_target_port_group_name}{RESET}")
    migrate_vms(content, original_vds_name, dummy_port_group_name, final_target_port_group_name)
    print(f"{GREEN}\nVMs successfully migrated to {final_target_port_group_name}{RESET}")

Disconnect(si)