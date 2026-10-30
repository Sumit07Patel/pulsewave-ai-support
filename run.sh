#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
echo "Starting server. Open http://127.0.0.1:8000 in your browser."
uvicorn app.main:app --reload
