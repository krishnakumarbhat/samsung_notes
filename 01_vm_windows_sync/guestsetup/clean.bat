@echo off
powershell -NoProfile -Command "$keep='WindowsStore','StorePurchaseApp','SamsungNotes','SamsungAccount','DesktopAppInstaller','VCLibs','NET.Native','UI.Xaml','Services.Store','ShellExperienceHost','StartMenuExperienceHost','WindowsCalculator','SecHealthUI','WindowsDefender','Microsoft.Win32.WebView2','MicrosoftWindows.Client'; Get-AppxPackage | Where-Object { $n=$_.Name; -not ($keep | Where-Object { $n -like \"*$_*\" }) } | ForEach-Object { Write-Host \"Removing $($_.Name)\"; Remove-AppxPackage -Package $_.PackageFullName -ErrorAction SilentlyContinue }" > C:\Users\notes\clean.log 2>&1
echo APPX_DONE >> C:\Users\notes\clean.log
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --version >nul 2>&1
for /d %%d in ("C:\Program Files (x86)\Microsoft\Edge\Application\*") do if exist "%%d\Installer\setup.exe" "%%d\Installer\setup.exe" --uninstall --force-uninstall --system-level --verbose-logging >> C:\Users\notes\clean.log 2>&1
echo EDGE_DONE >> C:\Users\notes\clean.log
if exist "%SystemRoot%\SysWOW64\OneDriveSetup.exe" %SystemRoot%\SysWOW64\OneDriveSetup.exe /uninstall >> C:\Users\notes\clean.log 2>&1
if exist "%SystemRoot%\System32\OneDriveSetup.exe" %SystemRoot%\System32\OneDriveSetup.exe /uninstall >> C:\Users\notes\clean.log 2>&1
echo CLEAN_ALL_DONE >> C:\Users\notes\clean.log
