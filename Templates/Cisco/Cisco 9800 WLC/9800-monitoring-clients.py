#!/usr/bin/python3

# ---------------------------------------------------------------
# Python Script for monitoring client data on 9800 WLC for Zabbix Sender
# james@pylonone.com
# v1.2 22/Feb/23
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
    { 'host': '', 'username': '', 'password': '' }
]

# Output debugging data, will break piping output to zabbix sender
debug_output = False

# ------- End Configuration -------

# NetConf Payloads
accesspoint_payload = """
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
    <access-point-oper-data xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-wireless-access-point-oper">
        <capwap-data>
            <wtp-mac/>
            <ip-addr/>
            <name/>
            <tag-info>
                <policy-tag-info>
                  <policy-tag-name/>
                </policy-tag-info>
                <site-tag>
                  <site-tag-name/>
                </site-tag>
            </tag-info>
            <device-detail>
                <static-info>
                    <board-data>
                        <wtp-serial-num/>
                        <wtp-enet-mac/>
                    </board-data>
                    <ap-models>
                        <model/>
                    </ap-models>
                </static-info>
            </device-detail>
        </capwap-data>
    </access-point-oper-data>
</filter>
"""
clientdata_payload = """
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
    <client-oper-data xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-wireless-client-oper">
        <dot11-oper-data>
            <ms-mac-address/>
            <ap-mac-address/>
            <current-channel/>
            <vap-ssid/>
            <radio-type/>
            <ewlc-ms-phy-type/>
        </dot11-oper-data>
    </client-oper-data>
</filter>
"""
policytag_payload = """
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <wlan-cfg-data xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-wireless-wlan-cfg">
    <policy-list-entries>
      <policy-list-entry>
        <tag-name/>
        <wlan-policies>
          <wlan-policy>
            <wlan-profile-name/>
          </wlan-policy>
        </wlan-policies>
      </policy-list-entry>
    </policy-list-entries>
    <wlan-cfg-entries>
      <wlan-cfg-entry>
        <profile-name/>
        <apf-vap-id-data>
          <ssid/>
        </apf-vap-id-data>
      </wlan-cfg-entry>
    </wlan-cfg-entries>
  </wlan-cfg-data>
</filter>
"""

# Stderr Print Method
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

# Scrape Method
def scrape_controller(controller):
    try:
        # Response Data
        access_points = {}
        response_data = {
            'controller': controller['host'],
            'current_clients': 0,
            'clients_per_phy_type': {},
            'clients_per_ssid': {},
            'clients_per_policy': {},
            'clients_per_site': {},
            'clients_per_ap': {},
            'ssids': {},
            'aps': {}
        }

        # Connect to services
        m = manager.connect(host=controller['host'], port=830, username=controller['username'], device_params={'name': 'iosxr'},
            password=controller['password'], hostkey_verify=False, timeout=20)

        # Get Policy Data
        netconf_reply_policy = m.get(policytag_payload).xml
        netconf_dict_policy = xmltodict.parse(netconf_reply_policy)
        if netconf_dict_policy['rpc-reply']['data'] != None:
            policy_data = netconf_dict_policy['rpc-reply']['data']['wlan-cfg-data']['policy-list-entries']['policy-list-entry']
            wlan_data = netconf_dict_policy['rpc-reply']['data']['wlan-cfg-data']['wlan-cfg-entries']['wlan-cfg-entry']

            # Build a mapping for SSID -> Policy Tags
            if not isinstance(wlan_data, list):
                wlan_data = [wlan_data]

            for ssid in wlan_data:
                policies = []
                for profile in policy_data:
                    try:
                        # Fix case where a single policy tag results in the object not being a list
                        wlan_policies = profile['wlan-policies']['wlan-policy']
                        if not isinstance(wlan_policies, list):
                            wlan_policies = [profile['wlan-policies']['wlan-policy']]

                        # Zero set profile for output
                        if not profile['tag-name'] in response_data['clients_per_policy']:
                            response_data['clients_per_policy'][profile['tag-name']] = 0

                        # Loop over all policies in this tag
                        for sub_profile in wlan_policies:
                            if sub_profile['wlan-profile-name'] != ssid['profile-name']:
                               continue;
                            policies.append(profile['tag-name'])
                    except KeyError:
                        pass

                # Store the SSID -> Policy mapping
                response_data['clients_per_ssid'][ssid['apf-vap-id-data']['ssid']] = 0
                response_data['ssids'][ssid['apf-vap-id-data']['ssid']] = policies

        # Get AP Data
        netconf_reply_ap = m.get(accesspoint_payload).xml
        netconf_dict_ap = xmltodict.parse(netconf_reply_ap)
        if netconf_dict_ap['rpc-reply']['data'] != None:
            ap_data = netconf_dict_ap['rpc-reply']['data']['access-point-oper-data']['capwap-data']

            # Build data for each ap
            for i in range(0, len(ap_data)):
                this_ap = ap_data[i]
                response_data['clients_per_ap'][this_ap['name']] = 0
                access_points[this_ap['device-detail']['static-info']['board-data']['wtp-enet-mac']] = this_ap
                response_data['aps'][this_ap['name']] = this_ap['tag-info']['policy-tag-info']['policy-tag-name']

        # Get client data
        netconf_reply_client = m.get(clientdata_payload).xml
        netconf_dict_client = xmltodict.parse(netconf_reply_client)
        if netconf_dict_client['rpc-reply']['data'] != None:
            client_data = netconf_dict_client['rpc-reply']['data']['client-oper-data']['dot11-oper-data']

            # Set client count
            response_data['current_clients'] = len(client_data) + 1
            for i in range(0, len(client_data)):
                this_client = client_data[i]
                if this_client['radio-type'] == "dot11-radio-type-none":
                    continue

                # Clients by PHY Type
                try:
                    response_data['clients_per_phy_type'][this_client['ewlc-ms-phy-type']] = response_data['clients_per_phy_type'][this_client['ewlc-ms-phy-type']] + 1
                except KeyError:
                    response_data['clients_per_phy_type'][this_client['ewlc-ms-phy-type']] = 1

                # Clients by Access Point (Name or Mac Address)
                try:
                    if access_points[this_client['ap-mac-address']]:
                        access_point = access_points[this_client['ap-mac-address']]
                        response_data['clients_per_ap'][access_point['name']] = response_data['clients_per_ap'][access_point['name']] + 1
                    else:
                        response_data['clients_per_ap'][this_client['ap-mac-address']] = response_data['clients_per_ap'][this_client['ap-mac-address']] + 1
                except KeyError:
                    if access_points[this_client['ap-mac-address']]:
                        access_point = access_points[this_client['ap-mac-address']]
                        response_data['clients_per_ap'][access_point['name']] = 1
                    else:
                        response_data['clients_per_ap'][this_client['ap-mac-address']] = 1

                # Clients by SSID
                try:
                    response_data['clients_per_ssid'][this_client['vap-ssid']] = response_data['clients_per_ssid'][this_client['vap-ssid']] + 1
                except KeyError:
                    response_data['clients_per_ssid'][this_client['vap-ssid']] = 1

                # Clients by Site
                if access_points[this_client['ap-mac-address']]['tag-info']['site-tag']['site-tag-name']:
                    this_site_tag = access_points[this_client['ap-mac-address']]['tag-info']['site-tag']['site-tag-name']
                    try:
                        response_data['clients_per_site'][this_site_tag] = response_data['clients_per_site'][this_site_tag] + 1
                    except KeyError:
                        response_data['clients_per_site'][this_site_tag] = 1

                # Clients by Policy
                if access_points[this_client['ap-mac-address']]['tag-info']['policy-tag-info']['policy-tag-name']:
                    this_policy_tag = access_points[this_client['ap-mac-address']]['tag-info']['policy-tag-info']['policy-tag-name']
                    try:
                        response_data['clients_per_policy'][this_policy_tag] = response_data['clients_per_policy'][this_policy_tag] + 1
                    except KeyError:
                        response_data['clients_per_policy'][this_policy_tag] = 1

        # Return the controller data back to the parent thread
        return response_data

    except BaseException as err:
        eprint("Unable to connect to device {} - {}".format(controller['host'], err))
        return None

# Zabbix Print Methods
current_time = int(time.time())
def zabbix_sender_output(controller, typename, keyname, value):
    keyname = keyname.replace(' ', '-')
    print("{} {}[{}] {} {}".format(controller, typename, keyname, current_time, value))

def zabbix_sender_discovery(controller, typename, discovery_array):
    json_array = json.dumps(discovery_array, default=vars).replace("keyname", "{#KEYNAME}").replace("policytag", "{#POLICYTAGS}")
    print("{} wlan.{} {} {{\"data\":{}}}".format(controller, typename, current_time, json_array))

class DiscoveryObject:
    def __init__(self, value):
        self.keyname = value.replace(' ', '-')

class DiscoveryObjectWithTag:
    def __init__(self, value, policytag = None):
        self.keyname = value.replace(' ', '-')
        self.policytag = policytag

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
                    print("Total Clients: {}".format(response_data['current_clients']));
                    print("Clients by PHY Type:")
                    pp.pprint(response_data['clients_per_phy_type'])

                    print("Clients by AP:")
                    pp.pprint(response_data['clients_per_ap'])

                    print("Clients by SSID:")
                    pp.pprint(response_data['clients_per_ssid'])

                    print("Clients by Site-Tag:")
                    pp.pprint(response_data['clients_per_site'])

                    print("Clients by Policy-Tag:")
                    pp.pprint(response_data['clients_per_policy'])

                    print("Configured SSIDs:")
                    pp.pprint(response_data['ssids'])

                    print("AP Policy Tags:")
                    pp.pprint(response_data['aps'])
                    print("--------- END DEBUG OUTPUT - Retrieved Data from {} ---------".format(response_data['controller']))

                # Discovery Phase
                if debug_output:
                    print("-- Starting Discovery Phase --")

                phy_discovery = []
                for phy_type in response_data['clients_per_phy_type'].keys():
                    phy_discovery.append(DiscoveryObject(phy_type.replace('client-','').replace('-prot','')))
                zabbix_sender_discovery(response_data['controller'], "phy_type", phy_discovery)

                ap_discovery = []
                for ap_name in response_data['clients_per_ap'].keys():
                    ap_discovery.append(DiscoveryObjectWithTag(ap_name, response_data['aps'][ap_name]))
                zabbix_sender_discovery(response_data['controller'], "access_point", ap_discovery)

                ssid_discovery = []
                for ssid_name in response_data['clients_per_ssid'].keys():
                    ssid_discovery.append(DiscoveryObjectWithTag(ssid_name, ','.join(response_data['ssids'][ssid_name])))
                zabbix_sender_discovery(response_data['controller'], "ssid", ssid_discovery)

                sitetag_discovery = []
                for site_tag in response_data['clients_per_site'].keys():
                    sitetag_discovery.append(DiscoveryObject(site_tag))
                zabbix_sender_discovery(response_data['controller'], "site_tag", sitetag_discovery)

                policytag_discovery = []
                for policy_tag in response_data['clients_per_policy'].keys():
                    policytag_discovery.append(DiscoveryObject(policy_tag))
                zabbix_sender_discovery(response_data['controller'], "policy_tag", policytag_discovery)

                # Data Phase
                if debug_output:
                    print("-- Starting Data Phase --")

                for phy_type in response_data['clients_per_phy_type'].keys():
                    total =  response_data['clients_per_phy_type'][phy_type]
                    zabbix_sender_output(response_data['controller'], "wlan.phy_type.clients", phy_type.replace('client-','').replace('-prot',''), total)

                for ap_name in response_data['clients_per_ap'].keys():
                    total =  response_data['clients_per_ap'][ap_name]
                    zabbix_sender_output(response_data['controller'], "wlan.access_point.clients", ap_name, total)

                for ssid_name in response_data['clients_per_ssid'].keys():
                    total =  response_data['clients_per_ssid'][ssid_name]
                    zabbix_sender_output(response_data['controller'], "wlan.ssid.clients", ssid_name, total)

                for site_tag in response_data['clients_per_site'].keys():
                    total =  response_data['clients_per_site'][site_tag]
                    zabbix_sender_output(response_data['controller'], "wlan.site_tag.clients", site_tag, total)

                for policy_tag in response_data['clients_per_policy'].keys():
                    total =  response_data['clients_per_policy'][policy_tag]
                    zabbix_sender_output(response_data['controller'], "wlan.policy_tag.clients", policy_tag, total)

        # Return after all completed
        exit()

# Launch main method
if __name__ == '__main__':
    main()

