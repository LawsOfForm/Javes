# OpenCode — PDF Q&A with AppHub

AI-powered PDF reader running in Docker, using the Uni Greifswald AppHub API.

---

## Project Structure

```
OpenCode/
├── Dockerfile
├── main.py
├── opencode.json    ← provider config (placeholder key only — never real)
├── pyproject.toml
├── README.md
└── uv.lock
```

---

## Prerequisites

- Docker installed and running
- Python 3.12+ with [uv](https://github.com/astral-sh/uv)
- An AppHub API key stored in a JSON file, e.g.:

```json
{ "API_Apphub": "your-api-key-here" }
```

or

```json
{ "API_key": "your-api-key-here" }
```

Both field names are auto-detected.

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

> **Rotate reminder:** The key previously hardcoded in `opencode.json` has
> been removed. If you used the old version, revoke that key in the AppHub
> dashboard and generate a new one.

> **Permission check:** if the file is group- or world-readable, the code will
> warn you. Fix with:
> ```bash
> chmod 600 /home/niemannf/Documents/Linux/AI_API_Key/API.json
> ```
---

## First-time Setup

### 1. Clone and enter the project

```bash
git clone https://github.com/LawsOfForm/Javes.git
cd Javes
```

### 2. Pin Python and install dependencies

```bash
uv python pin 3.12
uv sync
```

### 3. Build the Docker image

```bash
docker build --no-cache -t opencode-app .
```

---

## Running

### Recommended — mount the key file (Option 2)

```bash
docker run --rm -it \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  -v /home/niemannf/Documents/Konzepte/TDCS_Flow:/pdfs:ro \
  --mount type=bind,source=/home/niemannf/Documents/Linux/AI_API_Key/API.json,target=/run/secrets/apphub_key.json,readonly \
  opencode-app
```

The key file is mounted read-only at `/run/secrets/` — it never appears in
`docker inspect`, shell history, or the image layers.

| Argument | Purpose |
|----------|---------|
| `-v .../Paper_zu_lesen:/pdfs:ro` | PDF folder, read-only inside the container |
| `--mount ...apphub_key.json,readonly` | API key file, read-only inside the container |
| `--read-only` | Container root filesystem is read-only |
| `--cap-drop=ALL` | Drop all Linux capabilities |
| `--security-opt=no-new-privileges` | Prevent privilege escalation |

> **Note:** Docker cannot mount files with non-ASCII characters in the path
> (e.g. `ü` in `Bürokratie`). Copy the key file to an ASCII path first:
>
> ```bash
> # One-time copy if path has non-ASCII chars:
>    "/home/niemannf/Documents/Linux/AI_API_Key/API.json"
> chmod 600 /home/niemannf/Documents/Linux/AI_API_Key/API.json
> ```

---

## Security hardening summary

| Concern | Mitigation |
|---------|------------|
| API key in git history | Removed from `opencode.json`; loaded at runtime only |
| API key in image layers | Never set via `ENV` or `ARG` in Dockerfile |
| API key in `docker inspect` | Key file is mounted read-only; never passed as env var |
| Key file permissions | `load_api_key()` warns if group/world readable |
| Container runs as root | No — runs as uid 10001 (unprivileged) |
| Container can write to host | No — `/pdfs` is `:ro`, root filesystem is `--read-only` |
| Privilege escalation | `--cap-drop=ALL` + `--security-opt=no-new-privileges` |

For even stronger isolation, run under
[gVisor](https://gvisor.dev/) (`--runtime=runsc`).

---

## Usage

```
=== PDF Q&A ===

  [0] paper1.pdf
  [1] paper2.pdf

Select a PDF by number: 0

Reading: paper1.pdf …
Extracted 12400 characters.

Your question (or 'quit'): What is the main contribution of this paper?

Thinking…

Answer: …
```

Type `quit`, `exit`, or `q` to exit.

---

## Rebuilding after code changes

```bash
docker build --no-cache -t opencode-app .
```

## Adding Python dependencies

```bash
uv add <package>
docker build --no-cache -t opencode-app .
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `No interpreter found for Python 3.7` | `uv python pin 3.12 && uv sync` |
| `exec format error` on buildx | `rm ~/.docker/cli-plugins/docker-buildx`, use plain `docker build` |
| `No PDFs found in /pdfs` | Check the `-v` mount path is correct |
| `No API key found` | Check that your key file exists, is valid JSON, and contains one of `API_key` / `API_Apphub` / `api_key` / `key` |
| `Key file has permissions 644` | `chmod 600 /path/to/key.json` |
| `IsADirectoryError` on API.json | Docker can't mount files with non-ASCII paths — use the ASCII copy at `/home/niemannf/Documents/Linux/AI_API_Key/API.json` |
| Container exits immediately | Debug with `docker run --rm -it --entrypoint bash opencode-app` |

---

## .gitignore

Add this to your `.gitignore` to prevent accidental key commits:

```
# Never commit API keys
*.key
*api*.json
!opencode.json
```
