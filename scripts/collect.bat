@echo off
rem Полный цикл: сбор -> анализ -> отчёт (для Планировщика заданий Windows, если сервер не запущен постоянно)
set PYTHONUTF8=1
cd /d "%~dp0.."
python -m radar pipeline
