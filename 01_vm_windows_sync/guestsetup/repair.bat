@echo off
echo === STEP 1: KILL EDGE ===
taskkill /f /im msedge.exe 2>nul
taskkill /f /im msedgewebview2.exe 2>nul
timeout /t 2

echo === STEP 2: REPAIR SYSTEM ===
DISM /Online /Cleanup-Image /RestoreHealth > C:\Users\notes\repair.log 2>&1
echo DISM_DONE >> C:\Users\notes\repair.log

echo === STEP 3: RE-REGISTER STORE ===
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-AppxPackage -AllUsers Microsoft.WindowsStore | ForEach-Object {Add-AppxPackage -DisableDevelopmentMode -Register \"$($_.InstallLocation)\AppXManifest.xml\" -ForceApplicationShutdown}" >> C:\Users\notes\repair.log 2>&1
echo REGISTER_DONE >> C:\Users\notes\repair.log

echo === STEP 4: LAUNCH STORE ===
timeout /t 5
taskkill /f /im WinStore.App.exe 2>nul
timeout /t 2
start "" "ms-windows-store:"
echo ALL_DONE >> C:\Users\notes\repair.log
echo === ALL DONE ===
