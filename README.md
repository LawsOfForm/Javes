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

> **The key that was previously committed in `opencode.json` has been removed.**
> If you used the old version, **rotate your key now** in the AppHub dashboard.

**Never put a real API key in any file that is committed to git** —
`opencode.json`, `Dockerfile`, `docker-compose.yml`, etc.

`load_api_key()` in `main.py` checks three sources in order, stopping at the
first match:

| Priority | Method | When to use |
|----------|--------|-------------|
| 1 | `APPHUB_API_KEY=<value>` env var | Quick local tests only — visible in `docker inspect` and shell history |
| 2 | `APPHUB_KEY_FILE=/path/to/key.json` env var | **Recommended** — JSON file mounted read-only into the container |
| 3 | `/run/secrets/apphub_key.json` (default path) | Docker Swarm / Compose `secrets:` block |

The loader recognises these field names inside the JSON: `API_key`,
`API_Apphub`, `api_key`, `key`.

> **Permission check:** `load_api_key()` warns if the key file is
> group-readable or world-readable. Fix with `chmod 600 /path/to/key.json`.

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
  -v /home/spencer/Dokumente/Paper_zu_lesen:/pdfs:ro \
  --mount type=bind,source=/home/spencer/api_greifswald.json,target=/run/secrets/apphub_key.json,readonly \
  -e APPHUB_KEY_FILE=/run/secrets/apphub_key.json \
  opencode-app
```

The key file is mounted read-only at `/run/secrets/` — it never appears in
`docker inspect`, shell history, or the image layers.

### Alternative — inline key (Option 1, local dev only)

```bash
docker run --rm -it \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  -v /home/spencer/Dokumente/Paper_zu_lesen:/pdfs:ro \
  -e APPHUB_API_KEY="$(python3 -c "import json; print(json.load(open('/home/spencer/api_greifswald.json'))['API_Apphub'])")" \
  opencode-app
```

> The `$(python3 ...)` sub-shell extracts the key on the **host** — the JSON
> file is never mounted. But the key appears in `docker inspect` output.

| Argument | Purpose |
|----------|---------|
| `-v .../Paper_zu_lesen:/pdfs:ro` | PDF folder, read-only inside the container |
| `--mount ...apphub_key.json,readonly` | API key file, read-only inside the container |
| `-e APPHUB_KEY_FILE=...` | Tells `main.py` where to find the key JSON |
| `--read-only` | Container root filesystem is read-only |
| `--cap-drop=ALL` | Drop all Linux capabilities |
| `--security-opt=no-new-privileges` | Prevent privilege escalation |

> **Note:** Docker cannot mount files with non-ASCII characters in the path
> (e.g. `ü` in `Bürokratie`). Copy the key file to an ASCII path first:
>
> ```bash
> cp "/home/spencer/Dokumente/Bürokratie/API_Greifswald/API.json" \
>    "/home/spencer/api_greifswald.json"
> chmod 600 /home/spencer/api_greifswald.json
> ```

---

## Security hardening summary

| Concern | Mitigation |
|---------|------------|
| API key in git history | Removed from `opencode.json`; loaded at runtime only |
| API key in image layers | Never set via `ENV` or `ARG` in Dockerfile |
| API key in `docker inspect` | Use `--mount` + `APPHUB_KEY_FILE` instead of `-e APPHUB_API_KEY` |
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
| `IsADirectoryError` on API.json | Docker can't mount files with non-ASCII paths — use the ASCII copy at `~/api_greifswald.json` |
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
