"""Config loader. Purpose: read .env/env into constants. Inputs: none. Outputs: API key, models, dirs."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT.parents[1]
DATA = ROOT / "data"
VAULT = DATA / "vault"
SVG_DIR = VAULT / "svg"
RECALL_DB = DATA / "recall.sqlite"
LEGACY_MEMORY_DB = DATA / "memory.db"
CHROMA_DIR = DATA / "chroma"


def _env_files() -> tuple[Path, ...]:
    """Return secret/config files in precedence order.

    The workspace lives on NTFS, where chmod is a no-op and every file is
    world-readable.  Real credentials therefore live in a mode-0600 file on the
    home filesystem.  The repository ``.env`` keeps only non-secret defaults and
    remains a fallback for portable checkouts.
    """
    secure = Path(os.environ.get("SAM_ENV_FILE", Path.home() / ".config/sam_win/.env"))
    return (secure, PROJECT_ROOT / ".env", ROOT / ".env")


def _load_env() -> None:
    """Load KEY=VALUE pairs without letting blank values mask later files."""
    # Secure settings win over repository defaults; process environment wins over both.
    for env_file in _env_files():
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                value = v.strip().strip("\"'")
                if value:
                    os.environ.setdefault(k.strip(), value)


_load_env()


def _first_key() -> str:
    """Pick the first usable API key (env GOOGLE_API_KEY or GEMINI_API_KEY from .env)."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        v = os.environ.get(name, "")
        if v and not v.startswith("your_"):
            return v
    return ""


GEMINI_KEY = _first_key()
# ponytail: fallback chain newest→cheapest; extend list if Google renames models
MODEL_CHAIN: list[str] = []
for m in (
    os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
):
    if m not in MODEL_CHAIN:
        MODEL_CHAIN.append(m)
EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
PROMPT_TOKEN_BUDGET = min(1900, max(256, int(os.environ.get("GEMINI_PROMPT_TOKEN_BUDGET", "1800"))))
DAILY_REVIEW_CAP = int(os.environ.get("DAILY_REVIEW_CAP", "15"))
NOTES_DIR = Path(os.environ["NOTES_DIR"]) if os.environ.get("NOTES_DIR") else None
if NOTES_DIR is None:
    NOTES_DIR = PROJECT_ROOT / "02_extracted_notes_pdf" / "notes"
elif not NOTES_DIR.is_absolute():
    NOTES_DIR = (PROJECT_ROOT / NOTES_DIR).resolve()
