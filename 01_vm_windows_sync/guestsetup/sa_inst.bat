@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
echo === INSTALL SAMSUNG ACCOUNT ===
"%WG%" install 9P98T77876KZ -s msstore --accept-package-agreements --accept-source-agreements > C:\Users\notes\sa_inst.log 2>&1
type C:\Users\notes\sa_inst.log
echo === VERIFY ===
powershell -NoProfile -Command "Get-AppxPackage *SamsungAccount* | Select Name,Version | Format-List"
echo === DONE ===
