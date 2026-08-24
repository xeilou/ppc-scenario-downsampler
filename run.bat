@echo off
REM Double-click this to run the downsampler with the settings you edited
REM at the top of downsample_pipeline.py. The window stays open afterward
REM so you can read the result.
cd /d "%~dp0"
python downsample_pipeline.py
echo.
pause
