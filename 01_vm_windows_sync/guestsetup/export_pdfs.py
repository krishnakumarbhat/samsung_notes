import os, sys, time, sqlite3, subprocess
from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys

PDFROOT = r"C:\Users\notes\pdfs"
DB = os.path.expandvars(r"%LOCALAPPDATA%\Packages\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy\LocalState\Storage.sqlite")
AUMID = r"shell:AppsFolder\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy!App"
LOG = open(r"C:\Users\notes\export.log", "a", encoding="utf-8")

def log(m):
    print(m, flush=True)
    LOG.write(m + "\n"); LOG.flush()

def folder_map():
    db = sqlite3.connect(DB)
    cur = db.cursor()
    cats = {r[0]: (r[1], r[2]) for r in cur.execute(
        "SELECT UUID, ParentUUID, DisplayName FROM CategoryTreeDB WHERE IsDeleted=0")}
    def path_of(u):
        parts, seen = [], set()
        while u and u not in seen and u in cats:
            seen.add(u)
            parent, name = cats[u]
            if name and name != "Folders": parts.append(name.strip())
            u = parent
        return list(reversed(parts))
    notes = []
    for uuid, cat, title in cur.execute(
            "SELECT UUID, CategoryUUID, Title FROM NoteDB WHERE DeletedStatus=0"):
        notes.append((title.strip() or "Untitled", path_of(cat)))
    db.close()
    return notes

def launch_app():
    subprocess.Popen(["explorer.exe", AUMID])
    time.sleep(8)

def find_desktop():
    return Desktop(backend="uia")

def export_one(title, relpath):
    dest = os.path.join(PDFROOT, *relpath)
    os.makedirs(dest, exist_ok=True)
    out = os.path.join(dest, safe_name(title) + ".pdf")
    if os.path.exists(out):
        log(f"SKIP exists: {out}")
        return True
    log(f"EXPORT: {title} -> {out}")
    # Search in app
    send_keys("^f")
    time.sleep(2)
    send_keys("{^}a{DELETE}")
    send_keys(title, with_spaces=True, pause=0.05)
    time.sleep(3)
    send_keys("{ENTER}")
    time.sleep(4)
    # Open the note (first result)
    d = find_desktop()
    win = d.window(title_re="Samsung Notes", control_type="Window")
    win.set_focus()
    # try double click first list item
    try:
        lst = win.descendants(control_type="ListItem")
        if not lst:
            lst = win.descendants(control_type="DataItem")
        lst[0].double_input_double_click()
        time.sleep(6)
    except Exception as e:
        log(f"list click failed: {e}")
    # Export: Ctrl+P? No -> use '...' menu via Alt? Try keyboard: Ctrl+Shift+S? Unknown.
    # Use the app menu: click the 'More options' via keyboard: Alt+Space? UWP: use Menu button search
    try:
        more = win.descendants(title="More options", control_type="Button")
        if more: more[0].click_input()
        time.sleep(2)
        dsk = find_desktop()
        saveitem = dsk.window(title_re="Samsung Notes", control_type="Window").descendants(name="Save as file", control_type="MenuItem") or \
                   dsk.window(title_re="Samsung Notes", control_type="Window").descendants(name="Save as file")
        (saveitem[0] if isinstance(saveitem, list) else saveitem).click_input()
        time.sleep(3)
    except Exception as e:
        log(f"menu nav failed: {e}")
    # Format dialog: select PDF then Save
    try:
        dsk = find_desktop()
        dlg = dsk.window(title_re="Samsung Notes|Save", control_type="Window")
        pdfbtn = dlg.descendants(name="PDF")
        if pdfbtn: pdfbtn[0].click_input()
        time.sleep(1)
        savebtn = dlg.descendants(name="Save")
        if savebtn: savebtn[0].click_input()
        time.sleep(2)
    except Exception as e:
        log(f"format dlg failed: {e}")
    # Windows save dialog: type full path
    try:
        dsk = find_desktop()
        sdlg = dsk.top_window()
        sdlg.set_focus()
        send_keys("{^}a")
        send_keys(out, with_spaces=True, pause=0.03)
        send_keys("{ENTER}")
        time.sleep(5)
        # overwrite confirm?
        try:
            dsk.window(title_re="Confirm|Replace", control_type="Window").descendants(name="Yes")[0].click_input()
        except Exception:
            pass
    except Exception as e:
        log(f"save dlg failed: {e}")
    time.sleep(3)
    # go back to all notes
    send_keys("{ESC}{ESC}")
    time.sleep(2)
    ok = os.path.exists(out)
    log(f"RESULT {'OK' if ok else 'MISS'}: {out}")
    return ok

def safe_name(t):
    for ch in '\\/:*?"<>|.':
        t = t.replace(ch, "_")
    return t.strip() or "Untitled"

def main():
    notes = folder_map()
    log(f"NOTES: {notes}")
    launch_app()
    results = {}
    for title, rel in notes:
        try:
            results[title] = export_one(title, rel)
        except Exception as e:
            log(f"FAIL {title}: {e}")
            results[title] = False
    log(f"DONE: {results}")

if __name__ == "__main__":
    main()
