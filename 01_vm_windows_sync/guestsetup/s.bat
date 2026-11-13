@echo off
echo === SSH SETUP V3 ===
net session >nul 2>&1
if errorlevel 1 (
  echo Requesting admin rights...
  powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c %~f0' -Verb RunAs"
  exit /b
)
echo === Downloading OpenSSH ===
curl -L -o %TEMP%\o.zip https://github.com/PowerShell/Win32-OpenSSH/releases/download/v9.5.0.0p1-Beta/OpenSSH-Win64.zip
if not exist %TEMP%\o.zip (echo DOWNLOAD FAILED & pause & exit /b 1)
echo === Extracting ===
tar -xf %TEMP%\o.zip -C "C:\Program Files"
if not exist "C:\Program Files\OpenSSH-Win64\install-sshd.ps1" (echo EXTRACT FAILED & pause & exit /b 1)
echo === Installing service ===
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Program Files\OpenSSH-Win64\install-sshd.ps1"
sc config sshd start= auto
net start sshd
echo === Firewall rule ===
netsh advfirewall firewall add rule name=SSH dir=in action=allow protocol=TCP localport=22
del %TEMP%\o.zip
echo.
echo === DONE - SSH REACHABLE ===
pause
