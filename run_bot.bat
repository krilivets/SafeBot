@echo off
chcp 65001 >nul
echo Installing requirements...
python -m pip install -r requirements.txt
echo Starting bot...
python bot.py
pause
