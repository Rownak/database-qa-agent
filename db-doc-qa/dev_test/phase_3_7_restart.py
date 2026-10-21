"""Phase 3.7: forces a new backend revision mid-session and asks the same hybrid question again.

Expected: the UI gets a 404, recreates the session from the table and documents it holds, shows
"the chat was reset", and the new instance rebuilds data.db and docs.db and gives the same facts.
Needs Playwright, a local Chrome and gcloud (Cloud SDK `bin` on PATH). Run from `db-doc-qa/`:

    $env:UI_URL = "https://data-qa-ui-701944778941.us-central1.run.app"
    uv run --no-project --with playwright python -m dev_test.phase_3_7_restart
"""
import os
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

UI = os.environ.get('UI_URL', 'https://data-qa-ui-701944778941.us-central1.run.app')
QUESTION = 'Shipment 12345 is delayed. What should I do?'
results = []


def check(name, ok, detail=''):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name + (f' -- {detail}' if detail else ''), flush=True)


def ask(page, question, reset=False, timeout=240000):
    msgs = page.locator('[data-testid=stChatMessage]')
    before = msgs.count()
    page.get_by_test_id('stChatInputTextArea').fill(question)
    page.keyboard.press('Enter')
    waited, previous = 0, None
    while True:
        page.wait_for_timeout(1500)
        waited += 1500
        # After a backend restart the chat is cleared and only the retried question remains (2 messages).
        if msgs.count() >= (2 if reset else before + 2):
            current = msgs.last.inner_text()
            if 'Thinking...' not in current and len(current.strip()) > 20 and current == previous:
                break
            previous = current
        if waited > timeout:
            raise TimeoutError('no answer')
    last = msgs.last
    text = last.inner_text()
    if 'Intent:' not in text:  # the expander is closed; after a session reset it can already be open
        last.get_by_text('How this was answered').click()
        page.wait_for_timeout(700)
        text = last.inner_text()
    tools = [l.strip() for l in text.splitlines() if l.strip() in ('run_sql', 'search_documents')]
    return text, tools


with sync_playwright() as p:
    b = p.chromium.launch(channel='chrome', headless=True)
    page = b.new_page(viewport={'width': 1400, 'height': 1000})
    page.goto(UI)
    page.get_by_text('Current data:').first.wait_for(timeout=150000)
    t1, tools1 = ask(page, QUESTION)
    check('before restart: run_sql then search_documents', tools1[:1] == ['run_sql'] and 'search_documents' in tools1, tools1)

    mark = str(int(time.time()))
    env = dict(os.environ, PATH=os.environ['PATH'] + r';C:\Users\rowna\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin')
    out = subprocess.run(['cmd', '/c', 'gcloud', 'run', 'services', 'update', 'data-qa-api', '--region', 'us-central1',
                          f'--update-env-vars=RESTART_MARK={mark}'], capture_output=True, text=True, env=env)
    revision = re.search(r'revision \[(\S+)\]', out.stdout + out.stderr)
    print('new revision:', revision.group(1) if revision else out.stderr[-300:], flush=True)

    t2, tools2 = ask(page, QUESTION, reset=True)
    check('chat was reset notice shown', 'chat was reset' in page.locator('body').inner_text())
    check('only the retried question is in the chat', page.locator('[data-testid=stChatMessage]').count() == 2)
    check('after restart: same tool sequence start', tools2[:1] == ['run_sql'] and 'search_documents' in tools2, tools2)
    facts = lambda t: all(w in t.lower() for w in ('cleveland', 'express', 'from the documents'))
    check('same facts before and after (location, type, document section)', facts(t1) and facts(t2))
    b.close()

print(f'\n{sum(results)}/{len(results)} checks passed')
sys.exit(0 if all(results) else 1)
