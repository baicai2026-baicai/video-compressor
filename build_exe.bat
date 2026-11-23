@echo off
setlocal
pushd "%~dp0" || exit /b 1

python -m pip install pyinstaller
if errorlevel 1 goto failed
python -m PyInstaller -F -w video_compressor.py
if errorlevel 1 goto failed

if exist "ffmpeg.exe" copy /y "ffmpeg.exe" "dist\ffmpeg.exe" >nul
if exist "ffprobe.exe" copy /y "ffprobe.exe" "dist\ffprobe.exe" >nul
echo Built dist\video_compressor.exe.
echo If missing, add ffmpeg.exe and ffprobe.exe to dist or PATH before running.
popd
pause
exit /b 0

:failed
echo Build failed. See the error above.
popd
pause
exit /b 1
