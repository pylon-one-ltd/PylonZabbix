#!/usr/bin/python3

# ---------------------------------------------------------------
# Python Script for monitoring AP Up/Down events on 9800 WLC
# james@pylonone.com
# v1.3 22/Feb/23
# ---------------------------------------------------------------

from datetime import datetime,timezone
from ncclient import manager
import concurrent.futures
import xmltodict
import psycopg2
import time
import sys
import pprint

# ------- Configuration -------

# List of WLCs to monitor
controllers = [
    { 'host': '', 'username': '', 'password': '' }
]

# Postgres configuration
db_database = 'ap_monitoring'
db_hostname = ''
db_password = ''
db_user = 'ap_monitoring'

# ------- End Configuration -------

# NetConf Payloads
accesspoint_payload = """
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
    <access-point-oper-data xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-wireless-access-point-oper">
        <capwap-data>
            <wtp-mac/>
            <name/>
            <tag-info>
                <policy-tag-info-info>
                    <policy-tag-info-name/>
                </policy-tag-info-info>
            </tag-info>
            <ap-time-info>
                <boot-time/>
                <join-time/>
            </ap-time-info>
            <reboot-stats>
                <reboot-reason/>
            </reboot-stats>
        </capwap-data>
    </access-point-oper-data>
</filter>
"""

# Scrape Method
def scrape_controller(controller):
    try:
        # Open NetConf connection to controller
        netconf_mgr = manager.connect(host=controller['host'], port=830, username=controller['username'], device_params={'name': 'iosxr'},
            password=controller['password'], hostkey_verify=False, timeout=5)

        # Get Local Data
        netconf_reply_local = netconf_mgr.get(accesspoint_payload).xml
        netconf_dict_local = xmltodict.parse(netconf_reply_local)

        # Return radio data
        if netconf_dict_local['rpc-reply']['data'] is None:
            print("No ap data returned for device {} via netconf".format(controller['host']))
            return (controller['host'], [])
        else:
            print("Retrieved ap data for device {} via netconf".format(controller['host']))
            return (controller['host'], netconf_dict_local['rpc-reply']['data']['access-point-oper-data']['capwap-data'])

    except BaseException as err:
        print("Unable to connect to device {} - {}".format(controller['host'], err))
        return None

# Use the Database data and the response object to detect offline access points
def locate_down_aps(db, response_data):
    ctrl_ip = response_data[0]
    db_cur = db.cursor()
    aps_controller = {}
    aps_database = {}

    # Get list of online access points in controller and database
    print("Processing down APs for controller {} - {} APs currently online".format(ctrl_ip, len(response_data[1])))

    # Fix case where a single policy tag results in the object not being a list
    access_points = response_data[1]
    if not isinstance(access_points, list):
        access_points = [response_data[1]]

    for ap in access_points:
        aps_controller[ap['wtp-mac']] = ap

    db_cur.execute("SELECT * FROM public.\"ap_status\" WHERE controller = %s", (ctrl_ip,))
    for data in db_cur.fetchall():
        aps_database[data[0]] = data

    try:
        # Compare the dicts to confirm what is up/down
        for i in aps_database:
            data = aps_database[i]
            try:
                # Parse the timestamps into a unix timestamp
                this_ap = aps_controller[data[0]]
                capwap_time = 0
                boot_time = 0
                try:
                    parse_date_capwap = datetime.strptime(this_ap['ap-time-info']['join-time'], '%Y-%m-%dT%H:%M:%S.%f%z')
                    parse_date_boot = datetime.strptime(this_ap['ap-time-info']['boot-time'], '%Y-%m-%dT%H:%M:%S%z')
                    capwap_time = int(parse_date_capwap.replace(tzinfo=timezone.utc).timestamp())
                    boot_time = int(parse_date_boot.replace(tzinfo=timezone.utc).timestamp())

                except ValueError as err:
                    print("Unable to convert ap-time-info to a timestamp - {}",format(err))

                # Check if AP has changed name
                if data[2] != this_ap['name']:
                    # Update the AP name in the database if it has changed
                    print("Access Point {} has been renamed from {} to {}".format(data[0], data[2], this_ap['name']))
                    db_cur.execute("UPDATE public.ap_status SET ap_name = %s WHERE ap_mac = %s", (this_ap['name'], data[0]))

                # Check if AP has changed controllers
                if data[1] != ctrl_ip:
                    # Update the AP controller in the database if it has changed
                    print("Access Point {} has moved from controller {} to controller {}".format(data[0], data[1], ctrl_ip))
                    db_cur.execute("UPDATE public.ap_status SET controller = %s WHERE ap_mac = %s", (ctrl_ip, data[0]))

                # Check if AP has changed policy tag
                if data[3] != this_ap['tag-info']['policy-tag-info']['policy-tag-name']:
                    # Update the AP policy tag in the database if it has changed
                    print("Access Point {} has changed policy tag from {} to {}".format(data[0], data[3], this_ap['tag-info']['policy-tag-info']['policy-tag-name']))
                    db_cur.execute("UPDATE public.ap_status SET policy_tag = %s WHERE ap_mac = %s", (this_ap['tag-info']['policy-tag-info']['policy-tag-name'], data[0]))

                # Check if ap boot/join timestamps do not match our database values
                if capwap_time != data[5] or boot_time != data[4]:
                    # Update the Boot/Join time
                    print("Access Point {} has changed boot/join timestamp".format(data[0],))
                    db_cur.execute("UPDATE public.ap_status SET ap_uptime = %s, capwap_uptime = %s WHERE ap_mac = %s", (boot_time, capwap_time, data[0]))

                # Check if the AP is marked offline in db
                if data[8] == 0:
                    # Mark AP as online
                    print("Access Point {} online in controller but offline in database - Marking as online".format(this_ap['name']))
                    db_cur.execute("UPDATE public.ap_status SET state = 1, policy_tag = %s, ap_uptime = %s, capwap_uptime = %s, offline_time = NULL, reboot_reason = %s, controller = %s WHERE ap_mac = %s",
                        (this_ap['tag-info']['policy-tag-info']['policy-tag-name'], boot_time, capwap_time, this_ap['reboot-stats']['reboot-reason'], ctrl_ip, data[0]))

            # AP no longer on the controller, so it is offline
            except KeyError:
                if data[8] == 1:
                    # Mark AP as offline if it is currently online in the database
                    print("Access Point {} offline in controller but online in database - Marking as offline".format(data[2]))
                    db_cur.execute("UPDATE public.ap_status SET state = 0, ap_uptime = 0, capwap_uptime = 0, offline_time = %s WHERE ap_mac = %s", (int(time.time()), data[0]))

        # Handle any aps that exist only in aps_controller as these are new
        for i in aps_controller:
            this_ap = aps_controller[i]
            try:
                # Check if this AP exists in the database, if it does, do nothing
                this_ap_database = aps_database[this_ap['wtp-mac']]

            except KeyError:
                # Parse the timestamps into a unix timestamp
                capwap_time = 0
                boot_time = 0
                try:
                    parse_date_capwap = datetime.strptime(this_ap['ap-time-info']['join-time'], '%Y-%m-%dT%H:%M:%S.%f%z')
                    parse_date_boot = datetime.strptime(this_ap['ap-time-info']['boot-time'], '%Y-%m-%dT%H:%M:%S%z')
                    capwap_time = int(parse_date_capwap.replace(tzinfo=timezone.utc).timestamp())
                    boot_time = int(parse_date_boot.replace(tzinfo=timezone.utc).timestamp())

                except ValueError as err:
                    print("Unable to convert ap-time-info to a timestamp - {}",format(err))

                # Add AP to Database
                print("New access point {} on controller {} added to monitoring".format(this_ap['name'], ctrl_ip))
                db_cur.execute("INSERT INTO public.ap_status (ap_mac, controller, ap_name, policy_tag, ap_uptime, capwap_uptime, reboot_reason, state) VALUES (%(ap_mac)s,%(controller)s,%(ap_name)s,%(policy_tag)s,%(ap_uptime)s,%(capwap_uptime)s,%(reboot_reason)s,1) ON CONFLICT (ap_mac) DO UPDATE SET controller = %(controller)s",
                    ({'ap_mac': this_ap['wtp-mac'], 'controller': ctrl_ip, 'ap_name': this_ap['name'], 'policy_tag': this_ap['tag-info']['policy-tag-info']['policy-tag-name'], 'ap_uptime': boot_time, 'capwap_uptime': capwap_time, 'reboot_reason': this_ap['reboot-stats']['reboot-reason']}))


    except BaseException as err:
        print("Error while processing new APs - {}".format(err))
        db_cur.close()

    # Commit database changes
    db.commit()
    db_cur.close()

# Main Method
def main():

    print("--- Script start at {}".format(datetime.now().isoformat()))
    
    # Connect to services
    db = psycopg2.connect(host=db_hostname, database=db_database, user=db_user,
        password=db_password)

    # Loop over radios in parallel
    with concurrent.futures.ProcessPoolExecutor() as executor:

        # Execute each task in parallel
        process_pool = [executor.submit(scrape_controller, host) for host in controllers]

        # Loop over the results and process
        for proc in concurrent.futures.as_completed(process_pool):
            response_data = proc.result()
            if response_data != None:
                locate_down_aps(db, response_data)

    # Close DB Connection
    db.close()

    print("--- Script end at {}".format(datetime.now().isoformat()))

# Launch main method
if __name__ == '__main__':
    main()
