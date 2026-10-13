@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Сборка Словарика в один .exe

set "PYC="
where python.exe >nul 2>&1 && set "PYC=python.exe"
if not defined PYC (
    where py.exe >nul 2>&1 && set "PYC=py.exe"
)
if not defined PYC (
    echo Python не найден. Установите его с https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Устанавливаю PyInstaller и keyboard...
%PYC% -m pip install --upgrade --quiet pip pyinstaller keyboard

echo.
echo Собираю exe (это займёт минуту-две)...
%PYC% -m PyInstaller --noconfirm --clean --onefile --noconsole --name Slovarik slovarik.py

if errorlevel 1 (
    echo.
    echo Что-то пошло не так — смотрите текст ошибки выше.
    pause
    exit /b 1
)

echo.
echo   Готово! Файл: dist\Slovarik.exe
echo   Его можно переименовать в "Словарик.exe" и запускать двойным щелчком.
echo.
pause
