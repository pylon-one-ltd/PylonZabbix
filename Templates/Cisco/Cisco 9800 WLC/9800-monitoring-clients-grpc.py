#!/usr/bin/python3

# ---------------------------------------------------------------
# Python Script for monitoring client data on 9800 WLC for 
# zabbix-sender via gRPC Telemetry
# james@pylonone.com
# v1.0 25/Sep/2025
# ---------------------------------------------------------------

from google.protobuf import json_format
from concurrent import futures
import traceback
import pprint
import proto # proto directory containing our protobuf scaffolds
import time
import json
import grpc
import sys

# ------- Configuration -------

# Output debugging data, will break piping output to zabbix-sender
# Note, all other messages sent as stderr so stdout is clean for piping into sender
debug_output = False

# ------- End Configuration -------

# Stderr Print Method
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

# Zabbix Print Methods
def zabbix_sender_output(controller, typename, keyname, value, current_time):
    if keyname is None:
        print("{} {} {} {}".format(controller, typename, current_time, value))
    else:
        keyname = keyname.replace(' ', '-')
        print("{} {}[{}] {} {}".format(controller, typename, keyname, current_time, value))

def zabbix_sender_discovery(controller, typename, discovery_array, current_time):
    json_array = json.dumps(discovery_array, default=vars).replace("keyname", "{#KEYNAME}").replace("policytag", "{#POLICYTAGS}")
    print("{} wlan.{} {} {{\"data\":{}}}".format(controller, typename, current_time, json_array))

# Main Classes
class DiscoveryObject:
    def __init__(self, value):
        self.keyname = value.replace(' ', '-')

class DiscoveryObjectWithTag:
    def __init__(self, value, policytag = None):
        self.keyname = value.replace(' ', '-')
        self.policytag = policytag

class MdtDialout(proto.mdt_grpc_dialout_pb2_grpc.gRPCMdtDialoutServicer):
    client_collection_round = 1
    controller_config_data = {}
    join_last_message = {}

    def MdtDialout(self, request_iterator, context):
        for request in request_iterator:
            try:
                telemetry_pb = proto.telemetry_pb2.Telemetry()
                telemetry_pb.ParseFromString(request.data)

                # Check we have a handler for the incoming data
                json_data = json_format.MessageToDict(telemetry_pb, preserving_proto_field_name=True)
                if not 'data_gpbkv' in json_data:
                    continue

                # Determine the peer address
                socket_source_address = context.peer()
                if socket_source_address is None:
                    eprint("[{}] Unable to handle message - Unknown message type {}".format(request.ReqId, json_data['encoding_path']))
                eprint("[{}] MdtDialout connection received from {} for {}".format(request.ReqId, socket_source_address, json_data['encoding_path']))
                
                # Create dict for controller in config data if it doesn't exist
                controller_ip_address = socket_source_address.split(':')[1]
                if not controller_ip_address in self.controller_config_data:
                    self.controller_config_data[controller_ip_address] = {}
                    self.controller_config_data[controller_ip_address]["current_aps"] = {}
                    self.controller_config_data[controller_ip_address]["current_ssids"] = {}

                # Pass message onto appropriate handler
                match json_data['encoding_path']:
                    case "Cisco-IOS-XE-wireless-client-oper:client-oper-data/dot11-oper-data":
                        self.HandleClientOperData(json_data["data_gpbkv"], int(json_data["collection_id"]), controller_ip_address)
                    case "Cisco-IOS-XE-wireless-access-point-oper:access-point-oper-data/capwap-data":
                        self.HandleAccessPointData(json_data["data_gpbkv"], int(json_data["collection_id"]), controller_ip_address)
                    case "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-data/policy-list-entries/policy-list-entry":
                        self.HandleWlanConfigData(json_data["data_gpbkv"], int(json_data["collection_id"]), controller_ip_address)
                    case _:
                        eprint("[{}] Unable to handle message - Unknown message type {}".format(request.ReqId, json_data['encoding_path']))

            except Exception as e:
                eprint("[{}] Unable to handle message - Exception thrown - {} - {} {}".format(request.ReqId, controller_ip_address, type(e), e))
                eprint("[{}] Message handler: {}".format(request.ReqId, json_data['encoding_path']))
                eprint(traceback.format_exc())

            finally:
                # Acknowledge message from controller
                yield proto.mdt_grpc_dialout_pb2.MdtDialoutArgs(ReqId=request.ReqId)

    def HandleClientOperData(self, oper_data, collection_round, controller_ip):
        if len(oper_data) == 0:
            return

        new_client_array = {}
        try:
            for each_client in oper_data:
                client_mac_address = "00:00:00:00:00:00"
                this_client_data = {}

                for client_data in each_client["fields"]:
                    if client_data["name"] == "keys":
                        client_mac_address = client_data["fields"][0]["string_value"]
                    if client_data["name"] == "content":
                        for fields in client_data["fields"]:
                            if fields["name"] == "ewlc-ms-phy-type":
                                this_client_data["ewlc-ms-phy-type"] = fields["string_value"]
                            if fields["name"] == "ap-mac-address":
                                this_client_data["ap-mac-address"] = fields["string_value"]
                            if fields["name"] == "vap-ssid":
                                this_client_data["vap-ssid"] = fields["string_value"]
                            if fields["name"] == "wlan-profile":
                                this_client_data["wlan-profile"] = fields["string_value"]
                            if fields["name"] == "dot11-6ghz-cap":
                                this_client_data["dot11-6ghz-cap"] = fields["bool_value"]
                            if fields["name"] == "ms-ap-slot-id":
                                this_client_data["ms-ap-slot-id"] = "slot_{}".format(fields["uint32_value"])
                            if fields["name"] == "radio-type":
                                 this_client_data["radio-type"] = fields["string_value"]
                            if fields["name"] == "ms-wifi":
                                for fields2 in fields["fields"]:
                                    if fields2["name"] == "wpa-version":
                                        this_client_data["wpa-version"] = fields2["string_value"]
                                    if fields2["name"] == "auth-key-mgmt":
                                        this_client_data["auth-key-mgmt"] = fields2["string_value"]

                # Update client data
                if this_client_data["vap-ssid"] == "" or ("radio-type" in this_client_data and this_client_data["radio-type"] == "dot11-radio-type-none"):
                    continue
                new_client_array[client_mac_address] = this_client_data

        except Exception as e:
            eprint("Failed to process HandleClientOperData for controller {} - {} {}".format(controller_ip,type(e),e))
            eprint(traceback.format_exc())
            #pprint.pprint(oper_data)
            return

        # Generate Zabbix Output
        if self.client_collection_round != collection_round:
            # New collection round, print output of last round
            response_data = self.GetControllerMonitoring(self.join_last_message, controller_ip)
            self.ZabbixFormatStage(response_data, controller_ip)

            # Reset collection of data
            self.join_last_message = new_client_array
            self.client_collection_round = collection_round
        else:
            # Join data with last collection round
            self.join_last_message = { **self.join_last_message, **new_client_array }

    def HandleAccessPointData(self, oper_data, collection_round, controller_ip):
        if len(oper_data) == 0:
            return

        try:
            for each_ap in oper_data:
                ap_mac_address = "00:00:00:00:00:00"
                this_ap_data = {}
                for ap_data in each_ap["fields"]:
                    if ap_data["name"] == "content":
                        for fields in ap_data["fields"]:
                            if fields["name"] == "device-detail":
                                for fields2 in fields["fields"]:
                                    if fields2["name"] == "static-info":
                                        for fields3 in fields2["fields"]:
                                            if fields3["name"] == "board-data":
                                                for fields4 in fields3["fields"]:
                                                    if fields4["name"] == "wtp-enet-mac":
                                                        ap_mac_address = fields4["string_value"]
                            if fields["name"] == "name":
                                this_ap_data["ap_name"] = fields["string_value"]
                            if fields["name"] == "tag-info":
                                for fields2 in fields["fields"]:
                                    if fields2["name"] == "policy-tag-info":
                                        this_ap_data["ap_policy_tag"] = fields2["fields"][0]["string_value"]
                                    if fields2["name"] == "site-tag":
                                        for fields3 in fields2["fields"]:
                                            if fields3["name"] == "site-tag-name":
                                                this_ap_data["ap_site_tag"] = fields3["string_value"]

                # Update mapping of AP to PT
                self.controller_config_data[controller_ip]["current_aps"][ap_mac_address] = this_ap_data

        except Exception as e:
            eprint("Failed to process HandleAccessPointData for controller {} - {} {}".format(controller_ip,type(e),e))
            eprint(traceback.format_exc())
            pprint.pprint(oper_data)
            return

    def HandleWlanConfigData(self, oper_data, collection_round, controller_ip):
        if len(oper_data) == 0:
            return

        try:
            for each_tag in oper_data:
                policy_tag_name = "unknown"
                for policy_tag in each_tag["fields"]:
                    if policy_tag["name"] == "keys":
                        policy_tag_name = policy_tag["fields"][0]["string_value"]
                    if policy_tag["name"] == "content":
                        if not 'fields' in policy_tag:
                            continue

                        for fields in policy_tag["fields"]:
                            if fields["name"] == "wlan-policies":
                                for fields2 in fields["fields"]:
                                    for fields3 in fields2["fields"]:
                                        if fields3["name"] == "wlan-profile-name":
                                            ssid_name = fields3["string_value"] # This is wlan-profile-name not SSID name
                                            if not ssid_name in self.controller_config_data[controller_ip]["current_ssids"]:
                                                self.controller_config_data[controller_ip]["current_ssids"][ssid_name] = []
                                            if not policy_tag_name in self.controller_config_data[controller_ip]["current_ssids"][ssid_name]:
                                                self.controller_config_data[controller_ip]["current_ssids"][ssid_name].append(policy_tag_name)

        except Exception as e:
            eprint("Failed to process HandleWlanConfigData for controller {} - {} {}".format(controller_ip,type(e),e))
            eprint(traceback.format_exc())
            pprint.pprint(oper_data)
            return

    def GetControllerMonitoring(self, clients, controller_ip):
        response_data = {
            'current_clients': 0,
            'clients_6ghz_capable': 0,
            'clients_per_key_mgmt_type': {},
            'clients_per_wpa_version': {},
            'clients_per_phy_type': {},
            'clients_per_ssid': {},
            'clients_per_policy': {},
            'clients_per_site': {},
            'clients_per_ap': {},
            'ssids': {},
            'aps': {}
        }

        # Get data for this controller
        if not controller_ip in self.controller_config_data:
            return

        this_controller = self.controller_config_data[controller_ip]
        try:
            # Get Policy Data
            for ssid, policytags in this_controller["current_ssids"].items():
                response_data['clients_per_ssid'][ssid] = 0
                response_data['ssids'][ssid] = policytags

            # Get AP Data
            for ap_mac, this_ap in this_controller["current_aps"].items():
                response_data['aps'][this_ap['ap_name']] = this_ap['ap_policy_tag']
                response_data['clients_per_ap'][this_ap['ap_name']] = {}
                response_data['clients_per_ap'][this_ap['ap_name']]["slot_0"] = 0
                response_data['clients_per_ap'][this_ap['ap_name']]["slot_1"] = 0
                response_data['clients_per_ap'][this_ap['ap_name']]["slot_2"] = 0
                response_data['clients_per_ap'][this_ap['ap_name']]["slot_3"] = 0

            # Get client data
            response_data['current_clients'] = len(clients)
            for client_mac, this_client in clients.items():
                # Clients by PHY Type
                try:
                    response_data['clients_per_phy_type'][this_client['ewlc-ms-phy-type']] = response_data['clients_per_phy_type'][this_client['ewlc-ms-phy-type']] + 1
                except KeyError:
                    response_data['clients_per_phy_type'][this_client['ewlc-ms-phy-type']] = 1

                # Clients by Key Management Type
                try:
                    response_data['clients_per_key_mgmt_type'][this_client['auth-key-mgmt']] = response_data['clients_per_key_mgmt_type'][this_client['auth-key-mgmt']] + 1
                except KeyError:
                    response_data['clients_per_key_mgmt_type'][this_client['auth-key-mgmt']] = 1

                # Clients by WPA Version
                try:
                    response_data['clients_per_wpa_version'][this_client['wpa-version']] = response_data['clients_per_wpa_version'][this_client['wpa-version']] + 1
                except KeyError:
                    response_data['clients_per_wpa_version'][this_client['wpa-version']] = 1

                # Clients 6GHz Capable
                if this_client['dot11-6ghz-cap']:
                    response_data['clients_6ghz_capable'] = response_data['clients_6ghz_capable'] + 1

                # Resolve AP name
                access_point_name = this_client['ap-mac-address']
                if this_client['ap-mac-address'] in this_controller["current_aps"]:
                    access_point_name = this_controller["current_aps"][this_client['ap-mac-address']]['ap_name']
                if not access_point_name in response_data['clients_per_ap']:
                    response_data['clients_per_ap'][access_point_name] = {}
                    response_data['clients_per_ap'][access_point_name]["slot_0"] = 0
                    response_data['clients_per_ap'][access_point_name]["slot_1"] = 0
                    response_data['clients_per_ap'][access_point_name]["slot_2"] = 0
                    response_data['clients_per_ap'][access_point_name]["slot_3"] = 0

                # Clients by Access Point (Name or Mac Address)
                try:
                    response_data['clients_per_ap'][access_point_name][this_client['ms-ap-slot-id']] = response_data['clients_per_ap'][access_point_name][this_client['ms-ap-slot-id']] + 1
                except KeyError:
                    response_data['clients_per_ap'][access_point_name][this_client['ms-ap-slot-id']] = 1

                # Clients by SSID
                try:
                    response_data['clients_per_ssid'][this_client['wlan-profile']] = response_data['clients_per_ssid'][this_client['wlan-profile']] + 1
                except KeyError:
                    response_data['clients_per_ssid'][this_client['wlan-profile']] = 1

                # Clients by Site
                if this_client['ap-mac-address'] in this_controller["current_aps"]:
                    if this_controller["current_aps"][this_client['ap-mac-address']]['ap_site_tag']:
                        this_site_tag = this_controller["current_aps"][this_client['ap-mac-address']]['ap_site_tag']
                        try:
                            response_data['clients_per_site'][this_site_tag] = response_data['clients_per_site'][this_site_tag] + 1
                        except KeyError:
                            response_data['clients_per_site'][this_site_tag] = 1

                    # Clients by Policy
                    if this_controller["current_aps"][this_client['ap-mac-address']]['ap_policy_tag']:
                        this_policy_tag = this_controller["current_aps"][this_client['ap-mac-address']]['ap_policy_tag']
                        try:
                            response_data['clients_per_policy'][this_policy_tag] = response_data['clients_per_policy'][this_policy_tag] + 1
                        except KeyError:
                            response_data['clients_per_policy'][this_policy_tag] = 1 

        except Exception as e:
            eprint("Failed to process GetControllerMonitoring for controller {} - {} {}".format(controller_ip,type(e),e))
            eprint(traceback.format_exc())
            return

        return response_data

    def ZabbixFormatStage(self, response_data, controller_ip):
        current_time = int(time.time())
        pp = pprint.PrettyPrinter(indent=4)
        try:
            # Output data to console
            if debug_output:
                print("--------- DEBUG OUTPUT - Retrieved Data from {} at {} ---------".format(controller_ip, current_time))
                print("Total Clients 6GHz Capable: {}".format(response_data['clients_6ghz_capable']));
                print("Total Clients: {}".format(response_data['current_clients']));
                print("\nClients by PHY Type:")
                pp.pprint(response_data['clients_per_phy_type'])

                print("\nClients by AP:")
                pp.pprint(response_data['clients_per_ap'])

                print("\nClients by SSID:")
                pp.pprint(response_data['clients_per_ssid'])

                print("\nClients by Site-Tag:")
                pp.pprint(response_data['clients_per_site'])

                print("\nClients by Policy-Tag:")
                pp.pprint(response_data['clients_per_policy'])

                print("\nClients by WPA Version:")
                pp.pprint(response_data['clients_per_wpa_version'])

                print("\nClients by Key Management Type:")
                pp.pprint(response_data['clients_per_key_mgmt_type'])

                print("\nConfigured SSIDs:")
                pp.pprint(response_data['ssids'])

                print("\nAP Policy Tags:")
                pp.pprint(response_data['aps'])
                print("--------- END DEBUG OUTPUT - Retrieved Data from {} at {} ---------".format(controller_ip, current_time))

            # Discovery Phase
            if debug_output:
                print("-- Starting Discovery Phase --")

            phy_discovery = []
            for phy_type in response_data['clients_per_phy_type'].keys():
                phy_discovery.append(DiscoveryObject(phy_type.replace('client-','').replace('-prot','')))
            zabbix_sender_discovery(controller_ip, "phy_type", phy_discovery, current_time)

            ap_discovery = []
            for ap_name in response_data['clients_per_ap'].keys():
                if ap_name in response_data['aps']:
                    ap_discovery.append(DiscoveryObjectWithTag(ap_name, response_data['aps'][ap_name]))
            zabbix_sender_discovery(controller_ip, "access_point", ap_discovery, current_time)

            ssid_discovery = []
            for ssid_name in response_data['clients_per_ssid'].keys():
                if ssid_name in response_data['ssids']:
                    #ssid_discovery.append(DiscoveryObjectWithTag(ssid_name, ','.join(response_data['ssids'][ssid_name])))
                    ssid_discovery.append(DiscoveryObject(ssid_name))
            zabbix_sender_discovery(controller_ip, "ssid", ssid_discovery, current_time)

            sitetag_discovery = []
            for site_tag in response_data['clients_per_site'].keys():
                sitetag_discovery.append(DiscoveryObject(site_tag))
            zabbix_sender_discovery(controller_ip, "site_tag", sitetag_discovery, current_time)

            policytag_discovery = []
            for policy_tag in response_data['clients_per_policy'].keys():
                policytag_discovery.append(DiscoveryObject(policy_tag))
            zabbix_sender_discovery(controller_ip, "policy_tag", policytag_discovery, current_time)

            wpa_version_discovery = []
            for wpa_version in response_data['clients_per_wpa_version'].keys():
                wpa_version_discovery.append(DiscoveryObject(wpa_version))
            zabbix_sender_discovery(controller_ip, "wpa_version", wpa_version_discovery, current_time)

            key_management_type_discovery = []
            for key_mgmt_type in response_data['clients_per_key_mgmt_type'].keys():
                key_management_type_discovery.append(DiscoveryObject(key_mgmt_type))
            zabbix_sender_discovery(controller_ip, "key_mgmt_type", key_management_type_discovery, current_time)

            # Data Phase
            if debug_output:
                print("-- Starting Data Phase --")
            zabbix_sender_output(controller_ip, "wlan.total_clients", None, response_data['current_clients'], current_time)
            zabbix_sender_output(controller_ip, "wlan.total_6ghz_capable_clients", None, response_data['clients_6ghz_capable'], current_time)

            for phy_type, total in response_data['clients_per_phy_type'].items():
                zabbix_sender_output(controller_ip, "wlan.phy_type.clients", phy_type.replace('client-','').replace('-prot',''), total, current_time)

            for ap_name, ap_client_data in response_data['clients_per_ap'].items():
                total = 0
                for slotname, slot in ap_client_data.items():
                    total = total + slot
                zabbix_sender_output(controller_ip, "wlan.access_point.clients", ap_name, total, current_time)

            for ap_name, ap_client_data in response_data['clients_per_ap'].items():
                for slotname, slot in ap_client_data.items():
                    zabbix_sender_output(controller_ip, "wlan.access_point.{}.clients".format(slotname), ap_name, slot, current_time)

            for ssid_name, total in response_data['clients_per_ssid'].items():
                zabbix_sender_output(controller_ip, "wlan.ssid.clients", ssid_name, total, current_time)

            for site_tag, total in response_data['clients_per_site'].items():
                zabbix_sender_output(controller_ip, "wlan.site_tag.clients", site_tag, total, current_time)

            for policy_tag, total in response_data['clients_per_policy'].items():
                zabbix_sender_output(controller_ip, "wlan.policy_tag.clients", policy_tag, total, current_time)

            for wpa_version, total in response_data['clients_per_wpa_version'].items():
                zabbix_sender_output(controller_ip, "wlan.wpa_version.clients", wpa_version, total, current_time)

            for key_mgmt_type, total in response_data['clients_per_key_mgmt_type'].items():
                zabbix_sender_output(controller_ip, "wlan.key_mgmt_type.clients", key_mgmt_type, total, current_time)

        except Exception as e:
            eprint("Failed to process ZabbixFormatStage for controller {} - {} {}".format(controller_ip,type(e),e))
            eprint(traceback.format_exc())
            return

# Main Method
def main():
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    proto.mdt_grpc_dialout_pb2_grpc.add_gRPCMdtDialoutServicer_to_server(
        MdtDialout(), grpc_server
    )
    grpc_server.add_insecure_port('[::]:57000')
    eprint("Starting gRPC listener on port 57000")

    try:
        grpc_server.start()
        grpc_server.wait_for_termination()
    except Exception:
        pass

# Launch main method
if __name__ == '__main__':
    main()
