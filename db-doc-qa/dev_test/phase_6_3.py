"""Round trip against the deployed private API: two uploads in state, then a cross-table /run.

Needs `gcloud` logged in as a user allowed to invoke `data-qa-api`. Run from `db-doc-qa/`:

    uv run python -m dev_test.phase_6_3

The first request after idle can take about 30 seconds (cold start).
"""
import base64
import json
import os
import subprocess
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get('API_URL', 'https://data-qa-api-701944778941.us-central1.run.app')
GCLOUD = os.environ.get('GCLOUD', r'C:\Users\rowna\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd')
APP, USER = 'data_agent', 'dev'
sid = uuid.uuid4().hex
S = Path('sample_data/database')
token = subprocess.run([GCLOUD, 'auth', 'print-identity-token'], capture_output=True, text=True, check=True).stdout.strip()


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data, {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}, method=method)
    return json.load(urllib.request.urlopen(req, timeout=120)) if method != 'DELETE' else urllib.request.urlopen(req, timeout=60).status


# The request body is the initial state itself (not wrapped in {"state": ...}).
state = {'uploads': [{'name': f.name, 'b64': base64.b64encode(f.read_bytes()).decode()}
                     for f in (S / 'multi_db' / 'customers.csv', S / 'multi_db' / 'orders.csv')]}
print('list apps:', call('/list-apps'))
print('create session:', call(f'/apps/{APP}/users/{USER}/sessions/{sid}', state)['id'])
events = call('/run', {'app_name': APP, 'user_id': USER, 'session_id': sid,
                       'new_message': {'role': 'user', 'parts': [{'text': 'Which customer has the highest total order amount?'}]}})
for e in events:
    for p in (e.get('content') or {}).get('parts', []):
        if 'functionCall' in p:
            print('tool call:', p['functionCall']['name'], p['functionCall'].get('args'))
        if 'text' in p:
            print('answer:', p['text'])
print('delete session:', call(f'/apps/{APP}/users/{USER}/sessions/{sid}', method='DELETE'))
