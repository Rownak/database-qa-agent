from streamlit.testing.v1 import AppTest

from data_agent import sessions

at = AppTest.from_file('../app.py', default_timeout=30).run()
print('exceptions:', list(at.exception))
print('subheader:', at.subheader[0].value, '|', at.caption[0].value)
folder = sessions.ROOT / at.session_state.session_id
print('folder exists:', folder.is_dir(), sorted(f.name for f in folder.iterdir()))

at.button[0].click().run()
print('after End session, folder exists:', folder.is_dir())
print('new session started:', at.session_state.session_id != folder.name)
