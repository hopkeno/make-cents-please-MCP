# make-cents-please

A local [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (such as Claude Desktop) read your budget. YNAB is the first supported platform. It also works with [Charity Record](https://charityrecord.com) donation files (see [Charity Record](#charity-record)).

- **Read-only by default.** Every YNAB read endpoint is covered. Write tools (create, edit, delete) exist but stay switched off unless you enable them, and every write needs a preview and an explicit confirmation.
- **Local and single-user.** It runs on your machine over stdio. There is no hosting, no multi-user support and no UI.
- **Your token stays local.** The token is read from an environment variable and is never logged or returned in tool output.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A YNAB account and a personal access token

## Setup

1. Clone the repo and install dependencies:

   ```sh
   git clone https://github.com/hopkeno/make-cents-please-MCP.git
   cd make-cents-please-MCP
   uv sync
   ```

2. Create a YNAB personal access token. In YNAB, go to **Account Settings > Developer Settings > New Token** ([app.ynab.com/settings/developer](https://app.ynab.com/settings/developer)). Copy it right away, because YNAB only shows it once.

3. Copy the example env file and add your token:

   ```sh
   cp .env.example .env
   # then edit .env and set YNAB_ACCESS_TOKEN=...
   ```

   `.env` is git-ignored. Never commit it.

4. Optional check: run the tests.

   ```sh
   uv run pytest
   ```

### Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `YNAB_ACCESS_TOKEN` | yes | | Your YNAB personal access token |
| `YNAB_DEFAULT_PLAN_ID` | no | `last-used` | Plan (budget) used when a tool call has no `plan_id`. Find ids with the `ynab_list_plans` tool. |
| `YNAB_CACHE_TTL_SECONDS` | no | `300` | How long a cached response counts as fresh |
| `LOG_LEVEL` | no | `WARNING` | Log level for stderr output |
| `YNAB_ALLOW_WRITES` | no | `false` | Set to `true` to enable the write tools (see [Write tools](#write-tools)) |
| `CHARITYRECORD_DIR` | no | | Folder for Charity Record CSV files; turns on the Charity Record tools (see [Charity Record](#charity-record)) |

YNAB now calls budgets "plans", and the API and SDK use that name.

## Claude Desktop

Open the Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the server and use the absolute path to your clone:

```json
{
  "mcpServers": {
    "make-cents-please": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABSOLUTE/PATH/TO/make-cents-please-MCP",
        "run",
        "make-cents-please"
      ]
    }
  }
}
```

The server reads `YNAB_ACCESS_TOKEN` from the `.env` file in the repo. You can instead put the token in an `"env": { "YNAB_ACCESS_TOKEN": "..." }` block in this config. That file is outside the repo, but it is stored in plain text, so protect it.

Restart Claude Desktop after editing. If Claude Desktop cannot find `uv`, replace `"uv"` with its full path (`which uv` on macOS/Linux, `where uv` on Windows).

Try asking: "What are my account balances?", "Which categories are overspent this month?" or "How much did I spend on dining out in September?"

## Tools

These read tools are always available. Each takes an optional `plan_id`. List tools accept `limit` and `offset` and return `count`, `total` and, when more rows exist, `next_offset`.

| Tool | What it returns | Useful filters |
|---|---|---|
| `ynab_list_plans` | Plans with ids, currency and month range | `include_accounts` |
| `ynab_get_plan_settings` | Currency and date format | |
| `ynab_get_plan_overview` | Net worth, on-budget balance and entity counts, from the full plan export | |
| `ynab_get_user` | The token owner's user id | |
| `ynab_list_accounts` / `ynab_get_account` | Accounts and balances | `on_budget`, `include_closed`, `account_type` |
| `ynab_list_categories` / `ynab_get_category` | Assigned, activity, available and goal info | `month`, `group`, `name_contains`, `only_overspent`, `only_underfunded`, `include_hidden` |
| `ynab_list_months` / `ynab_get_month` | Month totals (income, assigned, activity, Ready to Assign, age of money), with category detail from `get_month` | `since_month`, `until_month`, `group`, `only_overspent`, `include_zero` |
| `ynab_list_payees` / `ynab_get_payee` | Payees | `name_contains`, `include_transfers` |
| `ynab_list_payee_locations` / `ynab_get_payee_location` | GPS locations recorded by the mobile app | `payee_id` |
| `ynab_list_transactions` | Transactions, newest first, with inflow/outflow/net totals for all matches | `since_date`, `until_date`, `month`, `account_id`, `category_id`, `payee_id`, `status` (uncategorized/unapproved), `cleared`, `direction`, `min_amount`, `max_amount`, `text` |
| `ynab_get_transaction` | One transaction with ids and splits | |
| `ynab_list_scheduled_transactions` / `ynab_get_scheduled_transaction` | Upcoming and recurring transactions | `account_id`, `category_id`, `due_before` |
| `ynab_list_money_movements` / `ynab_list_money_movement_groups` | Money moved between categories | `month` |
| `ynab_get_api_status` | Requests used this hour and cache settings, with no API call | |

### Output conventions and assumptions

- **Amounts are decimal strings** in the plan's currency, such as `"-12.34"`, converted from YNAB's milliunits. Negative means outflow.
- **Months** accept `current`, `YYYY-MM` or `YYYY-MM-01`. `current` means the current month in UTC.
- **Transactions default to the last 30 days** when no date or month is given, to keep responses small. YNAB's own default is one year.
- **Hidden categories, internal categories, closed accounts, transfer payees and deleted items** are left out unless you ask for them.
- **Results from scoped endpoints.** When you filter transactions by category or payee, YNAB returns matching split lines as separate rows, linked by `parent_transaction_id`.

## Write tools

Write tools are only registered when `YNAB_ALLOW_WRITES=true`. Without it, the server cannot change your budget at all.

| Tool | What it does |
|---|---|
| `ynab_create_transactions` | Create one or more transactions, including split transactions |
| `ynab_update_transaction` | Change fields of one transaction; omitted fields are kept |
| `ynab_update_transactions` | Change several transactions at once, e.g. approve or recategorize a batch |
| `ynab_delete_transaction` | Delete a transaction |
| `ynab_import_transactions` | Import new transactions from linked accounts |
| `ynab_create_scheduled_transaction` / `ynab_update_scheduled_transaction` / `ynab_delete_scheduled_transaction` | Manage scheduled and recurring transactions |
| `ynab_set_category_assigned` | Set the amount assigned to a category for a month |
| `ynab_create_category` / `ynab_update_category` | Create a category, or rename, move, or set or remove its goal |
| `ynab_create_category_group` / `ynab_update_category_group` | Create or rename a category group |
| `ynab_create_payee` / `ynab_update_payee` | Create or rename a payee |
| `ynab_create_account` | Create a manual (unlinked) account with a starting balance |

### How confirmation works

Every write takes two calls:

1. **Preview.** The assistant calls the tool without `confirmation_token`. The server checks the input and that the ids exist, then returns a preview (for updates and deletes, the current values too) and a one-time token. Nothing is changed.
2. **Confirm.** After you approve the preview, the assistant calls the same tool again with the same arguments plus the token. Only then is the request sent to YNAB.

The token is tied to the exact request that was previewed. If any argument changes, the token is refused and a new preview is needed. Each token works once and expires after 10 minutes.

What this does and does not guarantee: the server ensures that nothing is written without a preview and that what is written is exactly what was previewed. It cannot see your chat, so it relies on the assistant to show you the preview and wait for your answer, which the server's instructions and every tool description tell it to do. Claude Desktop also asks you to approve tool calls, which adds a second check; keep that approval on for write tools.

### Names in the approval prompt

Claude Desktop's approval prompt shows a tool's raw arguments. So that you approve names rather than ids, every write call must include:

- `plan_id` and `plan_name`. Writes never fall back to the default plan.
- A matching name for every other id: `account_name`, `category_name`, `category_group_name` and `payee_name`.

The server checks each name against its id and refuses the call if they don't match. The check is lenient:

- Case, extra spaces, emoji and other symbols are ignored.
- A shortened name is accepted if it is how the real name begins and stops at the end of a word, so "Home Maintenance" matches "Home Maintenance, Repairs, Appliances, and Other House Things" but "Home Maint" does not.

The id always decides what changes; the name is there so the approval prompt is readable. Renames take the current name plus `new_name`.

Transactions themselves have no name, so update and delete calls still show the transaction id. Their preview shows the date, payee and amount.

### Things to know

- **Amounts** are entered in currency units, such as `-23.45`. Negative means money out.
- **Assigning money:** `ynab_set_category_assigned` sets the total assigned for the month; it does not add to it. To move money between categories, lower one and raise the other.
- **Long names:** YNAB allows category names of at most 50 characters, and the server applies the same limit to category group names. It appears to check the existing name on every update, so a category whose current name is longer can't be changed until it is renamed. The preview warns about this, and the error explains it.
- **Split transactions:** their amount and categories cannot be changed through the API. Change those in the YNAB app, or delete and recreate the transaction.
- **After any write,** the cache is cleared so the next read shows fresh data.
- **Network errors during a write** may leave it unclear whether the change was applied. The error says so; check the data before trying again.

## Charity Record

[Charity Record](https://charityrecord.com) tracks charitable donations for taxes. It has no API, only CSV import and export, so these tools work with files in a folder you choose (`CHARITYRECORD_DIR`). Nothing is sent to Charity Record directly.

- **Reading:** in Charity Record, use Download data to export a year's CSVs into the folder. Subfolders one level deep are fine, so you can unzip a "Download All" file there.
- **Writing:** the assistant writes new import files into the folder. You upload them on Charity Record's import page and check its import preview before importing.

| Tool | What it does |
|---|---|
| `charityrecord_list_files` | List CSV files in the folder with their detected type, years and donation counts |
| `charityrecord_list_donations` | List exported donations, filtered by year, charity, type or dates, with totals |
| `charityrecord_summarize` | Totals by year, by donation type and by charity |
| `charityrecord_find_ynab_donations` | Find donations in YNAB by category or payee and shape them as cash donations |
| `charityrecord_reconcile` | Compare YNAB donations with your exports: missing in Charity Record, only in Charity Record, and amount mismatches |
| `charityrecord_create_import_files` | Write import CSVs, one per donation type, from donations you describe or that came from YNAB |

Things to know:

- **Donation types:** import files can hold any of Charity Record's types (items, cash, mileage, stock, vehicle, digital asset and art), in the column layout from Charity Record's [Import Format Guide](https://charityrecord.com/help/import-format-guide/). You can describe donations in plain language, for example "I drove 26 miles from home to the shelter for Charity X on October 12" or "here's what I gave the thrift store on October 20".
- **Mileage** is recorded as miles, the rate per mile and the dollar value. The rate defaults to 14 cents per mile, the charitable rate set by law (26 U.S.C. 170(i)), and can be overridden.
- **Values** are the ones you give. The assistant must not estimate fair market values.
- **Confirmation:** writing import files uses the same preview and one-time confirmation as other writes. It only writes local files, so it doesn't need `YNAB_ALLOW_WRITES`.
- **Duplicates:** donations that already appear in your exports (same type, charity, date and value) are left out by default. Charity Record adds imported rows rather than replacing them, so a duplicate would be recorded twice.
- **Never overwritten:** generated files are named `charityrecord-import-<date-time>-<type>.csv`, and existing files are never overwritten.
- **Export format:** Charity Record doesn't document its export columns. The reader was checked against a real 2026 export:
  - Every file matches the import layouts.
  - The cash file spells one column "Addtional Payment Details". Both spellings are accepted when reading, and import files use the export's spelling.
  - The Overall summary uses the types "Item" and "Money".
  - If a file isn't recognized, `charityrecord_list_files` shows the headers it found.
- **Recurring donations** are exported with the date "Various" and a times-per-year frequency. They are listed with their tax year, taken from the export's file name (for example `CharityRecord_2026_cash_....csv`), and left out of date-range filters. Reconcile reports a YNAB payment to a charity with a recurring entry as "possibly covered by recurring" rather than missing. The value Charity Record exports for a recurring donation is used as is, and it matches the Overall summary.

## Rate limits and caching

YNAB allows **200 requests per hour per token**. To stay under that limit:

- **In-memory cache.** Responses are kept in memory only and never written to disk, so the cache resets when the server restarts. Repeat questions within `YNAB_CACHE_TTL_SECONDS` make no requests.
- **Delta requests.** When cached data goes stale, it is refreshed with a delta request (`last_knowledge_of_server`) wherever YNAB supports one: the plan export, accounts, categories, months, payees, transactions and scheduled transactions. The changes are merged into the cache.
- **Request tracking.** The server counts requests in a sliding one-hour window and also reads YNAB's `X-Rate-Limit` header.
- **Near the limit.** Above 180 requests in the window, cached data is served with a `warning` saying it may be out of date.
- **At the limit, or after a 429 response.** Cached data is served if there is any. Otherwise the tool returns an error saying roughly how long to wait.

## Errors

Errors are short and tell the model what to do next. For example, a 404 says to look up valid ids with the matching `list_*` tool, a 401 says to replace the token, and a 5xx says to retry later. Malformed ids and dates are rejected before any request is sent.

## Safety

- **The token** comes only from the environment or `.env`. It is never logged, echoed or included in errors. Logs go to stderr, because stdout carries the MCP protocol, and a redaction filter strips the token and any `Bearer ...` value.
- **Fixtures.** `.env` is git-ignored. Test fixtures are synthetic: no real budget data, names or ids.
- **Writes.** Write tools are off unless `YNAB_ALLOW_WRITES=true`, and every write needs a preview and a matching one-time confirmation token. See [How confirmation works](#how-confirmation-works).

## Development

```sh
uv run pytest            # tests run against mocked YNAB HTTP responses; no token or network needed
uv run ruff check .
uv run ruff format .
```

Layout:

```
src/make_cents_please/
  __main__.py            entry point (stdio)
  server.py              builds the MCP server
  config.py              env config; token wrapped so it never prints
  logging_setup.py       stderr logging with redaction
  providers/ynab/
    client.py            SDK wrapper: rate tracking, caching, error mapping
    cache.py             in-memory TTL cache and delta merge
    formatting.py        compact rows, amounts, dates, pagination
    tools.py             read tool definitions
    write_tools.py       write tool definitions (only with YNAB_ALLOW_WRITES=true)
    confirm.py           preview and one-time confirmation tokens
  providers/charityrecord/
    formats.py           CSV layouts, donation models, writer and export reader
    files.py             folder-restricted file access, no overwrites
    tools.py             Charity Record tool definitions (only with CHARITYRECORD_DIR)
tests/                   pytest suite with a fake YNAB HTTP layer (tests/fakes.py)
```

Support for other platforms goes in a new folder under `providers/`.

GitHub Actions runs the same lint, format and test checks on every pull request and on pushes to `main`, on Python 3.11, 3.12 and 3.13 (`.github/workflows/ci.yml`).

## Contributing

This repository is published from a private development repository at each release. See [CHANGELOG.md](CHANGELOG.md) for what changed. Issues are welcome. Pull requests are welcome too, but accepted changes are applied in the development repository and arrive here with the next release rather than being merged directly.

## License

MIT. See [LICENSE](LICENSE).
