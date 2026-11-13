@echo off
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
echo === TIME CHECK ===
echo %DATE% %TIME% >> C:\Users\notes\dbg.log
w32tm /stripchart /computer:time.windows.com /samples:1 /dataonly >> C:\Users\notes\dbg.log 2>&1
echo === WINGET DOWNLOAD SAMSUNG ACCOUNT ===
"%WG%" download 9P98T77876KZ -s msstore -d C:\Users\notes\sa_dl --accept-package-agreements --accept-source-agreements --skip-microsoft-agreement > C:\Users\notes\dbg.log 2>&1
echo DL_EXIT=%ERRORLEVEL% >> C:\Users\notes\dbg.log
dir C:\Users\notes\sa_dl >> C:\Users\notes\dbg.log 2>&1
echo === INSTALL WHAT WAS DOWNLOADED ===
powershell -NoProfile -Command "Get-ChildItem C:\Users\notes\sa_dl -Recurse -Include *.appx,*.msix,*.appxbundle,*.msixbundle | Sort-Object Name | ForEach-Object { Write-Host \"Installing $($_.Name)\"; Add-AppxPackage -Path $_.FullName -ErrorAction Continue }" >> C:\Users\notes\dbg.log 2>&1
echo === VERIFY ===
powershell -NoProfile -Command "Get-AppxPackage *SamsungAccount* | Select Name,Version" >> C:\Users\notes\dbg.log 2>&1
echo ALL_DONE >> C:\Users\notes\dbg.log
