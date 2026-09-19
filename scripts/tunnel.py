"""Start cloudflared, capture the URL it mints, and write it into .env.

trycloudflare hands out a NEW hostname every time cloudflared starts, so the
PUBLIC_API_URL in .env goes stale the moment the tunnel restarts -- and n8n
Cloud then calls a dead hostname and gets a 502. Doing this by hand is the
single most repeatable way to break the demo ten minutes before it starts.

    python scripts/tunnel.py

Leave it running. It waits for the API on 127.0.0.1:PORT first, prints the
public URL, rewrites PUBLIC_API_URL in .env, and then just relays cloudflared's
output until you Ctrl-C it.

Needs cloudflared on PATH:  winget install --id Cloudflare.cloudflared
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def wait_for_api(seconds: int = 20) -> bool:
    import httpx
    url = f"http://127.0.0.1:{config.PORT}/api/health"
    for _ in range(seconds):
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return True
        except Exception:      # noqa: BLE001
            pass
        time.sleep(1)
    return False


def write_env(url: str) -> None:
    """Replace PUBLIC_API_URL in place, preserving every other line."""
    env = ROOT / ".env"
    if not env.exists():
        print(f"  !! {env} does not exist -- set PUBLIC_API_URL={url} yourself")
        return
    lines = env.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("PUBLIC_API_URL="):
            out.append(f"PUBLIC_API_URL={url}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"PUBLIC_API_URL={url}")
    env.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"  .env updated: PUBLIC_API_URL={url}")


def main() -> int:
    exe = shutil.which("cloudflared")
    if not exe:
        common = Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
        if common.exists():
            exe = str(common)
    if not exe:
        print("cloudflared is not on PATH.\n"
              "  winget install --id Cloudflare.cloudflared\n"
              "then open a new terminal.")
        return 1

    if not wait_for_api():
        print(f"\nNothing is answering on http://127.0.0.1:{config.PORT}.\n"
              f"Start the API first (run.bat in another terminal), then rerun this.\n"
              f"Starting the tunnel anyway would just give n8n a 502.")
        return 1
    print(f"  api up on 127.0.0.1:{config.PORT}")

    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{config.PORT}",
         "--http-host-header", config.TUNNEL_HOST_HEADER],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace")

    seen = False
    try:
        for raw in proc.stdout:            # type: ignore[union-attr]
            line = raw.rstrip()
            match = URL_RE.search(line)
            if match and not seen:
                seen = True
                url = match.group(0)
                print(f"\n  public url: {url}")
                write_env(url)
                print("\n  next, in another terminal:\n"
                      "    python scripts/check_integrations.py n8n\n"
                      "    python scripts/build_n8n_workflows.py --push --activate\n"
                      "  leave THIS window open for the whole demo.\n")
            else:
                print(line)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
