
# install uv on github directory (or another environment)

- if you downloaded main.py from github and redirect the next step to this directory main.py will be changed. you might therefore save the main.py under a different name

curl -LsSf https://astral.sh/uv/install.sh | sh

# Initialize UV and install the openai dependency
mkdir path_to_github_dir
cd path_to_github_dir
uv init
uv add openai

Paste the code below into the main.py



# OpenCode — PDF Q&A with Gemma 3

AI-powered PDF reader running in Docker, using the Uni Greifswald AppHub API.

---

## Project Structure

```
OpenCode/
├── Dockerfile
├── main.py
├── opencode.json
├── pyproject.toml
├── README.md
└── uv.lock
```

---

## Prerequisites

- Docker installed and running
- Python 3.12+ with `uv`
- API key stored at:

```
/path/to/your/API/Key/API.json
```

The key file must look like this:

```json
{
  "API_Apphub": "your-api-key-here"
}
```

---

## First-time Setup

### 1. Enter the project

```bash
cd /home/spencer/Dokumente/Eigene_Projekte/AppHub_based_code/OpenCode/
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

The API key is read from the JSON file on the host and passed into the container as an environment variable — no file mounting inside the container needed.

```bash
docker run --rm -it \
  -v /home/spencer/Dokumente/Paper_zu_lesen:/pdfs:ro \
  -e APPHUB_API_KEY="$(python3 -c "import json; print(json.load(open('/home/spencer/api_greifswald.json'))['API_Apphub'])")" \
  opencode-app
```

> **Note:** Docker cannot mount files with non-ASCII characters in the path (e.g. `ü` in `Bürokratie`).
> Use an ASCII copy of the key file instead:
>
> ```bash
> # One-time copy
> cp "/home/spencer/Dokumente/Bürokratie/API_Greifswald/API.json" \
>    "/home/spencer/api_greifswald.json"
> ```

| Argument | Purpose |
|---|---|
| `-v Paper_zu_lesen:/pdfs:ro` | PDF folder, read-only inside the container |
| `-e APPHUB_API_KEY=...` | API key injected as env var at runtime, never baked into the image |

---

## How the API key is loaded

```python
import os

def load_api_key() -> str:
    key = os.environ.get("APPHUB_API_KEY")
    if not key:
        raise EnvironmentError("APPHUB_API_KEY not set")
    return key
```

---

## Usage

```
=== PDF Q&A with Gemma 3 ===

  [0] paper1.pdf
  [1] paper2.pdf

Select a PDF by number: 0

Reading: paper1.pdf ...
Extracted 12400 characters.

Your question (or 'quit'): What is the main contribution of this paper?

Thinking...

Answer: ...
```

Type `quit`, `exit`, or `q` to exit.

---

## Rebuilding after code changes

```bash
cd /home/spencer/Dokumente/Eigene_Projekte/AppHub_based_code/OpenCode/
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
|---|---|
| `No interpreter found for Python 3.7` | `uv python pin 3.12 && uv sync` |
| `exec format error` on buildx | `rm ~/.docker/cli-plugins/docker-buildx`, use plain `docker build` |
| `No PDFs found in /pdfs` | Check the `-v` mount path is correct |
| `EnvironmentError: APPHUB_API_KEY not set` | Verify the `$(python3 -c ...)` command prints a value on your host |
| `IsADirectoryError` on API.json | Docker can't mount files with non-ASCII paths — use the ASCII copy at `~/api_greifswald.json` |
| Container exits immediately | Debug with `docker run --rm -it --entrypoint bash opencode-app` |
