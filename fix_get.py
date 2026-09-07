import sys; sys.path.insert(0, '.')
content = open('fmp_client.py').read()

# Find and replace the _get function to handle /stable/ paths
old_get = '''def _get(path, params=None, timeout=20, retries=2):
    api_key = os.getenv("FMP_API_KEY", "")
    url = f"https://financialmodelingprep.com/api/v3/{path}"'''

new_get = '''def _get(path, params=None, timeout=20, retries=2):
    api_key = os.getenv("FMP_API_KEY", "")
    # Handle /stable/ endpoints separately (they use /stable/ not /api/v3/)
    if path.startswith("stable/"):
        url = f"https://financialmodelingprep.com/{path}"
    else:
        url = f"https://financialmodelingprep.com/api/v3/{path}"'''

content = content.replace(old_get, new_get)
open('fmp_client.py', 'w').write(content)
print('Fixed: _get() now handles /stable/ paths')
