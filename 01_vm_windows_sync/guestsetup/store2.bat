@echo off
echo === CHECK STORE HEALTH ===
powershell -NoProfile -Command "Get-Service InstallService,WSService,DevicesFlowUserDataSvc | Select Name,Status,StartType | Format-Table -AutoSize" > C:\Users\notes\store2.log 2>&1
echo === CHECK STORE PACKAGE ===
powershell -NoProfile -Command "Get-AppxPackage Microsoft.WindowsStore | Select Name,Version,Status | Format-List" >> C:\Users\notes\store2.log 2>&1
echo === RESET STORE CACHE ===
taskkill /f /im WinStore.App.exe 2>nul
taskkill /f /im Client.Server.exe 2>nul
taskkill /f /im wsreset.exe 2>nul
timeout /t 2
start wsreset.exe
timeout /t 15
taskkill /f /im wsreset.exe 2>nul
echo === RE-REGISTER STORE ===
powershell -NoProfile -Command "$p=Get-AppxPackage -AllUsers Microsoft.WindowsStore; if($p){Add-AppxPackage -DisableDevelopmentMode -Register \"$($p.InstallLocation)\AppXManifest.xml\"}" >> C:\Users\notes\store2.log 2>&1
echo === FORCE UPDATE CHECK ===
powershell -NoProfile -Command "Get-AppxPackage -AllUsers Microsoft.WindowsStore | ForEach-Object { \$_.PackageFamilyName }" >> C:\Users\notes\store2.log 2>&1
echo === LAUNCH STORE ===
timeout /t 5
start "" "ms-windows-store:"
echo === ALL DONE ===
type C:\Users\notes\store2.log
