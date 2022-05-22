#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2022, Ashley Hooper <ashleyghooper@gmail.com>
# Copyright: (c) 2019, Jarett D. Chaiken <jdc@salientcg.com>
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

ANSIBLE_METADATA = {
    'metadata_version': '2.3.0',
    'status': ['preview'],
    'supported_by': 'community',
}

DOCUMENTATION = r'''
---
module: orion_node
short_description: Creates/Removes/Edits Nodes in Solarwinds Orion NPM
description:
    - Create/Remove/Edit Nodes in SolarWinds Orion NPM.
version_added: "2.7"
author: "Jarett D Chaiken (@jdchaiken)"
options:
    orion_hostname:
        description:
            - Name of Orion host running SWIS service
        required: true
    orion_username:
        description:
            - Orion Username
            - Active Directory users may use DOMAIN\\username or username@DOMAIN format
        required: true
    orion_password:
        description:
            - Password for Orion user
        required: true
    state:
        description:
            - The desired state of the node
        required: false
        choices:
            - present
            - absent
            - remanaged
            - unmanaged
            - muted
            - unmuted
        default:
            - remanaged
    node_id:
        description:
            - node_id of the node
            - One of 'node_id', 'node_name', or 'ip_address' must be provided
        required: false
    node_name:
        description:
            - FQDN of the node
            - For adding a node this field is required
            - For all other states field is optional
        required: false
    ip_address:
        description:
            - IP Address of the node
            - One of 'node_id', 'node_name', or 'ip_address' must be provided
        required: false
    unmanage_from:
        description:
            - "The date and time (in ISO 8601 UTC format) to begin the unmanage period."
            - If this is in the past, the node will be unmanaged effective immediately.
            - If not provided, module defaults to now.
            - "ex: 2017-02-21T12:00:00Z"
        required: false
    unmanage_until:
        description:
            - "The date and time (in ISO 8601 UTC format) to end the unmanage period."
            - You can set this as far in the future as you like.
            - If not provided, module defaults to 24 hours from now.
            - "ex: 2017-02-21T12:00:00Z"
        required: false
    polling_method:
        description:
            - Polling method to use
        choices:
            - external
            - icmp
            - snmp
            - wmi
            - agent
        default: snmp
        required: false
    polling_engine_name:
        description:
            - Name of polling engine to move the node to after successful discovery
        required: false
        type: str
    discovery_polling_engine_name:
        description:
            - Name of polling engine that NPM will use for discovery only (only required if different to polling_engine_name)
        required: false
        type: str
    snmp_version:
        description:
            - SNMPv2c is used by default
            - SNMPv3 requires use of existing, named SNMPv3 credentials within Orion
        choices:
            - 2c
            - 3
        default: 2c
        required: false
        type: int
    snmp_port:
        description:
            - port that SNMP server listens on
        required: false
        default: 161
        type: int
    snmp_allow_64:
        description:
            - Set true if device supports 64-bit counters
        type: bool
        default: true
        required: false
    credential_name:
        description:
            - The named, existing credential to use to manage this device
        required: true
        type: str
    interface_filters:
        description:
            - List of SolarWinds Orion interface discovery filters
        required: false
        type: list
    volume_filters:
        description:
            - List of regular expressions by which to exclude volumes from monitoring
        required: false
        type: list
    custom_properties:
        description:
            - A dictionary containing custom properties and their values
        required: false
        type: dict
requirements:
    - orionsdk
    - datetime
    - dateutil
    - requests
    - traceback
'''

EXAMPLES = r'''
- name: Remove nodes
  hosts: all
  gather_facts: no
  tasks:
    - name:  Remove a node from Orion
      orion_node:
        orion_hostname: "{{ solarwinds_host }}"
        orion_username: "{{ solarwinds_username }}"
        orion_password: "{{ solarwinds_password }}"
        node_name: servername
        state: absent
      delegate_to: localhost
      throttle: 1

- name: Mute nodes
  hosts: all
  gather_facts: no
  tasks:
    - orion_node:
        orion_hostname: "{{ solarwinds_host }}"
        orion_username: "{{ solarwinds_username }}"
        orion_password: "{{ solarwinds_password }}"
        node_name: "{{ inventory_hostname }}"
        state: muted
        unmanage_from: "2020-03-13T20:58:22.033"
        unmanage_until: "2020-03-14T20:58:22.033"
      delegate_to: localhost
      throttle: 1
'''

import traceback
from datetime import datetime, timedelta, timezone
from dateutil.parser import parse
import re
import requests
import time
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils._text import to_native

try:
    from orionsdk import SwisClient
    HAS_ORION = True
except Exception as e:
    HAS_ORION = False

__SWIS__ = None

# These constants control how many times and at what interval this module
# will check the status of the Orion discovery job to see if it has completed.
# Total time will be retries multiplied by sleep seconds.
DISCOVERY_STATUS_CHECK_RETRIES = 60
DISCOVERY_RETRY_SLEEP_SECS = 3
# These control the discovery timeouts within Orion itself.
ORION_DISCOVERY_JOB_TIMEOUT_SECS = 300
ORION_DISCOVERY_SEARCH_TIMEOUT_MS = 20000
ORION_DISCOVERY_SNMP_TIMEOUT_MS = 30000
ORION_DISCOVERY_SNMP_RETRIES = 2
ORION_DISCOVERY_REPEAT_INTERVAL_MS = 3000
ORION_DISCOVERY_WMI_RETRIES_COUNT = 2
ORION_DISCOVERY_WMI_RETRY_INTERVAL_MS = 2000

requests.urllib3.disable_warnings()


def run_module():
    '''
    Module Main Function
    '''
    global __SWIS__

    module_args = {
        'orion_hostname': {'required': True},
        'orion_username': {'required': True, 'no_log': True},
        'orion_password': {'required': True, 'no_log': True},
        'state': {
            'required': False,
            'choices': [
                'present',
                'absent',
                'remanaged',
                'unmanaged',
                'muted',
                'unmuted',
            ],
            'default': 'managed'
        },
        'node_id': {'required': False},
        'ip_address': {'required': False},
        'node_name': {'required': False},
        'unmanage_from': {'required': False, 'default': None},
        'unmanage_until': {'required': False, 'default': None},
        'polling_method': {
            'required': False,
            'choices': [
                'external',
                'icmp',
                'snmp',
                'wmi',
                'agent'
            ],
            'default': 'snmp'
        },
        'polling_engine_name': {'required': False},
        'discovery_polling_engine_name': {'required': False},
        'polling_engine_id': {'required': False},
        'snmp_version': {
            'required': False,
            'choices': [
                '2c',
                '3'
            ],
            'default': '2c'
        },
        'snmp_port': {'required': False, 'default': 161},
        'snmp_allow_64': {'required': False, 'default': True},
        'credential_name': {'required': False},
        'interface_filters': {'required': False, 'type': 'list', 'default': []},
        'volume_filters': {'required': False, 'type': 'list', 'default': []},
        'custom_properties': {'required': False, 'type': 'dict', 'default': {}}
    }

    module = AnsibleModule(argument_spec=module_args, supports_check_mode=True)

    if not HAS_ORION:
        module.fail_json(msg='orionsdk required for this module')

    options = {
        'hostname': module.params['orion_hostname'],
        'username': module.params['orion_username'],
        'password': module.params['orion_password'],
    }

    __SWIS__ = SwisClient(**options)

    try:
        __SWIS__.query('SELECT uri FROM Orion.Environment')
    except Exception as e:
        module.fail_json(
            msg='Failed to query Orion. '
                'Check Hostname, Username, and/or Password: {0}'.format(str(e))
            )

    if module.params['state'] == 'present':
        add_node(module)
    elif module.params['state'] == 'absent':
        remove_node(module)
    elif module.params['state'] == 'remanaged':
        remanage_node(module)
    elif module.params['state'] == 'unmanaged':
        unmanage_node(module)
    elif module.params['state'] == 'muted':
        mute_node(module)
    elif module.params['state'] == 'unmuted':
        unmute_node(module)

def _get_node_by_uri(node_uri):
    if node_uri is not None:
        return __SWIS__.read(node_uri)

def _get_node(module):
    node = {}
    if module.params['node_id'] is not None:
        results = __SWIS__.query(
            'SELECT NodeID, Caption, Unmanaged, UnManageFrom, UnManageUntil, Uri FROM Orion.Nodes WHERE NodeID = @node_id',
            node_id=module.params['node_id']
            )
    elif module.params['ip_address'] is not None:
        results = __SWIS__.query(
            'SELECT NodeID, Caption, Unmanaged, UnManageFrom, UnManageUntil, Uri FROM Orion.Nodes WHERE IPAddress = @ip_address',
            ip_address=module.params['ip_address']
        )
    elif module.params['node_name'] is not None:
        results = __SWIS__.query(
            'SELECT NodeID, Caption, Unmanaged, UnManageFrom, UnManageUntil, Uri FROM Orion.Nodes WHERE Caption = @node_name OR DNS = @node_name',
            node_name=module.params['node_name']
        )
    else:
        # No Id provided
        module.fail_json(msg='You must provide either node_id, ip_address, or node_name')

    if results['results']:
        node['nodeid'] = results['results'][0]['NodeID']
        node['caption'] = results['results'][0]['Caption']
        if 'DNS' in results['results'][0]:
            node['dnsname'] = results['results'][0]['DNS']
        node['netobjectid'] = 'N:{}'.format(node['nodeid'])
        node['unmanaged'] = results['results'][0]['Unmanaged']
        node['unmanagefrom'] = parse(results['results'][0]['UnManageFrom']).isoformat()
        node['unmanageuntil'] = parse(results['results'][0]['UnManageUntil']).isoformat()
        node['uri'] = results['results'][0]['Uri']
    return node

def _get_credential_id(module):
    credential_name = module.params['credential_name']
    try:
        credentials_res = __SWIS__.query("SELECT ID FROM Orion.Credential WHERE Name = @credential_name", credential_name = credential_name)
        try:
            return next(c for c in credentials_res['results'])['ID']
        except Exception as e:
            module.fail_json(msg='Failed to query credential {}'.format(str(e)))
    except Exception as e:
        module.fail_json(msg='Failed to query credentials {}'.format(str(e)))

def _get_polling_engine_id(module, polling_engine_name):
    try:
        engines_res = __SWIS__.query("SELECT EngineID, ServerName, PollingCompletion FROM Orion.Engines WHERE ServerName = @engine_name", engine_name = polling_engine_name)
        return next(e for e in engines_res['results'])['EngineID']
    except Exception as e:
        module.fail_json(msg='Failed to query polling engines {}'.format(str(e)))

def _validate_fields(module):
    params = module.params
    # Setup properties for new node
    # module.fail_json(msg='FAIL NOW', **params)
    props = {
        'IPAddress': params['ip_address'],
        'Caption': params['node_name'].split('.')[0],
        'ObjectSubType': params['polling_method'].upper(),
        'External': True if params['polling_method'] == 'external' else False,
    }

    if '.' in params['node_name']:
        props['DNS'] = params['node_name']

    # Validate required fields
    if not props['IPAddress']:
        module.fail_json(msg='IP Address is required')

    if not props['External']:
        if not props['Caption']:
            module.fail_json(msg='Node name is required')

    if not props['ObjectSubType']:
        module.fail_json(msg='Polling Method is required [external, snmp, icmp, wmi, agent]')
    elif props['ObjectSubType'] == 'SNMP':
        props['SNMPVersion'] = params['snmp_version']
        props['AgentPort'] = params['snmp_port']
        props['Allow64BitCounters'] = params['snmp_allow_64']
        if not 'SNMPVersion' in props:
            print("Defaulting to SNMPv2")
            props['SNMPVersion'] = '2'
        if not 'AgentPort' in props:
            print("Using default SNMP port")
            props['AgentPort'] = '161'
        if not 'Allow64BitCounters' in props:
            props['Allow64BitCounters'] = True
    elif props['ObjectSubType'] == 'EXTERNAL':
        props['ObjectSubType'] = 'ICMP'

    if not params['credential_name']:
        module.fail_json(msg='A credential name is required')

    if params['polling_engine_name']:
        props['EngineID'] = _get_polling_engine_id(module, params['polling_engine_name'])
    else:
        print("Using default initial polling engine")
        props['EngineID'] = 1

    if 'discovery_polling_engine_name' in params and params['discovery_polling_engine_name'] != params['polling_engine_name']:
        props['DiscoveryEngineID'] = _get_polling_engine_id(module, params['discovery_polling_engine_name'])
    else:
        props['DiscoveryEngineID'] = props['EngineID']

    if params['state'] == 'present':
        if not props['Caption']:
            module.fail_json(msg='Node name is required')

    return props

def add_node(module):
    changed = False
    # Check if node already exists and exit if found
    # TODO: add ability to update an existing node
    node = _get_node(module)
    if node:
        module.exit_json(changed=False, ansible_facts=node)

    # Validate Fields
    props = _validate_fields(module)
    # Start to prepare our discovery profile
    core_plugin_context = {
        'BulkList': [{'Address': module.params['ip_address']}],
        'Credentials': [
            {
                'CredentialID': _get_credential_id(module),
                'Order': 1
            }
        ],
        'WmiRetriesCount': ORION_DISCOVERY_WMI_RETRIES_COUNT,
        'WmiRetryIntervalMiliseconds': ORION_DISCOVERY_WMI_RETRY_INTERVAL_MS
}

    try:
        core_plugin_config = __SWIS__.invoke('Orion.Discovery', 'CreateCorePluginConfiguration', core_plugin_context)
    except Exception as e:
        module.fail_json(msg='Failed to create core plugin configuration {}'.format(str(e)), **props)

    # TODO: Make some or all of these 'default' filters optional
    expression_filters = [
        {"Prop": "Descr", "Op": "!Any", "Val": "null"},
        {"Prop": "Descr", "Op": "!Any", "Val": "vlan"},
        {"Prop": "Descr", "Op": "!Any", "Val": "loopback"},
        {"Prop": "Descr", "Op": "!Regex", "Val": "^$"},
    ]
    expression_filters += module.params['interface_filters']

    interfaces_plugin_context = {
        "AutoImportStatus": ['Up'],
        "AutoImportVlanPortTypes": ['Trunk', 'Access', 'Unknown'],
        "AutoImportVirtualTypes": ['Physical', 'Virtual', 'Unknown'],
        "AutoImportExpressionFilter": expression_filters
    }

    try:
        interfaces_plugin_config = __SWIS__.invoke('Orion.NPM.Interfaces', 'CreateInterfacesPluginConfiguration', interfaces_plugin_context)
    except Exception as e:
        module.fail_json(msg='Failed to create interfaces plugin configuration {}'.format(str(e)), **props)

    discovery_name = "orion_node.py.{}.{}".format(module.params['node_name'],datetime.now().isoformat())
    discovery_desc = "Automated discovery from orion_node.py Ansible module"
    discovery_profile = {
        'Name': discovery_name,
        'Description': discovery_desc,
        'EngineID': props['DiscoveryEngineID'],
        'JobTimeoutSeconds': ORION_DISCOVERY_JOB_TIMEOUT_SECS,
        'SearchTimeoutMiliseconds': ORION_DISCOVERY_SEARCH_TIMEOUT_MS,
        'SnmpTimeoutMiliseconds': ORION_DISCOVERY_SNMP_TIMEOUT_MS,
        'RepeatIntervalMiliseconds': ORION_DISCOVERY_REPEAT_INTERVAL_MS,
        'SnmpRetries': ORION_DISCOVERY_SNMP_RETRIES,
        'SnmpPort': module.params['snmp_port'],
        'HopCount': 0,
        'PreferredSnmpVersion': 'SNMP' + str(module.params['snmp_version']),
        'DisableIcmp': False,
        'AllowDuplicateNodes': False,
        'IsAutoImport': True,
        'IsHidden': False,
        'PluginConfigurations': [
            {'PluginConfigurationItem': core_plugin_config},
            {'PluginConfigurationItem': interfaces_plugin_config}
        ]
    }

    # Initiate discovery job with above discovery profile
    try:
        discovery_res = __SWIS__.invoke('Orion.Discovery', 'StartDiscovery', discovery_profile)
        changed = True
    except Exception as e:
        module.fail_json(msg='Failed to start node discovery: {}'.format(str(e)), **props)
    discovery_profile_id = int(discovery_res)

    # Loop until discovery job finished
    # Discovery job statuses are:
    # 0 {"Unknown"} 1 {"InProgress"} 2 {"Finished"} 3 {"Error"} 4 {"NotScheduled"} 5 {"Scheduled"} 6 {"NotCompleted"} 7 {"Canceling"} 8 {"ReadyForImport"}
    # https://github.com/solarwinds/OrionSDK/blob/master/Samples/PowerShell/DiscoverSnmpV3Node.ps1
    discovery_active = True
    discovery_iter = 0
    while discovery_active:
        try:
            status_res = __SWIS__.query("SELECT Status FROM Orion.DiscoveryProfiles WHERE ProfileID = @profile_id", profile_id=discovery_profile_id)
        except Exception as e:
            module.fail_json(msg='Failed to query node discovery status: {}'.format(str(e)), **props)
        if len(status_res['results']) > 0:
            if next(s for s in status_res['results'])['Status'] == 2:
                discovery_active = False
        else:
            discovery_active = False
        discovery_iter += 1
        if discovery_iter >= DISCOVERY_STATUS_CHECK_RETRIES:
            module.fail_json(msg='Timeout while waiting for node discovery job to terminate', **props)
        time.sleep(DISCOVERY_RETRY_SLEEP_SECS)

    # Retrieve Result and BatchID to find items added to new node by discovery
    try:
        discovery_log_res = __SWIS__.query("SELECT Result, ResultDescription, ErrorMessage, BatchID FROM Orion.DiscoveryLogs WHERE ProfileID = @profile_id", profile_id=discovery_profile_id)
    except Exception as e:
        module.fail_json(msg='Failed to query discovery logs: {}'.format(str(e)), **props)
    discovery_log = discovery_log_res['results'][0]

    # Any of the below values for Result indicate a failure, so we'll abort
    if int(discovery_log['Result']) in [0, 3, 6, 7]:
        module.fail_json(msg='Node discovery did not complete successfully: {}'.format(str(discovery_log_res)))

    # Look up NodeID of node we discovered. We have to do all of these joins
    # because mysteriously, the NodeID in the DiscoveredNodes table has no
    # bearing on the actual NodeID of the host(s) discovered.
    try:
        discovered_nodes_res = __SWIS__.query("SELECT n.NodeID, Caption, n.Uri FROM Orion.DiscoveryProfiles dp JOIN Orion.DiscoveredNodes dn ON dn.ProfileID = dp.ProfileID JOIN Orion.Nodes n ON n.DNS = dn.DNS OR n.Caption = dn.SysName WHERE dp.Name = @discovery_name", discovery_name=discovery_name)
    except Exception as e:
        module.fail_json(msg='Failed to query discovered nodes: {}'.format(str(e)), **props)

    try:
        discovered_node = discovered_nodes_res['results'][0]
    except Exception as e:
        module.fail_json(msg="Node '{}' not found in discovery results (got {}): {}".format(module.params['node_name'], discovered_nodes_res['results'], str(e)), **props)

    discovered_node_id = discovered_node['NodeID']
    # Check if we need to re-set the caption for the discovered node
    if discovered_node['Caption'] != props['Caption']:
        try:
            __SWIS__.update(discovered_node['Uri'], caption=module.params['node_name'])
        except Exception as e:
            module.fail_json(msg="Failed to update node Caption from '{}' to '{}': {}".format(discovered_node['Caption'], module.params['node_name'], str(e)), **props)

    # Retrieve all items added by discovery profile
    try:
        discovered_objects_res = __SWIS__.query("SELECT EntityType, DisplayName, NetObjectID FROM Orion.DiscoveryLogItems WHERE BatchID = @batch_id", batch_id=discovery_log['BatchID'])
    except Exception as e:
        module.fail_json(msg='Failed to query discovered objects: {}'.format(str(e)), **props)

    volumes_to_remove = []
    for entry in discovered_objects_res['results']:
        if entry['EntityType'] == 'Orion.Volumes':
            for vol_filter in module.params['volume_filters']:
                vol_filter_regex = "^{} - {}".format(module.params['node_name'], vol_filter['regex'])
                if re.search(vol_filter_regex, entry['DisplayName']):
                    volumes_to_remove.append(entry)
    if len(volumes_to_remove) > 50:
        module.fail_json(msg='Too many volumes to remove ({}) - aborting for safety'.format(str(len(volumes_to_remove))), **props)

    volume_removal_uris = []
    for volume in volumes_to_remove:
        try:
            volume_lookup_res = __SWIS__.query("SELECT Uri FROM Orion.Volumes WHERE NodeID = @node_id AND Concat('V:', ToString(VolumeID)) = @net_object_id", node_id=discovered_node_id, net_object_id=volume['NetObjectID'])
        except Exception as e:
            module.fail_json(msg='Failed to query Uri for volume to remove: {}'.format(str(e)), **props)

        volume_uri = volume_lookup_res['results'][0]['Uri']
        if volume_uri:
            try:
                __SWIS__.delete(volume_uri)
            except Exception as e:
                module.fail_json(msg='Failed to delete volume: {}'.format(str(e)), **props)

    # Set DNS name of the node
    if 'DNS' in props:
        dns_name_update = {
            "DNS": props['DNS']
        }
        try:
            __SWIS__.update(discovered_node['Uri'], **dns_name_update)
        except Exception as e:
            module.fail_json(msg="Failed to set DNS name '{}': {}".format(props['DNS'], str(e)),**node)

    if not props['External']:
        # Add Custom Properties
        custom_properties = module.params['custom_properties'] if 'custom_properties' in module.params else {}

        if type(custom_properties) is dict:
            for k in custom_properties.keys():
                custom_property = { k: custom_properties[k] }
                try:
                    __SWIS__.update(discovered_node['Uri'] + '/CustomProperties', **custom_property)
                    changed = True
                except Exception as e:
                    module.fail_json(msg='Failed to add custom properties: {}'.format(str(e)),**node)

    node['changed'] = changed
    module.exit_json(**node)

    # Here we can move nodes to other polling engines after discovery. For use
    # when discovery by the desired polling engine fails.
    if props['DiscoveryEngineID'] != props['EngineID']:
        engine_update = {
            "EngineID": props['EngineID']
        }
        try:
            __SWIS__.update(discovered_node['Uri'], **engine_update)
        except Exception as e:
            module.fail_json(msg="Failed to move node to final polling engine '{}': {}".format(module.params['polling_engine_name'], str(e)),**node)

    return discovered_node['Uri'], changed

def remove_node(module):
    node = _get_node(module)
    if not node:
        module.exit_json(changed=False)

    try:
        __SWIS__.delete(node['uri'])
        node['changed'] = True
        module.exit_json(**node)
    except Exception as e:
        module.fail_json(msg='Error removing node: {}'.format(str(e)), **node)

def remanage_node(module):
    node = _get_node(module)
    if not node:
        module.fail_json(skipped=True, msg='Node not found')
    elif not node['unmanaged']:
        module.fail_json(changed=False, msg='Node is not currently unmanaged')
    try:
        __SWIS__.invoke('Orion.Nodes', 'Remanage', node['netobjectid'])
        module.exit_json(changed=True, msg="{0} has been remanaged".format(node['caption']))
    except Exception as e:
        module.fail_json(msg=to_native(e), exception=traceback.format_exc())

def unmanage_node(module):
    node = _get_node(module)
    if not node:
        module.fail_json(skipped=True, msg='Node not found')

    now_dt = datetime.now(timezone.utc)
    unmanage_from = module.params['unmanage_from']
    unmanage_until = module.params['unmanage_until']

    if unmanage_from:
        unmanage_from_dt = datetime.fromisoformat(unmanage_from)
    else:
        unmanage_from_dt = now_dt
    if unmanage_until:
        unmanage_until_dt = datetime.fromisoformat(unmanage_until)
    else:
        tomorrow_dt = now_dt + timedelta(days=1)
        unmanage_until_dt = tomorrow_dt

    if node['unmanaged']:
        if unmanage_from_dt.isoformat() == node['unmanagefrom'] and unmanage_until_dt.isoformat() == node['unmanageuntil']:
            module.exit_json(changed=False)

    try:
        __SWIS__.invoke(
               'Orion.Nodes',
               'Unmanage',
               node['netobjectid'],
               str(unmanage_from_dt.astimezone(timezone.utc)).replace('+00:00', 'Z'),
               str(unmanage_until_dt.astimezone(timezone.utc)).replace('+00:00', 'Z'),
               False  # use Absolute Time
        )
        msg = "Node {0} will be unmanaged from {1} until {2}".format(
            node['caption'],
            unmanage_from_dt.astimezone().isoformat("T", "minutes"),
            unmanage_until_dt.astimezone().isoformat("T", "minutes")
        )
        module.exit_json(changed=True, msg=msg, ansible_facts=node)
    except Exception as e:
        module.fail_json(msg="Failed to unmanage {0}".format(node['caption']), ansible_facts=node)

def mute_node(module):
    node = _get_node(module)
    if not node:
        module.exit_json(skipped=True, msg='Node not found')

    now_dt = datetime.now(timezone.utc)
    unmanage_from = module.params['unmanage_from']
    unmanage_until = module.params['unmanage_until']

    if unmanage_from:
        unmanage_from_dt = datetime.fromisoformat(unmanage_from)
    else:
        unmanage_from_dt = now_dt
    if unmanage_until:
        unmanage_until_dt = datetime.fromisoformat(unmanage_until)
    else:
        tomorrow_dt = now_dt + timedelta(days=1)
        unmanage_until_dt = tomorrow_dt

    unmanage_from_dt = unmanage_from_dt.astimezone()
    unmanage_until_dt = unmanage_until_dt.astimezone()

    # Check if already muted
    suppressed = __SWIS__.invoke('Orion.AlertSuppression','GetAlertSuppressionState',[node['uri']])[0]

    # If already muted, exit
    if suppressed['SuppressedFrom'] == unmanage_from and suppressed['SuppressedUntil'] == unmanage_until:
        node['changed']=False
        module.exit_json(changed=False, ansible_facts=node)

    # Otherwise Mute Node with given parameters
    try:
        __SWIS__.invoke(
                'Orion.AlertSuppression',
                'SuppressAlerts',
                [node['uri']],
                str(unmanage_from_dt.astimezone(timezone.utc)).replace('+00:00', 'Z'),
                str(unmanage_until_dt.astimezone(timezone.utc)).replace('+00:00', 'Z')
        )
        msg = "Node {0} will be muted from {1} until {2}".format(
            node['caption'],
            unmanage_from_dt.astimezone().isoformat("T", "minutes"),
            unmanage_until_dt.astimezone().isoformat("T", "minutes")
        )
        module.exit_json(changed=True, msg=msg, ansible_facts=node)
    except Exception as e:
        module.fail_json(msg="Failed to mute {0}".format(node['caption']), ansible_facts=node)

def unmute_node(module):
    node = _get_node(module)
    if not node:
        module.exit_json(skipped=True, msg='Node not found')

    # Check if already muted
    suppressed = __SWIS__.invoke('Orion.AlertSuppression','GetAlertSuppressionState',[node['uri']])[0]

    if suppressed['SuppressionMode'] == 0:
        node['changed'] = False
        module.exit_json(changed=False, ansible_facts=node)
    else:
        __SWIS__.invoke('Orion.AlertSuppression', 'ResumeAlerts',[node['uri']])
        module.exit_json(changed=True, msg="{0} has been unmuted".format(node['caption']), ansible_facts=node)


def main():
    run_module()

if __name__ == "__main__":
    main()
