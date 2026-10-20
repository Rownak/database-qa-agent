import asyncio
import tempfile
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from data_agent.agent import root_agent
from data_agent.ingest import load_file

p = Path("sample_data/database/sales.csv")
r = load_file(p.name, p.read_bytes(), Path(tempfile.mkdtemp()) / 'session')
state = {'db_path': str(r.db_path), 'column_map': r.column_map, 'schema_summary': r.schema_summary}


async def ask(question):
    svc = InMemorySessionService()
    await svc.create_session(app_name='qa', user_id='u', session_id='s', state=state)
    runner = Runner(agent=root_agent, app_name='qa', session_service=svc)
    msg = types.Content(role='user', parts=[types.Part(text=question)])
    async for ev in runner.run_async(user_id='u', session_id='s', new_message=msg):
        for call in ev.get_function_calls():
            print('TOOL CALL:', call.name, dict(call.args))
        if ev.is_final_response():
            print('ANSWER:', ev.content.parts[0].text)


asyncio.run(ask('How many total sales of Electronics category'))
