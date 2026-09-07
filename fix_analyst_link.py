import sys; sys.path.insert(0, '.')
content = open('analyst_ratings_poller.py').read()

# Replace website link with API JSON endpoint
old_link = '''        # Link to FMP analyst estimates page
        filing_url = f"https://site.financialmodelingprep.com/analyst-estimates/{ticker.upper()}"'''

new_link = '''        # Link to FMP analyst estimates API endpoint (JSON)
        fmp_api_key = os.getenv('FMP_API_KEY')
        filing_url = f"https://financialmodelingprep.com/stable/analyst-estimates?symbol={ticker.upper()}&apikey={fmp_api_key}"'''

content = content.replace(old_link, new_link)
open('analyst_ratings_poller.py', 'w').write(content)
print('✓ Updated: Feature 12 now links to FMP analyst-estimates API JSON endpoint')
