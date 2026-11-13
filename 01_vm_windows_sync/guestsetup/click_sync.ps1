# Trigger Sync now only after Samsung Notes is in a usable interactive state.
# Consent prompts are reported, never accepted by automation.
$ErrorActionPreference = 'Stop'
$out = 'C:\Users\notes\click_result.txt'

function Write-ClickResult([string]$message) {
    $message | Out-File -LiteralPath $out -Append -Encoding utf8
}

function Find-NotesWindow([int]$processId) {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $processId
    )
    return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}

function Find-Controls($window) {
    return $window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
}

function Find-DesktopControls {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    return $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
}

function Find-SettingsButton($controls) {
    foreach ($element in $controls) {
        if ($element.Current.AutomationId -eq 'Settings' -and
            $element.Current.ControlType.ProgrammaticName -eq 'ControlType.Button') {
            return $element
        }
    }
    return $null
}

function Find-SyncButton($controls) {
    foreach ($element in $controls) {
        $current = $element.Current
        if ($current.ControlType.ProgrammaticName -eq 'ControlType.Button' -and
            ($current.AutomationId -eq 'SyncNowButton' -or $current.Name -match '^\s*Sync now\s*$')) {
            return $element
        }
    }
    return $null
}

function Test-ConsentPrompt($controls) {
    foreach ($element in $controls) {
        if ($element.Current.Name -match '(?i)^\s*For your review\s*$|I agree to the Samsung Terms|I agree to the Privacy Policy') {
            return $true
        }
    }
    return $false
}

function Click-Element($element, [string]$label) {
    try {
        $invoke = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
        Write-ClickResult "$label activated with UI Automation."
        return
    } catch {
        Write-ClickResult "$label UI Automation invoke unavailable; using a single mouse click."
    }

    $rectangle = $element.Current.BoundingRectangle
    if ($rectangle.IsEmpty -or $element.Current.IsOffscreen -or -not $element.Current.IsEnabled) {
        throw "$label is not visible and enabled."
    }
    $x = [int](($rectangle.Left + $rectangle.Right) / 2)
    $y = [int](($rectangle.Top + $rectangle.Bottom) / 2)
    [void][MC]::SetCursorPos($x, $y)
    Start-Sleep -Milliseconds 150
    [MC]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 80
    [MC]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)
    Write-ClickResult "$label single mouse click sent."
}

"START $(Get-Date -Format 'HH:mm:ss')" | Out-File -LiteralPath $out -Encoding utf8
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class MC {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, IntPtr extra);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@

    $process = Get-Process SamsungNotes -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $process) {
        Write-ClickResult 'UI_ERROR: SamsungNotes is not running in the interactive session.'
        exit 1
    }
    $window = Find-NotesWindow $process.Id
    if (-not $window) {
        Write-ClickResult 'UI_ERROR: Samsung Notes window was not found by UI Automation.'
        exit 1
    }
    $handle = [IntPtr]$window.Current.NativeWindowHandle
    if ($handle -ne [IntPtr]::Zero) {
        [void][MC]::ShowWindow($handle, 9)
        [void][MC]::SetForegroundWindow($handle)
    }
    try { $window.SetFocus() } catch { }

    $controls = Find-Controls $window
    if (Test-ConsentPrompt (Find-DesktopControls)) {
        Write-ClickResult 'CONSENT_REQUIRED: Samsung Notes is displaying its Terms and Privacy review. No consent choice was made; review and accept manually in the VM before retrying.'
        exit 20
    }

    $settings = Find-SettingsButton $controls
    if (-not $settings) {
        Write-ClickResult 'UI_ERROR: Settings button was not found.'
        exit 1
    }
    if (-not $settings.Current.IsEnabled) {
        Write-ClickResult 'UI_ERROR: Settings is disabled; refusing to click while the app is blocked or busy.'
        exit 1
    }

    Click-Element $settings 'Settings'
    $syncButton = $null
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $window = Find-NotesWindow $process.Id
        if ($window) {
            $controls = Find-Controls $window
            if (Test-ConsentPrompt (Find-DesktopControls)) {
                Write-ClickResult 'CONSENT_REQUIRED: Samsung Notes is displaying its Terms and Privacy review. No consent choice was made; review and accept manually in the VM before retrying.'
                exit 20
            }
            $syncButton = Find-SyncButton $controls
            if (-not $syncButton) {
                $syncButton = Find-SyncButton (Find-DesktopControls)
            }
        }
    } while (-not $syncButton -and (Get-Date) -lt $deadline)

    if (-not $syncButton) {
        Write-ClickResult 'UI_ERROR: Sync now button was not found after opening Settings.'
        exit 1
    }
    if (-not $syncButton.Current.IsEnabled) {
        Write-ClickResult 'UI_ERROR: Sync now is disabled; refusing an ineffective click.'
        exit 1
    }

    Click-Element $syncButton 'Sync now'
    "END $(Get-Date -Format 'HH:mm:ss')" | Out-File -LiteralPath $out -Append -Encoding utf8
} catch {
    Write-ClickResult "UI_ERROR: $($_.Exception.GetType().Name)"
    exit 1
}
