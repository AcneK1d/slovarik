@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Словарик — запуск

set "PYC="
set "PYW="
where python.exe >nul 2>&1 && set "PYC=python.exe"
if not defined PYC (
    where py.exe >nul 2>&1 && set "PYC=py.exe"
)
where pythonw.exe >nul 2>&1 && set "PYW=pythonw.exe"
if not defined PYW (
    where pyw.exe >nul 2>&1 && set "PYW=pyw.exe"
)
if not defined PYW set "PYW=%PYC%"

if not defined PYC (
    echo.
    echo   Python не найден.
    echo.
    echo   Установите его с сайта https://www.python.org/downloads/
    echo   и обязательно отметьте галочку "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

%PYC% -c "import keyboard" >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю пакет keyboard — он нужен для глобальной горячей клавиши...
    %PYC% -m pip install --quiet keyboard
    echo Готово.
)

echo Запускаю Словарик. Нажмите Ctrl+Space, чтобы показать окно.
start "" "%PYW%" "slovarik.py"
exit /b 0
