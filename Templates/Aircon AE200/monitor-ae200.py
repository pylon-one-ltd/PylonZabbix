#!/usr/bin/python3

# ---------------------------------------------------------------
# Python Script for monitoring Mitshibushi Aircon
# james@pylonone.com
# v1.0 3/Nov/24
# ---------------------------------------------------------------

import websocket
import xmltodict
import json
import time
import ssl

# ------- Configuration -------

# Address of AE-200
server_address = "your-host-here.domain.com"

# ------- End Configuration -------

ae200_request_base = """
<?xml version="1.0" encoding="UTF-8" ?>
<Packet>
<Command>getRequest</Command>
<DatabaseManager>
%s
</DatabaseManager>
</Packet>
"""

ae200_request_get_names = """
<ControlGroup>
<MnetList />
</ControlGroup>
"""

ae200_request_get_status = """
<Mnet Group="%d" Drive="*" Mode="*" VentMode="*" ModeStatus="*" SetTemp1="*" InletTemp="*" AirDirection="*" FanSpeed="*" RemoCon="*" Schedule="*" ScheduleAvail="*" FilterSign="*" ErrorSign="*" CheckWater="*" />
"""

# Zabbix Print Methods
current_time = int(time.time())
def zabbix_sender_output(controller, key, group, data):
    print(f"{controller} {key.replace('@','')}[{group}] {current_time} {data}")

def zabbix_sender_discovery(controller, typename, discovery_array):
    json_array = json.dumps(discovery_array, default=vars).replace("keyname", "{#KEYNAME}").replace("unitname", "{#UNITNAME}")
    print(f"{controller} {typename} {current_time} {{\"data\":{json_array}}}")

# Main Method
def main():

	# All units used for discovery
	discovered_units = []

	# Make request
	try:
		# Get websocket
		ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
		ws.connect(f"wss://{server_address}/b_xmlproc/", header={"Sec-WebSocket-Protocol":"b_xmlproc", "Origin":"The Ori"})

		# Unit status request
		unit_status_request = ""

		# Send a request for all units
		ws.send(ae200_request_base % ae200_request_get_names)
		websocket_unitname_response = xmltodict.parse(ws.recv())
		if websocket_unitname_response['Packet']['DatabaseManager']['ControlGroup']['MnetList']['MnetRecord'] != None:
			list_data = websocket_unitname_response['Packet']['DatabaseManager']['ControlGroup']['MnetList']['MnetRecord']

			# Convert to a list if not already (for uniformity)
			if not isinstance(list_data, list):
				list_data = [websocket_unitname_response['Packet']['DatabaseManager']['ControlGroup']['MnetList']['MnetRecord']]

			# Add each unit for discovery/naming
			for unit_data in list_data:
				discovered_units.append({ 'keyname': unit_data['@Group'], 'unitname': unit_data['@GroupNameWeb'] })
				unit_status_request += ae200_request_get_status % int(unit_data['@Group'])

		# Send request for unit data
		ws.send(ae200_request_base % unit_status_request)
		response_data = xmltodict.parse(ws.recv())

		# Close websocket after recieving data
		ws.close()

		# Send Discovery
		zabbix_sender_discovery(server_address, 'ae200_unit', discovered_units)

		# Parse XML into dict
		current_time = int(time.time())
		if response_data['Packet']['DatabaseManager']['Mnet'] != None:
			list_data = response_data['Packet']['DatabaseManager']['Mnet']

			# Convert to a list if not already (for uniformity)
			if not isinstance(list_data, list):
				list_data = [response_data['Packet']['DatabaseManager']['Mnet']]

			# Send data for all available endpoints
			for unit_data in list_data:
				unit_group = int(unit_data['@Group'])
				if unit_group > 0 and unit_data is not None:
					for key in unit_data:
						if key != '@Group' and unit_data[key] != "":
							zabbix_sender_output(server_address, f"ae200_unit.{key}", unit_group, unit_data[key])

	except:
		exit(1)

	exit(0)

# Launch main method
if __name__ == '__main__':
    main()
