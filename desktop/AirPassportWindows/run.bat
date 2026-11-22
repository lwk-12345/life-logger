@echo off
chcp 65001 >nul
title AirPassport Receiver
echo ==============================================
echo   AirPassport Windows 跨平台接收器启动中...
echo ==============================================
python -m pip install -r requirements.txt -q
python air_passport.py
pause
