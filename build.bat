@echo off
REM Compilation locale sous Windows (Python 3.11 ou plus requis)
cd /d "%~dp0"
python -m venv .venv || goto :erreur
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller==6.22.3 || goto :erreur
pyinstaller supervision.spec --noconfirm || goto :erreur
echo.
echo Termine : dist\Supervision-ADRASEC06.exe
pause
exit /b 0
:erreur
echo La compilation a echoue.
pause
exit /b 1
