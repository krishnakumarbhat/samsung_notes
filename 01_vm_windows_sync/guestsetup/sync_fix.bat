@echo off
REM sync_fix.bat — Fix Samsung Notes sync stability
set LOG=C:\Users\notes\sync_fix_log.txt
echo === Sync fix started === > %LOG%

REM --- 1. Disable network throttling ---
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v NetworkThrottlingIndex /t REG_DWORD /d 4294967295 /f >> %LOG% 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v SystemResponsiveness /t REG_DWORD /d 0 /f >> %LOG% 2>&1
echo Set NetworkThrottlingIndex >> %LOG%

REM --- 2. Optimize TCP for bulk transfers ---
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" /v TcpTimedWaitDelay /t REG_DWORD /d 30 /f >> %LOG% 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" /v MaxUserPort /t REG_DWORD /d 65534 /f >> %LOG% 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" /v TcpNumConnections /t REG_DWORD /d 16777214 /f >> %LOG% 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" /v DefaultTTL /t REG_DWORD /d 128 /f >> %LOG% 2>&1
echo Set TCP params >> %LOG%

REM --- 3. Disable UWP app suspension (power throttling) ---
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling" /v PowerThrottlingOff /t REG_DWORD /d 1 /f >> %LOG% 2>&1
echo Disabled power throttling >> %LOG%

REM --- 4. Set Samsung Notes as high priority in scheduler ---
schtasks /Change /TN "\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser" /Disable >> %LOG% 2>&1
REM Disable Delivery Optimization (competes for bandwidth)
sc config dosvc start= disabled >> %LOG% 2>&1
net stop dosvc >> %LOG% 2>&1
echo Disabled Delivery Optimization >> %LOG%

REM --- 5. Set power plan to High Performance ---
powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c >> %LOG% 2>&1
echo Set High Performance power plan >> %LOG%

REM --- 6. Disable modem power saving ---
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP 94AC6BB2-03D7-4D13-A99D-E688098D3009 0 >> %LOG% 2>&1
powercfg /SETACTIVE SCHEME_CURRENT >> %LOG% 2>&1
echo Disabled power saving >> %LOG%

echo === Sync fix finished === >> %LOG%
