#!/usr/bin/env python3
from datetime import datetime, timezone
from zabbix_utils import ZabbixAPI
from slack_sdk import WebClient
import pprint
import json
import time

########### User Configuration ###########

# System Config
message_age_in_minutes = 15

# Slack Config
slack_channels = [ "channel-name-1" ]
slack_token_user = ""
slack_token = ""
bot_user = ""

# Zabbix Config
zabbix_host = ""
zabbix_token = ""

########### Script Start ###########

print("--- Script start at {}".format(datetime.now().isoformat()))

# Calculate expired timestamp
current_stamp = int(datetime.now(timezone.utc).timestamp())
messages_older_than = current_stamp - (60*message_age_in_minutes)

# Setup slack client
client_admin = WebClient(token=slack_token_user)
client = WebClient(token=slack_token)

# Setup zabbix client
zabbix_api = ZabbixAPI(url=zabbix_host)
zabbix_api.login(token=zabbix_token)

## Methods ##

# Check if a trigger still exists in zabbix
def message_exists_in_zabbix(channel_message, slack_channel):
    problems = zabbix_api.problem.get(selectSuppressionData="extend", tags=[{
        "tag": f"__message_ts_#{slack_channel['name']}",
        "value": channel_message['ts'],
        "operator": 1
    }])

    # If there is no entry at all then Zabbix no longer has a record and this is orphaned
    if len(problems) == 0:
        print(f"Zabbix returned no results for message: {channel_message['ts']}")
        return False

    # Check if we are in the active and non dependant triggers
    if not problems[0]['objectid'] in nondependant_active_triggers:
        if 'metadata' in channel_message and channel_message['metadata']['event_type'] == "message_hidden":
            pass
        else:
            print(f"Object ID {problems[0]['objectid']} is hidden by dependant triggers, hiding")
            hide_message(slack_channel['id'], channel_message)
    else:
        if 'metadata' in channel_message and channel_message['metadata']['event_type'] == "message_hidden":
            print(f"Object ID {problems[0]['objectid']} is no longer hidden by dependant triggers, unhiding")
            unhide_message(slack_channel['id'], channel_message)

    return True

# Make the decision if a message should be removed
def decide_delete_message(channel_message, slack_channel):
    try:
        # Message timestamp
        message_timestamp = int(channel_message['ts'].split('.')[0])

        # Always process channel join/leave messages
        try:
            if channel_message['subtype'] == "channel_join" or channel_message['subtype'] == "channel_leave":
                print("Marking join/leave message with id %s for deletion (%s)" % (channel_message['ts'], message_timestamp))
                return True

        except KeyError:
            pass

        # Ignore any messages which are hidden
        #if 'metadata' in channel_message and channel_message['metadata']['event_type'] == "message_hidden":
        #    return False

        # Check if we have a phantom deleted message with replies
        try:
            if channel_message['subtype'] == "tombstone" and channel_message['reply_count'] >= 1:
                print("Marking phanton message with id %s for deletion(%s)" % (channel_message['ts'], message_timestamp))
                return True

        except KeyError:
            pass

        # Skip any messages not from our bot user
        try:
            if not channel_message['user'] == bot_user:
                return False

        except KeyError:
            pass

        message = channel_message['attachments'][0]
        if "resolved in" in message['title'].lower():

            print(f"Compare stamp of message {message_timestamp} with expiry stamp of {messages_older_than}")
            if message_timestamp <= messages_older_than:
                # Message is resolved and older than our threshold
                print("Marking expired message with id %s for deletion (%s)" % (channel_message['ts'], message_timestamp))
                return True
            else:
                # Message is resolved but not older than expiry, so ignore for now
                return False

        elif not message_exists_in_zabbix(channel_message, slack_channel):

            # Message does not exist in Zabbix anymore
            print("Marking missing/removed message with id %s for deletion (%s)" % (channel_message['ts'], message_timestamp))
            return True

    except Exception as e:
        print("Failed to handle message %s" % e)
        pprint.pprint(channel_message)

# Fetch messages from Slack by Channel or Thread
def request_messages(channel_id, reply_thread_id = None):
    requested_messages = []
    request_cursor = None
    request_more = True

    while request_more:

        # Request the messages from the server, using a cursor if its an additional (paginated) request
        channel_messages = None
        if reply_thread_id == None:
            if request_cursor == None:
                print("Fetching initial records")
                channel_messages = client.conversations_history(channel=channel_id, include_all_metadata=True)
            else:
                print("Fetching additional records (%s)" % request_cursor)
                channel_messages = client.conversations_history(channel=channel_id, cursor=request_cursor, include_all_metadata=True)
        else:
            if request_cursor == None:
                print("Fetching initial records")
                channel_messages = client.conversations_replies(channel=channel_id, ts=reply_thread_id, include_all_metadata=True)
            else:
                print("Fetching additional records (%s)" % request_cursor)
                channel_messages = client.conversations_replies(channel=channel_id, ts=reply_thread_id, cursor=request_cursor, include_all_metadata=True)   

        # Update cursor
        request_more = channel_messages.data['has_more']
        if request_more:
            request_cursor = channel_messages.data['response_metadata']['next_cursor']

        # Append the requested messages to our return array
        requested_messages += channel_messages.data['messages']

        # Pause a moment before requesting more messages
        if request_more:
            time.sleep(1)

    # Return all the found messages
    return requested_messages

# Hide a message by hiding the content in metadata and replacing it
def hide_message(channel_id, channel_message):
    # Save attachment as metadata
    metadata = {"event_type": "message_hidden", "event_payload": json.dumps(channel_message['attachments'][0])}

    # Update attachment
    new_attachment = channel_message['attachments'][0]
    new_attachment['actions'] = []
    new_attachment['color'] = "CCCCCC"
    new_attachment['fields'] = [
        {
            'value': 'This alert is active but hidden by another, dependant alert'
        }
    ]

    # Update message
    client.chat_update(channel=channel_id, ts=channel_message['ts'], attachments=[new_attachment], metadata=metadata)

# Unhide a message by returning the content from the metadata
def unhide_message(channel_id, channel_message):
    # Retrieve attachment from metadata
    if 'metadata' in channel_message and channel_message['metadata']['event_type'] == "message_hidden":
        message_payload = channel_message['metadata']['event_payload']
        
        # Update message
        client.chat_update(channel=channel_id, ts=channel_message['ts'], attachments=[message_payload], metadata={})

## Main loop ##

# Get all active triggers
nondependant_active_triggers = {}
for active_triggers in zabbix_api.trigger.get(skipDependent=1, monitored=1, only_true=1):
    nondependant_active_triggers[active_triggers['triggerid']] = active_triggers

# Iterate over the list of channels to process
channel_list = client.conversations_list()
for slack_channel in channel_list.data['channels']:

    # Check if we need to process this slack channel
    if slack_channel['is_archived'] or not slack_channel['name'] in slack_channels:
        continue

    # Make an array of all message IDs to remove
    messages_to_remove = []

    # Process messages in this channel
    print("Processing messages in slack channel #%s (%s)" % (slack_channel['name'], slack_channel['id']))
    for channel_message in request_messages(slack_channel['id']):
        if decide_delete_message(channel_message, slack_channel):
            messages_to_remove.append(channel_message['ts'])
            if 'reply_count' in channel_message and channel_message['reply_count'] >= 1:
                print(f"Message has replies to delete, fetching replies:")
                for message_in_thread in request_messages(slack_channel['id'], channel_message['thread_ts']):
                    print("Marking phanton message with id %s for deletion" % (message_in_thread['ts']))
                    messages_to_remove.append(message_in_thread['ts'])

    # Delete all messages queued for deletion
    if len(messages_to_remove) == 0:
        print("No messages to remove")

    else:
        rate_limiter = 40
        print("Processing deletion requests [0/%d] - Please wait, rate limited to %d/min" % (len(messages_to_remove),rate_limiter))
        total_count = 0
        delete_count = 0
        for message in messages_to_remove:

            # Slack limits us to 50 deletions per minute
            delete_count = delete_count + 1
            if delete_count >= rate_limiter:
                total_count = total_count + delete_count
                print("Processing deletion requests [%d/%d] - Please wait, rate limited to %d/min" % (total_count,len(messages_to_remove),rate_limiter))

                delete_count = 0
                time.sleep(65-rate_limiter)

            # Attempt deletion
            try:
                client_admin.chat_delete(channel=slack_channel['id'], ts=message)
                time.sleep(1)
            except Exception as e:
                print("Failed to delete message %s" % e)

print("--- Script end at {}".format(datetime.now().isoformat()))
