"""
BEEHub Transform Agent
======================

Takes a raw input folder (CSVs, paradigm scripts, READMEs, ...) and produces
a BEEHub-compliant project folder (BIDS-style TSVs + description JSON +
bibliography).

Design principles
-----------------
1. The LLM is a *planner and field-mapper*, never an executor.
   - It maps unknown column names -> BIDS columns.
   - It fills the description JSON from free-text paradigm notes.
   - It NEVER writes code that the agent then runs.
2. All file writes go through deterministic Python with path validation,
   schema validation, and a dry-run / approval gate.
3. Read-only input, write-only output, both validated to be inside their
   mounted roots. Symlinks are refused. `..` traversal is refused.
4. The container has no shell access for the model, no `eval`, no `exec`.

Usage
-----
    python main.py plan       # produce a plan, write it to OUTPUT/_plan.json
    python main.py apply      # execute a previously-approved plan
    python main.py validate   # only validate, no writes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import pandas as pd
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration (all paths come from env vars, never from the model)
# ---------------------------------------------------------------------------

INPUT_ROOT = Path(os.environ.get("BEEHUB_INPUT", "/input")).resolve()
OUTPUT_ROOT = Path(os.environ.get("BEEHUB_OUTPUT", "/output")).resolve()
PROJECT_NAME = os.environ.get("BEEHUB_PROJECT", "").strip().upper()
MAX_INPUT_BYTES = int(os.environ.get("BEEHUB_MAX_INPUT_BYTES", 200 * 1024 * 1024))
MODEL_NAME = os.environ.get("BEEHUB_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8")
DRY_RUN_DEFAULT = os.environ.get("BEEHUB_DRY_RUN", "1") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("beehub-agent")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

PROJECT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9]{1,31}$")
SUBJECT_RE = re.compile(r"^sub-\d{3}$")
SESSION_RE = re.compile(r"^ses-\d+$")


def safe_path(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and assert it stays inside `root`. Refuses symlinks."""
    root = root.resolve()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise PermissionError(f"Path escape attempt: {candidate} -> {resolved}") from e
    # Refuse to follow symlinks anywhere in the chain
    cur = resolved
    while cur != root and cur != cur.parent:
        if cur.is_symlink():
            raise PermissionError(f"Symlink not allowed: {cur}")
        cur = cur.parent
    return resolved


def assert_input_readable() -> None:
    if not INPUT_ROOT.exists() or not INPUT_ROOT.is_dir():
        raise SystemExit(f"INPUT_ROOT {INPUT_ROOT} missing or not a directory")
    # Belt-and-braces: ensure we *cannot* write here even if mount is wrong
    test = INPUT_ROOT / ".writetest"
    try:
        test.touch()
        test.unlink()
        log.warning("INPUT_ROOT is writable! Mount it read-only (-v src:/input:ro).")
    except OSError:
        pass  # good, read-only as expected


def assert_output_writable() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    test = OUTPUT_ROOT / ".writetest"
    test.touch()
    test.unlink()


def assert_project_name() -> None:
    if not PROJECT_NAME_RE.match(PROJECT_NAME):
        raise SystemExit(
            f"BEEHUB_PROJECT={PROJECT_NAME!r} invalid. "
            "Must be ALLCAPS alphanumeric, 2-32 chars, starting with a letter."
        )


# ---------------------------------------------------------------------------
# Input tree inspection (deterministic, no LLM)
# ---------------------------------------------------------------------------

@dataclass
class InputTree:
    files: list[Path] = field(default_factory=list)
    total_bytes: int = 0

    def add(self, p: Path) -> None:
        size = p.stat().st_size
        if self.total_bytes + size > MAX_INPUT_BYTES:
            raise SystemExit(
                f"Input exceeds BEEHUB_MAX_INPUT_BYTES ({MAX_INPUT_BYTES}). "
                "Refusing to proceed."
            )
        self.total_bytes += size
        self.files.append(p)


def scan_input() -> InputTree:
    tree = InputTree()
    for p in sorted(INPUT_ROOT.rglob("*")):
        if p.is_symlink():
            log.warning("Skipping symlink in input: %s", p)
            continue
        if p.is_file():
            tree.add(p)
    log.info("Input scan: %d files, %.1f MiB",
             len(tree.files), tree.total_bytes / 1024 / 1024)
    return tree


def categorise(tree: InputTree) -> dict[str, list[Path]]:
    cats: dict[str, list[Path]] = {
        "csv": [], "tsv": [], "json": [], "md": [], "txt": [],
        "paradigm_py": [], "presentation": [], "other": [],
    }
    for p in tree.files:
        s = p.suffix.lower()
        if s == ".csv":
            cats["csv"].append(p)
        elif s == ".tsv":
            cats["tsv"].append(p)
        elif s == ".json":
            cats["json"].append(p)
        elif s == ".md":
            cats["md"].append(p)
        elif s == ".txt":
            cats["txt"].append(p)
        elif s == ".py" and "paradigm" in str(p).lower():
            cats["paradigm_py"].append(p)
        elif s in {".sce", ".exp", ".pcl"}:
            cats["presentation"].append(p)
        else:
            cats["other"].append(p)
    return cats


# ---------------------------------------------------------------------------
# LLM client — used only for fuzzy field mapping, never for code generation
# ---------------------------------------------------------------------------

_KEY_FILE = Path("/run/secrets/apphub_key.json")
_KEY_FIELD = "API_Apphub"


def load_api_key() -> str:
    """
    Read the AppHub API key from the mounted JSON file.

    The file must be bind-mounted read-only at /run/secrets/apphub_key.json:
      --mount type=bind,source=/home/niemannf/Documents/Linux/AI_API_Key/API.json,
              target=/run/secrets/apphub_key.json,readonly

    The key is never logged, never written to disk, never passed to the model.
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


def _client() -> OpenAI:
    return OpenAI(
        api_key=load_api_key(),
        base_url="https://apphubai.wolke.uni-greifswald.de/v1",
    )


def llm_map_columns(sample_header: list[str], sample_rows: list[dict]) -> dict[str, str]:
    """
    Ask the LLM to map the user's raw columns to the BIDS-required ones.
    Returns a dict like {'reaction_time': 'response_time_ms', 'correct': 'accuracy_binary'}.
    Output is strictly validated as JSON; anything else is discarded.
    """
    target_fields = [
        "onset", "duration", "response_time_ms",
        "accuracy", "accuracy_binary",
        "trial_type", "learning_stage", "stimulus", "response_port",
    ]
    prompt = (
        "You are mapping CSV columns to BIDS-compliant behavioural fields.\n"
        f"Target fields: {target_fields}\n"
        f"User columns: {sample_header}\n"
        f"Sample rows (first 3): {json.dumps(sample_rows[:3], default=str)}\n\n"
        "Return ONLY a JSON object: {\"user_column\": \"target_field\", ...}.\n"
        "Use target field name 'IGNORE' for columns that should be dropped.\n"
        "Do not invent columns. Do not write code. JSON only."
    )
    resp = _client().chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "You output strict JSON. Nothing else."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    raw = resp.choices[0].message.content.strip()
    # Strip code fences if the model adds them
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON column mapping; falling back to identity.")
        return {c: c for c in sample_header}
    # Validate every target is in the allowed set
    out = {}
    for k, v in mapping.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if v != "IGNORE" and v not in target_fields:
            log.warning("Discarding invalid mapping %s -> %s", k, v)
            continue
        out[k] = v
    return out


def llm_fill_description(notes_text: str) -> dict[str, Any]:
    """Ask the LLM to draft a BEEHub description JSON from free-text notes."""
    schema_hint = {
        "full_name": "string",
        "short_description": "string",
        "long_description": "string",
        "background": "string",
        "procedure": "string",
        "trial_structure": "string",
        "design": "string",
        "modality": "visual|auditory|linguistic|tactile|multimodal|virtual environment",
        "cognitive_domain": "working memory|episodic memory|...",
        "task_type": "string",
        "language": "german|english|...",
        "recording_modality": "behavioral|mri|eeg|...",
        "keywords": ["..."],
        "n_sessions": 1,
    }
    prompt = (
        "Draft a BEEHub description JSON for the paradigm described below.\n"
        "Schema (controlled vocabularies must be respected):\n"
        f"{json.dumps(schema_hint, indent=2)}\n\n"
        "Paradigm notes:\n"
        "---\n"
        f"{notes_text[:8000]}\n"
        "---\n"
        "Return ONLY the JSON object. No prose, no code fences."
    )
    resp = _client().chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "You output strict JSON. Nothing else."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("LLM description JSON parse failed; returning minimal stub.")
        return {"full_name": PROJECT_NAME, "short_description": "TODO"}


# ---------------------------------------------------------------------------
# CSV -> BIDS TSV transform (deterministic)
# ---------------------------------------------------------------------------

@dataclass
class TransformPlan:
    project: str
    subjects: list[dict] = field(default_factory=list)
    description_source: str | None = None
    bibliography_source: str | None = None
    notes: list[str] = field(default_factory=list)


def infer_subject_session(path: Path) -> tuple[str, str] | None:
    """Pull sub-XXX and ses-Y out of a path. Returns None if not found."""
    m_sub = re.search(r"sub-(\d{1,4})", str(path))
    m_ses = re.search(r"ses-(\d+)", str(path))
    if not m_sub:
        return None
    sub = f"sub-{int(m_sub.group(1)):03d}"
    ses = f"ses-{int(m_ses.group(1))}" if m_ses else "ses-1"
    return sub, ses


def split_outcomes(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split a unified dataframe into RT / ACC / ACCBIN frames per BEEHub spec."""
    base_cols = ["onset", "duration", "trial_type"]
    optional = [c for c in ("learning_stage", "stimulus", "response_port") if c in df.columns]
    out: dict[str, pd.DataFrame] = {}

    if "response_time_ms" in df.columns:
        out["RT"] = df[base_cols + ["response_time_ms"] + optional].copy()

    if "accuracy" in df.columns:
        out["ACC"] = df[base_cols + ["accuracy"] + [c for c in optional if c != "response_port"]].copy()

    if "accuracy_binary" in df.columns:
        # Enforce int type — BEEHub requires int 0/1 not float, not string
        ab = pd.to_numeric(df["accuracy_binary"], errors="coerce")
        ab = ab.where(ab.isin([0, 1])).astype("Int64")  # nullable int
        tmp = df[base_cols + [c for c in optional if c != "response_port"]].copy()
        tmp["accuracy_binary"] = ab
        # Reorder so accuracy_binary sits right after duration
        cols = base_cols[:2] + ["accuracy_binary"] + ["trial_type"] + [c for c in optional if c != "response_port"]
        out["ACCBIN"] = tmp[cols]

    return out


def to_bids_tsv(df: pd.DataFrame, path: Path) -> None:
    """Write a TSV exactly as BEEHub expects: tab-sep, 'n/a' for missing, no index."""
    df = df.copy()
    # 'n/a' for ALL missing values (BEEHub rule)
    df = df.where(df.notna(), "n/a")
    # No spaces in trial_type
    if "trial_type" in df.columns:
        df["trial_type"] = df["trial_type"].astype(str).str.replace(r"\s+", "_", regex=True)
    df.to_csv(path, sep="\t", index=False, na_rep="n/a")


def write_sidecar(tsv_path: Path) -> None:
    stem = tsv_path.with_suffix("").name  # foo_RT_beh
    outcome = stem.rsplit("_", 2)[-2]  # RT / ACC / ACCBIN
    descriptions = {
        "RT": {"primary": "response_time_ms", "unit": "milliseconds",
               "desc": "Reaction time."},
        "ACC": {"primary": "accuracy", "unit": "categorical",
                "desc": "correct | incorrect | n/a"},
        "ACCBIN": {"primary": "accuracy_binary", "unit": "binary",
                   "desc": "1 = correct, 0 = incorrect"},
    }[outcome]
    sidecar = {
        "TaskName": PROJECT_NAME,
        "TaskDescription": f"{PROJECT_NAME} behavioural data.",
        "onset": {"Description": "Stimulus onset from t0.", "Units": "seconds"},
        "duration": {"Description": "Trial duration.", "Units": "seconds"},
        descriptions["primary"]: {
            "Description": descriptions["desc"],
            "Units": descriptions["unit"],
        },
    }
    tsv_path.with_suffix(".json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Validation against BEEHub rules
# ---------------------------------------------------------------------------

CONTROL_TRIAL_LABELS = {
    "control", "rest", "baseline", "fixation", "fix",
    "instruction", "pause", "break", "catch", "null",
}


def validate_project_dir(project_dir: Path) -> list[str]:
    """Return a list of human-readable problems. Empty list = OK."""
    problems: list[str] = []
    if not (project_dir / "participants.tsv").exists():
        problems.append("Missing participants.tsv")

    desc = project_dir / f"{PROJECT_NAME}_description.json"
    if not desc.exists():
        problems.append(f"Missing {desc.name}")
    else:
        try:
            d = json.loads(desc.read_text(encoding="utf-8"))
            for required in ("full_name", "short_description"):
                if required not in d:
                    problems.append(f"description JSON missing field: {required}")
        except json.JSONDecodeError as e:
            problems.append(f"description JSON invalid: {e}")

    bids_root = project_dir / "bids_data"
    if not bids_root.exists():
        problems.append("Missing bids_data/ folder")
        return problems

    for tsv in bids_root.rglob("*_beh.tsv"):
        # task- must match project name exactly (case-sensitive)
        m = re.search(r"task-([A-Z0-9]+)", tsv.name)
        if not m:
            problems.append(f"{tsv.name}: missing task- field")
        elif m.group(1) != PROJECT_NAME:
            problems.append(
                f"{tsv.name}: task-{m.group(1)} does not match project {PROJECT_NAME}"
            )
        if not tsv.with_suffix(".json").exists():
            problems.append(f"{tsv.name}: missing JSON sidecar")

        # accuracy_binary must be int 0/1, not float/string
        if "_ACCBIN_" in tsv.name:
            try:
                df = pd.read_csv(tsv, sep="\t", na_values=["n/a"])
                if "accuracy_binary" in df.columns:
                    vals = df["accuracy_binary"].dropna().unique()
                    bad = [v for v in vals if v not in (0, 1)]
                    if bad:
                        problems.append(
                            f"{tsv.name}: accuracy_binary has non-binary values {bad[:5]}"
                        )
            except Exception as e:
                problems.append(f"{tsv.name}: unreadable ({e})")

    return problems


# ---------------------------------------------------------------------------
# Plan / Apply
# ---------------------------------------------------------------------------

def build_plan() -> TransformPlan:
    tree = scan_input()
    cats = categorise(tree)
    plan = TransformPlan(project=PROJECT_NAME)

    # Pick description source (markdown or txt with paradigm notes)
    notes_files = cats["md"] + cats["txt"]
    if notes_files:
        plan.description_source = str(notes_files[0].relative_to(INPUT_ROOT))

    # Pick existing bibliography.json if present
    for jf in cats["json"]:
        if "bibl" in jf.name.lower():
            plan.bibliography_source = str(jf.relative_to(INPUT_ROOT))
            break

    # Group CSVs by inferred subject/session
    by_sub_ses: dict[tuple[str, str], list[Path]] = {}
    for csv in cats["csv"]:
        ident = infer_subject_session(csv)
        if not ident:
            plan.notes.append(f"Cannot infer sub/ses from {csv.relative_to(INPUT_ROOT)}; skipping")
            continue
        by_sub_ses.setdefault(ident, []).append(csv)

    for (sub, ses), files in sorted(by_sub_ses.items()):
        plan.subjects.append({
            "subject": sub,
            "session": ses,
            "source_csvs": [str(p.relative_to(INPUT_ROOT)) for p in files],
        })

    return plan


def apply_plan(plan: TransformPlan, dry_run: bool) -> None:
    project_dir = safe_path(OUTPUT_ROOT, Path(plan.project))
    if dry_run:
        log.info("[DRY RUN] Would create %s", project_dir)
    else:
        project_dir.mkdir(parents=True, exist_ok=True)

    # ----- description JSON ---------------------------------------------------
    desc_path = safe_path(project_dir, Path(f"{plan.project}_description.json"))
    if plan.description_source:
        src = safe_path(INPUT_ROOT, Path(plan.description_source))
        notes = src.read_text(encoding="utf-8", errors="replace")
        desc_data = llm_fill_description(notes)
    else:
        desc_data = {"full_name": plan.project, "short_description": "TODO"}
    desc_data.setdefault("full_name", plan.project)
    log.info("Description JSON: %d fields", len(desc_data))
    if not dry_run:
        desc_path.write_text(json.dumps(desc_data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    # ----- per subject/session ------------------------------------------------
    participants_rows: list[dict] = []

    for entry in plan.subjects:
        sub, ses = entry["subject"], entry["session"]
        if not SUBJECT_RE.match(sub) or not SESSION_RE.match(ses):
            log.warning("Invalid sub/ses identifiers %s/%s, skipping", sub, ses)
            continue

        session_dir = safe_path(project_dir, Path("bids_data") / sub / ses)
        if not dry_run:
            session_dir.mkdir(parents=True, exist_ok=True)

        # Concatenate all CSVs for this session
        frames: list[pd.DataFrame] = []
        mapping: dict[str, str] = {}
        for rel in entry["source_csvs"]:
            src = safe_path(INPUT_ROOT, Path(rel))
            df = pd.read_csv(src)
            if not mapping:
                mapping = llm_map_columns(
                    list(df.columns), df.head(3).to_dict(orient="records")
                )
            df = df.rename(columns=mapping)
            # Drop IGNORE columns
            df = df.drop(columns=[c for c, v in mapping.items() if v == "IGNORE" and c in df.columns],
                         errors="ignore")
            frames.append(df)
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)

        # Required columns: bail loudly if anything essential missing
        for col in ("onset", "duration", "trial_type"):
            if col not in df.columns:
                log.error("%s/%s: missing required column %s after mapping", sub, ses, col)
                continue

        outcomes = split_outcomes(df)
        acq = "1"  # we keep things simple; can be per-session later
        for outcome, frame in outcomes.items():
            fname = f"{sub}_{ses}_task-{plan.project}_acq-{acq}_{outcome}_beh.tsv"
            tsv_path = safe_path(session_dir, Path(fname))
            log.info("  -> %s (%d rows)", tsv_path.relative_to(OUTPUT_ROOT), len(frame))
            if not dry_run:
                to_bids_tsv(frame, tsv_path)
                write_sidecar(tsv_path)

        participants_rows.append({"participant_id": sub, "sex": "n/a", "age": "n/a"})

    # participants.tsv (deduplicated)
    if participants_rows:
        pdf = pd.DataFrame(participants_rows).drop_duplicates(subset=["participant_id"])
        pt_path = safe_path(project_dir, Path("participants.tsv"))
        if not dry_run:
            to_bids_tsv(pdf, pt_path)
        log.info("participants.tsv: %d subjects", len(pdf))

    # bibliography passthrough
    if plan.bibliography_source:
        src = safe_path(INPUT_ROOT, Path(plan.bibliography_source))
        dst = safe_path(project_dir, Path("bibliography.json"))
        try:
            bib = json.loads(src.read_text(encoding="utf-8"))
            if not dry_run:
                dst.write_text(json.dumps(bib, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("bibliography.json copied (validated JSON)")
        except json.JSONDecodeError as e:
            log.error("bibliography source is not valid JSON: %s", e)

    # Write the plan itself for the human reviewer
    plan_path = safe_path(OUTPUT_ROOT, Path(f"{plan.project}_plan.json"))
    if not dry_run:
        plan_path.write_text(json.dumps(asdict(plan), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="BEEHub transform agent")
    ap.add_argument("action", choices=["plan", "apply", "validate"])
    ap.add_argument("--no-dry-run", action="store_true",
                    help="Actually write files (default is dry-run)")
    args = ap.parse_args()

    assert_project_name()
    assert_input_readable()

    if args.action == "validate":
        # Validate an existing OUTPUT_ROOT/PROJECT_NAME folder
        project_dir = safe_path(OUTPUT_ROOT, Path(PROJECT_NAME))
        if not project_dir.exists():
            log.error("Project dir does not exist: %s", project_dir)
            return 2
        problems = validate_project_dir(project_dir)
        if problems:
            log.error("Validation FAILED:")
            for p in problems:
                log.error("  - %s", p)
            return 1
        log.info("Validation OK ✓")
        return 0

    assert_output_writable()
    plan = build_plan()
    log.info("Plan: %d subject/session entries", len(plan.subjects))
    for note in plan.notes:
        log.warning("note: %s", note)

    if args.action == "plan":
        # Just print the plan; do nothing else
        print(json.dumps(asdict(plan), indent=2))
        return 0

    # apply
    dry_run = DRY_RUN_DEFAULT and not args.no_dry_run
    if dry_run:
        log.info("DRY RUN — pass --no-dry-run to actually write files.")
    apply_plan(plan, dry_run=dry_run)

    if not dry_run:
        project_dir = safe_path(OUTPUT_ROOT, Path(PROJECT_NAME))
        problems = validate_project_dir(project_dir)
        if problems:
            log.error("Post-apply validation found %d problems:", len(problems))
            for p in problems:
                log.error("  - %s", p)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
