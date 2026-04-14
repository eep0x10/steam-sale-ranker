@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set PYTHON="C:\Program Files (x86)\Python314-32\python.exe"
set SCRIPT="%~dp0steam_sale_ranker.py"

if "%1"=="" (
    %PYTHON% %SCRIPT% 10
) else (
    %PYTHON% %SCRIPT% %*
)

pause
