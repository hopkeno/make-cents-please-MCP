# Changelog

Each release is published to https://github.com/hopkeno/make-cents-please-MCP.

## v0.1.0

First public release.

- **YNAB, read:** every read endpoint of the YNAB API, as compact tools with filters for
  dates, months, accounts, categories, payees, amounts and text.
- **YNAB, write (opt-in):** 16 tools covering every YNAB write endpoint. They are off
  unless `YNAB_ALLOW_WRITES=true`. Every write needs a preview and a one-time
  confirmation token, and approval prompts show names that are checked against ids.
- **Charity Record (opt-in):** read and summarize Charity Record CSV exports, reconcile
  them with YNAB donations, and write import files from donations you describe (mileage,
  donated items, cash and more) or that come from YNAB. Turned on with `CHARITYRECORD_DIR`.
- **Caching and rate limits:** in-memory caching with YNAB delta requests, and handling
  for YNAB's 200 requests per hour limit.
- **Safety:** the YNAB token is never logged or returned, and logs go to stderr only.
