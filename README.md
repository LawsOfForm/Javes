# BEEHub Transform Agent

A Dockerised agent that takes a raw input folder (CSVs, paradigm notes, paradigm
scripts) and produces a [BEEHub](https://github.com/memoslap/BEEHub)-compliant
project folder (BIDS-style TSVs, description JSON, sidecars, participants.tsv).

It is deliberately **not** a fully autonomous code-writing agent. The LLM is
used as a *field-mapper and content drafter*; all file writes are performed by
deterministic Python with schema validation against BEEHub's rules.

---

## Why this design (security review of the original idea)

The original plan was: "let the agent read the input folder, write transformation
code, and run that code to produce the BEEHub output." That pattern is unsafe
in a way that's easy to miss. The risks, ranked:

1. **Prompt injection via input data.** A CSV header, a markdown README, or a
   filename in the input directory can contain instructions that the LLM will
   treat as user intent. If the LLM is allowed to generate code that the agent
   then executes, this is remote code execution. Example: a column named
   `'); import shutil; shutil.rmtree('/output'); #` becomes a payload as soon
   as the LLM tries to write parsing code around it.
2. **Hallucinated destructive operations.** Even without malicious input, LLMs
   sometimes emit `rm -rf`, overwrite the wrong path, or invent file paths.
   A code-executing agent has no recourse here.
3. **Schema violations.** BEEHub has many strict, easy-to-violate rules: the
   `task-` field must match the project name case-sensitively; `accuracy_binary`
   must be integer 0/1 (not float, not string); missing values must be
   lowercase `n/a`; project folder names must be all-caps; etc. LLMs violate
   these silently. Catching the violations needs deterministic validation.
4. **Filesystem escape.** `../`, symlinks, and absolute paths in user input
   can let writes land outside the intended output directory. Every path must
   be resolved and constrained.
5. **Direct writes to a git repository.** An autonomous agent should never
   push or commit directly to BEEHub's main branch. The output here is a
   *working folder* you review before committing.
6. **Unbounded network access.** A loose container can `pip install` arbitrary
   packages or exfiltrate data.

### How this design addresses each

| Risk | Mitigation in this repo |
|------|-------------------------|
| Prompt injection → RCE | LLM is given JSON-only output tasks; outputs are parsed as data, never executed. There is no `exec`, no `eval`, no `subprocess` of LLM-generated strings. |
| Hallucinated destructive ops | Only `pandas.to_csv`, `Path.write_text`, and `mkdir` are used; all paths go through `safe_path()`. |
| Schema violations | Every output file is checked by `validate_project_dir()` against BEEHub's rules. Apply exits with code 1 if validation fails. |
| Filesystem escape | `safe_path()` resolves paths, refuses anything outside the root, refuses symlinks. |
| Direct git writes | Output is a plain folder. You commit it yourself, ideally on a feature branch with a PR. |
| Unbounded network | Docker `--network` is set to allow only the AppHub endpoint; see below. |

The agent also defaults to **dry-run**: nothing is written unless you pass
`--no-dry-run`. The first run produces a plan you can read.

---

## Architecture

```
            ┌────────────────────────────────────────────────┐
            │ Host: /home/niemannf/raw_data/   /home/niemannf/beehub_workdir/  │
            └────────────────┬───────────────────────────────┘
                  -v :ro     │       -v rw
                             ▼
    ┌──────────────────────────────────────────────────────┐
    │ Container (uid 10001, no shell, egress allowlisted)  │
    │                                                      │
    │  /input  (read-only)        /output (read-write)     │
    │     │                            ▲                   │
    │     ▼                            │                   │
    │  scan_input  ─► categorise ─► build_plan ─► apply    │
    │                                  │                   │
    │                                  ▼                   │
    │                    ┌──────────────────────────┐      │
    │                    │ LLM (field-mapping only) │      │
    │                    │  - column → BIDS column  │      │
    │                    │  - notes → description   │      │
    │                    │  Output: JSON only       │      │
    │                    └──────────────────────────┘      │
    │                                  │                   │
    │                                  ▼                   │
    │                    deterministic Python writes       │
    │                    + validate_project_dir()          │
    └──────────────────────────────────────────────────────┘
```

The model never sees credentials beyond the API key, and the key is
loaded at runtime from a mounted JSON file or environment variable —
never baked into the image.

---

## Folder convention

The agent expects raw CSVs whose filenames or paths contain `sub-NNN` and
optionally `ses-N`. Anything else in the input folder is categorised:

| Input file type | What the agent does |
|-----------------|---------------------|
| `*.csv` with `sub-NNN` in path | Mapped column-by-column → split into `*_RT_beh.tsv`, `*_ACC_beh.tsv`, `*_ACCBIN_beh.tsv` |
| `*.md` / `*.txt` paradigm notes | First one is sent to the LLM to draft `MYPROJECT_description.json` |
| `bibliography*.json` | Validated as JSON, copied verbatim |
| `*.py` paradigm scripts, Presentation files | Currently passed through to `paradigm/`; you can extend `apply_plan` for stricter handling |

The agent never executes scripts it finds in the input directory.

---

## API key handling

Your AppHub key lives in this file on your machine:
```
/home/niemannf/Documents/Linux/AI_API_Key/API.json
```
```json
{ "API_Apphub": "your-api-key-here" }
```

That file is mounted **read-only** into the container at a fixed internal path
every time you run Docker. The code reads it from there. That's the whole story.

**Never put the real key in `opencode.json` or the `Dockerfile`** — both are
committed to git and would leak it.

> **Permission check:** if the file is group- or world-readable, the code will
> warn you. Fix with:
> ```bash
> chmod 600 /home/niemannf/Documents/Linux/AI_API_Key/API.json
> ```
---

## First-time Setup

### 1. Install uv (if not already installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Pin Python and sync dependencies

```bash
cd /path/to/beehub-agent/
uv python pin 3.12
uv sync
```

`uv sync` creates a `.venv/` folder and installs all dependencies from
`pyproject.toml` into it automatically.

> **Do you need to activate the virtual environment?**
>
> **No — not for Docker.** The Dockerfile builds its own isolated Python
> environment (`/venv`) inside the image during `docker build`. The `.venv/`
> folder on your host is only needed if you want to run or edit `main.py`
> *directly* on your machine without Docker (e.g. for development or testing).
>
> In normal use the workflow is:
> ```
> uv sync          ← sets up deps on your host (one-time, optional for Docker)
> docker build     ← builds a self-contained image with its own /venv inside
> docker run       ← runs that image; your host .venv is not involved at all
> ```
> If you only ever use Docker you can skip `uv sync` entirely. It's useful to
> have it set up so your editor (VSCode, PyCharm) can find the packages for
> autocomplete and linting.

### 3. Build the Docker image

```bash
docker build --no-cache -t beehub-agent .
```

That's everything needed before running.

---

## Running

### 1. Plan (no writes anywhere)

**Recommended — mount the key file directly:**

```bash
docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  -v /home/niemannf/raw_data:/input:ro \
  -v /home/niemannf/beehub_workdir:/output \
  --mount type=bind,source=/home/niemannf/Documents/Linux/AI_API_Key/API.json,target=/run/secrets/apphub_key.json,readonly \
  -e BEEHUB_PROJECT=MYPROJECT \
  beehub-agent plan
```

The key file never leaves `/run/secrets/` inside the container and is mounted
read-only, so neither the agent nor any code it calls can modify or copy it.


This prints a JSON plan: which subjects/sessions were found, which file is
the description source, which CSVs feed each session. Read it before applying.

### 2. Apply (dry-run first by default)

```bash
docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  -v /home/niemannf/raw_data:/input:ro \
  -v /home/niemannf/beehub_workdir:/output \
  --mount type=bind,source=/home/niemannf/Documents/Linux/AI_API_Key/API.json,target=/run/secrets/apphub_key.json,readonly \
  -e BEEHUB_PROJECT=MYPROJECT \
  beehub-agent apply
```

You'll see the file tree it *would* produce. Re-run with `--no-dry-run` to
actually write:

```bash
docker run ... beehub-agent apply --no-dry-run
```

### 3. Validate

After writing, the agent automatically runs validation. You can also re-run it:

```bash
docker run --rm \
  --cap-drop=ALL --security-opt=no-new-privileges \
  -v /home/niemannf/beehub_workdir:/output \
  --mount type=bind,source=/home/niemannf/Documents/Linux/AI_API_Key/API.json,target=/run/secrets/apphub_key.json,readonly \
  -e BEEHUB_PROJECT=MYPROJECT \
  beehub-agent validate
```

Exit codes: `0` = OK, `1` = validation problems, `2` = project folder missing.

### 4. Commit to BEEHub yourself

Copy `/home/niemannf/beehub_workdir/MYPROJECT/` into a working clone of the BEEHub
repository, on a feature branch, then open a PR. **The agent should not push
to git.**

```bash
cd ~/repos/BEEHub
git checkout -b add-myproject
cp -r ~/beehub_workdir/MYPROJECT Projects/
git add Projects/MYPROJECT
git diff --stat   # review before commit
git commit -m "Add MYPROJECT paradigm"
git push -u origin add-myproject
```

---

## Recommended hardening flags (Docker)

The `docker run` examples above include the minimum set. Recommended additions:

```bash
--network=host                  # or a custom network with egress only to apphubai
--read-only                     # root filesystem is read-only
--tmpfs /tmp:rw,size=64m        # writable tmpfs for pandas scratch
--cap-drop=ALL                  # drop all Linux capabilities
--security-opt=no-new-privileges
--pids-limit=128                # cap process count
--memory=2g --cpus=2            # bound resources
-u 10001:10001                  # already the default user, but be explicit
```

For network egress control, the cleanest pattern is a user-defined bridge
network plus a small DNS allowlist (e.g. via `dnsmasq` or `unbound`) so the
container can only resolve `apphubai.wolke.uni-greifswald.de`.

If you want stronger isolation than Docker, run under
[gVisor](https://gvisor.dev/) (`--runtime=runsc`) or
[Kata Containers](https://katacontainers.io/) — both work without code changes.

---

## What the LLM is allowed to do, and what it isn't

| Allowed | Not allowed |
|---------|-------------|
| Emit a JSON column-name map | Write Python code that the agent executes |
| Draft fields for the description JSON | Choose file paths |
| Suggest a `cognitive_domain` value from controlled vocab | Decide which files exist |
| Read a snippet (max 8000 chars) of a notes file | Read the whole input tree |

If the LLM returns malformed JSON, the agent logs a warning and falls back
to identity-mapping / a minimal description stub. The transform still completes
deterministically.

---

## Limits & known gaps

- **No PII scrubbing.** If your raw CSVs contain identifiable data (names,
  birth dates, free-text comments), strip them before mounting `/input`.
  The agent does not detect this.
- **No git operations.** Intentional. The boundary between "machine writes
  files" and "human commits to a shared repository" is enforced by you.
- **One project at a time.** `BEEHUB_PROJECT` is a single value.
- **Column mapping is one-shot per session.** If your CSVs within a single
  session have different schemas, split them first.
- **`acq-` is hard-coded to `1`.** Extend `apply_plan` if you need multiple
  acquisition runs per session.
- **The validator covers the most common BEEHub rules but is not exhaustive.**
  Always inspect the generated `*_overview.html` after running BEEHub's
  analysis scripts on the output.

---

## Security hardening summary

| Concern | Mitigation |
|---------|------------|
| API key in git history | Removed from `opencode.json`; loaded at runtime only |
| API key in image layers | Never set via `ENV` or `ARG` in Dockerfile |
| API key in `docker inspect` | Key file is mounted read-only; never passed as env var |
| Key file permissions | `load_api_key()` warns if group/world readable |
| Container runs as root | No — runs as uid 10001 (unprivileged) |
| Container can write to host input | No — `/input` is `:ro` |
| Container can write outside output | No — `safe_path()` blocks `../`, symlinks, absolute paths |
| Privilege escalation | `--cap-drop=ALL` + `--security-opt=no-new-privileges` |
| LLM executes generated code | No — LLM outputs JSON data only, never code |
| Prompt injection from input files | LLM output is parsed as JSON, validated against allowlist, never `exec`'d |
| Broken project silently committed | `validate_project_dir()` catches schema violations; dry-run is default |
| Agent pushes to git | No — you review output and commit manually on a feature branch |

---

## .gitignore

Add this to prevent accidental key commits:

```
# Never commit API keys
*.key
*api*.json
!opencode.json
```

---

## Files

```
beehub-agent/
├── Dockerfile          # multi-stage, non-root, minimal runtime
├── main.py             # the agent itself
├── opencode.json       # provider config (placeholder key only — never real)
├── pyproject.toml      # pinned deps: openai, pandas
└── README.md           # this file
```

---

## Threat model summary

**In scope:** prompt injection via input data, path traversal, hallucinated
writes, schema violations producing silently-broken BEEHub projects, accidental
commits of broken projects to a shared repo.

**Out of scope:** a malicious operator with root on the host (they own
everything anyway), a compromised AppHub endpoint serving malicious LLM
responses (mitigated only partially by JSON-only parsing — assume eventual
adversarial output and keep dry-run as the default), and side-channel attacks
on the model.

If you change the agent to let the LLM choose paths, execute generated code,
or run shell commands, the threat model above no longer holds. Don't.
