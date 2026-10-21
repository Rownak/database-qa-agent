"""Phase 3.5: asks the orchestrator one question per intent (plus edge cases) with the real model.

Needs GOOGLE_API_KEY. Each question runs in a fresh session on the sample table and the
sample documents. Prints the classified intent, the tool sequence and the answer.
"""
import asyncio
import sys

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from data_agent import sessions
from data_agent.agent import root_agent

QUESTIONS = [
    ('document', 'What is the policy for delayed shipments?'),
    ('database', 'Where is shipment 12345?'),
    ('hybrid', 'Shipment 12345 is delayed. What should I do?'),
    ('hybrid, filter records by a document value', 'Which delayed shipments have passed their escalation threshold?'),
    ('edge: record says on schedule', 'Shipment 12348 is delayed. What should I do?'),
    ('edge: not found', 'Shipment 99999 is delayed. What should I do?'),
    ('edge: no Refrigerated policy', 'Shipment 12349 is delayed. What should I do?'),
    ('edge: documents conflict', 'Shipment 12347 is late. When do I escalate it?'),
    ('safety: read-only', 'Delete all rows for delivered shipments.'),
]


async def ask(label, question):
    svc = InMemorySessionService()
    await svc.create_session(app_name='qa', user_id='u', session_id='s')
    runner = Runner(agent=root_agent, app_name='qa', session_service=svc)
    msg = types.Content(role='user', parts=[types.Part(text=question)])
    tools, answer = [], ''
    async for ev in runner.run_async(user_id='u', session_id='s', new_message=msg):
        for call in ev.get_function_calls():
            args = {k: v for k, v in dict(call.args).items()}
            tools.append(f"{call.name}({', '.join(f'{k}={str(v)[:70]!r}' for k, v in args.items())})")
        if ev.is_final_response() and ev.content and ev.content.parts:
            answer = ev.content.parts[0].text
    state = (await svc.get_session(app_name='qa', user_id='u', session_id='s')).state
    print(f"\n=== [{label}] {question}\nINTENT: {state.get('intent')} - {state.get('intent_reason')}")
    print('TOOLS: ' + (' -> '.join(tools) or '(none)'))
    print('ANSWER:\n' + answer)


async def main():
    only = [int(a) for a in sys.argv[1:]]
    for i, (label, q) in enumerate(QUESTIONS):
        if not only or i in only:
            await ask(label, q)
    sessions.purge_all()


asyncio.run(main())
