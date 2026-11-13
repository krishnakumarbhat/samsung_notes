@echo off
echo === KILL EDGE ===
taskkill /f /im msedge.exe 2>nul
timeout /t 2
echo === REPAIR STORE ===
powershell -NoProfile -Command "Get-AppxPackage -AllUsers Microsoft.WindowsStore | ForEach-Object {Add-AppxPackage -DisableDevelopmentMode -Register \"$($_.InstallLocation)\AppXManifest.xml\" -ErrorAction SilentlyContinue}" > C:\Users\notes\store.log 2>&1
echo === RESTART STORE ===
taskkill /f /im WinStore.App.exe 2>nul
taskkill /f /im Client.Server.exe 2>nul
timeout /t 2
echo === OPEN SAMSUNG ACCOUNT IN STORE ===
start ms-windows-store://pdp/?ProductId=9P98T77876KZ
echo === DONE ===
