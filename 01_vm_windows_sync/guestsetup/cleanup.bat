@echo off
REM cleanup.bat — Remove bloat, disable updates/telemetry, clear Samsung Notes cache
REM Must run via schtasks in interactive session

set LOG=C:\Users\notes\cleanup_log.txt
echo === Cleanup started %DATE% %TIME% === > %LOG%

REM --- 1. Disable Windows Update ---
sc config wuauserv start= disabled >> %LOG% 2>&1
net stop wuauserv >> %LOG% 2>&1
sc config bits start= disabled >> %LOG% 2>&1
net stop bits >> %LOG% 2>&1
sc config dosvc start= disabled >> %LOG% 2>&1
net stop dosvc >> %LOG% 2>&1
echo Disabled Windows Update >> %LOG%

REM --- 2. Disable telemetry services ---
sc config DiagTrack start= disabled >> %LOG% 2>&1
net stop DiagTrack >> %LOG% 2>&1
sc config dmwappushservice start= disabled >> %LOG% 2>&1
net stop dmwappushservice >> %LOG% 2>&1
sc config lfsvc start= disabled >> %LOG% 2>&1
echo Disabled telemetry >> %LOG%

REM --- 3. Disable startup via registry ---
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /f >> %LOG% 2>&1
echo Cleared user startup >> %LOG%

REM --- 4. Disable scheduled tasks ---
schtasks /Change /TN "\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser" /Disable >> %LOG% 2>&1
schtasks /Change /TN "\Microsoft\Windows\Application Experience\ProgramDataUpdater" /Disable >> %LOG% 2>&1
schtasks /Change /TN "\Microsoft\Windows\Customer Experience Improvement Program\Consolidator" /Disable >> %LOG% 2>&1
schtasks /Change /TN "\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip" /Disable >> %LOG% 2>&1
schtasks /Change /TN "\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector" /Disable >> %LOG% 2>&1
schtasks /Change /TN "\Microsoft\Windows\Feedback\Siuf\DmClient" /Disable >> %LOG% 2>&1
schtasks /Change /TN "\Microsoft\Windows\Maps\MapsUpdateTask" /Disable >> %LOG% 2>&1
schtasks /Change /TN "\Microsoft\Windows\Maps\MapsToastTask" /Disable >> %LOG% 2>&1
echo Disabled scheduled tasks >> %LOG%

REM --- 5. Clear Samsung Notes cache (keep LocalState = SQLite DB) ---
set SAMPKG=C:\Users\notes\AppData\Local\Packages\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy
if exist "%SAMPKG%\INetCache" rd /s /q "%SAMPKG%\INetCache" >> %LOG% 2>&1
if exist "%SAMPKG%\TempState" rd /s /q "%SAMPKG%\TempState" >> %LOG% 2>&1
if exist "%SAMPKG%\AC\INetCache" rd /s /q "%SAMPKG%\AC\INetCache" >> %LOG% 2>&1
if exist "%SAMPKG%\AC\TempState" rd /s /q "%SAMPKG%\AC\TempState" >> %LOG% 2>&1
echo Cleared Samsung Notes cache >> %LOG%

REM --- 6. Clean up disk space ---
rd /s /q "C:\Windows\Temp" >> %LOG% 2>&1
mkdir "C:\Windows\Temp" >> %LOG% 2>&1
rd /s /q "C:\Users\notes\AppData\Local\Temp" >> %LOG% 2>&1
mkdir "C:\Users\notes\AppData\Local\Temp" >> %LOG% 2>&1
rd /s /q "C:\Windows\SoftwareDistribution\Download" >> %LOG% 2>&1
mkdir "C:\Windows\SoftwareDistribution\Download" >> %LOG% 2>&1
del /q /f "C:\Windows\Logs\*.log" >> %LOG% 2>&1
del /s /q "C:\Windows\Logs\CBS\*.log" >> %LOG% 2>&1
echo Cleaned disk >> %LOG%

echo === Cleanup finished %DATE% %TIME% === >> %LOG%
