@echo off
REM sync_force.bat — Force Samsung Notes sync to complete without stopping
set LOG=C:\Users\notes\sync_force_log.txt
echo === Force sync started %DATE% %TIME% === > %LOG%

REM --- 1. Disable UWP app suspension completely ---
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling" /v PowerThrottlingOff /t REG_DWORD /d 1 /f >> %LOG% 2>&1

REM --- 2. Disable screen saver and lock ---
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f >> %LOG% 2>&1
reg add "HKCU\Control Panel\Desktop" /v ScreenSaverIsSecure /t REG_SZ /d 0 /f >> %LOG% 2>&1
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveTimeOut /t REG_SZ /d 0 /f >> %LOG% 2>&1

REM --- 3. Disable monitor timeout ---
powercfg /change monitor-timeout-ac 0 >> %LOG% 2>&1
powercfg /change monitor-timeout-dc 0 >> %LOG% 2>&1
powercfg /change standby-timeout-ac 0 >> %LOG% 2>&1
powercfg /change standby-timeout-dc 0 >> %LOG% 2>&1
powercfg /change hibernate-timeout-ac 0 >> %LOG% 2>&1
powercfg /change hibernate-timeout-dc 0 >> %LOG% 2>&1

REM --- 4. Disable Windows Update (already done but reinforce) ---
sc config wuauserv start= disabled >> %LOG% 2>&1

REM --- 5. Set Samsung Notes to never suspend (UWP background task policy) ---
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications" /v GlobalUserDisabled /t REG_DWORD /d 0 /f >> %LOG% 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Search" /v BackgroundAppGlobalToggle /t REG_DWORD /d 0 /f >> %LOG% 2>&1

REM --- 6. Run Samsung Notes at HIGH priority ---
wmic process where "name='SamsungNotes.exe'" CALL setpriority 128 >> %LOG% 2>&1

echo === Force sync finished %DATE% %TIME% === >> %LOG%
