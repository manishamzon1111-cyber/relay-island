#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then echo 'Python 3 is required.'; exit 1; fi
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
python3 -m pip install --disable-pip-version-check -r requirements.txt
exec python3 -m relay_web --media ./media
