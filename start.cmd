@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [orderflow] creating virtual environment in .venv ...
    where py >nul 2>nul
    if %errorlevel%==0 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto :fail
)

rem Reinstall only when pyproject.toml changed since the last install.
set "STAMP=.venv\.orderflow-installed"
set "NEEDINSTALL=1"
if exist "%STAMP%" (
    for /f %%A in ('powershell -NoProfile -Command "if ((Get-Item pyproject.toml).LastWriteTime -le (Get-Item '%STAMP%').LastWriteTime) { 'no' } else { 'yes' }"') do (
        if "%%A"=="no" set "NEEDINSTALL=0"
    )
)
if "%NEEDINSTALL%"=="1" (
    echo [orderflow] installing dependencies ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e .
    if errorlevel 1 goto :fail
    type nul > "%STAMP%"
)

".venv\Scripts\python.exe" -m orderflow %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo [orderflow] failed with exit code %errorlevel%. See messages above and logs\orderflow.log
echo Requires Python 3.11+ (https://www.python.org/downloads/) and internet access for setup.
pause
exit /b 1
