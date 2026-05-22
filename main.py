"""
OpenCode — PDF Q&A
==================
AI-powered PDF reader running in Docker, using the Uni Greifswald AppHub API.

API key priority (first match wins):
  1. APPHUB_API_KEY      env var  (plain string — visible in `docker inspect`)
  2. APPHUB_KEY_FILE     env var  pointing to a JSON file with field
                                  "API_key", "API_Apphub", "api_key", or "key"
  3. /run/secrets/apphub_key.json (Docker bind-mount secret — recommended)

The key is never logged, never written to disk.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path

from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("opencode")

# ---------------------------------------------------------------------------
# API key loading
# ---------------------------------------------------------------------------

_KEY_FIELD_NAMES = ("API_key", "API_Apphub", "api_key", "key")


def _load_key_from_json(path: Path) -> str:
    """Read one of the recognised field names from a JSON key file."""
    if not path.exists():
        raise FileNotFoundError(f"Key file not found: {path}")
    if not path.is_file():
        raise ValueError(f"Key path is not a file: {path}")

    # Warn if the file is group- or world-readable
    mode = path.stat().st_mode & 0o777
    if mode & 0o044:
        log.warning(
            "Key file %s has permissions %o — consider `chmod 600 %s`",
            path, mode, path,
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"Cannot read API key file {path}: {exc}") from exc

    for field in _KEY_FIELD_NAMES:
        if field in data and isinstance(data[field], str):
            value = data[field].strip()
            if value:
                log.info("API key loaded from %s (field: %s)", path, field)
                return value

    raise SystemExit(
        f"Key file {path} found but none of the expected fields "
        f"({', '.join(_KEY_FIELD_NAMES)}) contained a non-empty string."
    )


def load_api_key() -> str:
    """
    Load the API key with priority:
      1. APPHUB_API_KEY   plain env var
      2. APPHUB_KEY_FILE  path to a JSON key file
      3. /run/secrets/apphub_key.json  (Docker bind-mount secret)
    """
    # 1. Direct env var
    key = os.environ.get("APPHUB_API_KEY", "").strip()
    if key:
        return key

    # 2 & 3. JSON file — explicit path first, then Docker secret default
    candidates: list[Path] = []
    env_path = os.environ.get("APPHUB_KEY_FILE", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("/run/secrets/apphub_key.json"))

    for path in candidates:
        if path.exists():
            return _load_key_from_json(path)

    raise SystemExit(
        "No API key found. Provide one of:\n"
        "  APPHUB_API_KEY=<key>                         (plain env var)\n"
        "  APPHUB_KEY_FILE=/path/to/key.json            (JSON file)\n"
        "  --mount .../api.json:/run/secrets/apphub_key.json,readonly"
    )


# ---------------------------------------------------------------------------
# Client and constants
# ---------------------------------------------------------------------------

PDF_DIR = Path(os.environ.get("PDF_DIR", "/pdfs"))
MODEL_NAME = os.environ.get(
    "APPHUB_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"
)
BASE_URL = "https://apphubai.wolke.uni-greifswald.de/v1"
CONTEXT_CHARS = int(os.environ.get("CONTEXT_CHARS", 6000))


def _client() -> OpenAI:
    return OpenAI(api_key=load_api_key(), base_url=BASE_URL)


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def list_pdfs() -> list[Path]:
    if not PDF_DIR.exists():
        print(f"PDF directory {PDF_DIR} does not exist.")
        return []
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {PDF_DIR}")
        return []
    for i, p in enumerate(pdfs):
        print(f"  [{i}] {p.name}")
    return pdfs


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader  # lazy import — not needed for key tests
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not text.strip():
        log.warning("%s: no extractable text (scanned PDF?)", path.name)
    return text


# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------

def ask(context: str, question: str) -> str:
    completion = _client().chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful research assistant. "
                           "Answer based on the provided document excerpt only.",
            },
            {
                "role": "user",
                "content": (
                    f"Document:\n{context[:CONTEXT_CHARS]}\n\n"
                    f"Question: {question}"
                ),
            },
        ],
    )
    return completion.choices[0].message.content


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    print("\n=== PDF Q&A ===\n")

    pdfs = list_pdfs()
    if not pdfs:
        return 1

    raw = input("\nSelect a PDF by number: ").strip()
    try:
        idx = int(raw)
        pdf = pdfs[idx]
    except (ValueError, IndexError):
        print(f"Invalid selection: {raw!r}")
        return 1

    print(f"\nReading: {pdf.name} …")
    text = read_pdf(pdf)
    print(f"Extracted {len(text)} characters.\n")

    while True:
        try:
            question = input("Your question (or 'quit'): ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if question.lower() in ("quit", "exit", "q", ""):
            break
        print("\nThinking…\n")
        answer = ask(text, question)
        print(f"Answer: {answer}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
