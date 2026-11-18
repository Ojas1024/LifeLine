@echo off
REM Starts Lifeline. Windows will ask for administrator access (needed to read drives).
cd /d "%~dp0"
python -c "import PyQt6" 2>nul || python -m pip install -r requirements.txt
powershell -Command "Start-Process python -ArgumentList '-m','lifeline' -Verb RunAs -WorkingDirectory '%~dp0'"
