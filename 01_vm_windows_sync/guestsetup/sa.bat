@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
"%WG%" search Samsung -s msstore --accept-source-agreements > C:\Users\notes\sa.log 2>&1
echo DONE_SEARCH >> C:\Users\notes\sa.log
