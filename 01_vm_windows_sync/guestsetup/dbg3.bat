@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
"%WG%" download "Samsung account" -s msstore -d C:\Users\notes\sa_dl --accept-package-agreements --accept-source-agreements --platform windows > C:\Users\notes\dbg3.log 2>&1
echo DL_EXIT=%ERRORLEVEL% >> C:\Users\notes\dbg3.log
dir C:\Users\notes\sa_dl >> C:\Users\notes\dbg3.log 2>&1
echo ALL_DONE >> C:\Users\notes\dbg3.log
