#!/usr/bin/python3

# ---------------------------------------------------------------
# Python Script for monitoring controller data on 9800 WLC for Zabbix Sender
# james@pylonone.com
# v1.0 14/Jan/24
# ---------------------------------------------------------------

from datetime import datetime,timezone
from ncclient import manager
import concurrent.futures
import xmltodict
import pprint
import time
import json
import sys

# ------- Configuration -------

# List of WLCs to monitor
controllers = [
    { 'host': '', 'username': '', 'password': '', 'wncd': 5 }
]

# Output debugging data, will break piping output to zabbix sender
debug_output = False

# ------- End Configuration -------

# NetConf Payloads
wncddata_payload = """
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <cisco-platform-software xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-platform-software-oper">
    <system-usages>
      <system-usage>
        <chassis>%CHASSIS%</chassis>
        <process-system-usages>
          <process-system-usage>
            <pid/>
            <name>wncd_%PROC%</name>
            <five-seconds/>
            <allocated-memory-percent/>
          </process-system-usage>
        </process-system-usages>
      </system-usage>
    </system-usages>
  </cisco-platform-software>
</filter>
"""

redundata_payload = """
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <cisco-platform-software xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-platform-software-oper">
    <control-processes>
      <control-process>
        <chassis/>
        <chassis-state/>
      </control-process>
    </control-processes>
  </cisco-platform-software>
</filter>
"""

haredund_payload = """
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <ha-oper-data xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ha-oper">
    <ha-infra>
      <ha-state/>
      <peer-state/>
      <last-switchover-time/>
      <last-switchover-reason/>
      <image-version/>
      <leaf-mode/>
      <ha-enabled/>
      <has-switchover-occured/>
    </ha-infra>
  </ha-oper-data>
</filter>
"""

# Stderr Print Method
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

# Scrape Method
def scrape_controller(controller):
    try:
        # Response Data
        response_data = {
            'controller': controller['host'],
            'primary_controller': -1,
            'wncd_process': [],
            'ha_state': {}
        }

        # Connect to services
        m = manager.connect(host=controller['host'], port=830, username=controller['username'], device_params={'name': 'iosxr'},
            password=controller['password'], hostkey_verify=False, timeout=5)

        # Determine the active controller and SSO state
        netconf_reply_redundancy = m.get(redundata_payload).xml
        netconf_dict_redundancy = xmltodict.parse(netconf_reply_redundancy)
        if netconf_dict_redundancy['rpc-reply']['data'] != None:
            chassis_data = netconf_dict_redundancy['rpc-reply']['data']['cisco-platform-software']['control-processes']['control-process']
            if not isinstance(chassis_data, list):
                chassis_data = [chassis_data]

            # Build data for each chassis
            for i in range(0, len(chassis_data)):
                this_chassis = chassis_data[i]
                if this_chassis['chassis-state'] == "ha-role-active":
                    response_data['primary_controller'] = int(this_chassis['chassis'])
                    pass

        # Check we have a chassis ID
        if response_data['primary_controller'] == -1:
            eprint("Unable to determine active chassis {} - {}".format(controller['host'], netconf_dict_redundancy))
            return None

        # Get wncd Process Data
        for process_id in range(0, controller['wncd']):
            # Replace the XML with the variables
            replaced_xml = wncddata_payload.replace('%CHASSIS%', str(response_data['primary_controller']))
            replaced_xml = replaced_xml.replace('%PROC%', str(process_id))

            netconf_reply_process = m.get(replaced_xml).xml
            netconf_dict_process = xmltodict.parse(netconf_reply_process)
            if netconf_dict_process['rpc-reply']['data'] != None:
                process_data = netconf_dict_process['rpc-reply']['data']['cisco-platform-software']['system-usages']['system-usage']
                if 'process-system-usages' not in process_data:
                    response_data['wncd_process'].append({ 'allocated-memory-percent': '0', 'five-seconds': '0', 'name': 'wncd_{}'.format(process_id), 'pid': '0' })
                    continue

                response_data['wncd_process'].append(process_data['process-system-usages']['process-system-usage'])

        # Get HA Data
        netconf_reply_ha = m.get(haredund_payload).xml
        netconf_dict_ha = xmltodict.parse(netconf_reply_ha)
        if netconf_dict_ha['rpc-reply']['data'] != None:
            response_data['ha_state'] = netconf_dict_ha['rpc-reply']['data']['ha-oper-data']['ha-infra']

        # Return the controller data back to the parent thread
        return response_data

    except BaseException as err:
        eprint("Unable to connect to device {} - {}".format(controller['host'], err))
        return None

# Zabbix Print Methods
current_time = int(time.time())
def zabbix_sender_output_array(controller, typename, keyname, value):
    keyname = keyname.replace(' ', '-')
    print("{} {}[{}] {} {}".format(controller, typename, keyname, current_time, value))

def zabbix_sender_output(controller, typename, value):
    print("{} {} {} {}".format(controller, typename, current_time, value))

def zabbix_sender_discovery(controller, typename, discovery_array):
    json_array = json.dumps(discovery_array, default=vars).replace("keyname", "{#KEYNAME}").replace("policytag", "{#POLICYTAGS}")
    print("{} {} {} {{\"data\":{}}}".format(controller, typename, current_time, json_array))

class DiscoveryObject:
    def __init__(self, value):
        self.keyname = value.replace(' ', '-')

# Main Method
def main():

    # Loop over radios in parallel
    current_time = int(time.time())
    pp = pprint.PrettyPrinter(indent=4)
    with concurrent.futures.ProcessPoolExecutor() as executor:

        # Execute each task in parallel
        process_pool = [executor.submit(scrape_controller, host) for host in controllers]

        # Loop over the results and process
        for proc in concurrent.futures.as_completed(process_pool):
            response_data = proc.result()
            if response_data != None:

                # Output data to console
                if debug_output:
                    print("--------- DEBUG OUTPUT - Retrieved Data from {} ---------".format(response_data['controller']))
                    print("Primary Controller Chassis ID: {}".format(response_data['primary_controller']));

                    # WNCD Monitoring
                    print("wNCD Process Data:")
                    pp.pprint(response_data['wncd_process'])

                    # HA State
                    print("HA Data:")
                    pp.pprint(response_data['ha_state'])

                # Discovery Phase
                if debug_output:
                    print("-- Starting Discovery Phase --")

                # wncd Process Discovery
                process_discovery = []
                for process in response_data['wncd_process']:
                    process_discovery.append(DiscoveryObject(process['name']))
                zabbix_sender_discovery(response_data['controller'], "wlc.wncd_process", process_discovery)

                # Data Phase
                if debug_output:
                    print("-- Starting Data Phase --")

                # wmcd Process Data
                for process in response_data['wncd_process']:
                    zabbix_sender_output_array(response_data['controller'], "wlc.wncd_process.memory", process['name'], process['allocated-memory-percent'])
                    zabbix_sender_output_array(response_data['controller'], "wlc.wncd_process.cpu", process['name'], process['five-seconds'])
                    zabbix_sender_output_array(response_data['controller'], "wlc.wncd_process.pid", process['name'], process['pid'])

                # Redundancy Data
                ha_data = response_data['ha_state']
                if ha_data['ha-enabled'] == 'true':
                    zabbix_sender_output(response_data['controller'], "wlc.ha.enabled", 1)
                else:
                    zabbix_sender_output(response_data['controller'], "wlc.ha.enabled", 0)
                if ha_data['has-switchover-occured'] == 'true':
                    zabbix_sender_output(response_data['controller'], "wlc.ha.has_switchover_occured", 1)
                else:
                    zabbix_sender_output(response_data['controller'], "wlc.ha.has_switchover_occured", 0)
                zabbix_sender_output(response_data['controller'], "wlc.ha.primary_slot", response_data['primary_controller'])  
                zabbix_sender_output(response_data['controller'], "wlc.ha.primary_state", '"{}"'.format(ha_data['ha-state']))
                zabbix_sender_output(response_data['controller'], "wlc.ha.secondary_state", '"{}"'.format(ha_data['peer-state']))
                zabbix_sender_output(response_data['controller'], "wlc.ha.last_switchover_reason", '"{}"'.format(ha_data['last-switchover-reason']))
                zabbix_sender_output(response_data['controller'], "wlc.ha.mode", '"{}"'.format(ha_data['leaf-mode']))

                # Try to parse failover time
                try:
                    parse_date_failover = datetime.strptime(ha_data['last-switchover-time'], '%Y-%m-%dT%H:%M:%S%z')
                    failover_time = int(parse_date_failover.replace(tzinfo=timezone.utc).timestamp())
                    zabbix_sender_output(response_data['controller'], "wlc.ha.last_switchover", failover_time)

                except ValueError as err:
                    zabbix_sender_output(response_data['controller'], "wlc.ha.last_switchover", 0)
                    if debug_output:
                        print("Unable to convert last-switchover-time to a timestamp - {}",format(err))

        # Return after all completed
        exit()

# Launch main method
if __name__ == '__main__':
    main()

