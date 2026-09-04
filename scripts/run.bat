@echo off
rem M22 Product Radar — запуск веб-интерфейса с планировщиком (Windows)
set PYTHONUTF8=1
cd /d "%~dp0.."
python -m radar serve
