# NSE Portfolio Analyzer

A small, read-only command-line tool that shows where your money actually sits:
per holding, per sector, and per asset class — then warns you when you are more
concentrated than you meant to be.

The headline feature is **honest concentration**. A gold ETF is obviously gold.
A commodity exchange whose revenue tracks bullion volumes is *partly* gold. A
multi-asset fund holds gold inside it. This tool lets you tag those partial
exposures and adds them all up, so your real bullion (or any other) exposure
stops hiding behind labels.

## What this tool will never do

It is **read-only**. It does not connect to Zerodha, Groww, or any other broker.
It cannot place, modify, or cancel an order — there is no trading code here to
enable. Its only network call is a public price lookup on Yahoo Finance. There
are no API keys, tokens, or credentials anywhere in this project, and none are
needed.

Holdings are read from a CSV you maintain by hand. Nothing is fetched from a
broker account.

## Setup

You need Python 3.8 or newer.

```bash
cd portfolio

# Optional but recommended: keep dependencies out of your system Python.
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Running it

```bash
python analyze.py               # the report
python analyze.py --refresh     # ignore cached prices, fetch fresh ones
python analyze.py --json        # same numbers, as JSON
```

That is the whole interface. No server, no background process, no scheduling.

| Flag | What it does |
| --- | --- |
| `--refresh` | Skips the cache and re-fetches every price. |
| `--json` | Prints JSON to stdout instead of tables. Warnings go to stderr, so `python analyze.py --json > out.json` gives you a clean file. |
| `--holdings PATH` | Use a different holdings file. |
| `--sectors PATH` | Use a different sector map. |

### About the price cache

Fetched prices are written to `.price_cache.json` next to the script, with a
timestamp. A price younger than 60 minutes is reused instead of hitting the
network, so running the tool repeatedly in one sitting is instant. Change
`CACHE_TTL_MINUTES` at the top of `analyze.py` to adjust, or pass `--refresh`.

If a fetch fails but a cached price exists, the tool uses the old price and
tells you how stale it is — better than silently dropping a holding.

## Adding a holding

Open `holdings.csv` and add a row. Lines starting with `#` are ignored, so you
can leave yourself notes.

```csv
ticker,quantity,avg_buy_price,account,manual_value
INFY.NS,10,1450.00,equity,
```

| Column | Meaning |
| --- | --- |
| `ticker` | The Yahoo Finance symbol. NSE stocks and ETFs need the `.NS` suffix (`TCS.NS`, `INFY.NS`). |
| `quantity` | Shares or units held. |
| `avg_buy_price` | Your average buy price per unit, in rupees. |
| `account` | A free-text label. Not used in any calculation yet — it is there so you can group later. |
| `manual_value` | Optional. See below. |

### Editing these files in Excel or Numbers

You can. The tool copes with what spreadsheet apps do to a CSV on the way out:
the invisible marker Excel adds when saving as "CSV UTF-8", a semicolon
separator instead of a comma, Windows or old-Mac line endings, and headings with
odd capitalisation or stray spaces. Just make sure you **Save as CSV**, not as
`.xlsx`.

Two things a spreadsheet app can still break:

- **Deleting or renaming the heading row.** If a required column goes missing,
  the tool names the missing column and lists what it found instead.
- **The `#` comment lines.** They are notes to you, not data. If your spreadsheet
  scatters them into cells, delete them - nothing depends on them.

A plain text editor (TextEdit in plain-text mode, VS Code, Notepad) avoids all of
this if you prefer.

**Check the symbol first.** Search it on finance.yahoo.com — if `INFY.NS` shows a
price chart, the tool can fetch it. If a symbol does not resolve, the tool names
it in a warning and continues with everything else rather than crashing.

Then add it to `sectors.csv` (next section), or it will be counted as
`Unknown`/`other` and the tool will remind you.

### Holdings Yahoo cannot price (mutual funds)

`yfinance` has no data for Indian mutual funds. For those, put the **total
current value in rupees** in `manual_value`. When that cell has a number, the
tool uses it directly and never tries to fetch a price:

```csv
QUANTMULTIASSET,1,10000.00,mutual_fund,10000.00
```

Set `quantity` to your units held and `avg_buy_price` to your average NAV so the
gain/loss column is meaningful; both are on your CAS statement. Then update
`manual_value` to `units × latest NAV` whenever you want a current figure. These
rows are marked with a `*` in the report so you know they are not live prices.

## The sector map, and partial weights

`sectors.csv` is where the real thinking goes. It uses **one row per
(ticker, sector) pair**, which is what makes partial weights possible.

A holding that belongs entirely to one sector is a single row:

```csv
ticker,sector,weight,asset_class
ZYDUSLIFE.NS,Pharmaceuticals,1.0,equity
```

A holding whose exposure is genuinely split gets several rows that add up to
`1.0`:

```csv
MCX.NS,Precious Metals,0.5,equity
MCX.NS,Financials,0.5,equity
```

That says: MCX is a financial exchange, but roughly half of what drives it is
bullion trading volume, so count half its value as precious-metals exposure.
The number is a judgement call — it is yours to argue with. Change it.

| Column | Meaning |
| --- | --- |
| `ticker` | Must match `holdings.csv` exactly. |
| `sector` | Any label you like. Rows sharing a label are added together, so spell it consistently — `Pharma` and `Pharmaceuticals` are two different sectors here. |
| `weight` | This holding's share of this sector, `0.0`–`1.0`. Weights for one ticker should sum to `1.0`. |
| `asset_class` | `equity`, `precious_metals`, `multi_asset`, `debt`, or `other`. |

Two rules worth knowing:

- **Sector is split, asset class is not.** A holding can span several sectors,
  but sits in exactly one asset class. Use the same `asset_class` on every row
  belonging to a ticker; if they disagree, the tool warns and uses the first.
- **Weights are checked.** If a ticker's weights sum to something other than
  `1.0`, the tool warns you and scales them to fit, so a typo cannot quietly
  distort every percentage in the report.

### Why the two breakdowns disagree — and why that is the point

In the shipped starter portfolio, the **asset class** table shows precious
metals at about 13%, while the **sector** table shows precious metals at about
43%. Both are correct. The asset-class number counts only the holdings that
*are* bullion ETFs. The sector number also counts the half of MCX and the slice
of the multi-asset fund that ride on bullion.

That gap is the exposure you did not know you had.

### The special sector name

`Precious Metals` is the one sector label the code knows by name: every
holding's weighted share of it is summed for the combined bullion warning. If
you rename it in `sectors.csv`, change `PRECIOUS_METALS_SECTOR` at the top of
`analyze.py` to match. Every other sector label is free-form.

## The warnings

Three rules, checked against your total portfolio value:

| Warning | Default | Constant in `analyze.py` |
| --- | --- | --- |
| A single holding is too large | above 10% | `MAX_SINGLE_HOLDING_PCT` |
| A single sector is too large | above 25% | `MAX_SECTOR_PCT` |
| Combined precious metals is too large | above 15% | `MAX_PRECIOUS_METALS_PCT` |

All three live together at the top of `analyze.py`. They are deliberately
strict for a small portfolio — a beginner holding six things will trip the 10%
rule constantly, which is arithmetic, not a crisis. Loosen them as you go.

These are prompts to think, not advice. This tool has no idea what your goals
are.

## Reading the code

`analyze.py` is one file, split into seven labelled sections:

1. **Configuration** — every threshold and file path.
2. **Formatting helpers** — rupee formatting, table rendering.
3. **Loading the CSV files** — parsing and validation.
4. **Prices** — the cache and the one Yahoo Finance call.
5. **The analysis** — pure calculation, no I/O.
6. **Output** — the terminal report and the JSON.
7. **Entry point** — argument parsing, wiring it together.

Sections 3–5 are plain functions that take data and return data, so a future
`screener.py` can `from analyze import load_holdings, load_sectors, get_prices`
and reuse them without any restructuring.

## Not built yet

Backtesting, screening, alerts, charts, a web interface, and any scheduled or
background execution are deliberately out of scope for now.
