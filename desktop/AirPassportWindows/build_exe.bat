@echo off
chcp 65001 >nul
echo 正在安装打包依赖...
python -m pip install -r requirements.txt pyinstaller -q
echo 正在编译生成独立单文件 EXE...
pyinstaller --noconsole --onefile --name "AirPassport" air_passport.py
echo.
echo ==============================================
echo  编译完成！程序位于 dist/AirPassport.exe
echo ==============================================
pause
