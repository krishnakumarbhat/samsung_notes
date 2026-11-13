@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
echo {"sourcing":{"msstore":{"region":"US"}}} > "%LOCALAPPDATA%\Packages\Microsoft.DesktopAppInstaller_8wekyb3d8bbwe\LocalState\settings.json"
echo === INSTALL ===
"%WG%" install 9NBLGGH43VHV -s msstore --accept-package-agreements --accept-source-agreements > C:\Users\notes\wg4.log 2>&1
type C:\Users\notes\wg4.log
echo === FINISHED ===
