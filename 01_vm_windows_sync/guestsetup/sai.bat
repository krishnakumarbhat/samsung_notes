@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
"%WG%" install 9P98T77876KZ -s msstore --accept-package-agreements --accept-source-agreements > C:\Users\notes\sai.log 2>&1
echo DONE_INSTALL >> C:\Users\notes\sai.log
