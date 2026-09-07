# Data Collection Sources and Processing Summary

This document summarizes the data collection, processing, validation, notification, and storage flows present in this backend. It expands the earlier summary with the missing `services` news pipelines, large transactions, all major announcement families, price alerts, and detailed heatmap behavior.

## 1. Services News

### Sources

The `services` app has a broad company-news ingestion layer, separate from the macro/economy news roundup. It fetches market and stock-specific news from these external feeds/APIs/pages:

- GNews business headlines API: `https://gnews.io/api/v4/top-headlines?category=business&lang=en&country=in&expand=content&max=25&page=<page_no>`
- GNews company search API: `https://gnews.io/api/v4/search?q=<ticker_or_company>&expand=content&lang=en`
- Moneycontrol latest news RSS: `https://www.moneycontrol.com/rss/latestnews.xml`
- Moneycontrol business RSS: `https://www.moneycontrol.com/rss/business.xml`
- Moneycontrol buzzing stocks RSS: `https://www.moneycontrol.com/rss/buzzingstocks.xml`
- Moneycontrol economy RSS: `https://www.moneycontrol.com/rss/economy.xml`
- Moneycontrol market reports RSS: `https://www.moneycontrol.com/rss/marketreports.xml`
- Livemint companies RSS: `https://www.livemint.com/rss/companies`
- Livemint markets RSS: `https://www.livemint.com/rss/markets`
- Livemint money RSS: `https://www.livemint.com/rss/money`
- Investing.com popular news RSS: `https://in.investing.com/rss/news_285.rss`
- Investing.com stock market RSS: `https://in.investing.com/rss/news_25.rss`
- Investing.com economy RSS: `https://in.investing.com/rss/news_14.rss`
- CNBC business RSS: `https://www.cnbctv18.com/commonfeeds/v1/cne/rss/business.xml`
- CNBC market RSS: `https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml`
- CNBC latest RSS: `https://www.cnbctv18.com/commonfeeds/v1/cne/rss/latest.xml`
- Business Standard markets RSS: `https://www.business-standard.com/rss/markets-106.rss`
- Business Standard latest RSS: `https://www.business-standard.com/rss/latest.rss`
- Business Standard top stories RSS: `https://www.business-standard.com/rss/home_page_top_stories.rss`
- Business Standard editor's pick RSS: `https://www.business-standard.com/rss/bsrss.xml`
- Business Standard companies RSS: `https://www.business-standard.com/rss/companies-101.rss`
- Business Standard companies people RSS: `https://www.business-standard.com/rss/companies/people-10121.rss`
- Business Standard companies interviews RSS: `https://www.business-standard.com/rss/companies/interviews-10122.rss`
- Business Standard companies news RSS: `https://www.business-standard.com/rss/companies/news-10101.rss`
- Business Standard industry RSS: `https://www.business-standard.com/rss/industry-217.rss`
- Business Standard auto industry RSS: `https://www.business-standard.com/rss/industry/auto-21701.rss`
- Business Standard SME industry RSS: `https://www.business-standard.com/rss/industry/sme-21702.rss`
- Business Standard banking industry RSS: `https://www.business-standard.com/rss/industry/banking-21703.rss`
- Business Standard agriculture industry RSS: `https://www.business-standard.com/rss/industry/agriculture-21704.rss`
- Business Standard industry news RSS: `https://www.business-standard.com/rss/industry/news-21705.rss`
- Business Standard market interviews RSS: `https://www.business-standard.com/rss/markets/interviews-10624.rss`
- Business Standard market news RSS: `https://www.business-standard.com/rss/markets/news-10601.rss`
- Business Standard stock market news RSS: `https://www.business-standard.com/rss/markets/stock-market-news-10618.rss`
- Business Line companies RSS: `https://www.thehindubusinessline.com/companies/feeder/default.rss`
- Business Line markets RSS: `https://www.thehindubusinessline.com/markets/feeder/default.rss`
- Economic Times CFO top stories RSS: `https://cfo.economictimes.indiatimes.com/rss/topstories`
- Economic Times CFO recent stories RSS: `https://cfo.economictimes.indiatimes.com/rss/recentstories`
- Economic Times CFO leadership RSS: `https://cfo.economictimes.indiatimes.com/rss/leadership`
- Economic Times CFO corporate finance RSS: `https://cfo.economictimes.indiatimes.com/rss/corporate-finance`
- Twitter feeder RSS endpoints from `settings.TWITTER_FEEDER_SERVICE_URL`, for handles such as `/twitter/moneycontrolcom/rss`, `/twitter/CNBCTV18News/rss`, `/twitter/livemint/rss`, `/twitter/bsindia/rss`, `/twitter/EconomicTimes/rss`, `/twitter/businessline/rss`, `/twitter/FinancialXpress/rss`, `/twitter/NDTVProfit/rss`, `/twitter/business_today/rss`, and `/twitter/ETNOWlive/rss`.
- Custom development/test feed: `test_csv_files/test_custom_news_feed.csv`, where each CSV row contains the source article `link`.

### Processing

The news flow parses feed entries or article pages, identifies relevant companies, maps them to `accounts.Stock`, filters irrelevant or duplicate items, summarizes relevant articles, assigns impact, and stores results in:

- `NewsUseful` for actionable stock-linked news.
- `NewsNotUseful` for skipped items with a reason.
- `RssFeedDataCount` and `SchedulerTimeMonitoring` for feed health and run monitoring.

News is delivered only to users whose watchlists include the mapped stock. The user-facing feed API combines `NewsUseful` and `AnnouncementUseful` for watchlist symbols in `services/views.py`.

### Prompts and Models

Services news uses prompts in `services/prompts`, including:

- `Prompt_N1_NewsSkipCheck.py` for skip/relevance checks.
- `Prompt_N2_CompanyNamesIdentification.py` for company extraction.
- `Prompt_N3_EntityRelevanceIdentification.py` for entity relevance.
- `Prompt_S1N_NewsSummarization.py` for news summary.
- `Prompt_C1_ImpactClassification.py` for impact scoring.
- `Prompt_V2_SimilarityCheck.py` for similar-summary filtering.
- `Prompt_V3_RelevanceCheck.py` and `Prompt_V1_SummaryValidation.py` for quality checks.

The active LLM wrapper is in `services/llm.py`; crawler/PDF flows currently use Gemini-family models through the local wrapper where required.

### Scheduling

Some legacy in-process scheduler jobs are now commented as moved to microservices, but the code retains the original references. Historical schedules include frequent RSS/news jobs, GNews ingestion, and custom feed ingestion. The Django scheduler still handles downstream notifications every 10 seconds and message queue stats every 15 seconds.

## 2. News Roundup

### Sources

The roundup flow is separate from stock-specific services news. It builds macro/economy digests from:

- `https://www.thehindubusinessline.com/economy/feeder/default.rss`
- `https://www.thehindu.com/business/Economy/feeder/default.rss`
- `https://www.moneycontrol.com/news/business/economy/`
- `https://www.business-standard.com/rss/economy-102.rss`
- `https://www.cnbctv18.com/commonfeeds/v1/cne/rss/economy.xml`
- `https://tradingeconomics.com/ws/stream.ashx?start=0&size=100&c=india`
- `https://tradingeconomics.com/ws/stream.ashx?start=0&size=100&i=economy`

### Trigger Times

- Morning: 09:00 AM, Sunday to Saturday.
- Evening: 09:00 PM, Monday to Friday.

### Prompts and Model

Roundup prompts include Indian-news filtering, similar-news filtering, top-N priority selection, fact checking, and final summarization. The earlier implementation used `gpt-4o-search-preview` through OpenAI API for web-aware roundup generation.

### Output

Roundup items are saved in `NewsRoundUp` with `headline`, `source`, `url`, `round_up_type`, and `timeframe`. Notifications are mapped through `NotificationNewsRoundupMapping`.

## 3. Announcements

### Announcement Families Covered

The services app handles normal PDFs, exchange RSS/XML, and XBRL filings from both NSE and BSE. The covered families include:

- General corporate announcements.
- Board meetings.
- Financial results.
- Shareholding pattern.
- Voting results.
- Insider trading.
- Related party transactions.
- Corporate actions.
- Credit ratings.
- Regulation 29 and Regulation 31 non-XBRL filings.
- Integrated filing financial results.
- Earnings-call identification on relevant announcements.
- Large transactions converted into actionable announcement notifications.

### Sources

NSE sources include:

- NSE announcement RSS/XML archives such as `Online_announcements.xml`, `Board_Meetings.xml`, `Financial_Results.xml`, and `Shareholding_Pattern.xml`.
- NSE XBRL/RSS service endpoints through `settings.XBRL_RSS_SERVICE_URL`.
- NSE APIs:
  - `https://www.nseindia.com/api/corporate-share-holdings-master`
  - `https://www.nseindia.com/api/corporates-corporateActions`
  - `https://www.nseindia.com/api/corporates-financial-results`
  - `https://www.nseindia.com/api/integrated-filing-results`
  - `https://www.nseindia.com/api/quote-equity`

BSE sources include:

- `https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w`
- `https://api.bseindia.com/BseIndiaAPI/api/Integratedfinancedata/w`
- `https://api.bseindia.com/BseIndiaAPI/api/SHPQNewFormat/w`
- BSE XBRL and corporate pages including financial results, voting result, shareholding, insider trading, related party transaction, corporate action, and credit-rating pages.

### Processing

The crawler layer fetches links and files, validates company mapping, calculates content hashes, downloads PDFs/ZIPs where needed, uploads PDF bytes to S3 for durable access, extracts text from PDFs, falls back to OCR for scanned or low-text PDFs, and then generates structured summaries.

The output is stored in:

- `AnnouncementUseful` for actionable items.
- `AnnouncementNotUseful` for duplicate, invalid, stale, unsupported, unknown-stock, or low-quality items.
- `TempFinancialResults` and `TempRelatedPartyTransaction` for staged filings.
- `AnnouncementXBRLDataframes` for structured dataframe payloads.
- `XBRLMetricStore` and `SourceDocument` for newer source-document and metric storage.

### XBRL Templates and Summary Types

XBRL-specific processing maps filing types to template names such as:

- `voting_results_summary`
- `shp_xbrl_summary`
- `it_xbrl_summary`
- `credit_rating_xbrl_summary`
- financial-result templates for different financial statement shapes.

Some filings produce multiple summaries for the same source document. These are stored with `summary_type` values such as `N1`, `N2`, etc., and guarded by a unique `(content_hash, summary_type)` constraint.

### Validations

Important validations include:

- Stock/company matching before saving.
- Subscribed-user availability before expensive processing in some flows.
- Content hash duplicate checks.
- NSE/BSE cross-source duplicate checks.
- XSD recognition for XBRL.
- Latest-period validation for shareholding pattern.
- Strict parameter count checks before WhatsApp template notification.
- Incomplete-summary and apparent-issue checks.
- OCR gibberish detection and fallback.
- JSON repair/fallback when LLM output is malformed.

## 4. Large Transactions

### Types

Large transaction tracking covers:

- NSE bulk deals.
- NSE block deals.
- NSE short deals where supported by the NSE large-deal payload.
- BSE bulk deals.

### Sources

- NSE bulk deals CSV: `https://nsearchives.nseindia.com/content/equities/bulk.csv`
- NSE block deals CSV: `https://nsearchives.nseindia.com/content/equities/block.csv`
- NSE large-deal snapshot API: `https://www.nseindia.com/api/snapshot-capital-market-largedeal`
- BSE bulk deal page: `https://www.bseindia.com/markets/equity/EQReports/bulk_deals.aspx?expandable=3`

### Processing

The tracker normalizes exchange columns, maps symbols/names to `Stock`, skips stocks without subscribed users, removes same-client same-day buy/sell pairs, merges repeated rows by date/symbol/client/action, calculates quantity-weighted average trade price, formats total quantity and value in Indian units, and stores each unique message in `LargeTransaction`.

New large transactions are also written as `AnnouncementUseful` with types such as `Bulk Deals` or `Block Deals`, `HIGH` impact, and template parameters. They are then distributed through the normal announcement notification path.

### Deduplication

The tracker looks back five days in `LargeTransaction` and avoids re-sending the same generated message. `AnnouncementUseful` also applies content-hash and announcement-id duplicate checks.

## 5. Price, Volume, and Technical Alerts

### Sources

Alerts are based on subscribed stocks and the external price-action service:

- `settings.PRICE_ACTION_SERVICE_URL + "get-all-data"`

The service returns fields such as last price, previous close, last trade time, price z-score, volume z-score, average volume, RSI flags, 200-SMA flags, 52-week high/low flags, and rising/falling price with high volume flags.

### Alert Types

The alert system supports:

- Price alert: extraordinary price move based on price z-score.
- Volume alert: extraordinary volume based on volume z-score and traded value.
- RSI overbought and oversold alerts.
- 200-day moving average crossover and crossunder alerts.
- 52-week high and 52-week low breakout alerts.
- Rising price with high volume.
- Falling price with high volume.

### Processing

`AlertChecker.send_alerts()` handles intraday price and volume alerts. `AlertChecker.check_for_ta_alerts()` handles technical alerts. Each alert checks that the latest trade is from the current date, computes current price change from previous close, validates the configured `ALERT_Z_SCORE_THRESHOLD` where relevant, generates TradingView chart payloads, stores a `PriceAlert`, and creates user notifications through `add_price_alerts_to_user_notifications`.

### Validations

- Alerts are sent only during the configured market window, currently 09:00 to 17:30 local server time.
- Each stock/alert type is sent at most once per day through `check_for_today_alerts`.
- Alerts require a mapped subscribed stock.
- Template parameters must be exactly three values before storing.
- Chart payloads are generated with different study settings for price/volume, RSI, 200-SMA, 52-week levels, and high-volume trend alerts.

## 6. Heatmaps

The heatmap system creates image-based notifications under `media/heatmaps` and stores them as `HeatmapMessages`. Images are attached to WhatsApp/app notifications through `Notification.heatmap`.

### Common Rendering Rules

All standard heatmaps use a 958-pixel wide image, a black header, Finxray logo, title, subtitle, date, and a grid of colored tiles.

For sector and watchlist heatmaps:

- Grid columns: 5.
- Cell height: 180 px.
- Gap: 12 px.
- Tile count: dynamic based on input rows.
- Tile colors: green for positive return, red for negative return, white around neutral.
- Color intensity is normalized against the maximum absolute return in that heatmap.
- Text color is selected automatically based on background luminance.

### 6.1 Sector Heatmap

The sector heatmap summarizes Indian sector/index performance.

Sources:

- NIFTY Indices constituent CSVs from `https://www.niftyindices.com/IndexConstituent/`.
- NSE bhavcopy ZIP files from `https://nsearchives.nseindia.com/products/content/`.
- NSE equity quote API `https://www.nseindia.com/api/quote-equity`.
- Zerodha quote/historical service through `settings.ZERODHA_SERVICE_URL`.

Periods:

- `DAILY`: today versus previous close/trading day.
- `WEEKLY`: current/last available close versus previous week close, only when the run date is the last trading day of the week.
- `MONTHLY`: current/last available close versus previous month close, only when the run date is the last trading day of the month.

Scheduling:

- Daily sector heatmap: Monday to Saturday at 12:31 and 15:31 in the current scheduler.
- Weekly sector heatmap: Monday to Saturday at 16:41, guarded by last-trading-day-of-week logic.
- Monthly sector heatmap: Monday to Saturday at 17:31, guarded by last-trading-day-of-month logic.

Tile count:

- The generator can render any number of sector rows because height is dynamic.
- The current sector payload should normally stay around the number of configured sector/index buckets, keeping it readable in WhatsApp.

Output:

- Image file named like `sector_heatmap_<PERIOD>_<timestamp>.jpg`.
- `HeatmapMessages` record.
- Notification section points users to `sector-heatmap`.

### 6.2 Watchlist / Personal Heatmap

The personal heatmap shows performance of stocks in a single user watchlist.

Sources:

- User watchlist from `CustomUser.watch_list`.
- Stock metadata from `accounts.Stock`.
- Historical prices through Zerodha service.
- Shared date, holiday, previous trading day, and return calculation helpers from `SectorDataProcessor`.

How it works:

- The processor builds stock performance data for the period.
- For each user, it checks each watchlist stock against available performance data.
- The user is skipped when no watchlist stocks exist.
- The user is also skipped if successful performance coverage is less than 75 percent of the watchlist.
- Remaining items are sorted by return from best to worst.

What if the user has too many stocks:

- If the watchlist performance list has more than 25 stocks, it is trimmed.
- The trim keeps the top 12 gainers and bottom 12 losers.
- That means very large watchlists show the most informative extremes instead of an unreadably tall image.
- The effective maximum rendered watchlist tile count is therefore 24 after trimming, even though the code log says trimming to 25 entries.

Tile count:

- For 1 to 25 stocks, the heatmap is dynamic with 5 columns and enough rows.
- For more than 25 available stocks, the rendered set is top 12 plus bottom 12.
- Missing middle performers are intentionally omitted to preserve readability and highlight action.

Output:

- Image file named like `watchlist_heatmap_<PERIOD>_<user_id><timestamp>.jpg`.
- User-specific `HeatmapMessages` and notification.
- Notification button points to `watchlist-heatmap`.

Current scheduler status:

- Watchlist heatmap scheduler entries exist but are commented in the main scheduler. The implementation is present and can be enabled or moved to a separate service.

### 6.3 Country / World Equity Heatmap

The country heatmap shows global equity index performance by country and region.

Sources:

- Country/index configuration inside the country heatmap module.
- TradingEconomics-style historical price links and TE index identifiers.
- Market open/close status per index.
- Local flag assets in `media/flags`.

Periods and run types:

- Daily, with morning/afternoon/evening style run types supported in code.
- Weekly.
- Monthly.

Rendering:

- The image is grouped by run, continent, then country.
- Each tile includes country flag, country name, return, and market status chip: `OPEN` or `CLOSED`.
- Grid columns: 5.
- Country heatmap cell size: 169 x 195 px.
- Continents are separated visually so users can scan region-by-region.

Scheduling:

- Country heatmap jobs are present but commented in the main scheduler:
  - Daily morning around 09:00.
  - Daily afternoon around 14:30.
  - Daily evening around 20:30.
  - Weekly Saturday around 14:30.
  - Monthly on day 1 around 14:30.

Output:

- Image file named like `country_heatmap_<period>_<run_type>_<timestamp>.jpg`.
- `HeatmapMessages` record with heatmap type `COUNTRY_HEATMAP`.

### 6.4 Heatmap Notification Personalization

Sector and country heatmaps are broad-market notifications. They are added through `add_heatmap_to_user_notifications` with user exclusion flags and section names such as:

- `sector-heatmap`
- `country-heatmap`

Watchlist heatmaps are personal. They are added through `add_watchlist_heatmap_to_user_notifications`, receive a specific `user_id`, and generate a personalized URL containing a token and `section=watchlist-heatmap`.

## 7. Mutual Funds

### Sources

Mutual fund ingestion uses these external APIs:

- CMOTS fund house API: `https://gquantapi.cmots.com/api/Fund_House`
- CMOTS scheme master API: `https://gquantapi.cmots.com/api/SchemeMaster`
- CMOTS daily NAV API: `https://gquantapi.cmots.com/api/DailyNAV`
- CMOTS historical NAV API: `https://gquantapi.cmots.com/api/SchemeNAVHistorical/<scheme_code>/W/<weeks_missing>`
- CMOTS long-term historical NAV API: `https://gquantapi.cmots.com/api/SchemeNAVHistorical/<scheme_code>/Y/50`
- CMOTS 75-year NAV/API history call used by performance ingestion: `https://gquantapi.cmots.com/api/SchemeNAVHistorical/<scheme_code>/Y/75`
- CMOTS 10-year NAV/API history call: `https://gquantapi.cmots.com/api/SchemeNAVHistorical/<scheme_code>/Y/10`
- CMOTS company master API: `https://gquantapi.cmots.com/api/CompanyMaster`
- CMOTS fund manager API: `https://gquantapi.cmots.com/api/FundManager/<scheme_code>`
- CMOTS portfolio in API: `https://gquantapi.cmots.com/api/Whats_InOut/in/<scheme_code>`
- CMOTS portfolio out API: `https://gquantapi.cmots.com/api/Whats_InOut/out/<scheme_code>`
- CMOTS expense ratio/profile API: `https://gquantapi.cmots.com/api/SchemeProfileExpRatio`
- CMOTS mutual fund holding API: `https://gquantapi.cmots.com/api/MFHolding/<scheme_code>`
- NSE debt/event disclosure API: `https://www.nseindia.com/api/corporate-event-disclosure?index=Ebddata`

### Processing and Output

The mutual fund service stores structured fund, NAV, performance, manager, holding, and notification data in the mutual-funds app. It does not rely on LLM summarization for its core ingestion.

## 8. Finxray IPO

### Sources

- Chittorgarh main IPO dashboard: `https://www.chittorgarh.com/ipo/ipo_dashboard.asp`
- Chittorgarh SME IPO dashboard: `https://www.chittorgarh.com/ipo/ipo_dashboard.asp?a=sme`
- Investorgain live IPO GMP page: `https://www.investorgain.com/report/live-ipo-gmp/331/all/`
- Investorgain GMP data API: `https://webnodejs.investorgain.com/cloud/report/data-read/331/1/<month>/<year>/2024-25/0/all?search=`
- NSE current IPO issue API: `https://www.nseindia.com/api/ipo-current-issue`
- NSE IPO detail API: `https://www.nseindia.com/api/ipo-detail?symbol=<symbol>&series=<series>`
- NSE issue information page: `https://www.nseindia.com/market-data/issue-information?symbol=<symbol>&series=<series>&type=Active`
- BSE public issue API: `https://api.bseindia.com/BseIndiaAPI/api/GetPublicIssue/w`
- BSE public issue detail page: `https://www.bseindia.com/markets/publicIssues/DisplayIPO.aspx?id=<scrip_no>&type=IPO&idtype=1&status=F&IPONo=<ipo_num>&startdt=<start_date>`
- SEBI public issues filing page: `https://www.sebi.gov.in/filings/public-issues`
- RHP/DRHP PDF links discovered from Chittorgarh, SEBI, BSE, and NSE issue pages.

### Processing

IPO data is scraped hourly, RHP sections are extracted, and long documents are processed iteratively by section type. Summaries include risk factors, objectives, industry context, business, management, financial report, general details, and executive summary.

### Output

IPO details are stored locally and served through the IPO service endpoints. IPO notifications are generated every few seconds by the IPO notification manager.

## 9. Twitter Scrapper

### Sources

The standalone Twitter scraper uses `https://api.twitterapi.io` for handles such as Moneycontrol, Livemint, Business Standard, CNBC-TV18, and related market/news accounts.

### Processing and Output

It extracts tweet text and links, validates URLs from tweet entities/media, and serves RSS feeds through FastAPI endpoints such as `/twitter/moneycontrolcom/rss`.

## 10. Notification and Storage Summary

Key output models:

- `NewsUseful` and `NewsNotUseful`.
- `NewsRoundUp` and `NotificationNewsRoundupMapping`.
- `AnnouncementUseful`, `AnnouncementNotUseful`, and archive variants.
- `LargeTransaction`.
- `PriceAlert` and `AlertNotifications`.
- `HeatmapMessages`.
- `Notification`, `NotificationMessage`, and `ScheduledNotification`.
- `SourceDocument`, `InformationVersion`, `XBRLMetricStore`, and `LastProcessedTimestamp`.

Key delivery paths:

- Watchlist news and announcements are filtered to users whose watchlist contains the affected stock.
- Price alerts are delivered to users subscribed to the alerted stock.
- Sector and country heatmaps are broad-market messages with opt-out/exclusion flags.
- Watchlist heatmaps are generated per user and include only that user's watchlist performance.
- WhatsApp delivery is mediated by template metadata in `WhatsAppUtilityTemplates`, while queueing and retries are handled by the messaging queue service.
