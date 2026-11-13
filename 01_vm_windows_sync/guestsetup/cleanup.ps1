# cleanup.ps1 — Remove bloat, disable updates/telemetry, clear Samsung Notes cache
# Run via schtasks in interactive session

$ErrorActionPreference = 'SilentlyContinue'
$log = "C:\Users\notes\cleanup_log.txt"
"=== Cleanup started $(Get-Date) ===" | Out-File $log

# --- 1. Remove unwanted apps (MSIX provisioned + per-user) ---
$removeApps = @(
    '*Microsoft.Gethelp*'
    '*Microsoft.Getstarted*'
    '*Microsoft.Microsoft3DViewer*'
    '*Microsoft.MicrosoftSolitaireCollection*'
    '*Microsoft.People*'
    '*Microsoft.SkypeApp*'
    '*Microsoft.MicrosoftStickyNotes*'
    '*Microsoft.WindowsAlarms*'
    '*Microsoft.WindowsFeedbackHub*'
    '*Microsoft.WindowsMaps*'
    '*Microsoft.YourPhone*'
    '*Microsoft.ZuneMusic*'
    '*Microsoft.ZuneVideo*'
    '*Microsoft.WindowsCommunicationsApps*'
    '*Microsoft.BingWeather*'
    '*Microsoft.BingNews*'
    '*Microsoft.BingFinance*'
    '*Microsoft.BingSports*'
    '*Microsoft.WindowsSoundRecorder*'
    '*Microsoft.WindowsCamera*'
    '*Microsoft.WindowsCalculator*'
    '*Microsoft.ScreenSketch*'
    '*Microsoft.Xbox*'
    '*Microsoft.GamingApp*'
    '*Microsoft.549981C3F5F10*'
    '*Clipchamp*'
    '*Microsoft.PowerAutomateDesktop*'
    '*Microsoft.Todos*'
    '*Disney.37853FC22B2CE*'
    '*SpotifyAB.SpotifyMusic*'
    '*BytedancePte.Ltd.TikTok*'
    '*Facebook*'
    '*Instagram*'
    '*king.com*'
)

foreach ($pattern in $removeApps) {
    try {
        Get-AppxPackage -AllUsers $pattern | Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue
        Get-AppxProvisionedPackage -Online | Where-Object {$_.PackageName -like $pattern.Replace('*','')} | Remove-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue
    } catch {}
}
"Removed bloat apps" | Out-File $log -Append

# --- 2. Disable Windows Update ---
sc.exe config wuauserv start= disabled 2>$null
Stop-Service wuauserv -Force 2>$null
sc.exe config bits start= disabled 2>$null
Stop-Service bits -Force 2>$null
sc.exe config dosvc start= disabled 2>$null
Stop-Service dosvc -Force 2>$null
"Disabled Windows Update services" | Out-File $log -Append

# --- 3. Disable telemetry & tracking ---
# Disable Connected User Experiences and Telemetry
sc.exe config DiagTrack start= disabled 2>$null
Stop-Service DiagTrack -Force 2>$null
sc.exe config dmwappushservice start= disabled 2>$null
Stop-Service dmwappushservice -Force 2>$null

# Disable Customer Experience Improvement Program
$ceipPath = "HKLM:\SOFTWARE\Policies\Microsoft\SQMClient\Windows"
if (!(Test-Path $ceipPath)) { New-Item -Path $ceipPath -Force | Out-Null }
Set-ItemProperty -Path $ceipPath -Name "CEIPEnable" -Value 0 -Type DWord

# Disable advertising ID
$advPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo"
if (!(Test-Path $advPath)) { New-Item -Path $advPath -Force | Out-Null }
Set-ItemProperty -Path $advPath -Name "DisabledByGroupPolicy" -Value 1 -Type DWord

# Disable location tracking
sc.exe config lfsvc start= disabled 2>$null

# Disable diagnostics data
$diagPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection"
if (!(Test-Path $diagPath)) { New-Item -Path $diagPath -Force | Out-Null }
Set-ItemProperty -Path $diagPath -Name "AllowTelemetry" -Value 0 -Type DWord

# Disable Cortana
$cortanaPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search"
if (!(Test-Path $cortanaPath)) { New-Item -Path $cortanaPath -Force | Out-Null }
Set-ItemProperty -Path $cortanaPath -Name "AllowCortana" -Value 0 -Type DWord

"Disabled telemetry" | Out-File $log -Append

# --- 4. Disable startup programs ---
$startupPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runnerPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
# Clear user startup
Set-ItemProperty -Path $startupPath -Name "(Default)" -Value "" -ErrorAction SilentlyContinue
Get-ItemProperty -Path $startupPath | ForEach-Object {
    $_.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object {
        Remove-ItemProperty -Path $startupPath -Name $_.Name -ErrorAction SilentlyContinue
    }
}
# Disable scheduled tasks (common bloat)
$schTasks = @*
Disable-ScheduledTask -TaskName "*MicrosoftEdge*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*OneDrive*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*Office*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*Xbox*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*Cortana*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*Defender*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*Consolidator*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*UsbCeip*" -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName "*KernelCeip*" -ErrorAction SilentlyContinue
"Disabled startup" | Out-File $log -Append

# --- 5. Clear Samsung Notes cache (but keep data) ---
$samNotesPackage = "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy"
$cachePath = "C:\Users\notes\AppData\Local\Packages\$samNotesPackage"
# Clear INetCache, TempState, but keep LocalState (has the SQLite DB)
if (Test-Path "$cachePath\INetCache") { Remove-Item "$cachePath\INetCache\*" -Recurse -Force }
if (Test-Path "$cachePath\TempState") { Remove-Item "$cachePath\TempState\*" -Recurse -Force }
if (Test-Path "$cachePath\AC\INetCache") { Remove-Item "$cachePath\AC\INetCache\*" -Recurse -Force }
if (Test-Path "$cachePath\AC\TempState") { Remove-Item "$cachePath\AC\TempState\*" -Recurse -Force }
"Cleared Samsung Notes cache" | Out-File $log -Append

# --- 6. Clean up disk space ---
# Windows temp
Remove-Item "C:\Windows\Temp\*" -Recurse -Force -ErrorAction SilentlyContinue
# User temp
Remove-Item "C:\Users\notes\AppData\Local\Temp\*" -Recurse -Force -ErrorAction SilentlyContinue
# Windows Update cache
Stop-Service wuauserv -Force 2>$null
Remove-Item "C:\Windows\SoftwareDistribution\Download\*" -Recurse -Force -ErrorAction SilentlyContinue
# Windows log files
Remove-Item "C:\Windows\Logs\*.log" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "C:\Windows\Logs\CBS\*.log" -Recurse -Force -ErrorAction SilentlyContinue
# Recycle Bin
Clear-RecycleBin -Force -ErrorAction SilentlyContinue
# Browser caches
Remove-Item "C:\Users\notes\AppData\Local\Microsoft\Edge\User Data\Default\Cache\*" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "C:\Users\notes\AppData\Local\Microsoft\Edge\User Data\Default\Code Cache\*" -Recurse -Force -ErrorAction SilentlyContinue
"Cleaned disk" | Out-File $log -Append

"=== Cleanup finished $(Get-Date) ===" | Out-File $log -Append
