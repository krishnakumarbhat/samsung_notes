# keep_alive.py v3 — Aggressive Samsung Notes sync keep-alive
# Uses multiple techniques to prevent UWP throttling
import time
import ctypes
import subprocess
import sys
import os

# Windows API constants
SW_RESTORE = 9
SW_SHOW = 5
HWND_TOP = 0
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001

PROCESS_SET_INFORMATION = 0x0100
PROCESS_SET_QUOTA = 0x0100
ProcessPowerThrottling = 4
PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040
ES_DISPLAY_REQUIRED = 0x00000002

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]

def prevent_sleep():
    kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED | ES_DISPLAY_REQUIRED
    )

def allow_sleep():
    kernel32.SetThreadExecutionState(ES_CONTINUOUS)

def find_pid(name):
    result = subprocess.run(
        ['tasklist', '/FI', f'IMAGENAME eq {name}', '/FO', 'CSV', '/NH'],
        capture_output=True, text=True
    )
    for line in result.stdout.strip().split('\n'):
        if line:
            parts = line.strip('"').split('","')
            if len(parts) >= 2 and parts[0] == name:
                return int(parts[1])
    return None

def find_samsung_notes_hwnd():
    EnumWindows = user32.EnumWindows
    GetWindowText = user32.GetWindowTextW
    IsWindowVisible = user32.IsWindowVisible
    hwnd_found = []

    def enum_callback(hwnd, lParam):
        if IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                GetWindowText(hwnd, buff, length + 1)
                title = buff.value
                if 'Samsung Notes' in title:
                    hwnd_found.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
    EnumWindows(WNDENUMPROC(enum_callback), 0)
    return hwnd_found[0] if hwnd_found else None

def disable_power_throttling(pid):
    state = PROCESS_POWER_THROTTLING_STATE()
    state.Version = 1
    state.ControlMask = 0
    state.StateMask = 0

    hProcess = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
    if hProcess:
        kernel32.SetProcessInformation(
            hProcess, ProcessPowerThrottling,
            ctypes.byref(state), ctypes.sizeof(state)
        )
        kernel32.CloseHandle(hProcess)
        return True
    return False

def set_realtime_priority(pid):
    """Set to REAL-TIME priority (256) - highest possible"""
    subprocess.run(
        ['wmic', 'process', 'where', f'ProcessId={pid}', 'CALL', 'setpriority', '256'],
        capture_output=True, text=True
    )

def set_high_io_priority(pid):
    """Use PowerShell to set high I/O priority"""
    ps_cmd = f"""
    $proc = Get-Process -Id {pid} -ErrorAction SilentlyContinue
    if ($proc) {{
        $proc.PriorityClass = 'RealTime'
        $proc.ProcessorAffinity = (Get-WmiObject Win32_Processor).ProcessorId
    }}
    """
    subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], capture_output=True)

def bring_to_foreground_aggressive(hwnd):
    """Aggressively bring window to foreground"""
    # Show window
    user32.ShowWindow(hwnd, SW_RESTORE)
    # Set as topmost temporarily
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    # Bring to foreground
    user32.SetForegroundWindow(hwnd)
    # Allow foreground
    user32.AllowSetForegroundWindow(0xFFFFFFFF)
    user32.SetForegroundWindow(hwnd)
    # Remove topmost
    time.sleep(0.1)
    user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)

def main():
    print("Samsung Notes aggressive sync keep-alive v3")
    print("Prevents UWP throttling + keeps system awake")
    print("Press Ctrl+C to stop")
    sys.stdout.flush()

    prevent_sleep()
    print("System sleep prevention: ON")
    sys.stdout.flush()

    cycle = 0
    pid = None
    hwnd = None
    try:
        while True:
            new_pid = find_pid('SamsungNotes.exe')
            if new_pid:
                if new_pid != pid:
                    pid = new_pid
                    print(f"[{cycle}] Samsung Notes PID={pid}")
                    sys.stdout.flush()
                    disable_power_throttling(pid)
                    set_realtime_priority(pid)
                    hwnd = find_samsung_notes_hwnd()
                    if hwnd:
                        print(f"[{cycle}] Window found hwnd={hwnd}")
                    sys.stdout.flush()

                if hwnd is None:
                    hwnd = find_samsung_notes_hwnd()

                if hwnd:
                    bring_to_foreground_aggressive(hwnd)

                # Re-apply priority every cycle
                if cycle % 6 == 0:
                    set_realtime_priority(pid)
                    prevent_sleep()
            else:
                if pid:
                    print(f"[{cycle}] Samsung Notes stopped")
                    sys.stdout.flush()
                    pid = None
                    hwnd = None

            prevent_sleep()
            cycle += 1
            if cycle % 30 == 0:
                print(f"[{cycle}] Still alive - Samsung Notes PID={pid} hwnd={hwnd}")
                sys.stdout.flush()
            time.sleep(5)
    except KeyboardInterrupt:
        print("Stopping keep-alive")
    finally:
        allow_sleep()

if __name__ == '__main__':
    main()
