"""Round trip against a local `uv run adk api_server` (http://localhost:8000): upload in state, then /run."""
import base64
import json
import urllib.request
import uuid
from pathlib import Path

BASE, APP, USER = 'http://localhost:8000', 'data_agent', 'dev'
sid = uuid.uuid4().hex
f = Path('sample_data/multi_sheet.xlsx')


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=120))


# The request body is the initial state itself (not wrapped in {"state": ...}).
state = {'upload_name': f.name, 'upload_b64': base64.b64encode(f.read_bytes()).decode()}
print('create session:', post(f'/apps/{APP}/users/{USER}/sessions/{sid}', state)['id'])
events = post('/run', {'app_name': APP, 'user_id': USER, 'session_id': sid,
                       'new_message': {'role': 'user', 'parts': [{'text': 'How many records are there?'}]}})
for e in events:
    for p in (e.get('content') or {}).get('parts', []):
        if 'functionCall' in p:
            print('tool call:', p['functionCall']['name'])
        if 'text' in p:
            print('answer:', p['text'])
req = urllib.request.Request(f'{BASE}/apps/{APP}/users/{USER}/sessions/{sid}', method='DELETE')
print('delete session:', urllib.request.urlopen(req).status)
