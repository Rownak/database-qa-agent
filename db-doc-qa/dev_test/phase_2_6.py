"""Browser run of the Sprint 1.7 checks against the deployed UI, plus session isolation.

Needs Playwright and a local Chrome, neither of which is a project dependency. Run from
`db-doc-qa/` (PowerShell):

    $env:UI_URL = "https://data-qa-ui-701944778941.us-central1.run.app"
    uv run --no-project --with playwright python -m dev_test.phase_2_6

Calls the real Gemini model through the real services, so answers can vary in wording.
"""
import os
import tempfile
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

UI = os.environ.get('UI_URL', 'https://data-qa-ui-701944778941.us-central1.run.app')
S = Path('sample_data/database').resolve()
SALES = Path('sample_data/database/sales.csv').resolve()


def msgs(page):
    return page.locator('[data-testid=stChatMessage]')


def ask(page, q, timeout=120000):
    """Sends a question and returns (answer text, expander shown?)."""
    before = msgs(page).count()
    page.get_by_test_id('stChatInputTextArea').fill(q)
    page.keyboard.press('Enter')
    # Done when the new assistant message exists, has stopped "Thinking...", and ends with a Source line.
    page.wait_for_function(
        """n => { const m = document.querySelectorAll('[data-testid=stChatMessage]');
                 if (m.length < n) return false;
                 const t = m[m.length - 1].innerText;
                 return !t.includes('Thinking...') && t.includes('Source:'); }""",
        arg=before + 2, timeout=timeout)
    page.wait_for_timeout(1000)
    last = msgs(page).last
    return last.inner_text().replace('\n', ' | '), last


def settled(page):
    page.wait_for_timeout(1500)
    page.locator('[data-testid=stStatusWidget]').wait_for(state='detached', timeout=60000)


def open_app(browser):
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    page.goto(UI)
    page.get_by_text('Current data:').first.wait_for(timeout=120000)
    return page


def upload(page, path):
    page.set_input_files('input[type=file]', str(path))
    page.wait_for_timeout(4000)
    settled(page)


def current(page):
    return page.get_by_text('Current data:').first.inner_text()


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f' -- {detail}' if detail else ''))


df = pd.read_csv(SALES)
avg = round(df['sales_amount'].mean(), 2)
top = df.groupby('category')['revenue'].sum().idxmax()

with sync_playwright() as p:
    b = p.chromium.launch(channel='chrome', headless=True)
    page = open_app(b)

    print('--- valid files ---')
    check('sample on load', 'sales.csv (sample)' in current(page) and '50 rows' in page.get_by_text('rows ×').first.inner_text())
    upload(page, S / 'sales.xlsx')
    check('sales.xlsx', 'sales.xlsx' in current(page) and '50 rows × 5 columns' in page.get_by_text('rows ×').first.inner_text())
    upload(page, S / 'multi_sheet.xlsx')
    check('multi_sheet.xlsx lists ignored sheet', 'summary' in page.locator('[data-testid=stAlert]').first.inner_text())

    print('--- invalid files (each must show an error and keep the data) ---')
    for n in ['notes.txt', 'empty.csv', 'headerless.csv', 'numeric_header.csv', 'duplicate_header.csv', 'corrupt.xlsx']:
        upload(page, S / n)
        errs = [e.inner_text().strip()[:90] for e in page.locator('[data-testid=stAlert]').all()]
        # The uploader itself rejects other file types in the browser ("Error: ... not allowed").
        errs += [l for l in page.locator('[data-testid=stFileUploader]').inner_text().splitlines() if 'Error' in l]
        err = [e for e in errs if 'summary' not in e]
        check(n, bool(err) and 'multi_sheet.xlsx' in current(page), err[0].replace('\n', ' ') if err else 'no error shown')
    page.get_by_role('button', name='End session').click()
    page.get_by_text('Current data: sales.csv (sample)').wait_for(timeout=60000)
    settled(page)

    print('--- questions on the sample ---')
    answers = {}
    for q in ['How many records are there?', 'What is the average sales amount?',
              'Which category has the highest total revenue?', 'Show 5 sample rows.',
              'Give me statistics for the revenue column.', 'What is the profit margin?',
              'Delete all rows', 'How many records are there?']:
        text, last = ask(page, q)
        answers[q] = text
        check(f'"{q}" has Source:', 'Source:' in text, text[:120])
        if q.startswith('Which category'):
            last.get_by_text('How this was answered').click()
            page.wait_for_timeout(500)
            check('GROUP BY shown in expander', 'GROUP BY' in last.inner_text().upper(), '')
    check('count is 50', '50' in answers['How many records are there?'])
    check(f'average is {avg}', f'{avg:.2f}' in answers['What is the average sales amount?'].replace(',', ''), '')
    check(f'top category is {top}', top in answers['Which category has the highest total revenue?'])
    pm = answers['What is the profit margin?'].lower()
    check('profit margin explained, no number', any(w in pm for w in ('cost', 'not exist', 'missing', 'not available')) and '%' not in pm)
    check('delete refused', any(w in answers['Delete all rows'].lower() for w in ('read-only', 'cannot', "can't", 'unable', 'not able')))

    print('--- End session ---')
    page.get_by_role('button', name='End session').click()
    page.get_by_text('Current data: sales.csv (sample)').wait_for(timeout=60000)
    settled(page)
    check('chat cleared and sample restored', msgs(page).count() == 0)

    print('--- session isolation (two windows, two uploads) ---')
    tmp = Path(tempfile.mkdtemp())
    (tmp / 'five.csv').write_text('id,item\n' + '\n'.join(f'{i},alpha{i}' for i in range(5)))
    (tmp / 'twelve.csv').write_text('id,item\n' + '\n'.join(f'{i},beta{i}' for i in range(12)))
    a = open_app(b)
    c = open_app(b)
    upload(a, tmp / 'five.csv')
    upload(c, tmp / 'twelve.csv')
    ta, _ = ask(a, 'How many records are there?')
    tc, _ = ask(c, 'How many records are there?')
    ta2, _ = ask(a, 'What is the value of item in the first row?')
    check('window A sees its 5 rows', '5' in ta and 'twelve' not in ta, ta[:100])
    check('window B sees its 12 rows', '12' in tc, tc[:100])
    check('window A still its own data', 'alpha' in ta2.lower(), ta2[:100])
    b.close()
