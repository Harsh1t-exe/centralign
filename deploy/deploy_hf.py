"""Deploy the AI Worker to a free Hugging Face Space (Docker SDK, free CPU hardware).

One-time setup:
    1. Create a free account at https://huggingface.co/join
    2. Create a token with "write" access at https://huggingface.co/settings/tokens
    3. Make the token available, either:
         PowerShell:  $env:HF_TOKEN="hf_..."      (or bash: export HF_TOKEN=hf_...)
       or log in once:  python -c "from huggingface_hub import login; login()"

Deploy (re-run any time to update):
    python deploy/deploy_hf.py
    python deploy/deploy_hf.py --space your-name/ai-worker --access-code letmein

The Gemini key is stored as a private Space secret, never in the code. Read from GEMINI_API_KEY,
or prompted for if that is unset.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "runs", "__pycache__", ".pytest_cache", ".venv", "venv", "deploy"}
SKIP_FILES = {".env"}

SPACE_CARD = """---
title: AI Worker
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Autonomous AI worker that does back-office tasks in a browser
---

# AI Worker (live demo)

Give it a task in plain English, for example *"Find the latest invoice from Globex, extract the amount
and due date, enter it into our AP system, and tell me once it is done."*. It then works in a real
browser inside a simulated company (Acme Corp): it reads emails and PDF invoices, follows company
policy, asks for approval when needed, recovers from errors, and has its work independently verified.

The simulated company runs privately inside this container; you can watch what the agent sees in
the "Agent's screen" panel. Model: Google Gemini (free tier), so runs take a few minutes.
{github}
"""


def files_to_upload() -> list[Path]:
    out = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if path.is_dir() or any(part in SKIP_DIRS for part in rel.parts) or rel.name in SKIP_FILES:
            continue
        if rel.as_posix() == "README.md" or path.suffix == ".pyc":
            continue
        out.append(path)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--space", help="Space id, e.g. your-name/ai-worker (default: <your user>/ai-worker)")
    p.add_argument("--access-code", default=None,
                   help="Optional code visitors must enter to start tasks (protects your free quota)")
    p.add_argument("--github", default="", help="GitHub repo URL to link from the Space page")
    a = p.parse_args()

    api = HfApi()
    try:
        user = api.whoami()["name"]
    except Exception:
        print("Not logged in to Hugging Face. Create a write token at huggingface.co/settings/tokens, then set "
              "HF_TOKEN=<token> (or run: python -c \"from huggingface_hub import login; login()\").")
        return 1
    space = a.space or f"{user}/ai-worker"

    key = os.environ.get("GEMINI_API_KEY") or getpass.getpass("Gemini API key (free, aistudio.google.com/apikey): ")
    if not key.strip():
        print("A Gemini API key is required for the deployed worker to think.")
        return 1
    code = a.access_code
    if code is None:
        code = input("Access code for visitors (leave empty for none): ").strip()

    print(f"Creating/updating Space {space} ...")
    api.create_repo(space, repo_type="space", space_sdk="docker", exist_ok=True)
    api.add_space_secret(space, "GEMINI_API_KEY", key.strip())
    if code:
        api.add_space_secret(space, "WORKER_ACCESS_CODE", code)
    else:
        try:
            api.delete_space_secret(space, "WORKER_ACCESS_CODE")
        except Exception:
            pass

    github = f"\nSource code: {a.github}\n" if a.github else ""
    ops = [CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=SPACE_CARD.format(github=github).encode())]
    ops += [CommitOperationAdd(path_in_repo=f.relative_to(ROOT).as_posix(), path_or_fileobj=str(f))
            for f in files_to_upload()]
    api.create_commit(space, operations=ops, commit_message="Deploy AI Worker", repo_type="space")

    url = f"https://huggingface.co/spaces/{space}"
    print(f"\nUploaded {len(ops)} files. The Space is building now (first build takes ~5-10 minutes).")
    print(f"Space page:  {url}")
    print(f"Direct app:  https://{space.replace('/', '-').replace('_', '-').lower()}.hf.space")
    if code:
        print(f"Visitors need the access code: {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
