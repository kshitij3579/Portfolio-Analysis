#!/usr/bin/env python3
"""
app.py - a small local web interface for the portfolio analyzer.

Run it and a page opens in your browser where you can edit your holdings, edit
the sector map, and read the report - no terminal typing, no CSV editing by hand.

How this relates to analyze.py
------------------------------
All the thinking still lives in analyze.py. This file does no arithmetic of its
own: it calls analyze.run_analysis() and renders whatever comes back. That is
deliberate. If the numbers here ever disagreed with the numbers in the terminal
report, one of them would be wrong, and you would have no way to tell which. By
sharing one engine, they cannot disagree.

Still read-only where it counts
-------------------------------
It writes to exactly two files - holdings.csv and sectors.csv - because that is
what "edit your holdings in a browser" means. It has no broker connection, no
credentials, and no way to place a trade, exactly as before.

It listens on 127.0.0.1 only, which means the address is reachable from this
computer and nowhere else. Nobody on your wifi, or anywhere on the internet, can
open it. Your holdings never leave your Mac.
"""

import logging
import os
import threading
import webbrowser

from flask import Flask, redirect, render_template, request, url_for
from werkzeug.serving import run_simple

import analyze

# Bind to the loopback address only. 0.0.0.0 would expose this to your whole
# network; 127.0.0.1 means "this computer only".
HOST = "127.0.0.1"
PORT = 5057

app = Flask(__name__)


@app.template_filter("inr")
def inr(value):
    """Format a number as rupees, using the same function the terminal report uses."""
    try:
        return analyze.format_inr(float(value))
    except (TypeError, ValueError):
        return "n/a"


# =============================================================================
# Helpers shared by the pages
# =============================================================================

def load_analysis(force_refresh=False):
    """Run the analysis, converting a hard failure into something displayable."""
    try:
        result = analyze.run_analysis(force_refresh=force_refresh)
    except SystemExit as error:
        # analyze.py stops with sys.exit() on an unusable file. In a terminal
        # that is the right behaviour; in a web page it would kill the server,
        # so catch it and show the message instead.
        return None, str(error)
    if result is None:
        return None, ("No holding could be priced, so there is nothing to "
                      "report yet. Add a holding, or give one a manual value.")
    return result, None


# A completely empty table still needs one blank row, or there is nothing for
# the "add a row" button to copy and you would be locked out of your own editor.
BLANK_HOLDING = {"ticker": "", "quantity": 0, "avg_buy_price": 0,
                 "account": "", "manual_value": None}
BLANK_SECTOR = {"ticker": "", "sector": "", "weight": 1.0, "asset_class": "equity"}


def safe_load(loader, path, fallback):
    """Load a file, surviving the failures that would stop the command-line tool.

    analyze.py ends the process with sys.exit() when a file is unusable. That is
    right for a terminal, but here it would kill the page mid-request and leave
    you with no way to fix the file through the interface that broke it. So the
    exit is caught and turned into an empty table plus an explanation.
    """
    try:
        return loader(path), None
    except SystemExit as error:
        return fallback, str(error)


def form_rows(*field_names):
    """Read parallel lists of form fields into a list of dicts, one per row.

    The editor pages submit one input per cell, all sharing a name - so a table
    of three rows sends three "ticker" values, three "quantity" values, and so
    on. zip() lines them back up into rows. Blank rows are dropped, which is how
    deleting a row works: the page clears it and submits.
    """
    columns = [request.form.getlist(name) for name in field_names]
    rows = []
    for values in zip(*columns):
        row = dict(zip(field_names, (v.strip() for v in values)))
        if any(row.values()):          # skip rows the user emptied out
            rows.append(row)
    return rows


def to_number(text, default=None):
    """Parse a form field into a float, falling back to `default` if it is not one."""
    return analyze.parse_number(text, default=default)


# =============================================================================
# Pages
# =============================================================================

@app.route("/")
def dashboard():
    """The report: warnings first, then the charts, then the detail."""
    result, error = load_analysis()
    return render_template("dashboard.html", result=result, error=error,
                           thresholds={
                               "holding": analyze.MAX_SINGLE_HOLDING_PCT,
                               "sector": analyze.MAX_SECTOR_PCT,
                               "metals": analyze.MAX_PRECIOUS_METALS_PCT,
                           })


@app.route("/refresh")
def refresh():
    """Force a fresh price fetch, then go back to the report."""
    load_analysis(force_refresh=True)
    return redirect(url_for("dashboard"))


@app.route("/holdings", methods=["GET", "POST"])
def holdings():
    """View and edit holdings.csv."""
    if request.method == "POST":
        rows = form_rows("ticker", "quantity", "avg_buy_price", "account",
                         "manual_value")
        cleaned = []
        for row in rows:
            ticker = row["ticker"].upper()
            if not ticker:
                continue
            cleaned.append({
                "ticker": ticker,
                "quantity": to_number(row["quantity"], 0.0),
                "avg_buy_price": to_number(row["avg_buy_price"], 0.0),
                "account": row["account"],
                # An empty manual_value means "fetch a live price for this row".
                "manual_value": to_number(row["manual_value"]),
            })
        analyze.save_holdings(analyze.HOLDINGS_FILE, cleaned)
        return redirect(url_for("holdings", saved=1))

    analyze.NOTICES.clear()
    rows, error = safe_load(analyze.load_holdings, analyze.HOLDINGS_FILE, [])
    return render_template("holdings.html", rows=rows or [BLANK_HOLDING],
                           notices=list(analyze.NOTICES), error=error,
                           saved=request.args.get("saved"))


@app.route("/sectors", methods=["GET", "POST"])
def sectors():
    """View and edit sectors.csv, including the partial weights."""
    if request.method == "POST":
        rows = form_rows("ticker", "sector", "weight", "asset_class")
        cleaned = []
        for row in rows:
            ticker = row["ticker"].upper()
            if not ticker or not row["sector"]:
                continue
            cleaned.append({
                "ticker": ticker,
                "sector": row["sector"],
                "weight": to_number(row["weight"], 1.0),
                "asset_class": row["asset_class"] or analyze.UNKNOWN_ASSET_CLASS,
            })
        analyze.save_sectors(analyze.SECTORS_FILE, cleaned)
        return redirect(url_for("sectors", saved=1))

    analyze.NOTICES.clear()
    mapping, error = safe_load(analyze.load_sectors, analyze.SECTORS_FILE, {})

    # Flatten {ticker: {weights: {...}}} back into one row per (ticker, sector),
    # which is what the file looks like and what the table edits.
    rows = []
    for ticker, entry in mapping.items():
        for sector, weight in entry["weights"].items():
            rows.append({"ticker": ticker, "sector": sector,
                         "weight": round(weight, 4),
                         "asset_class": entry["asset_class"]})
    rows.sort(key=lambda r: (r["ticker"], r["sector"]))

    # Which tickers are held but not yet mapped - the most common thing to fix.
    holdings_rows, _ = safe_load(analyze.load_holdings, analyze.HOLDINGS_FILE, [])
    held = {h["ticker"] for h in holdings_rows}
    unmapped = sorted(held - set(mapping))

    return render_template("sectors.html", rows=rows or [BLANK_SECTOR],
                           unmapped=unmapped, error=error,
                           notices=list(analyze.NOTICES),
                           saved=request.args.get("saved"),
                           metals_sector=analyze.PRECIOUS_METALS_SECTOR)


# =============================================================================
# Starting up
# =============================================================================

def open_browser():
    """Open the page once the server is actually accepting connections."""
    webbrowser.open(f"http://{HOST}:{PORT}/")


if __name__ == "__main__":
    print(f"\nPortfolio analyzer running at http://{HOST}:{PORT}/")
    print("This address works on this computer only.")
    print("Press Ctrl+C in this window to stop it.\n")

    threading.Timer(1.0, open_browser).start()

    # Silence the per-request log lines. They are noise for a single user
    # clicking around their own machine.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    # run_simple is the server function Flask's own app.run() calls. Using it
    # directly skips the red "this is a development server, do not use it in
    # production" banner that app.run() prints. That warning is about serving a
    # site to the public internet; this server is bound to 127.0.0.1, meaning it
    # is reachable from this computer and nowhere else, so it does not apply here
    # and would only be alarming.
    #
    # use_debugger stays off deliberately: the debugger exposes a Python console
    # to anything that can reach the port.
    run_simple(HOST, PORT, app, use_reloader=False, use_debugger=False)
