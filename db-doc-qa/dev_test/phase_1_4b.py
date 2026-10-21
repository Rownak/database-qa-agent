import asyncio

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from data_agent.agent import root_agent


async def ask(question):
    svc = InMemorySessionService()
    await svc.create_session(app_name='qa', user_id='u', session_id='s')  # empty state, like adk web
    runner = Runner(agent=root_agent, app_name='qa', session_service=svc)
    msg = types.Content(role='user', parts=[types.Part(text=question)])
    async for ev in runner.run_async(user_id='u', session_id='s', new_message=msg):
        if ev.is_final_response() and ev.content:
            print('ANSWER:', ev.content.parts[0].text)


asyncio.run(ask('How many records are there?'))
