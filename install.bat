@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 필요한 프로그램 설치 중 (인터넷 필요, 2~5분)...
python -m pip install -U pip >nul
python -m pip install -U yt-dlp PySide6-Essentials imageio-ffmpeg
echo.
echo 설치 끝. run.bat 을 더블클릭해 실행하세요.
pause
