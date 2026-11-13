@echo off
echo === WINGET INSTALL ===
curl -s -o %TEMP%\vclibs.appx http://10.0.2.2:8123/vclibs.appx
curl -s -o %TEMP%\xaml.appx http://10.0.2.2:8123/xaml.appx
curl -s -o %TEMP%\winget.msixbundle http://10.0.2.2:8123/winget.msixbundle
if not exist %TEMP%\winget.msixbundle (echo DL FAILED & exit /b 1)
powershell -NoProfile -Command "Add-AppxPackage %TEMP%\vclibs.appx; Add-AppxPackage %TEMP%\xaml.appx; Add-AppxPackage %TEMP%\winget.msixbundle"
del %TEMP%\vclibs.appx %TEMP%\xaml.appx %TEMP%\winget.msixbundle
winget --version
echo === WINGET DONE ===
