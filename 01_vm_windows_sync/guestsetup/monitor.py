# monitor.py — Passive Samsung Notes sync diagnostic monitor
# Watches %LOCALAPPDATA%\...\SamsungNotes\LocalState\Logs for new etl files
# and auto-traces them, printing any quota / sync-error lines.
#
# Run on the VM (in background):
#   C:\py\python.exe -u C:\Users\notes\monitor.py > C:\Users\notes\monitor_out.txt 2>&1
#
# 2026-08-29 — see the VM synchronization runbook for host setup details.
import time, os, subprocess, json

OUT_DIR  = r'C:\Users\notes\auto_logs'
ETL_DIR  = (r'C:\Users\notes\AppData\Local\Packages'
             r'\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy'
             r'\LocalState\Logs')
WDOC     = (r'C:\Users\notes\AppData\Local\Packages'
             r'\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy'
             r'\LocalState\wdoc')
KEEP_KW  = ('Quota', '429', 'onFailed', 'SyncException',
            'Remaing', 'startSync', 'IsLidDown', 'Too Many')

os.makedirs(OUT_DIR, exist_ok=True)
seen = set(os.listdir(ETL_DIR))
print('monitoring %d known etls in %s' % (len(seen), ETL_DIR), flush=True)

# last wdoc mtime
def wdoc_mtime():
    try:
        return os.path.getmtime(WDOC)
    except OSError:
        return 0
last_wdoc = wdoc_mtime()
last_wdoc_report = last_wdoc
print('wdoc mtime baseline: %s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_wdoc)), flush=True)

for _ in range(180):  # ~30 min
    time.sleep(10)
    try:
        cur = set(os.listdir(ETL_DIR))
    except OSError:
        continue
    new = cur - seen
    for fn in sorted(new):
        full = os.path.join(ETL_DIR, fn)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size < 5000 or 'SystemAgent' in fn:
            continue
        out_base = os.path.join(OUT_DIR, fn.replace('.etl', ''))
        out_xml  = out_base + '.xml'
        subprocess.run(['cmd', '/c', 'tracerpt', full,
                        '-o', out_base, '-of', 'XML', '-y'],
                       capture_output=True)
        seen.add(fn)
        print('NEW: %s  size=%d' % (fn, size), flush=True)
        if not os.path.exists(out_xml):
            continue
        try:
            with open(out_xml, 'r', errors='replace') as f:
                c = f.read()
            for ln in c.split('<Data Name="Log">')[1:]:
                end = ln.find('</Data>')
                msg = ln[:end] if end > 0 else ln[:300]
                if any(k in msg for k in KEEP_KW):
                    print('  >> ' + msg[:300], flush=True)
        except Exception as e:
            print('  err: ' + str(e), flush=True)
    # Check if wdoc was updated (successful download)
    cur_wdoc = wdoc_mtime()
    if cur_wdoc > last_wdoc_report:
        print('WROC_UPDATED: wdoc mtime %s (was %s) — notes downloaded!' %
              (time.strftime('%H:%M:%S', time.localtime(cur_wdoc)),
               time.strftime('%H:%M:%S', time.localtime(last_wdoc_report))),
              flush=True)
        last_wdoc_report = cur_wdoc
    last_wdoc = cur_wdoc
