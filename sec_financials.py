"""
Stub SEC financials module for financial data fetching.
Replace with actual implementation if needed.
"""

class SECFinancials:
    def __init__(self):
        pass
    
    def get_income_statement(self, cik, filing_type='10-K'):
        return {}
    
    def get_balance_sheet(self, cik, filing_type='10-K'):
        return {}
    
    def get_cash_flow(self, cik, filing_type='10-K'):
        return {}

# Create default instance
financials = SECFinancials()
