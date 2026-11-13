@echo off
echo === BACKGROUND PERMISSIONS ===
powershell -NoProfile -Command "Get-StartApps | Where-Object {$_.Name -like '*Samsung Notes*'} | Format-List" > C:\Users\notes\bg.log 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy" /v Disabled /t REG_DWORD /d 0 /f >> C:\Users\notes\bg.log 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications\SAMSUNGELECTRONICSCO.LTD.SamsungAccount_wyx1vj98g3asy" /v Disabled /t REG_DWORD /d 0 /f >> C:\Users\notes\bg.log 2>&1
echo BG_DONE >> C:\Users\notes\bg.log
type C:\Users\notes\bg.log | findstr /i "done operation"
