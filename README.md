# Energy dashboard data

Daily data bundle for the [North American Energy Markets Dashboard](https://energy-research.mv34214.chatgpt.site/reports/energy-markets-dashboard/) on the Energy Research site.

- `fetch_data.py` downloads public, key-free sources (EIA petroleum and natural gas history workbooks, the Alberta Economic Dashboard API, Bank of Canada exchange rates, and Yahoo Finance adjusted closes for equity baskets), validates each download, derives spreads, cracks and seasonal tables, and writes `data/series.json`, `data/manifest.json` and `data/bundle.json`.
- A GitHub Actions workflow runs it every weekday and commits the bundle when the data changed. The dashboard page loads `data/bundle.json` on open and falls back to its embedded copy.
- The canonical copy of the fetcher lives in the site repository's `research/energy-markets-dashboard/`; it is synced here by the weekly site refresh.

Sources: U.S. Energy Information Administration (public domain); Government of Alberta, Alberta Economic Dashboard (Open Government Licence – Alberta); Bank of Canada indicative daily exchange rates; equity data from Yahoo Finance via yfinance, published only as indexed levels and percentage returns.
