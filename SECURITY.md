# Security policy

## What is sensitive

- **Note PDFs** under `02_extracted_notes_pdf/notes/` — personal study, health
  and career material.
- **The Windows VM disk** — contains an authenticated Samsung account session and
  the `notes` user profile.
- **`.env` files** — Gemini API keys and the guest SSH password
  (`SAMSUNG_NOTES_PASSWORD`).
- **OCR/index databases** — derived text from the notes.
- **`ai_agend_history/`** — logs and manifests that may contain titles and paths.

## Repository rules

- No `.env`, key, token, password or private key may be tracked.
  `scripts/verify_repo.py` enforces this before every push.
- No note payload, VM disk, model weight or Python environment is tracked.
- The tracked-file ceiling is 99,000,000 bytes (GitHub rejects at 100 MB).
- Never paste note text, UUIDs with titles, or credentials into a public issue.
- The GitHub repository is public. A pushed secret is compromised; rotate it
  immediately.

## Secret layout

| Secret | Preferred location | Mode |
|---|---|---|
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | `~/.config/sam_win/.env` | 0600 |
| `SAMSUNG_NOTES_PASSWORD` | `02_extracted_notes_pdf/.env` (untracked) | 0600 |
| VM disk | `01_vm_windows_sync/vm/notes.qcow2` | encrypted volume |

`src/config.py` and `src/gemini_client.py` read the secure file first and ignore
blank values, so the sanitized repository `.env` cannot mask a real key. Override
the secure path with `SAM_ENV_FILE`.

## Network boundaries

- The dashboard binds `127.0.0.1` by default. `RECALL_HOST=0.0.0.0` is an
  explicit opt-in and exposes the app to the LAN; use a firewall and trusted
  network only.
- Corpus embeddings and OCR are local. Gemini is called only for explicit
  reasoning features, with selected snippets.
- Samsung Notes production databases are opened read-only
  (`mode=ro`, `PRAGMA query_only=ON`).
- The guest SSH connection uses password authentication on a host-forwarded
  loopback port (`127.0.0.1:2222`). Do not expose port 2222 publicly.

## Destructive operations

The pipeline does not:

- delete Samsung Notes records or cloud objects;
- overwrite original PDFs with OCR output;
- enable lossy image downsampling or JBIG2 substitution automatically;
- replace or delete cloud notes to meet quota;
- accept Samsung Terms/Privacy on the user's behalf;
- force-kill QEMU.

Repairs preserve the previous export in `preserved_originals/` and record its
hash. `--repair` flags are opt-in and verified.

## Reporting

This is a personal repository. If you find a leaked credential or a note payload
in Git history, open a private security advisory or contact the owner directly;
do not paste the secret in a public issue.