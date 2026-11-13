$ErrorActionPreference = 'Stop'

$taskRoot = 'C:\Users\notes'
$appFamily = 'SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy'
$trayName = 'SamsungNotesSysTray.exe'
$uiName = 'SamsungNotes.exe'

function Get-SessionProcess([string]$name, [int]$sessionId) {
    Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -eq $sessionId } |
        Select-Object -First 1
}

function Wait-ForSessionProcess([string]$name, [int]$sessionId, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    do {
        $found = Get-SessionProcess $name $sessionId
        if ($found) { return $found }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    return $null
}

try {
    $activeSessionId = $null
    foreach ($line in (& quser 2>$null)) {
        if ($line -match '^\s*>?\s*notes\s+\S+\s+(\d+)\s+Active\b') {
            $activeSessionId = [int]$Matches[1]
            break
        }
    }
    if ($null -eq $activeSessionId) {
        throw 'No active interactive notes session; refusing Session 0 UI automation.'
    }
    Write-Output "Interactive notes session ID=$activeSessionId"

    $tray = Get-SessionProcess $trayName $activeSessionId
    if (-not $tray) {
        Write-Output 'Tray not yet running; allowing normal app startup for 60 seconds.'
        $tray = Wait-ForSessionProcess $trayName $activeSessionId 60
    }
    if (-not $tray) {
        $app = Get-AppxPackage -Name 'SAMSUNGELECTRONICSCoLtd.SamsungNotes' |
            Select-Object -First 1
        if (-not $app) { throw 'Samsung Notes AppX package is not registered.' }
        $manifest = Join-Path $app.InstallLocation 'AppxManifest.xml'
        if (-not (Test-Path -LiteralPath $manifest)) {
            throw "Samsung Notes AppX manifest is missing: $manifest"
        }
        Write-Output 'Re-registering the Samsung Notes manifest to restore its tray service.'
        Add-AppxPackage -Register $manifest -ForceApplicationShutdown
        $tray = Wait-ForSessionProcess $trayName $activeSessionId 60
    }
    if (-not $tray) { throw 'SamsungNotesSysTray.exe did not start after recovery.' }
    Write-Output "Tray ready in session $activeSessionId (PID $($tray.ProcessId))."

    $ui = Get-SessionProcess $uiName $activeSessionId
    if (-not $ui) {
        $launchTask = 'SamsungNotesLaunchOnce'
        $launchXml = Join-Path $taskRoot 'launchnotes_task.xml'
        Register-ScheduledTask -TaskName $launchTask -Xml (Get-Content -LiteralPath $launchXml -Raw) -Force | Out-Null
        try {
            Start-ScheduledTask -TaskName $launchTask
            $ui = Wait-ForSessionProcess $uiName $activeSessionId 60
        } finally {
            Unregister-ScheduledTask -TaskName $launchTask -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
    if (-not $ui) { throw 'Samsung Notes UI did not start in the interactive session.' }
    Write-Output "Samsung Notes UI ready in session $activeSessionId (PID $($ui.ProcessId))."

    $quiescence = Join-Path $taskRoot 'wait_sync_quiescence.py'
    Write-Output 'Checking for pre-existing local note changes before requesting sync.'
    & 'C:\py\python.exe' $quiescence --timeout 300 --interval 10 --stable 3
    if ($LASTEXITCODE -ne 0) {
        throw 'Local notes were not clean and settled before sync; refusing to trigger a cloud operation.'
    }

    $clickResult = Join-Path $taskRoot 'click_result.txt'
    Remove-Item -LiteralPath $clickResult -Force -ErrorAction SilentlyContinue
    $clickTaskXml = Join-Path $taskRoot 'clicksync_task.xml'
    Register-ScheduledTask -TaskName 'ClickSync' -Xml (Get-Content -LiteralPath $clickTaskXml -Raw) -Force | Out-Null
    $clickStart = Get-Date
    Start-ScheduledTask -TaskName 'ClickSync'

    $clickDeadline = (Get-Date).AddMinutes(2)
    $clickText = ''
    do {
        Start-Sleep -Seconds 2
        if (Test-Path -LiteralPath $clickResult) {
            $clickText = Get-Content -LiteralPath $clickResult -Raw
        }
        if ($clickText -match 'CONSENT_REQUIRED') {
            throw 'Samsung Notes requires manual Terms and Privacy review in the VM. No consent was selected by automation.'
        }
        if ($clickText -match 'UI_ERROR') {
            throw ($clickText -split "`r?`n" | Where-Object { $_ -match 'UI_ERROR' } | Select-Object -Last 1)
        }
    } while (((Get-Date) -lt $clickDeadline) -and
        ((-not $clickText.Contains('END ')) -or
         ((Get-Item -LiteralPath $clickResult -ErrorAction SilentlyContinue).LastWriteTime -lt $clickStart)))

    if (-not $clickText.Contains('END ') -or
        $clickText -match '(?i)SamsungNotes not running|window not found|SyncNowButton not found' -or
        $clickText -notmatch 'Settings (activated with UI Automation|single mouse click sent)' -or
        $clickText -notmatch 'Sync now (activated with UI Automation|single mouse click sent)') {
        throw "Interactive Sync now click failed. Result file: $clickResult"
    }
    Write-Output 'Sync now click completed in the interactive session.'
    Get-Content -LiteralPath $clickResult | ForEach-Object { Write-Output $_ }

    & 'C:\py\python.exe' $quiescence --timeout 900 --interval 10 --stable 3
    if ($LASTEXITCODE -ne 0) { throw "Read-only sync quiescence check failed with code $LASTEXITCODE." }
} catch {
    Write-Error $_
    exit 1
}
