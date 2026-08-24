#!/bin/bash
# Double-click this file in Finder to start the portfolio analyzer.
#
# On the first run it builds a private folder of the libraries the tool needs,
# which takes a minute or two. After that it starts straight away.
#
# Close this window, or press Ctrl+C in it, to stop the app.

# Work in the folder this script lives in, wherever that ends up being. Without
# this, double-clicking would run it from your home folder and nothing would be
# found.
cd "$(dirname "$0")" || exit 1

echo "Starting the portfolio analyzer..."
echo

# Find a Python. macOS does not ship one, so this is the python.org install.
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "Python 3 is not installed."
  echo "Download it from https://www.python.org/downloads/ and run this again."
  echo
  read -r -p "Press Return to close this window."
  exit 1
fi

# Build the private library folder on first run only.
if [ ! -d .venv ]; then
  echo "First run - setting up. This takes a minute or two."
  "$PYTHON" -m venv .venv || { echo "Could not create the environment."; read -r; exit 1; }
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt || {
    echo "Could not install the libraries. Are you online?"
    read -r -p "Press Return to close this window."
    exit 1
  }
  echo "Setup done."
  echo
fi

# Hand over to the app. It opens your browser itself.
./.venv/bin/python app.py

# If the app stops, keep the window open long enough to read why.
echo
read -r -p "The analyzer has stopped. Press Return to close this window."
