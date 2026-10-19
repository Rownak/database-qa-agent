from streamlit.testing.v1 import AppTest

at = AppTest.from_file('../app.py', default_timeout=90).run()
at.chat_input[0].set_value('How many records are there?').run()
print('exceptions:', list(at.exception))
for m in at.session_state.messages:
    print(m['role'], '->', m['content'], [c['name'] for c in m.get('calls', [])])
