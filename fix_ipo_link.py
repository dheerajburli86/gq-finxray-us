import sys; sys.path.insert(0, '.')
content = open('main.py').read()

# Add FMP_IPO to FEATURES_WITH_LINKS
old = '''FEATURES_WITH_LINKS = {
    ("SEC_EDGAR", "8-K"),
    ("SEC_EDGAR", "10-Q"),
    ("SEC_EDGAR", "10-K"),
    ("SEC_EDGAR", "S-1"),
    ("SEC_EDGAR", "4"),'''

new = '''FEATURES_WITH_LINKS = {
    ("SEC_EDGAR", "8-K"),
    ("SEC_EDGAR", "10-Q"),
    ("SEC_EDGAR", "10-K"),
    ("SEC_EDGAR", "S-1"),
    ("SEC_EDGAR", "4"),
    ("FMP_IPO", "IPO_UPCOMING"),'''

content = content.replace(old, new)
open('main.py', 'w').write(content)
print('✓ Added IPO to FEATURES_WITH_LINKS')
