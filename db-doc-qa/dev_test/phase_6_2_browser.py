"""Browser run of the Sprint 6 multi-file checks against a running Streamlit UI.

Needs Playwright and a local Chrome, neither of which is a project dependency. Run from
`db-doc-qa/` with the UI already started (local mode, or API mode with API_URL set):

    uv run streamlit run app.py --server.port 8501 --server.headless true
    $env:UI_URL = "http://localhost:8501"
    uv run --no-project --with playwright python -m dev_test.phase_6_2_browser

Calls the real Gemini model, so answers can vary in wording; the checks look for values and
table names, not exact text. Pass the same UI_URL of a hosted UI to reuse it in Phase 6.3.
"""
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

UI = os.environ.get('UI_URL', 'http://localhost:8501')
S = Path('sample_data/database').resolve()
D = Path('sample_data/documents').resolve()
TMP = Path(tempfile.mkdtemp())
FAILS = []


def check(name, ok, detail=''):
    if not ok:
        FAILS.append(name)
    print(('PASS ' if ok else 'FAIL ') + name + (f' -- {detail}' if detail else ''))


def settled(page):
    page.wait_for_timeout(1500)
    page.locator('[data-testid=stStatusWidget]').wait_for(state='detached', timeout=60000)


def open_app(browser):
    page = browser.new_page(viewport={'width': 1400, 'height': 1000})
    page.goto(UI)
    page.get_by_text('Data files').first.wait_for(timeout=120000)
    settled(page)
    return page


def upload(page, path, index=0):
    """Sets a file on the data (0) or document (1) uploader."""
    page.locator('input[type=file]').nth(index).set_input_files(str(path))
    page.wait_for_timeout(3000)
    settled(page)


def tables(page):
    """The data-file lines, e.g. ['sales.csv | table sales · 50 rows × 5 columns']."""
    texts = page.locator('[data-testid=stMarkdownContainer]').all_inner_texts()
    return [t.replace('\n', ' | ') for t in texts if 'rows ×' in t]


def errors(page):
    out = [e.inner_text().strip().replace('\n', ' ') for e in page.locator('[data-testid=stAlert]').all()]
    out += [l for l in page.locator('[data-testid=stFileUploader]').first.inner_text().splitlines() if 'Error' in l]
    return out


def remove_first_data_file(page):
    page.get_by_role('button', name='Remove', exact=True).first.click()
    page.wait_for_timeout(3000)
    settled(page)


def end_session(page):
    page.get_by_role('button', name='End session').click()
    page.get_by_text('(sample)').first.wait_for(timeout=60000)
    settled(page)


def msgs(page):
    return page.locator('[data-testid=stChatMessage]')


def ask(page, q, timeout=120000):
    before = msgs(page).count()
    page.get_by_test_id('stChatInputTextArea').fill(q)
    page.keyboard.press('Enter')
    page.wait_for_function(
        """n => { const m = document.querySelectorAll('[data-testid=stChatMessage]');
                 if (m.length < n) return false;
                 const t = m[m.length - 1].innerText;
                 return !t.includes('Thinking...') && t.includes('Source'); }""",
        arg=before + 2, timeout=timeout)
    page.wait_for_timeout(1000)
    last = msgs(page).last
    return last.inner_text().replace('\n', ' | '), last


def sql_shown(last):
    last.get_by_text('How this was answered').click()
    last.page.wait_for_timeout(500)
    return last.inner_text().upper()


# A same-named copy of orders.csv with different content, and a file over the 500 KB limit.
(TMP / 'v2').mkdir()
(TMP / 'v2' / 'orders.csv').write_text('order_id,customer_id,amount\n1,1,1.0\n2,2,2.0\n', encoding='utf-8')
(TMP / 'big.csv').write_text('a,b\n' + '1,xxxxxxxxxx\n' * 50000, encoding='utf-8')
(TMP / 'upper').mkdir()
(TMP / 'upper' / 'Sales.CSV').write_bytes((S / 'multi_db' / 'sales.csv').read_bytes().replace(b'1001', b'9001'))

with sync_playwright() as p:
    b = p.chromium.launch(channel='chrome', headless=True)
    page = open_app(b)

    print('--- sample on load ---')
    check('sample shown', any('table data · 60 rows × 11 columns' in t for t in tables(page)), str(tables(page)))

    print('--- single file ---')
    upload(page, S / 'multi_db' / 'sales.csv')
    t = tables(page)
    check('sample replaced by sales.csv', len(t) == 1 and 'sales.csv' in t[0] and 'table sales · 50 rows × 5 columns' in t[0], str(t))
    text, last = ask(page, 'How many records are there?')
    check('count answer is 50', '50' in text, text[:150])

    print('--- three files and a cross-table question ---')
    upload(page, S / 'multi_db' / 'customers.csv')
    upload(page, S / 'multi_db' / 'orders.csv')
    t = tables(page)
    check('three tables', len(t) == 3 and all(x in ' '.join(t) for x in ('table sales', 'table customers', 'table orders')), str(t))
    text, last = ask(page, 'Which customer has the highest total order amount, and what is the total?')
    check('Grace Hopper with 499.99', 'Grace Hopper' in text and '499.99' in text, text[:200])
    check('JOIN shown in expander', 'JOIN' in sql_shown(last))

    print('--- limit and replacement ---')
    page.get_by_test_id('stChatInputTextArea').fill('')
    upload(page, S / 'multi_sheet.xlsx')
    check('fourth file rejected', any('Up to 3 data files' in e for e in errors(page)) and len(tables(page)) == 3, str(errors(page)))
    upload(page, TMP / 'v2' / 'orders.csv')
    t = tables(page)
    check('same name replaces at the limit', len(t) == 3 and 'orders.csv' in t[2] and '2 rows × 3 columns' in t[2] and not errors(page), str(t))

    print('--- invalid files keep the loaded tables ---')
    for n in ['notes.txt', 'empty.csv', 'headerless.csv', 'numeric_header.csv', 'duplicate_header.csv', 'corrupt.xlsx']:
        upload(page, S / n)
        check(n, bool(errors(page)) and len(tables(page)) == 3, (errors(page) or ['no error shown'])[0][:100])
    upload(page, TMP / 'big.csv')
    check('over 500 KB', any('500 KB' in e or 'too large' in e.lower() or 'limit' in e.lower() for e in errors(page)) and len(tables(page)) == 3, str(errors(page))[:120])

    print('--- name collisions ---')
    end_session(page)
    upload(page, S / 'multi_db' / 'sales.csv')
    upload(page, S / 'sales.xlsx')
    t = tables(page)
    check('sales and sales_2', len(t) == 2 and 'table sales ·' in t[0] and 'table sales_2 ·' in t[1], str(t))
    upload(page, TMP / 'upper' / 'Sales.CSV')
    t = tables(page)
    check('Sales.CSV replaces sales.csv in place', len(t) == 2 and 'Sales.CSV' in t[0] and 'table sales ·' in t[0] and 'table sales_2 ·' in t[1], str(t))

    print('--- multi-sheet workbook ---')
    end_session(page)
    upload(page, S / 'multi_sheet.xlsx')
    check('ignored sheet warning', any('summary' in e for e in errors(page)), str(errors(page)))

    print('--- Remove ---')
    end_session(page)
    upload(page, S / 'multi_db' / 'sales.csv')
    upload(page, S / 'multi_db' / 'customers.csv')
    remove_first_data_file(page)
    t = tables(page)
    check('removing one keeps the other', len(t) == 1 and 'customers.csv' in t[0], str(t))
    remove_first_data_file(page)
    check('removing all returns the sample', any('(sample)' in t for t in page.locator('[data-testid=stMarkdownContainer]').all_inner_texts()))

    print('--- documents with data files loaded ---')
    upload(page, S / 'multi_db' / 'sales.csv')
    docs_remove = page.get_by_role('button', name='Remove', exact=True).nth(1)  # nth(0) is the data file
    docs_remove.click()
    page.wait_for_timeout(3000)
    settled(page)
    body = page.inner_text('body')
    check('a sample document was removed', body.count('chunks') == 2, '')
    upload(page, D / 'structured.md', index=1)
    body = page.inner_text('body')
    check('document added', 'structured.md' in body and body.count('chunks') == 3, f"{body.count('chunks')} chunks; {errors(page)}")
    (TMP / 'docs').mkdir()
    (TMP / 'docs' / 'structured.md').write_text('# Other\n\n## 1. Intro\n\nReplacement text.\n', encoding='utf-8')
    upload(page, TMP / 'docs' / 'structured.md', index=1)
    check('document replaced by name', page.inner_text('body').count('structured.md') >= 1 and page.inner_text('body').count('chunks') == 3)
    text, last = ask(page, 'What does the shipment delay policy say about notifying customers of a delay?')
    check('policy question answered with a citation', 'shipment_delay_policy' in text, text[:200])
    check('data file still loaded', len(tables(page)) == 1 and 'sales.csv' in tables(page)[0])

    b.close()

print('\nFAILED: ' + ', '.join(FAILS) if FAILS else '\nALL CHECKS PASSED')
