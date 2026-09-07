"""
Stub SEC client module for EDGAR data fetching.
Replace with actual implementation if needed.
"""

class SECClient:
    def __init__(self, api_key=None):
        self.api_key = api_key
    
    def get_company_facts(self, cik):
        return {}
    
    def get_submissions(self, cik):
        return {}
    
    def search_filings(self, **kwargs):
        return []

# Create default instance
client = SECClient()
