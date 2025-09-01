from pyDataverse.api import Api
import json
import dvconfig
import os
import time
import requests
from io import StringIO

base_url = dvconfig.base_url
api_token = dvconfig.api_token

try:
    api_token = os.environ['API_TOKEN']
    print("Using API token from $API_TOKEN.")
except Exception:
    print("Using API token from config file.")

paths = dvconfig.sample_data
api = Api(base_url, api_token)
print(api.status)

# Helpers
def read_json_or_none(json_path, label=""):
    """Return parsed JSON or None, printing a warning if missing or invalid."""
    if not os.path.isfile(json_path):
        print(f"⚠️  {label} JSON not found: {json_path}. Skipping.")
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"⚠️  {label} JSON invalid ({json_path}): {e}. Skipping.")
        return None
    except Exception as e:
        print(f"⚠️  {label} JSON could not be read ({json_path}): {e}. Skipping.")
        return None

# TODO: limit amount of recursion
def check_dataset_lock(dataset_dbid):
    query_str = '/datasets/' + str(dataset_dbid) + '/locks'
    params = {}
    resp = api.get_request(query_str, params=params, auth=True)
    locks = resp.json().get('data', [])
    if locks:
        print('Lock found for dataset id ' + str(dataset_dbid) + '... sleeping...')
        time.sleep(2)
        check_dataset_lock(dataset_dbid)

resp = api.get_dataverse(':root')
buff = StringIO("")
if resp.status_code == 401:
    print('Publishing root dataverse.')
    resp = api.publish_dataverse(':root')
    print(resp)

for path in paths:
    # Normalize and sanity-check the path structure to avoid IndexError
    norm_path = os.path.normpath(path)
    parts = norm_path.split(os.sep)
    if len(parts) < 4:
        print(f"⚠️  Path too short to parse type/parent/file: {norm_path}. Skipping.")
        continue

    json_file = parts[-1]
    dvtype = parts[-3]
    dvtype = 'dataverse' if dvtype == 'dataverses' else 'dataset'
    parent = parts[-4]
    parent = ':root' if parent == 'data' else parent

    if dvtype == 'dataverse':
        print(f'Creating {dvtype} {json_file} in dataverse {parent}')
        dv_json = norm_path

        metadata = read_json_or_none(dv_json, label="Dataverse")
        if metadata is None:
            # Skip this entry if the JSON isn't available
            continue

        print(metadata)
        # FIXME: Why is "identifier" required?
        identifier = metadata.get('alias')
        if not identifier:
            print(f"⚠️  Dataverse JSON missing 'alias' (identifier): {dv_json}. Skipping.")
            continue

        resp = api.create_dataverse(identifier, json.dumps(metadata), parent=parent)
        print(resp)
        resp = api.publish_dataverse(identifier)
        print(resp)

    else:
        print(f'Creating {dvtype} {json_file} in dataverse {parent}')
        dataset_json = norm_path

        metadata = read_json_or_none(dataset_json, label="Dataset")
        if metadata is None:
            # Skip this entry if the JSON isn't available
            continue

        dataverse = parent
        resp = api.create_dataset(dataverse, json.dumps(metadata))
        print(resp)

        # Safely extract dataset identifiers
        try:
            resp_json = resp.json()
        except Exception as e:
            print(f"⚠️  Failed to parse dataset creation response JSON: {e}. Skipping dataset.")
            continue

        data_section = resp_json.get('data', {})
        dataset_pid = data_section.get('persistentId')
        dataset_dbid = data_section.get('id')

        if not dataset_pid or not dataset_dbid:
            print("⚠️  Failed to create dataset (no persistentId and/or id):")
            print(resp.status_code)
            print(resp_json)
            # Skip to next path rather than exiting
            continue

        files_dir = os.path.join(os.path.dirname(norm_path), 'files')
        filemetadata_dir = os.path.join(os.path.dirname(norm_path), '.filemetadata')
        print(files_dir)

        # If files directory doesn't exist, skip uploads but still publish the dataset
        if not os.path.isdir(files_dir):
            print(f"ℹ️  Files directory not found: {files_dir}. Skipping file uploads.")

        for walk_path, subdir, files in os.walk(files_dir) if os.path.isdir(files_dir) else []:
            for name in files:
                filepath = os.path.join(walk_path, name)
                relpath = os.path.relpath(filepath, files_dir)
                # "directoryLabel" is used to populate "File Path"
                directoryLabel, filename = os.path.split(relpath)

                # If the file disappeared between os.walk and now, skip it
                if not os.path.isfile(filepath):
                    print(f"⚠️  File no longer exists: {filepath}. Skipping.")
                    continue

                resp = api.upload_file(dataset_pid, "'" + filepath + "'")
                print(resp)

                try:
                    file_id = resp['data']['files'][0]['dataFile']['id']
                except Exception as e:
                    print(f"⚠️  Could not get uploaded file id for {filepath}: {e}. Skipping metadata for this file.")
                    continue

                # Prevent permanent lock if a tabular file was uploaded first.
                check_dataset_lock(dataset_dbid)

                # Optional per-file metadata ("sidecar")
                filemetadatapath = os.path.join(filemetadata_dir, relpath)
                if os.path.exists(filemetadatapath):
                    file_metadata = read_json_or_none(filemetadatapath, label="File metadata")
                    if file_metadata is None:
                        file_metadata = {}
                else:
                    file_metadata = {}

                file_metadata['directoryLabel'] = directoryLabel
                jsonData = json.dumps(file_metadata)
                data = {'jsonData': jsonData}
                headers = {
                    'X-Dataverse-key': api_token,
                }
                resp = requests.post(
                    base_url + '/api/files/' + str(file_id) + '/metadata',
                    data=data,
                    headers=headers,
                    stream=True,
                    files=buff
                )
                print(resp)

                # Optionally restrict file if requested
                if file_metadata.get('restricted'):
                    headers['Content-Type'] = 'application/octet-stream'
                    resp = requests.put(
                        base_url + '/api/files/' + str(file_id) + '/restrict',
                        data='true',
                        headers=headers
                    )
                    print(resp)

        # Sleep a little more to avoid org.postgresql.util.PSQLException: ERROR: deadlock detected
        time.sleep(2)
        print('Publishing dataset id ' + str(dataset_dbid))
        # TODO: Switch to pyDataverse api.publish_dataset after this issue is fixed: https://github.com/AUSSDA/pyDataverse/issues/24
        resp = requests.post(base_url + '/api/datasets/' + str(dataset_dbid) + '/actions/:publish?type=major&key=' + api_token)
        print(resp)
