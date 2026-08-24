#!/usr/bin/env python3
"""
analyze.py - a read-only concentration analyzer for an Indian (NSE) portfolio.

What it does
------------
Reads two CSV files you maintain by hand (holdings.csv and sectors.csv), fetches
the latest prices from Yahoo Finance via yfinance, and prints where your money
actually sits: per holding, per sector, and per asset class - then warns you
about the concentrations you may not have noticed.

What it deliberately does NOT do
--------------------------------
This tool is READ-ONLY by design. It never places, modifies or cancels an order,
and it talks to no broker API of any kind. Its only network call is a public
price lookup. There are no credentials or API keys anywhere in this project, and
nothing here is one small edit away from being able to trade.

Usage
-----
    python analyze.py               # normal run (uses cached prices if fresh)
    python analyze.py --refresh     # ignore the cache, fetch fresh prices
    python analyze.py --json        # same numbers as machine-readable JSON
"""

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone


# =============================================================================
# SECTION 1: CONFIGURATION
# Every tunable number lives here. Change these, not the code below.
# =============================================================================

# --- Concentration thresholds (percent of total portfolio value) -------------
# Cross one of these and the report prints a warning. They are deliberately
# strict for a beginner's portfolio; loosen them as your portfolio grows.
MAX_SINGLE_HOLDING_PCT = 10.0   # no one stock/ETF should dominate
MAX_SECTOR_PCT = 25.0           # no one sector should dominate
MAX_PRECIOUS_METALS_PCT = 15.0  # total gold + silver exposure, however held

# The sector label that counts as bullion exposure. Every holding's weighted
# share of this sector is summed for the precious-metals warning, so a stock
# tagged 0.5 to this sector contributes half its value.
PRECIOUS_METALS_SECTOR = "Precious Metals"

# --- File locations ----------------------------------------------------------
# Paths are resolved relative to this script, so the tool works no matter which
# directory you run it from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.csv")
SECTORS_FILE = os.path.join(BASE_DIR, "sectors.csv")
CACHE_FILE = os.path.join(BASE_DIR, ".price_cache.json")

# --- Price cache -------------------------------------------------------------
# Prices are saved to CACHE_FILE with a timestamp. A cached price younger than
# this many minutes is reused instead of hitting the network again, so running
# the tool repeatedly in one sitting is instant. --refresh overrides this.
CACHE_TTL_MINUTES = 60

# --- Fallbacks for tickers missing from sectors.csv --------------------------
UNKNOWN_SECTOR = "Unknown"
UNKNOWN_ASSET_CLASS = "other"

# How much price history to request. We only want the most recent close, but
# asking for a few days means weekends and market holidays still return data.
PRICE_HISTORY_DAYS = "5d"
# If that short window contains no usable price at all - a long holiday, or a
# thinly traded ETF - try again over a wider one before giving up.
PRICE_HISTORY_FALLBACK = "1mo"


# =============================================================================
# SECTION 2: SMALL FORMATTING HELPERS
# Pure text formatting - no calculations, no data loading.
# =============================================================================

def format_inr(amount):
    """Format a number as rupees using Indian digit grouping.

    Indian grouping is not the same as Western grouping: the last three digits
    are one group, and everything before that is grouped in PAIRS.
    1234567.89 becomes  Rs 12,34,567.89  (not 1,234,567.89).
    """
    # Guard against nan/inf reaching the formatter. It should never happen now
    # that prices are validated, but printing "n/a" beats printing "Rs nan."
    if not math.isfinite(amount):
        return "n/a"

    is_negative = amount < 0
    whole, _, decimals = f"{abs(amount):.2f}".partition(".")

    if len(whole) > 3:
        head, last_three = whole[:-3], whole[-3:]
        # Peel two digits at a time off the right-hand end of the remaining head.
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        whole = ",".join(pairs) + "," + last_three

    sign = "-" if is_negative else ""
    return f"{sign}₹{whole}.{decimals}"


def format_pct(value):
    """Format a percentage with a sign, e.g. '+12.34%' or '-3.10%'."""
    return f"{value:+.2f}%"


def render_table(headers, rows, right_align_from=1):
    """Render a list of rows as an aligned plain-text table.

    headers          list of column titles
    rows             list of lists of already-formatted strings
    right_align_from index of the first column to right-align (numbers read
                     better right-aligned; the leading label column does not)
    """
    if not rows:
        return "  (nothing to show)"

    columns = len(headers)
    # Column width = the widest cell in that column, header included.
    widths = [
        max(len(str(headers[i])), max(len(str(row[i])) for row in rows))
        for i in range(columns)
    ]

    def render_row(cells):
        out = []
        for i, cell in enumerate(cells):
            text = str(cell)
            out.append(text.rjust(widths[i]) if i >= right_align_from
                       else text.ljust(widths[i]))
        return "  " + "  ".join(out)

    separator = "  " + "  ".join("-" * w for w in widths)
    return "\n".join([render_row(headers), separator] + [render_row(r) for r in rows])


def print_heading(text):
    """Print a section heading with a rule under it."""
    print()
    print(text)
    print("=" * len(text))


# =============================================================================
# SECTION 3: LOADING THE CSV FILES
# Both files allow '#' comment lines and blank lines so you can annotate them.
# =============================================================================

def read_csv_rows(path):
    """Read a CSV file into (rows, column_names), ignoring '#' comments and blanks.

    This is deliberately forgiving, because these files are meant to be edited by
    hand - often in Excel or Numbers, which quietly reformat what they save.
    Three things it tolerates:

    * A byte order mark. Saving as "CSV UTF-8" puts three invisible bytes at the
      front of the file, which would otherwise glue themselves to the first
      column heading and turn "ticker" into a name nothing matches. Opening with
      encoding="utf-8-sig" strips it if present and does no harm if it is not.
    * A semicolon separator, which some spreadsheet apps use instead of a comma
      depending on the computer's regional settings.
    * Headings with odd capitalisation or stray spaces - "Ticker" and " ticker "
      are both accepted.

    csv.DictReader cannot skip comment lines itself, so we filter them out first
    and hand it only the real content.
    """
    if not os.path.exists(path):
        sys.exit(f"ERROR: could not find {path}")

    with open(path, newline="", encoding="utf-8-sig") as handle:
        lines = [line for line in handle
                 if line.strip() and not line.lstrip().startswith("#")]

    if not lines:
        sys.exit(f"ERROR: {path} has no data rows")

    # Whichever separator appears more often in the heading row is the real one.
    heading = lines[0]
    delimiter = ";" if heading.count(";") > heading.count(",") else ","

    reader = csv.DictReader(lines, delimiter=delimiter)
    # Reading .fieldnames consumes the heading row; assigning to it then replaces
    # those names for every row that follows.
    reader.fieldnames = [(name or "").strip().lower()
                         for name in (reader.fieldnames or [])]
    return list(reader), reader.fieldnames


def require_columns(path, found, needed):
    """Stop with a readable message if the file is missing a column we need."""
    missing = [name for name in needed if name not in found]
    if missing:
        sys.exit(
            f"ERROR: {os.path.basename(path)} is missing the column(s): "
            f"{', '.join(missing)}\n"
            f"       The columns found were: {', '.join(found) or '(none)'}\n"
            f"       Expected a heading row reading: {','.join(needed)}"
        )


def parse_number(text, default=None):
    """Turn a CSV cell into a float. Empty or unparseable cells give `default`."""
    if text is None:
        return default
    text = text.strip().replace(",", "")   # tolerate '1,234.50'
    if text == "":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def load_holdings(path):
    """Load holdings.csv into a list of dicts.

    A row with a number in manual_value is priced from that number instead of
    being fetched - that is how mutual funds and anything else Yahoo does not
    cover get into the report.
    """
    rows, columns = read_csv_rows(path)
    require_columns(path, columns, ["ticker", "quantity", "avg_buy_price"])

    holdings = []
    for row_number, row in enumerate(rows, start=1):
        ticker = (row.get("ticker") or "").strip()
        if not ticker:
            print(f"WARNING: skipping data row {row_number} of "
                  f"{os.path.basename(path)} - it has no ticker")
            continue

        quantity = parse_number(row.get("quantity"))
        avg_buy_price = parse_number(row.get("avg_buy_price"))
        if quantity is None or avg_buy_price is None:
            print(f"WARNING: skipping {ticker} - quantity or avg_buy_price is "
                  f"missing or not a number")
            continue
        if not math.isfinite(quantity) or not math.isfinite(avg_buy_price):
            print(f"WARNING: skipping {ticker} - quantity or avg_buy_price is "
                  f"not a usable number")
            continue

        # A manual_value that is not a sensible positive number would poison every
        # total it entered, so reject it and fall back to fetching a price.
        manual_value = parse_number(row.get("manual_value"))
        if manual_value is not None and not is_usable_price(manual_value):
            print(f"WARNING: ignoring the manual_value on {ticker} - "
                  f"'{row.get('manual_value')}' is not a positive number")
            manual_value = None

        holdings.append({
            "ticker": ticker,
            "quantity": quantity,
            "avg_buy_price": avg_buy_price,
            "account": (row.get("account") or "").strip(),
            # None here means "please fetch a live price for this one".
            "manual_value": manual_value,
        })

    if not holdings:
        sys.exit(
            f"ERROR: {os.path.basename(path)} has a valid heading row but no "
            f"usable holdings below it.\n"
            f"       Every row was skipped - see the warnings above for why.\n"
            f"       Each row needs a ticker, a quantity, and an avg_buy_price."
        )
    return holdings


def load_sectors(path):
    """Load sectors.csv into {ticker: {"asset_class": str, "weights": {sector: w}}}.

    One ticker may span several rows; their weights are collected into one dict.
    Weights that do not add up to 1.0 are scaled so they do, with a warning -
    otherwise a typo would quietly distort every percentage in the report.
    """
    rows, columns = read_csv_rows(path)
    require_columns(path, columns, ["ticker", "sector"])

    mapping = {}

    for row in rows:
        ticker = (row.get("ticker") or "").strip()
        sector = (row.get("sector") or "").strip()
        if not ticker or not sector:
            continue

        weight = parse_number(row.get("weight"), default=1.0)
        asset_class = (row.get("asset_class") or UNKNOWN_ASSET_CLASS).strip()

        entry = mapping.setdefault(ticker, {"asset_class": asset_class, "weights": {}})

        # Asset class is one bucket per ticker, so later rows must agree with the
        # first one. Disagreement is a typo worth surfacing.
        if entry["asset_class"] != asset_class:
            print(f"WARNING: {ticker} has conflicting asset classes in sectors.csv "
                  f"('{entry['asset_class']}' and '{asset_class}'); using "
                  f"'{entry['asset_class']}'")

        # += rather than = so a repeated (ticker, sector) pair adds up instead of
        # silently overwriting.
        entry["weights"][sector] = entry["weights"].get(sector, 0.0) + weight

    # Normalise each ticker's weights to sum to 1.0.
    for ticker, entry in mapping.items():
        total = sum(entry["weights"].values())
        if total <= 0:
            print(f"WARNING: {ticker} has zero total sector weight in sectors.csv; "
                  f"treating it as 100% {UNKNOWN_SECTOR}")
            entry["weights"] = {UNKNOWN_SECTOR: 1.0}
            continue
        if abs(total - 1.0) > 0.001:
            print(f"WARNING: sector weights for {ticker} add up to {total:.2f}, "
                  f"not 1.00 - scaling them to fit")
            entry["weights"] = {s: w / total for s, w in entry["weights"].items()}

    return mapping


# =============================================================================
# SECTION 4: PRICES (CACHE + YAHOO FINANCE)
# The only part of this program that touches the network.
# =============================================================================

def is_usable_price(value):
    """True only for a real, finite, positive number.

    Yahoo can hand back a blank (nan) price, and a blank that is allowed through
    silently poisons every sum it touches: nan plus anything is nan, so one bad
    price turns the whole report into "nan". Everything entering the program as a
    price goes through this check first.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def load_cache(path):
    """Read the price cache. Any problem returns an empty cache - never crashes."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        print("WARNING: price cache was unreadable and will be rebuilt")
        return {}


def save_cache(path, cache):
    """Write the price cache. A failure here is not worth crashing over."""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2)
    except OSError as error:
        print(f"WARNING: could not write the price cache ({error})")


def cache_age_minutes(entry):
    """How many minutes old a cache entry is. Unparseable entries are 'ancient'."""
    try:
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60.0


def fetch_one_price(ticker):
    """Fetch the latest closing price for one ticker, or None if that fails.

    Importing yfinance inside the function keeps startup fast and means a run
    served entirely from cache does not need the library loaded at all.
    """
    import yfinance as yf

    stock = yf.Ticker(ticker)

    for period in (PRICE_HISTORY_DAYS, PRICE_HISTORY_FALLBACK):
        # auto_adjust=False matters. Left at its default of True, yfinance returns
        # prices retro-adjusted for dividends and splits - useful for charting a
        # return over time, but not the price the share actually changed hands at.
        # For "what is my holding worth", the real traded close is the right
        # number, and it is the one your broker shows.
        history = stock.history(period=period, auto_adjust=False)
        if history.empty or "Close" not in history:
            continue

        # Yahoo often includes a row for a session that has not produced a close
        # yet - the date is there but the price cell is blank (nan). Taking the
        # last row blindly picks up that blank. dropna() discards those rows so
        # we take the most recent row that actually has a price in it.
        closes = history["Close"].dropna()
        if closes.empty:
            continue   # nothing usable in this window; try the wider one

        price = float(closes.iloc[-1])
        if not is_usable_price(price):
            continue

        # Record WHICH session this close belongs to. Without it there is no way
        # to tell a price from an hour ago from one from last Friday, which makes
        # any difference against your broker's screen impossible to explain.
        try:
            as_of = closes.index[-1].strftime("%Y-%m-%d")
        except (AttributeError, ValueError):
            as_of = None

        return {"price": price, "as_of": as_of}

    return None


def get_prices(tickers, cache_path, force_refresh=False):
    """Return {ticker: price} for every ticker that could be priced.

    Fresh cache entries are reused. Anything missing, stale, or requested with
    --refresh is fetched. A ticker that cannot be fetched is reported by name and
    simply left out of the result - one bad symbol never stops the whole report.
    """
    cache = load_cache(cache_path)
    prices = {}
    served_from_cache = 0
    fetched_now = 0
    served_stale = 0   # prices reused past their TTL because a fetch failed

    for ticker in tickers:
        entry = cache.get(ticker)
        # An entry written before prices were validated may hold a blank. Treat
        # any unusable cached price as if it were not there, so the bad value is
        # re-fetched rather than reused.
        if entry and not is_usable_price(entry.get("price")):
            entry = None

        if entry and not force_refresh and cache_age_minutes(entry) < CACHE_TTL_MINUTES:
            prices[ticker] = {"price": entry["price"], "as_of": entry.get("as_of")}
            served_from_cache += 1
            continue

        try:
            quote = fetch_one_price(ticker)
        except Exception as error:
            # Deliberately broad: network errors, Yahoo outages and yfinance's own
            # exceptions all mean the same thing here - carry on without this one.
            print(f"WARNING: could not fetch {ticker} ({type(error).__name__}: {error})")
            quote = None

        if quote is None:
            print(f"WARNING: no price data for {ticker}. Check the symbol resolves "
                  f"on finance.yahoo.com (NSE symbols need the '.NS' suffix), or "
                  f"give the row a manual_value in holdings.csv.")
            # Fall back to a stale cached price rather than dropping the holding.
            if entry:
                age_hours = cache_age_minutes(entry) / 60.0
                print(f"         using the last cached price for {ticker} "
                      f"({age_hours:.1f} hours old)")
                prices[ticker] = {"price": entry["price"], "as_of": entry.get("as_of")}
                served_stale += 1
            continue

        prices[ticker] = quote
        cache[ticker] = {
            "price": quote["price"],
            "as_of": quote["as_of"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        fetched_now += 1

    if fetched_now:
        save_cache(cache_path, cache)

    return prices, {
        "from_cache": served_from_cache,
        "fetched": fetched_now,
        "stale_fallback": served_stale,
    }


# =============================================================================
# SECTION 5: THE ANALYSIS
# Pure calculation on data already in memory. No I/O, no network.
# =============================================================================

def build_positions(holdings, sectors, prices):
    """Combine holdings, sector tags and prices into one list of positions.

    Returns (positions, unpriced) where `unpriced` names the holdings that had to
    be left out because no price could be found for them.
    """
    positions = []
    unpriced = []

    for holding in holdings:
        ticker = holding["ticker"]
        quantity = holding["quantity"]

        if holding["manual_value"] is not None:
            # Manually valued row: manual_value IS the current value.
            current_value = holding["manual_value"]
            current_price = current_value / quantity if quantity else 0.0
            price_source = "manual"
            price_as_of = None
        else:
            quote = prices.get(ticker)
            if quote is None:
                unpriced.append(ticker)
                continue
            current_price = quote["price"]
            current_value = current_price * quantity
            price_source = "market"
            price_as_of = quote.get("as_of")

        invested = quantity * holding["avg_buy_price"]
        gain = current_value - invested
        # Guard against dividing by zero if invested is 0 for some reason.
        gain_pct = (gain / invested * 100.0) if invested else 0.0

        mapping = sectors.get(ticker)
        if mapping is None:
            print(f"WARNING: {ticker} is not listed in sectors.csv - counting it as "
                  f"{UNKNOWN_SECTOR}/{UNKNOWN_ASSET_CLASS}. Add a row to fix this.")
            mapping = {"asset_class": UNKNOWN_ASSET_CLASS,
                       "weights": {UNKNOWN_SECTOR: 1.0}}

        positions.append({
            "ticker": ticker,
            "account": holding["account"],
            "quantity": quantity,
            "avg_buy_price": round(holding["avg_buy_price"], 2),
            "current_price": round(current_price, 2),
            "current_value": round(current_value, 2),
            "invested": round(invested, 2),
            "gain": round(gain, 2),
            "gain_pct": round(gain_pct, 2),
            "price_source": price_source,
            "price_as_of": price_as_of,
            "asset_class": mapping["asset_class"],
            "sector_weights": mapping["weights"],
        })

    # Biggest position first - the report is about concentration, so the things
    # most worth looking at belong at the top.
    positions.sort(key=lambda p: p["current_value"], reverse=True)
    return positions, unpriced


def summarise_totals(positions):
    """Total current value, total invested, and the overall gain/loss."""
    total_value = sum(p["current_value"] for p in positions)
    total_invested = sum(p["invested"] for p in positions)
    total_gain = total_value - total_invested
    gain_pct = (total_gain / total_invested * 100.0) if total_invested else 0.0
    return {
        "total_value": round(total_value, 2),
        "total_invested": round(total_invested, 2),
        "total_gain": round(total_gain, 2),
        "total_gain_pct": round(gain_pct, 2),
    }


def breakdown_by_sector(positions, total_value):
    """Rupee value and portfolio share per sector, honouring partial weights.

    A holding tagged 0.5 to a sector contributes half its value to that sector,
    which is what makes a part-bullion business show up in bullion exposure.
    """
    totals = {}
    for position in positions:
        for sector, weight in position["sector_weights"].items():
            totals[sector] = totals.get(sector, 0.0) + position["current_value"] * weight

    rows = [
        {
            "sector": sector,
            "value": round(value, 2),
            "pct": round(value / total_value * 100.0, 2) if total_value else 0.0,
        }
        for sector, value in totals.items()
    ]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def breakdown_by_asset_class(positions, total_value):
    """Rupee value and portfolio share per asset class.

    Unlike sectors, asset class is not split - each holding sits in exactly one.
    """
    totals = {}
    for position in positions:
        bucket = position["asset_class"]
        totals[bucket] = totals.get(bucket, 0.0) + position["current_value"]

    rows = [
        {
            "asset_class": bucket,
            "value": round(value, 2),
            "pct": round(value / total_value * 100.0, 2) if total_value else 0.0,
        }
        for bucket, value in totals.items()
    ]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def find_warnings(positions, sector_rows, total_value):
    """Apply the three concentration rules and return a list of warning dicts."""
    warnings = []
    if not total_value:
        return warnings

    # Rule 1: any single holding above MAX_SINGLE_HOLDING_PCT.
    for position in positions:
        pct = position["current_value"] / total_value * 100.0
        if pct > MAX_SINGLE_HOLDING_PCT:
            warnings.append({
                "type": "single_holding",
                "subject": position["ticker"],
                "pct": round(pct, 2),
                "threshold": MAX_SINGLE_HOLDING_PCT,
                "message": (f"{position['ticker']} is {pct:.2f}% of the portfolio "
                            f"(limit {MAX_SINGLE_HOLDING_PCT:.0f}%)"),
            })

    # Rule 2: any single sector above MAX_SECTOR_PCT.
    for row in sector_rows:
        if row["pct"] > MAX_SECTOR_PCT:
            warnings.append({
                "type": "sector",
                "subject": row["sector"],
                "pct": row["pct"],
                "threshold": MAX_SECTOR_PCT,
                "message": (f"Sector '{row['sector']}' is {row['pct']:.2f}% of the "
                            f"portfolio (limit {MAX_SECTOR_PCT:.0f}%)"),
            })

    # Rule 3: combined precious-metals exposure, summed across every holding that
    # carries any weight in that sector - including partial tags. This is read
    # straight off the sector breakdown, which already applied the weights.
    metals_pct = next((r["pct"] for r in sector_rows
                       if r["sector"] == PRECIOUS_METALS_SECTOR), 0.0)
    if metals_pct > MAX_PRECIOUS_METALS_PCT:
        contributors = sorted(
            (p["ticker"] for p in positions
             if p["sector_weights"].get(PRECIOUS_METALS_SECTOR, 0) > 0)
        )
        warnings.append({
            "type": "precious_metals",
            "subject": PRECIOUS_METALS_SECTOR,
            "pct": metals_pct,
            "threshold": MAX_PRECIOUS_METALS_PCT,
            "contributors": contributors,
            "message": (f"Combined precious-metals exposure is {metals_pct:.2f}% "
                        f"(limit {MAX_PRECIOUS_METALS_PCT:.0f}%), via "
                        f"{', '.join(contributors)}"),
        })

    return warnings


# =============================================================================
# SECTION 6: OUTPUT
# =============================================================================

def describe_price_dates(positions):
    """Explain which trading session the fetched prices belong to.

    This exists because the commonest 'is it broken?' moment is seeing a price
    here that differs from a broker app by a percent or two. Almost always the
    answer is that these are closing prices and the broker is showing a live one.
    Printing the date turns a mystery into an obvious, checkable fact.
    """
    dates = sorted({p["price_as_of"] for p in positions
                    if p["price_source"] == "market" and p["price_as_of"]})
    if not dates:
        return []

    lines = []
    if len(dates) == 1:
        lines.append(f"  Fetched prices are closing prices from the trading "
                     f"session of {dates[0]}.")
    else:
        lines.append(f"  Fetched prices are closing prices, and not all from the "
                     f"same session ({dates[0]} to {dates[-1]}):")
        for position in positions:
            if position["price_source"] == "market" and position["price_as_of"] != dates[-1]:
                lines.append(f"    {position['ticker']} is priced as of "
                             f"{position['price_as_of'] or 'an unknown date'}")
    lines.append("  If your broker shows something different, it is likely quoting "
                 "a live price.")
    return lines


def print_report(positions, totals, sector_rows, asset_rows, warnings, unpriced,
                 cache_stats):
    """Print the full terminal report."""
    total_value = totals["total_value"]

    # --- 1. Holdings ---------------------------------------------------------
    print_heading("1. HOLDINGS")
    rows = []
    for position in positions:
        # A trailing '*' marks a row priced from manual_value rather than fetched.
        label = position["ticker"] + (" *" if position["price_source"] == "manual" else "")
        rows.append([
            label,
            f"{position['quantity']:g}",
            format_inr(position["current_price"]),
            format_inr(position["current_value"]),
            format_inr(position["invested"]),
            format_inr(position["gain"]),
            format_pct(position["gain_pct"]),
            f"{position['current_value'] / total_value * 100:.2f}%" if total_value else "-",
        ])
    print(render_table(
        ["TICKER", "QTY", "PRICE", "VALUE", "INVESTED", "GAIN/LOSS", "GAIN %", "% PORT"],
        rows,
    ))
    if any(p["price_source"] == "manual" for p in positions):
        print("\n  * valued from manual_value in holdings.csv, not a fetched price")

    # --- 2. Totals -----------------------------------------------------------
    print_heading("2. TOTALS")
    print(f"  Current value : {format_inr(totals['total_value'])}")
    print(f"  Invested      : {format_inr(totals['total_invested'])}")
    print(f"  Gain / loss   : {format_inr(totals['total_gain'])} "
          f"({format_pct(totals['total_gain_pct'])})")

    # --- 3. Concentration breakdown ------------------------------------------
    print_heading("3. CONCENTRATION BY SECTOR")
    print(render_table(
        ["SECTOR", "VALUE", "% PORT"],
        [[r["sector"], format_inr(r["value"]), f"{r['pct']:.2f}%"] for r in sector_rows],
    ))
    print("\n  (holdings tagged with partial weights are split across sectors)")

    print_heading("4. CONCENTRATION BY ASSET CLASS")
    print(render_table(
        ["ASSET CLASS", "VALUE", "% PORT"],
        [[r["asset_class"], format_inr(r["value"]), f"{r['pct']:.2f}%"] for r in asset_rows],
    ))

    # --- 5. Warnings ---------------------------------------------------------
    print_heading("5. CONCENTRATION WARNINGS")
    if warnings:
        for warning in warnings:
            print(f"  [!] {warning['message']}")
        print(f"\n  Thresholds: single holding {MAX_SINGLE_HOLDING_PCT:.0f}%, "
              f"sector {MAX_SECTOR_PCT:.0f}%, precious metals "
              f"{MAX_PRECIOUS_METALS_PCT:.0f}%. "
              f"Edit them at the top of analyze.py.")
    else:
        print("  No thresholds breached.")

    # --- Footer --------------------------------------------------------------
    if unpriced:
        print()
        print(f"  NOTE: left out of every number above because no price was found: "
              f"{', '.join(unpriced)}")

    print()
    for line in describe_price_dates(positions):
        print(line)

    stale_note = (f", {cache_stats['stale_fallback']} reused past the cache expiry "
                  f"because the fetch failed" if cache_stats["stale_fallback"] else "")
    print(f"  Prices: {cache_stats['fetched']} fetched, "
          f"{cache_stats['from_cache']} from cache{stale_note} "
          f"(cache is reused for {CACHE_TTL_MINUTES} minutes; "
          f"use --refresh to force a fetch)")
    print("  This tool is read-only and cannot place trades.")
    print()


def build_json(positions, totals, sector_rows, asset_rows, warnings, unpriced,
               cache_stats):
    """Assemble exactly the same information as one JSON-serialisable dict."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "max_single_holding_pct": MAX_SINGLE_HOLDING_PCT,
            "max_sector_pct": MAX_SECTOR_PCT,
            "max_precious_metals_pct": MAX_PRECIOUS_METALS_PCT,
        },
        "totals": totals,
        "positions": [
            {
                **position,
                "pct_of_portfolio": round(
                    position["current_value"] / totals["total_value"] * 100.0, 2
                ) if totals["total_value"] else 0.0,
                # Round the weights so the JSON stays readable.
                "sector_weights": {s: round(w, 4)
                                   for s, w in position["sector_weights"].items()},
            }
            for position in positions
        ],
        "by_sector": sector_rows,
        "by_asset_class": asset_rows,
        "warnings": warnings,
        "unpriced_tickers": unpriced,
        "price_source_counts": cache_stats,
    }


# =============================================================================
# SECTION 7: ENTRY POINT
# Wires the sections above together in order: load -> price -> analyse -> print.
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Read-only concentration analyzer for an NSE portfolio.",
    )
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached prices and fetch fresh ones")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the report as JSON instead of a table")
    parser.add_argument("--holdings", default=HOLDINGS_FILE,
                        help="path to holdings.csv")
    parser.add_argument("--sectors", default=SECTORS_FILE,
                        help="path to sectors.csv")
    args = parser.parse_args()

    # In --json mode, warnings from loading would corrupt the JSON on stdout, so
    # send everything that is not the JSON document itself to stderr.
    if args.as_json:
        original_stdout = sys.stdout
        sys.stdout = sys.stderr

    # 1. Load the two files you maintain.
    holdings = load_holdings(args.holdings)
    sectors = load_sectors(args.sectors)

    # 2. Fetch prices, but only for rows that need one (rows with a manual_value
    #    are already priced, and asking Yahoo about a mutual fund would just fail).
    tickers_to_fetch = [h["ticker"] for h in holdings if h["manual_value"] is None]
    prices, cache_stats = get_prices(tickers_to_fetch, CACHE_FILE,
                                     force_refresh=args.refresh)

    # 3. Crunch the numbers.
    positions, unpriced = build_positions(holdings, sectors, prices)
    if not positions:
        sys.exit("ERROR: no holdings could be priced, so there is nothing to report")

    totals = summarise_totals(positions)
    sector_rows = breakdown_by_sector(positions, totals["total_value"])
    asset_rows = breakdown_by_asset_class(positions, totals["total_value"])
    warnings = find_warnings(positions, sector_rows, totals["total_value"])

    # 4. Show the result.
    if args.as_json:
        sys.stdout = original_stdout
        print(json.dumps(
            build_json(positions, totals, sector_rows, asset_rows, warnings,
                       unpriced, cache_stats),
            indent=2,
        ))
    else:
        print_report(positions, totals, sector_rows, asset_rows, warnings,
                     unpriced, cache_stats)


if __name__ == "__main__":
    main()
