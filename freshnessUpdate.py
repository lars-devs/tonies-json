#!/usr/bin/python3

import yaml
import os
import copy
import requests
import binascii
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.poolmanager import PoolManager

from proto.toniebox.pb.freshness_check import fc_request_pb2, fc_response_pb2
from proto.toniebox.pb import taf_header_pb2

class HostNameIgnoringAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(num_pools=connections,
                                       maxsize=maxsize,
                                       block=block,
                                       assert_hostname=False)

def ruid_to_int_uid(hex_str):
    hex_bytes = bytes.fromhex(hex_str)
    return int.from_bytes(hex_bytes, byteorder='little')

def get_server_data(endpoint_path, data=None, method='GET', auth=None, max_length=0):
    # Paths to your PEM files
    client_cert_file = 'work/certs/client.pem'
    private_key_file = 'work/certs/private.pem'
    custom_ca_file = 'work/certs/ca.pem'

    base_url = 'https://prod.de.tbs.toys'
    url = f'{base_url}/{endpoint_path}'

    headers = {}
    if auth:
        headers['Authorization'] = auth

    # Create a session and configure it for client authentication
    session = requests.Session()
    session.cert = (client_cert_file, private_key_file)
    session.verify = custom_ca_file
    session.mount('https://', HostNameIgnoringAdapter())

    # Make the request with the specified method (GET or POST)
    if method == 'GET':
        response = session.get(url, headers=headers, stream=True)
    elif method == 'POST' and data is not None:
        response = session.post(url, data=data, headers=headers, stream=True)
    else:
        print("Invalid method or missing data for POST request")
        return None

    if response.status_code == 200:
        content = b''
        for chunk in response.iter_content(chunk_size=1024):
            content += chunk
            if max_length and len(content) >= max_length:
                break

        if max_length and len(content) > max_length:
            content = content[:max_length]  # Truncate content to max_length

        return content
    else:
        print(f"Request failed with status code {response.status_code}: {response.text}")
        return None

# Example usage:
# Send a GET request
endpoint_path_get = 'v1/time'
time_result = get_server_data(endpoint_path_get)

if time_result is not None:
    print("Response (GET):", time_result)

# Define the folder path containing YAML files
content_yaml_folder = 'work/content/'

fc_request = fc_request_pb2.TonieFreshnessCheckRequest()

yaml_infos = {}
article_infos = {}

for filename in os.listdir(content_yaml_folder):
    if filename.endswith('.auth.yaml'):
        # Read and parse the YAML file
        fileAuth = os.path.join(content_yaml_folder, filename)
        fileData = os.path.join(content_yaml_folder, "data", filename.replace('.auth.yaml', '.data.yaml'))
        with open(fileAuth, 'r') as yaml_file:
           auths = yaml.safe_load(yaml_file)
        article = filename.replace('.auth.yaml', '')

        data = {
            'audio-id': 0,
            'hash': '0000000000000000000000000000000000000000',
            'size': 0,
            'tracks': 0
        }
        # Look for a corresponding .data.yaml file
        if os.path.exists(fileData):
            with open(fileData, 'r') as data_yaml_file:
                tmp_data = yaml.safe_load(data_yaml_file)
                if tmp_data is not None:
                    for name in data:
                        if name in tmp_data:
                            data[name] = tmp_data[name]
                    

        article_info = []
        for pair in auths:
            yaml_info = {
                'fileAuth' : fileAuth,
                'fileData' : fileData,
                'article': article,
                'auth' : pair,
                'data' : copy.deepcopy(data),
                'article_info' : article_info,
                'updated' : False
            }

            # Create a TonieFCInfo message and populate it with data from the YAML file
            tonie_info = fc_request.tonie_infos.add()
            tonie_info.uid = ruid_to_int_uid(pair["ruid"])  # Replace with the actual YAML field name
            tonie_info.audio_id = int(data["audio-id"])
            print(f"uid: {tonie_info.uid:016x}, audio_id: {tonie_info.audio_id}")

            if tonie_info.uid in yaml_infos:
                print(f"Warning: UID {tonie_info.uid} is already in the YAML data, skipping {filename}.")
            else:
                yaml_infos[tonie_info.uid] = yaml_info
                article_info.append(yaml_info)
        article_infos[yaml_info["article"]] = article_info

# Send a POST request with data
endpoint_path_post = 'v1/freshness-check'
fc_response_data = get_server_data(endpoint_path_post, data=fc_request.SerializeToString(), method='POST')

if fc_response_data is not None:
    fc_response = fc_response_pb2.TonieFreshnessCheckResponse()
    fc_response.ParseFromString(fc_response_data)
    print("Marked UIDs:")
    for marked in fc_response.tonie_marked:
        print(format(marked, '016x'))
        yaml_info = yaml_infos[marked]

        endpoint = None
        auth = None
        if "auth" in yaml_info["auth"]:
            auth = f'BD {yaml_info["auth"]["auth"]}'
            endpoint = f'/v2/content/{yaml_info["auth"]["ruid"]}'
        else:
            endpoint = f'/v1/content/{yaml_info["auth"]["ruid"]}'

        taf_header_raw = get_server_data(endpoint, method='GET', auth=auth, max_length=4096)

        if taf_header_raw is None:
            print("Could not download TAF-header")
        elif taf_header_raw[:4] != b'\x00\x00\x0f\xfc':
            print("Invalid TAF-header")
            print(taf_header_raw)
        else:
            taf_header = taf_header_pb2.TonieboxAudioFileHeader()
            taf_header.ParseFromString(taf_header_raw[4:])

            yaml_info["data"]["audio-id"] = taf_header.audio_id
            yaml_info["data"]["hash"] = binascii.hexlify(taf_header.sha1_hash).decode('utf-8')
            yaml_info["data"]["tracks"] = len(taf_header.track_page_nums)
            yaml_info["data"]["size"] = taf_header.num_bytes
            yaml_info["updated"] = True

            print(f'-  Audio ID: {yaml_info["data"]["audio-id"]}')
            print(f'   Hash: {yaml_info["data"]["hash"]}')
            print(f'   Tracks: {yaml_info["data"]["tracks"]}')
            print(f'   Size: {yaml_info["data"]["size"]}')
    
    for article, article_info in article_infos.items():
        last_yaml_info = None
        length = len(article_info)
        error = False
        updated = False
        for yaml_info in article_info:
            if last_yaml_info is None:
                last_yaml_info = yaml_info
            else:
                if last_yaml_info["data"] != yaml_info["data"]:
                    error = True
            if last_yaml_info["updated"]:
                updated = True
        if error:
            print(f'Different data for article {article}:')
            for yaml_info in article_info:
                print(f'   Audio ID: {yaml_info["data"]["audio-id"]}, Hash: {yaml_info["data"]["hash"]}, Tracks: {yaml_info["data"]["tracks"]}, Size: {yaml_info["data"]["size"]}')

        elif updated:
            fileData = last_yaml_info["fileData"]
            print(f'Updated data for article {article} to {fileData}:')
            print(f'   Audio ID: {last_yaml_info["data"]["audio-id"]}, Hash: {last_yaml_info["data"]["hash"]}, Tracks: {last_yaml_info["data"]["tracks"]}, Size: {last_yaml_info["data"]["size"]}')

            with open(fileData, "w") as yaml_file:
                yaml.safe_dump(last_yaml_info["data"], yaml_file)

