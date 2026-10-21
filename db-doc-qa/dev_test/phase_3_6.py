"""Phase 3.6: drives the real Streamlit app (local mode, real model) with Streamlit's AppTest.

Needs GOOGLE_API_KEY. Removes one sample document, asks a hybrid question, and prints what the
expander would show: the intent and each tool call (SQL, or search query, filters and citations).
"""
from pathlib import Path

from streamlit.testing.v1 import AppTest

import ui_client
from data_agent import sessions

at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / 'app.py'), default_timeout=180).run()
print('documents:', [e['name'] for e in at.session_state.doc_entries])
at.button(key='remove_customer_communication_guidelines.pdf').click().run()
print('after Remove:', [e['name'] for e in at.session_state.doc_entries], '| chat reset:', at.session_state.messages == [])

at.chat_input[0].set_value('Shipment 12345 is delayed. What should I do?').run()
msg = at.session_state.messages[-1]
print('\nINTENT:', msg['intent'])
for c in msg['calls']:
    r = c['response'] or {}
    print('TOOL:', c['name'], c['args'])
    for p in r.get('passages', []):
        print('   cites:', ui_client.format_citation(p))
print('\nANSWER:\n' + msg['content'])
print('\nexpander shown:', bool(at.expander))
sessions.end_session(sessions.ROOT / at.session_state.session_id)
