import base64
import json
import subprocess
import logging
import os
from datetime import datetime, timezone
import time
import requests
import schedule
import toml

class ConsumerMonitor():
    def __init__(self,
                 config_file: str="config.toml"):
        """
        Read in the config file settings.
        """
        with open(config_file, "r") as config_toml:
            config = toml.load(config_toml)
        try:
            self.MONITOR_PERIOD = int(config['monitor_period'])
            self.CHAIN_BINARY = config['gaia_path']
            self.HERMES_BINARY = config['hermes_path']
            self.HERMES_CONFIG = config['hermes_config_path']
            self.CHAIN_ID = config['chain_id']
            self.API_NODE = config['api']
            self.RPC_NODE = config['rpc']
            self.GRPC_NODE = config['grpc']
            self.ALARM_DELAY = int(config['matured_packet_grace_period'])
            self.MATRIX_HOMESERVER_URL = config['homeserver']
            self.MATRIX_ROOM = config['room']
            self.MATRIX_TOKEN = config['token']
            self.RECORD_FILE = config['record_file']
        except:
            logging.error("Configuration file could not be processed.")
        
        self.send_blocks = []
        self.recv_txs = []
        self.chain_vsc = {}

    def get_unbonding_periods(self):
        """
        Read the unbonding periods for each chain using the Gaia binary.
        """
        logging.info("Collecting consumer chain unbonding periods")
        for chain in self.chain_vsc.keys():
            result = subprocess.run([f"{self.CHAIN_BINARY}",
                    "q", "provider", "consumer-genesis", chain,
                    "--output",
                    "json",
                    "--node",
                    self.RPC_NODE],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
            try:
                result.check_returncode()
                ccv = json.loads(result.stdout)
                unbonding_period = int(ccv['params']['unbonding_period'][:-1])
                alarm_period = unbonding_period + self.ALARM_DELAY
                self.chain_vsc[chain]['unbonding_period'] = unbonding_period
                self.chain_vsc[chain]['alarm_period'] = alarm_period

            except subprocess.CalledProcessError as cpe:
                logging.error("Exception:", result.stderr)

    def get_connection_id(self, client_id):
        """
        Get the connection id for the consumer chain using Hermes.
        """
        result = subprocess.run([f"{ self.HERMES_BINARY }",
                "--json",
                "--config", self.HERMES_CONFIG,
                "query", "client", "connections",
                "--chain", self.CHAIN_ID, "--client",
                client_id],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
        lines = result.stdout
        connection_json = json.loads(lines.split('\n')[-2]) 
        connection_id = connection_json['result'][0]
        return connection_id

    def get_channel_id(self, connection_id):
        """
        Get the connection id for the consumer chain using Hermes.
        """
        result = subprocess.run([f"{self.HERMES_BINARY}",
                        "--json",
                        "--config", self.HERMES_CONFIG,
                        "query", "connection", "channels",
                        "--chain", self.CHAIN_ID, "--connection",
                        connection_id],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True)
        lines = result.stdout
        channel_json = json.loads(lines.split('\n')[-2])
        for channel in channel_json['result']:
            if channel['port_id'] == 'provider':
                channel_id = channel['channel_id']
                return channel_id

    def detect_consumer_chains(self):
        """
        Query the API endpoint to list all available consumer chains.
        """
        logging.info("Updating consumer chains list")
        response = requests.get(self.API_NODE + '/interchain_security/ccv/provider/consumer_chains').json()
        chains = [chain for chain in response['chains']]
        if chains:
            for chain in chains:
                if chain['chain_id'] not in self.chain_vsc.keys():
                    self.chain_vsc[chain['chain_id']] = {
                        'unbonding_period': 0,
                        'alarm_period': 0,
                        'client': chain['client_id'],
                        'connection': '',
                        'channel': '',
                        'vsc_ids': {}
                    }

            remove_chains = []
            for chain in self.chain_vsc.keys():
                chain_names = [chain_name['chain_id'] for chain_name in chains]
                if chain not in chain_names:
                    remove_chains.append(chain)

            for chain in remove_chains:
                self.chain_vsc.pop(chain)

            # Populate connection and channel id fields
            for chain in self.chain_vsc.keys():
                self.chain_vsc[chain]['connection'] = self.get_connection_id(self.chain_vsc[chain]['client'])
                self.chain_vsc[chain]['channel'] = self.get_channel_id(self.chain_vsc[chain]['connection'])

    def search_send_blocks(self, start_height: int=1):
        """
        Query the RPC endpoint to search all blocks that include
        transactions with “provider” as the source port.
        """
        logging.info("Searching blocks that include packets sent from the provider port")
        send_query=f'query="send_packet.packet_src_port=\'provider\'"'
        page = 1
        try:
            block_search = requests.get(self.RPC_NODE + '/block_search?' + send_query + \
            f'&per_page=100&page={page}', timeout = (3, 20)).json()
        except Exception as exc:
            logging.error("Exception:", exc)
        collected = len(block_search['result']['blocks'])
        total_blocks = int(block_search['result']['total_count'])
        block_list = block_search['result']['blocks']

        while collected<total_blocks:
            page = page + 1
            block_search = requests.get(self.RPC_NODE + '/block_search?' + send_query + \
            f'&per_page=100&page={page}', timeout = (3, 20)).json()
            collected = collected + len(block_search['result']['blocks'])
            block_list.extend(block_search['result']['blocks'])
        
        self.send_blocks = [
            int(block['block']['header']['height']) 
            for block in block_list
            if int(block['block']['header']['height']) > start_height
            ]

    def decode_attributes(self, attributes: list):
        """
        Base64 decode the list of attributes and return a decoded list
        """
        decoded_attributes = []
        for encoded_attr in attributes:
            key = base64.b64decode(encoded_attr['key']).decode('utf-8')
            value = base64.b64decode(encoded_attr['value']).decode('utf-8')
            decoded_attributes.append(
                {
                    'key': key,
                    'value': value
                }
            )
        return decoded_attributes

    def add_valset_ids(self, height: int):
        """
        Query the RPC endpoint to get all the validator set ids
        sent out and add them to the relevant consumer chain.
        """
        try:
            events = requests.get(self.RPC_NODE + '/block_results?height=' + str(height)).json()
            block_info = requests.get(self.RPC_NODE + '/block?height=' + str(height)).json()
            block_time_str = block_info['result']['block']['header']['time']
            result = events['result']
            end_block_events = result['end_block_events']
            if end_block_events:
                for ev in end_block_events:
                    if ev['type'] == 'send_packet':
                        decoded = self.decode_attributes(ev['attributes'])
                        vsc_id = 0
                        channel = ''
                        for attribute in decoded:
                            if attribute['key'] == 'packet_data':
                                packet_data = json.loads(attribute['value'])
                                if 'valset_update_id' in packet_data.keys():
                                    vsc_id = int(packet_data['valset_update_id'])
                            if attribute['key'] == 'packet_src_channel':
                                channel = attribute['value']
                        if vsc_id > 0 and channel:
                            for chain in self.chain_vsc.keys():
                                if channel == self.chain_vsc[chain]['channel']:
                                    if vsc_id not in self.chain_vsc[chain]['vsc_ids']:
                                        self.chain_vsc[chain]['vsc_ids'][vsc_id] = {
                                           'state': 'unbonding_period',
                                           'start': block_time_str
                                    }


        except Exception as exc:
            print("Error!: ", exc)
            return

    def record_valset_updates(self):
        """
        Record the validator set updates by querying all the blocks in self.send_blocks
        """
        logging.info("Recording validator set updates")
        for block_no, height in enumerate(self.send_blocks):
            self.add_valset_ids(height)

    def search_recv_txs(self):
        """
        Query the RPC endpoint to Search all transactions that include
        packets with “provider” as the destination port.
        """
        logging.info("Searching transactions that include packets received by the provider port")
        recv_query=f'query="recv_packet.packet_dst_port=\'provider\'"'
        matured_vsc_ids = {}
        page = 1
        channels = [chain_data['channel'] for _, chain_data in self.chain_vsc.items()]

        try:
            tx_search = requests.get(self.RPC_NODE + '/tx_search?' + recv_query + f'&per_page=100&page={page}').json()
        except Exception as exc:
            print(exc)
        collected = len(tx_search['result']['txs'])
        total_txs = int(tx_search['result']['total_count'])
        tx_list = tx_search['result']['txs']

        while collected<total_txs:
            page = page + 1
            tx_search = requests.get(self.RPC_NODE + '/tx_search?' + recv_query + f'&per_page=100&page={page}').json()
            collected = collected + len(tx_search['result']['txs'])
            tx_list.extend(tx_search['result']['txs'])

        if tx_list:
            for tx in tx_list:
                events = tx['tx_result']['events']
                if events:
                    for event in events:
                        if event['type'] == 'recv_packet':
                            vsc_id = 0
                            channel = ''
                            decoded_attr = self.decode_attributes(event['attributes'])
                            for attr in decoded_attr:
                                key = attr['key']
                                value = attr['value']
                                if key == 'packet_data':
                                    packet_data = json.loads(value)
                                    if packet_data['type'] == 'CONSUMER_PACKET_TYPE_VSCM':
                                        vsc_id = int(packet_data['vscMaturedPacketData']['valset_update_id'])
                                if key == 'packet_dst_channel' and value in channels:
                                    channel = value
                            if vsc_id and channel:
                                if vsc_id not in matured_vsc_ids.keys():
                                    matured_vsc_ids[vsc_id] = [channel]
                                else:
                                    matured_vsc_ids[vsc_id].append(channel)

        for chain in self.chain_vsc.keys():
            for vsc_id in matured_vsc_ids.keys():
                if vsc_id in list(self.chain_vsc[chain]['vsc_ids'].keys()):
                    for channel in matured_vsc_ids[vsc_id]:
                        if channel == self.chain_vsc[chain]['channel']:
                            self.chain_vsc[chain]['vsc_ids'][vsc_id]['state'] = 'pass'

    def evaluate_age(self):
        """
        Check for packets that have:
        - matured
        - reached an alarm state
        - reached the VSC timeout
        """
        logging.info("Marking VSC ids that have reached the matured or alarm state")
        current_time = datetime.now(timezone.utc)
        for chain, chain_data in self.chain_vsc.items():
            for vsc_id, vsc_data in chain_data['vsc_ids'].items():
                start_time_str = vsc_data['start']
                time_sections = start_time_str.split('.')
                start_time_str = time_sections[0]+'Z'
                start_time = datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

                if vsc_data['state'] != 'pass':
                    time_difference_seconds = (current_time - start_time).total_seconds()
                    if time_difference_seconds > chain_data['alarm_period']:
                        vsc_data['state'] = 'alarm'
                    elif time_difference_seconds > chain_data['unbonding_period']:
                        vsc_data['state'] = 'matured'

    def alert_matrix(self, message: str):
        """
        Send alert message to Matrix room
        """
        logging.info(f'Sending alert: "{message}"')
        message_endpoint = f'/_matrix/client/r0/rooms/{self.MATRIX_ROOM}/send/m.room.message'
        payload = { 'msgtype': 'm.text', 'body': message }
        full_url = self.MATRIX_HOMESERVER_URL + message_endpoint + f'?access_token={self.MATRIX_TOKEN}'
        print(full_url)
        r = requests.post(full_url, data=json.dumps(payload))
        print(r.text)

    def update(self):
        """
        Update the available consumer chains and their unbonding periods,
        collect new VSC ids, detect matured packets, and
        issue any relevant alarm messages.
        """
        self.detect_consumer_chains()
        self.get_unbonding_periods()
        # Adjust starting height if record file is available
        # Look for the earliest height of a vsc among all chains
        # that has not reached the 'pass' state
        starting_height = 1
        if os.path.isfile(self.RECORD_FILE):
            with open(self.RECORD_FILE, 'r') as record_file:
                self.chain_vsc = json.load(record_file)
                for _, chain_data in self.chain_vsc.items():
                    for vsc_id in chain_data['vsc_ids'].keys():
                        if starting_height == 1:
                            starting_height = int(vsc_id)
                        else:
                            if int(vsc_id) < starting_height and chain_data['vsc_ids'][vsc_id]['state'] != 'pass':
                                starting_height = int(vsc_id)

        self.search_send_blocks(starting_height)
        self.record_valset_updates()
        self.search_recv_txs()
        self.evaluate_age()
        with open(self.RECORD_FILE, 'w', encoding='utf-8') as output:
            json.dump(self.chain_vsc, output, indent=4)

        for chain, chain_data in self.chain_vsc.items():
            for vsc_id, vsc_data in chain_data['vsc_ids'].items():
                if vsc_data['state'] == 'matured' or vsc_data['state'] == 'alarm':
                    message = f'Consumer monitor> VSC ID {vsc_id} has reached the {vsc_data["state"]} state for chain {chain}.'
                    self.alert_matrix(message)
        logging.info("Update complete")

    def check_hermes_config(self):
        """
        Creates a hermes config file is one is not found.
        """
        if not os.path.isfile(self.HERMES_CONFIG):
            trimmed_rpc = self.RPC_NODE.split('://')[1]
            lines = [
                "[[chains]]\n",
                f"id = '{self.CHAIN_ID}'\n",
                f"rpc_addr = '{self.RPC_NODE}'\n",
                f"grpc_addr = '{self.GRPC_NODE}'\n",
                "event_source = { mode = 'push', url = 'ws://" + trimmed_rpc + "/websocket', batch_delay = '500ms' }\n", # Hermes >= v1.6.0
                # f"websocket_addr = 'ws://{trimmed_rpc}/websocket'\n", # Hermes < v1.6.0
                "account_prefix = 'cosmos'\n",
                "key_name = 'wallet'\n",
                "store_prefix = 'ibc'\n",
                "gas_price = { price = 0.0025, denom = 'uatom' }\n"
            ]
            with open(self.HERMES_CONFIG, 'w') as config_file:
                config_file.writelines(lines)

    def start(self):
        """
        Call this function to begin monitoring.
        """
        self.check_hermes_config()
        self.update()
        schedule.every(self.MONITOR_PERIOD).seconds.do(self.update)
        while True:
            schedule.run_pending()
            time.sleep(10)

logging.basicConfig(
    filename=None,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

monitor = ConsumerMonitor()
monitor.start()
