import os
from functools import cache
from pathlib import Path

import yaml
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent


def load():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["companies"] = yaml.safe_load((ROOT / "companies.yaml").read_text())
    cfg["env"] = load_env()
    for track in cfg["tracks"].values():
        track["resume"] = str(Path(track["resume"]).expanduser())
    return cfg


def load_env():
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    # Real environment variables win over .env.
    for key in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "APOLLO_API_KEY", "NOTION_TOKEN"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


@cache
def resume_text(path):
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
