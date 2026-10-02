@echo off
rem Birdsong launcher for Windows.
rem
rem First run: downloads uv (a small Python manager, ~15 MB) into .runtime\,
rem which then fetches its own Python 3.12 and the libraries into .runtime\
rem as well. Nothing is installed system-wide; delete the folder to remove it.
rem
rem   run.bat              listen + serve the page (runs setup the first time)
rem   run.bat setup        change settings      run.bat demo    preview
rem   run.bat --help       all commands
setlocal
cd /d "%~dp0"
set "RT=%~dp0.runtime"
set "UV=%RT%\uv\uv.exe"
if exist "%UV%" goto haveuv
echo First run: downloading uv (Python manager) into .runtime\ ...
if not exist "%RT%\uv" mkdir "%RT%\uv"
set "ARCH=x86_64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=aarch64"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $z=Join-Path $env:TEMP 'birdsong-uv.zip'; Invoke-WebRequest -UseBasicParsing ('https://github.com/astral-sh/uv/releases/latest/download/uv-'+$env:ARCH+'-pc-windows-msvc.zip') -OutFile $z; Expand-Archive -Force $z (Join-Path $env:RT 'uv'); Remove-Item $z"
if not exist "%UV%" (
  echo Could not download uv. Check your internet connection and try again.
  goto fail
)
:haveuv
set "UV_PYTHON_INSTALL_DIR=%RT%\python"
set "UV_CACHE_DIR=%RT%\cache"
set "UV_PROJECT_ENVIRONMENT=%RT%\venv"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_NO_PROGRESS=1"
"%UV%" run --quiet --frozen python server.py %*
if errorlevel 1 goto fail
exit /b 0
:fail
if not defined BIRDSONG_NOPAUSE pause
exit /b 1
