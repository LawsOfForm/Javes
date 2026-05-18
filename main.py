import json
import os
from pathlib import Path
from openai import OpenAI


def load_api_key() -> str:
    key = os.environ.get("APPHUB_API_KEY")
    if not key:
        raise EnvironmentError("APPHUB_API_KEY not set")
    return key


client = OpenAI(
    api_key=load_api_key(),
    base_url="https://apphubai.wolke.uni-greifswald.de/v1"
)

PDF_DIR = Path("/pdfs")


def list_pdfs():
    pdfs = list(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print("No PDFs found in /pdfs")
        return []
    for i, p in enumerate(pdfs):
        print(f"  [{i}] {p.name}")
    return pdfs


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def ask(context: str, question: str) -> str:
    completion = client.chat.completions.create(
        model="Qwen/Qwen3-VL-30B-A3B-Instruct-FP8",
        messages=[
            {"role": "system", "content": "You are a helpful research assistant. Answer based on the document."},
            {"role": "user", "content": f"Document:\n{context[:6000]}\n\nQuestion: {question}"}
        ],
    )
    return completion.choices[0].message.content


def main():
    print("\n=== PDF Q&A with Gemma 3 ===\n")
    pdfs = list_pdfs()
    if not pdfs:
        return

    choice = input("\nSelect a PDF by number: ").strip()
    pdf = pdfs[int(choice)]
    print(f"\nReading: {pdf.name} ...")
    text = read_pdf(pdf)
    print(f"Extracted {len(text)} characters.\n")

    while True:
        question = input("Your question (or 'quit'): ").strip()
        if question.lower() in ("quit", "exit", "q"):
            break
        print("\nThinking...\n")
        print(f"Answer: {ask(text, question)}\n")


if __name__ == "__main__":
    main()
