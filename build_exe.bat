@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install pyinstaller
for /f "delims=" %%i in (%python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"%) do set FF=%%i
copy /y "%FF%" ffmpeg.exe >nul
pyinstaller --onefile --windowed --name 담다 --icon damda.ico --add-binary "ffmpeg.exe;." --add-data "damda.ico;." damda.py
echo.
echo dist\담다.exe 가 생겼습니다. 이 파일 하나만 나눠 주면 됩니다.
pause
