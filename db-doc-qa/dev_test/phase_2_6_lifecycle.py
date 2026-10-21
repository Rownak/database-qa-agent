"""Instance lifecycle check: replace the backend instance under an open tab, then ask again.

Same setup as phase_2_6.py, plus `gcloud` on the PATH (or GCLOUD=full path). The backend
keeps sessions in memory, so a new revision forgets them; the UI must recreate the
session from the upload it still holds, show the reset notice and answer with the same data.
Adds then removes an env var on data-qa-api (two extra revisions).
"""
import os
import subprocess
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

UI = os.environ.get('UI_URL', 'https://data-qa-ui-701944778941.us-central1.run.app')
GCLOUD = os.environ.get('GCLOUD', 'gcloud')


def gcloud_update(*flags):
    out = subprocess.run([GCLOUD, 'run', 'services', 'update', 'data-qa-api', '--region', 'us-central1', *flags, '--quiet'],
                         capture_output=True, text=True, shell=True)
    return out.returncode


def ask(page, q, timeout=180000, after_reset=False):
    """Sends a question; after a reset the chat is cleared, so only 2 messages are expected."""
    before = 0 if after_reset else page.locator('[data-testid=stChatMessage]').count()
    page.get_by_test_id('stChatInputTextArea').fill(q)
    page.keyboard.press('Enter')
    if after_reset:  # the old answer is still on screen until the UI reports the reset
        page.get_by_text('chat was reset').wait_for(timeout=timeout)
    page.wait_for_function(
        """n => { const m = document.querySelectorAll('[data-testid=stChatMessage]');
                 if (m.length < n) return false;
                 const t = m[m.length - 1].innerText;
                 return !t.includes('Thinking...') && t.includes('Source:'); }""",
        arg=before + 2, timeout=timeout)
    page.wait_for_timeout(1000)
    return page.locator('[data-testid=stChatMessage]').last.inner_text().replace('\n', ' | ')


tmp = Path(tempfile.mkdtemp()) / 'lifecycle.csv'
tmp.write_text('id,item\n' + '\n'.join(f'{i},gamma{i}' for i in range(7)))

with sync_playwright() as p:
    b = p.chromium.launch(channel='chrome', headless=True)
    page = b.new_page(viewport={'width': 1400, 'height': 900})
    page.goto(UI)
    page.get_by_text('Current data:').first.wait_for(timeout=120000)
    page.set_input_files('input[type=file]', str(tmp))
    page.get_by_text('Current data: lifecycle.csv').wait_for(timeout=60000)
    page.wait_for_timeout(3000)
    print('before:', ask(page, 'How many records are there?')[:110])

    print('replacing the backend instance (new revision)...')
    print('update rc:', gcloud_update('--update-env-vars', 'LIFECYCLE_TEST=1'))

    t = time.time()
    after = ask(page, 'What is the value of item in the first row?', after_reset=True)
    print(f'after ({time.time() - t:.1f}s incl. cold start): {after[:140]}')
    notice = page.get_by_text('chat was reset').count()
    print('reset notice shown:', bool(notice), '| answer uses the uploaded data:', 'gamma0' in after,
          '| only the retried question in chat:', page.locator('[data-testid=stChatMessage]').count() == 2)
    b.close()

print('restoring: removing the test env var, rc:', gcloud_update('--remove-env-vars', 'LIFECYCLE_TEST'))
