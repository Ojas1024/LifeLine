@echo off
REM Builds dist\Lifeline.exe: one portable file that asks for admin rights on launch.
REM Run this on a DIFFERENT drive than the one you're recovering from.
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin --name Lifeline ^
  --icon lifeline\assets\icon.ico --add-data "lifeline\assets;lifeline\assets" ^
  --collect-submodules lifeline launcher.py
echo.
echo Done: dist\Lifeline.exe  (copy it to a USB stick and run it from there)
pause
