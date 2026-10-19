"""Checks the deployed services from outside: UI is public and healthy, backend is private."""
import urllib.error
import urllib.request

UI = 'https://data-qa-ui-701944778941.us-central1.run.app'
API = 'https://data-qa-api-701944778941.us-central1.run.app'


def status(url):
    try:
        return urllib.request.urlopen(url, timeout=60).status
    except urllib.error.HTTPError as e:
        return e.code


print('UI page:', status(UI + '/'), '(expect 200)')
print('UI health:', urllib.request.urlopen(UI + '/_stcore/health', timeout=60).read().decode(), '(expect ok)')
print('API without a token:', status(API + '/list-apps'), '(expect 403)')
