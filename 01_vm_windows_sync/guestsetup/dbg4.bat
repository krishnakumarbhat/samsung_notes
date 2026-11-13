@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
if exist C:\Users\notes\sa_dl rmdir /s /q C:\Users\notes\sa_dl
"%WG%" download --id 9P98T77876KZ -s msstore -d C:\Users\notes\sa_dl --accept-package-agreements --accept-source-agreements --skip-license > C:\Users\notes\dbg4.log 2>&1
echo DL_EXIT=%ERRORLEVEL% >> C:\Users\notes\dbg4.log
dir C:\Users\notes\sa_dl >> C:\Users\notes\dbg4.log 2>&1
echo ALL_DONE >> C:\Users\notes\dbg4.log
