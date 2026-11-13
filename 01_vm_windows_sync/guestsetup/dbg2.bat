@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
"%WG%" download --help > C:\Users\notes\dlhelp.log 2>&1
"%WG%" download --ProductId 9P98T77876KZ -s msstore -d C:\Users\notes\sa_dl --accept-package-agreements --accept-source-agreements > C:\Users\notes\dbg.log 2>&1
echo DL_EXIT=%ERRORLEVEL% >> C:\Users\notes\dbg.log
dir C:\Users\notes\sa_dl >> C:\Users\notes\dbg.log 2>&1
echo ALL_DONE >> C:\Users\notes\dbg.log
