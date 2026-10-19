"""Drives the Streamlit app in API mode against a local `uv run adk api_server` (http://localhost:8000)."""
import os

os.environ['API_URL'] = 'http://localhost:8000'

from streamlit.testing.v1 import AppTest

at = AppTest.from_file('../app.py', default_timeout=120).run()
print('exceptions:', list(at.exception), '| errors:', [e.value for e in at.error])
print('backend session:', at.session_state.api_sid)

at.chat_input[0].set_value('How many records are there?').run()
for m in at.session_state.messages:
    print(m['role'], '->', m['content'], [c['name'] for c in m.get('calls', [])])

f = open('sample_data/multi_sheet.xlsx', 'rb')
at.file_uploader[0].upload('multi_sheet.xlsx', f.read()).run()
print('after upload:', at.subheader[0].value, '| backend session:', at.session_state.api_sid)
at.chat_input[0].set_value('What is the total revenue?').run()
print(at.session_state.messages[-1]['content'])

at.button[0].click().run()
print('after End session, new session:', at.session_state.api_sid)
