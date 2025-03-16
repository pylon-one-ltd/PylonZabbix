#!/usr/bin/env python3
from datetime import datetime, timezone
from zabbix_utils import ZabbixAPI
import pprint
import json
import time

########### User Configuration ###########

# Config
host_age_in_minutes = 2880

# Zabbix Config
zabbix_host = ""
zabbix_token = ""
zabbix_proxies_to_cleanup = [
    ""
]

########### Script Start ###########

print("--- Script start at {}".format(datetime.now().isoformat()))

# Calculate expired timestamp
current_stamp = int(datetime.now(timezone.utc).timestamp())
hosts_older_than = current_stamp - (60*host_age_in_minutes)

# Setup zabbix client
zabbix_api = ZabbixAPI(url=zabbix_host)
zabbix_api.login(token=zabbix_token)

## Methods ##

# Get the IDs of all the proxies
def get_proxyids_from_zabbix():
    proxies = zabbix_api.proxy.get(output=["proxyid","name","lastaccess","state"], filter={"state": [1]})
    if len(proxies) == 0:
        print("No offline proxies returned from Zabbix")
        return []
    return proxies

# Get all templates that start with AD
def get_templateids_from_zabbix():
    templates = zabbix_api.template.get(output=["templateid"], search={"name": ["AD*"]}, searchWildcardsEnabled=True)
    if len(templates) == 0:
        print("No AD templates returned from Zabbix")
        return []

    templateids = []
    for template in templates:
        templateids.append(int(template["templateid"]))
    return templateids

# Get all editable hosts that are mapped to this proxy and are in one of the supplied templates
def get_hosts_for_proxy_from_zabbix(proxyid, templateids):
    if proxyid == 0:
        raise ValueError("Please provide a valid proxyid")
    if len(templateids) == 0:
        raise ValueError("Please provide valid templateids")

    hosts = zabbix_api.host.get(output=["hostid","host"], editable=True, templateids=templateids, filter={"proxyid": [proxyid]})
    if len(hosts) == 0:
        print("No editable hosts returned from Zabbix")
        return []

    hostids = []
    for host in hosts:
        print("Discovered host %s (%s)" % (host['host'], host['hostid']))
        hostids.append(int(host["hostid"]))
    return hostids


## Main loop ##

# Get Autodiscovery template IDs
autodiscovery_templateids = get_templateids_from_zabbix()
print("Found AutoDiscovery templates with IDs:")
pprint.pprint(autodiscovery_templateids)

# Iterate over the list of channels to process
proxy_list = get_proxyids_from_zabbix()
for proxy in proxy_list:

    # Check if we need to process this proxy
    if not proxy['name'] or not proxy['name'] in zabbix_proxies_to_cleanup:
        print("Skipping proxy %s (%s) - Excluded" % (proxy['name'], proxy['proxyid']))
        continue

    # Confirm the proxy is in the correct state for processing
    last_access_int = int(proxy['lastaccess'])
    if proxy['state'] != "1" or last_access_int > hosts_older_than:
        print("Skipping proxy %s (%s) - Online or not reached expiry deadline (%d > %d)" % (proxy['name'], proxy['proxyid'], last_access_int, hosts_older_than))
        continue   

    # Process hosts in this proxy
    print("Processing hosts under proxy %s (%s)" % (proxy['name'], proxy['proxyid']))
    print("Proxy is offline and was last seen on %s" % ( datetime.fromtimestamp(last_access_int, timezone.utc) ))
    
    # Find hosts that are in our AD templates
    vulnerable_hosts = get_hosts_for_proxy_from_zabbix(proxy['proxyid'], autodiscovery_templateids)
    if len(vulnerable_hosts) > 0:
        print("Deleting %d hosts" % (len(vulnerable_hosts)))
        zabbix_api.host.delete(vulnerable_hosts)

print("--- Script end at {}".format(datetime.now().isoformat()))
