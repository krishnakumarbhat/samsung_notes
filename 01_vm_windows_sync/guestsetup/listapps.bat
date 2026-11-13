@echo off
powershell -NoProfile -Command "Get-AppxPackage | Select -Expand Name | Sort" > C:\Users\notes\apps.log 2>&1
echo LIST_DONE >> C:\Users\notes\apps.log
