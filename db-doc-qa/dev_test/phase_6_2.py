"""Round trip against a local `uv run adk api_server` (http://localhost:8000): two uploads in state, then a cross-table /run."""
import base64
import json
import urllib.request
import uuid
from pathlib import Path

BASE, APP, USER = 'http://localhost:8000', 'data_agent', 'dev'
sid = uuid.uuid4().hex
S = Path('sample_data/database')


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=120))


# The request body is the initial state itself (not wrapped in {"state": ...}).
state = {'uploads': [{'name': f.name, 'b64': base64.b64encode(f.read_bytes()).decode()}
                     for f in (S / 'customers.csv', S / 'orders.csv')]}
print('create session:', post(f'/apps/{APP}/users/{USER}/sessions/{sid}', state)['id'])
events = post('/run', {'app_name': APP, 'user_id': USER, 'session_id': sid,
                       'new_message': {'role': 'user', 'parts': [{'text': 'Which customer has the highest total order amount?'}]}})
for e in events:
    for p in (e.get('content') or {}).get('parts', []):
        if 'functionCall' in p:
            print('tool call:', p['functionCall']['name'], p['functionCall'].get('args'))
        if 'text' in p:
            print('answer:', p['text'])
req = urllib.request.Request(f'{BASE}/apps/{APP}/users/{USER}/sessions/{sid}', method='DELETE')
print('delete session:', urllib.request.urlopen(req).status)
