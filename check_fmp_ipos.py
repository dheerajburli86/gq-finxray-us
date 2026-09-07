import sys; sys.path.insert(0, '.')
import fmp_client
from datetime import date, timedelta

from_date = date.today().isoformat()
to_date = (date.today() + timedelta(days=30)).isoformat()
ipos = fmp_client.get_ipo_calendar(from_date, to_date)
print(f'FMP IPO Calendar returned: {len(ipos)} IPOs')
for ipo in ipos[:3]:
    print(f'  {ipo.get("symbol")}: {ipo.get("company")}, {ipo.get("date")}')
