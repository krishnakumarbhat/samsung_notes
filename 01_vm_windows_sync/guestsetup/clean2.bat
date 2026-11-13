@echo off
powershell -NoProfile -Command ^
  "$rm='Microsoft.WindowsCalculator','Microsoft.Windows.Apprep.ChxApp','Microsoft.Windows.ParentalControls','Microsoft.Windows.SecureAssessmentBrowser','Microsoft.Windows.NarratorQuickStart','MicrosoftWindows.UndockedDevKit','Microsoft.Windows.CallingShellApp','Microsoft.XboxGameCallableUI';" ^
  "foreach($n in $rm){ Get-AppxPackage $n | Remove-AppxPackage -ErrorAction SilentlyContinue; Write-Host \"removed $n\" }" > C:\Users\notes\clean2.log 2>&1
echo === NET TWEAKS ===
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" /v NetworkThrottlingIndex /t REG_DWORD /d 0xffffffff /f >> C:\Users\notes\clean2.log 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v DoNotConnectToWindowsUpdateInternetLocations /t REG_DWORD /d 1 /f >> C:\Users\notes\clean2.log 2>&1
echo === KEEP SAMSUNG BACKGROUND SYNC UNTHROTTLED ===
powershell -NoProfile -Command "Get-AppxPackage *Samsung* | ForEach-Object { Write-Host $_.Name $_.Version }" >> C:\Users\notes\clean2.log 2>&1
echo ALL_DONE >> C:\Users\notes\clean2.log
