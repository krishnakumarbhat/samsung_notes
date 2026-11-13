# Windows VM and Samsung Notes guest

Samsung Notes sync is a Windows Store app and its sync path checks the OEM
identity. The workspace runs a Windows 11 guest under QEMU/KVM with Samsung
SMBIOS strings, exposes SSH on `127.0.0.1:2222`, and keeps the guest databases
strictly read-only from the host.

> This VM is for **exporting your own notes**. It is not a bypass for Samsung
> account security. You must sign in, accept Samsung's terms yourself, and
> comply with the service terms. The automation never accepts consent dialogs.

## Files

| Path | Purpose |
|---|---|
| `vm/notes.qcow2` | Windows system disk (34 GB; not in Git) |
| `vm/OVMF_VARS.fd` | UEFI variable store (contains boot entries) |
| `vm/notes-boot.iso` | Windows installer ISO, used only when `.installed` is absent |
| `vm/.installed` | Marker that the guest is provisioned |
| `run-notes.sh` | Headless start/stop/status/VNC helper |
| `build-vm.sh` | Create the disk + OVMF vars and link the installer ISO |
| `guestsetup/` | PowerShell/Python helpers copied into the guest at sync time |

## Rebuild the VM

```bash
export WINDOWS_ISO="$HOME/Downloads/Win11_24H2_English_x64.iso"
bash build-vm.sh --iso "$WINDOWS_ISO" --size 80G

bash run-notes.sh start
bash run-notes.sh vnc      # connect a VNC client to 127.0.0.1:5901
```

The first boot starts the Windows installer. Complete the installation manually.

### Guest checklist

1. Create the local user **`notes`** (the scripts assume `C:/Users/notes`).
2. Install **OpenSSH Server**:
   ```powershell
   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
   Set-Service sshd -StartupType Automatic
   Start-Service sshd
   ```
3. Confirm `ssh notes@127.0.0.1 -p 2222` works from the host.
4. Install **Samsung Notes** from the Microsoft Store and sign in.
5. Open Samsung Notes, review and accept Terms/Privacy manually.
6. Keep the session unlocked; disable sleep, hibernation and Fast Startup.
7. Install Python at `C:\py\python.exe` (the inventory helper calls it). The
   bundled `guestsetup/pyembed.zip`, `get-pip.py` and `py.bat` are the original
   bootstrap helpers.
8. Install the WebView2 runtime if Samsung Notes requires it
   (`guestsetup/wv2.exe`).
9. Verify the tray app starts at login and the note list renders.
10. From the host, run:
    ```bash
    bash guestsetup/sync_now.sh
    touch vm/.installed
    ```

`sync_now.sh` uploads `click_sync.ps1`, `pre_sync.ps1`,
`wait_sync_quiescence.py` and the scheduled-task XML, clicks **Sync now** in the
interactive session, then waits for `IsDirty=0` and WDoc quiescence. It uses
`SAMSUNG_NOTES_PASSWORD` from the environment or
`02_extracted_notes_pdf/.env`; the value is passed through `SSHPASS`, never
`argv`.

## QEMU profile

`run-notes.sh` uses:

- KVM, q35, 2 vCPU, 6 GB RAM by default;
- UEFI (OVMF 4 MB code + a per-VM vars file);
- Samsung SMBIOS type 1/2 strings;
- AHCI disk with discard;
- user-mode networking with `hostfwd tcp:127.0.0.1:2222-:22`;
- QXL/SPICE plus VNC on `127.0.0.1:5901`;
- QEMU monitor and QMP Unix sockets;
- USB tablet for absolute pointer input.

Guardrails: the script refuses to boot with less than **15 GiB** free or when the
project exceeds **55 GB**, and `stop` only requests ACPI powerdown. It never
force-kills QEMU.

## Sync quiescence

The guest helper checks, in order:

1. an active interactive session exists (not a service session);
2. the Samsung Notes tray/UI process is alive;
3. the **Sync now** button was clicked successfully;
4. `Storage.sqlite` reports `IsDirty=0` through a read-only connection;
5. the WDoc files stop changing for a stable interval.

If Samsung Notes shows a Terms/Privacy review, automation stops without making a
choice. Accept it in the VM, then re-run.

## Security

- The VM disk contains an authenticated Samsung session; keep it on an encrypted
  volume and never commit it.
- Port 2222 is bound to loopback only. Do not expose it.
- The guest user password is stored in an untracked `.env` with mode 0600.
- The host never writes to `Storage.sqlite` or the WDoc store.
- `run-notes.sh` validates the PID against the specific disk path before
  signalling anything.

## Troubleshooting

See [`docs/TROUBLESHOOTING.md`](../docs/TROUBLESHOOTING.md) for SSH, KVM
permissions, sync stalls and disk-guard failures.