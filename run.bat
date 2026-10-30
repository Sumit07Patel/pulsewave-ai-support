@echo off
cd /d "%~dp0"
if not exist venv (
  echo Creating virtual environment...
  python -m venv venv
)
call venv\Scripts\activate
echo Installing requirements (first time takes a few minutes)...
pip install -r requirements.txt
echo.
echo Starting server. Open http://127.0.0.1:8000 in your browser.
uvicorn app.main:app --reload
pause
