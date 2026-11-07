@echo off
cd /d "%~dp0"
if not exist .venv (
    echo First start: creating environment...
    py -m venv .venv
)
call .venv\Scripts\activate.bat
echo Checking libraries, please wait...
pip install -q -r requirements.txt
if not exist .env (
    copy .env.example .env >nul
    echo File .env was created. Open it in Notepad, paste BOT_TOKEN, save, then run start.bat again.
    pause
    exit /b
)
python bot.py
pause
