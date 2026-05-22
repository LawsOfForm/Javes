"""
OpenCode — PDF Q&A
==================
AI-powered PDF reader running in Docker, using the Uni Greifswald AppHub API.

The key is read from /home/niemannf/Documents/Linux/AI_API_Key/API.json,
mounted read-only into the container. The key is never logged or written to disk.
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

_KEY_FILE = Path("/run/secrets/apphub_key.json")
_KEY_FIELD = "API_Apphub"


def load_api_key() -> str:
    """
    Read the AppHub API key from the mounted JSON file.

    The file must be bind-mounted read-only at /run/secrets/apphub_key.json:
      --mount type=bind,source=/home/niemannf/Documents/Linux/AI_API_Key/API.json,
              target=/run/secrets/apphub_key.json,readonly

    The key is never logged, never written to disk.
    """
    if not _KEY_FILE.exists():
        raise SystemExit(
            f"Key file not found at {_KEY_FILE}.\n"
            "Mount your API.json with:\n"
            "  --mount type=bind,"
            "source=/home/niemannf/Documents/Linux/AI_API_Key/API.json,"
            "target=/run/secrets/apphub_key.json,readonly"
        )

    mode = _KEY_FILE.stat().st_mode & 0o777
    if mode & 0o044:
        log.warning(
            "Key file %s has permissions %o — fix with: chmod 600 %s",
            _KEY_FILE, mode,
            "/home/niemannf/Documents/Linux/AI_API_Key/API.json",
        )

    try:
        data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"Cannot read key file {_KEY_FILE}: {exc}") from exc

    value = data.get(_KEY_FIELD, "").strip()
    if not value:
        raise SystemExit(
            f"Key file {_KEY_FILE} has no field '{_KEY_FIELD}' or it is empty."
        )
    return value


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
