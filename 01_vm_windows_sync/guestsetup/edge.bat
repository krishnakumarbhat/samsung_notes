@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
echo === INSTALL EDGE ===
"%WG%" install Microsoft.Edge --accept-package-agreements --accept-source-agreements > C:\Users\notes\edge.log 2>&1
type C:\Users\notes\edge.log
echo === VERIFY ===
"%WG%" list Microsoft.Edge 2>nul
echo === DONE ===
