"""Probe Cognee, Sarvam, Gemini and n8n from this machine.

Nothing here changes the demo. It calls each service once, prints exactly what
came back, and tells you what to fix. Run it before flipping any flag.

    python scripts/check_integrations.py              # all four
    python scripts/check_integrations.py cognee       # just one
    python scripts/check_integrations.py sarvam n8n

Model IDs and endpoints move. This script is how you find that out on a quiet
afternoon rather than at 2am.
"""
from __future__ import annotations

import base64
import json
import math
import struct
import sys
import wave
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

OK, BAD, SKIP = "  ok  ", " FAIL ", " skip "
problems: list[str] = []


def line(status: str, label: str, detail: str = "") -> None:
    print(f"{status} {label:<40} {detail}")


def fail(label: str, detail: str) -> None:
    line(BAD, label, detail)
    problems.append(f"{label}: {detail}")


def _tone_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A one-second tone. Enough to prove the endpoint and auth work."""
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for i in range(int(rate * seconds)):
            w.writeframes(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / rate))))
    return buf.getvalue()


# ============================================================ cognee
def check_cognee() -> None:
    print("\n--- Cognee ---")
    if not config.COGNEE_API_KEY:
        line(SKIP, "COGNEE_API_KEY", "not set in .env")
        return
    if not config.COGNEE_BASE_URL:
        fail("COGNEE_BASE_URL", "not set in .env")
        return
    line(OK, "base url", config.COGNEE_BASE_URL)
    line(OK, "tenant", config.COGNEE_TENANT_ID or "(none)")

    from backend.graph.cognee_store import CogneeClient, CogneeError
    try:
        client = CogneeClient()
    except CogneeError as exc:
        fail("client", str(exc))
        return

    try:
        datasets = client.list_datasets()
        line(OK, "GET /datasets/", f"{len(datasets)} dataset(s): "
                                   f"{', '.join(d.get('name', '?') for d in datasets) or '(none)'}")
    except Exception as exc:      # noqa: BLE001
        fail("GET /datasets/", str(exc)[:200])
        return

    try:
        ds_id = client.ensure_dataset()
        line(OK, f"dataset '{client.dataset}'", ds_id)
    except Exception as exc:      # noqa: BLE001
        fail("create dataset", str(exc)[:200])
        return

    try:
        probe = ("A food stall in a college area with mid volume saw evening sales "
                 "fall 21% below its own baseline. The merchant ran an evening offer "
                 "of Rs 10 off for three days. Evening revenue recovered 28%. "
                 "Verdict: recovered.")
        client.add_text([probe])
        line(OK, "POST /add_text", "1 probe card accepted")
    except Exception as exc:      # noqa: BLE001
        fail("POST /add_text", str(exc)[:200])
        return

    try:
        res = client.cognify(background=True, with_model=True)
        line(OK, "POST /cognify (graph_model)", json.dumps(res)[:90] if res else "queued")
    except Exception as exc:      # noqa: BLE001
        line(BAD, "POST /cognify (graph_model)", str(exc)[:160])
        problems.append("cognify with graph_model failed; retrying without it")
        try:
            res = client.cognify(background=True, with_model=False)
            line(OK, "POST /cognify (no model)", "accepted -- set GRAPH_MODEL aside")
        except Exception as exc2:      # noqa: BLE001
            fail("POST /cognify", str(exc2)[:200])
            return

    # The shared-API spec and the tenant disagree about the search_type enum,
    # so probe rather than assume -- same approach as the Sarvam model ids.
    from backend.graph.cognee_store import SEARCH_TYPE_CANDIDATES
    accepted_types = []
    for stype in SEARCH_TYPE_CANDIDATES:
        try:
            hits = client.search("evening decline what worked", search_type=stype)
            n = len(hits) if isinstance(hits, list) else len(hits or {})
            accepted_types.append(stype)
            line(OK, f"search {stype}", f"accepted ({n} item(s) back)")
        except Exception as exc:      # noqa: BLE001
            msg = str(exc)
            if "'" in msg and "Input should be" in msg:
                allowed = msg.split("Input should be", 1)[1][:300]
                line(SKIP, f"search {stype}", f"rejected; tenant allows:{allowed}")
            else:
                line(SKIP, f"search {stype}", msg[:150])
    if accepted_types:
        prefer = [t for t in ("CHUNKS", "INSIGHTS", "SUMMARIES") if t in accepted_types]
        line(OK, "recommended COGNEE_SEARCH_TYPE", prefer[0] if prefer else accepted_types[0])
    else:
        fail("search", "no search_type accepted -- see the allowed list above")

    summary = client.graph_summary()
    if summary:
        line(OK, "graph summary", json.dumps(summary)[:120])
    else:
        line(SKIP, "graph summary", "empty -- cognify may still be running")

    print("\n  note: cognify runs in the background on the tenant. If the graph "
          "summary\n  is empty, wait a minute and run this again.")


# ============================================================ sarvam
def check_sarvam() -> None:
    print("\n--- Sarvam ---")
    if not config.SARVAM_API_KEY:
        line(SKIP, "SARVAM_API_KEY", "not set in .env")
        return

    import httpx
    from backend.voice import adapter as va

    line(OK, "tts model", f"{config.SARVAM_TTS_MODEL} / speaker {config.SARVAM_SPEAKER}")

    # ---- TTS
    try:
        r = httpx.post(va.TTS_URL,
                       headers={"api-subscription-key": config.SARVAM_API_KEY},
                       json={"inputs": ["Namaste, main aapka Business Saathi hoon."],
                             "target_language_code": config.SARVAM_LANGUAGE,
                             "speaker": config.SARVAM_SPEAKER,
                             "model": config.SARVAM_TTS_MODEL},
                       timeout=20.0)
        if r.status_code < 400 and (r.json().get("audios") or []):
            audio = base64.b64decode(r.json()["audios"][0])
            out = ROOT / "data" / "sarvam_test.wav"
            out.write_bytes(audio)
            line(OK, "POST /text-to-speech", f"{len(audio):,} bytes -> {out.name} (play it)")
        else:
            fail("POST /text-to-speech", f"{r.status_code}: {r.text[:180]}")
    except Exception as exc:      # noqa: BLE001
        fail("POST /text-to-speech", str(exc)[:200])

    # ---- STT: probe the candidates and report which the account accepts
    tone = _tone_wav()
    candidates = [config.SARVAM_STT_MODEL, config.SARVAM_STT_FALLBACK,
                  "saarika:v2.5", "saarika:v2", "saarika:v1", "saaras:v4", "saaras:v2"]
    seen, accepted = set(), []
    for model in [m for m in candidates if m and not (m in seen or seen.add(m))]:
        url = va.STT_TRANSLATE_URL if "saaras" in model else va.STT_URL
        data = {"model": model}
        if "saaras" not in model:
            data["language_code"] = config.SARVAM_LANGUAGE
        try:
            r = httpx.post(url,
                           headers={"api-subscription-key": config.SARVAM_API_KEY},
                           files={"file": ("audio.wav", tone, "audio/wav")},
                           data=data, timeout=20.0)
            if r.status_code < 400:
                accepted.append(model)
                line(OK, f"stt {model}", "accepted (a tone transcribes to nothing, "
                                         "that is expected)")
            else:
                line(SKIP, f"stt {model}", f"{r.status_code}: {r.text[:110]}")
        except Exception as exc:      # noqa: BLE001
            line(SKIP, f"stt {model}", str(exc)[:110])

    if not accepted:
        fail("stt", "no model accepted -- check the dashboard for current model ids")
    else:
        transcribe_only = [m for m in accepted if "saarika" in m]
        pick = transcribe_only[0] if transcribe_only else accepted[0]
        line(OK, "recommended SARVAM_STT_MODEL", pick)
        if not transcribe_only:
            print("\n  warning: only saaras models were accepted. saaras TRANSLATES to\n"
                  "  English, so the merchant's Hinglish wording is lost before the\n"
                  "  intent router sees it. Check the dashboard for a saarika model.")


# ============================================================ model providers
# The agent needs TOOL CALLING, not just JSON, so that is what is probed here.
# A provider that returns JSON but ignores a tool definition cannot run the
# loop, and finding that out on stage is not the plan.
PROBE_TOOL = {
    "name": "get_business_health",
    "description": "Headline figures for this merchant's payments.",
    "parameters": {"type": "object",
                   "properties": {"window_days": {"type": "integer"}},
                   "required": [], "additionalProperties": False},
}
PROBE_MESSAGES = [
    {"role": "system", "content": "You are a merchant assistant. Use the tool."},
    {"role": "user", "content": "How has my shop done over the last 7 days?"},
]


def _check_provider(label: str, name: str, model: str, configured: bool) -> None:
    print(f"\n--- {label} ---")
    if not configured:
        line(SKIP, f"{label} key", "not set in .env")
        return
    from backend.reasoning import providers
    try:
        out = providers.chat(PROBE_MESSAGES, [PROBE_TOOL], timeout=20.0,
                             provider=name)
    except Exception as exc:      # noqa: BLE001
        fail(f"{label} tool calling", str(exc)[:200])
        return
    calls = out.get("calls") or []
    if calls:
        line(OK, f"{label} model {model}",
             f"called {calls[0]['name']}({json.dumps(calls[0]['arguments'])})")
    else:
        fail(f"{label} tool calling",
             f"returned text instead of a tool call: {(out.get('text') or '')[:120]}. "
             f"The agent loop needs tool calling; this model cannot drive it.")


def check_openai() -> None:
    _check_provider("OpenAI", "openai", config.OPENAI_MODEL, config.openai_ready())


def check_gemini() -> None:
    _check_provider("Gemini", "gemini", config.GEMINI_MODEL, config.gemini_ready())


def check_nvidia() -> None:
    _check_provider("NVIDIA NIM", "nvidia-nim", config.NVIDIA_MODEL,
                    config.nvidia_ready())


# ============================================================ n8n
def check_n8n() -> None:
    print("\n--- n8n ---")
    import httpx

    # Probe the local API FIRST. The tunnel has two independent failure modes
    # and the local probe is what tells them apart:
    #   API down   -> cloudflared has nothing to forward to -> Cloudflare 502
    #   API up      -> a 502/1033 means the URL in .env is from a dead
    #                  cloudflared run (trycloudflare mints a new hostname
    #                  every single start)
    local = f"http://127.0.0.1:{config.PORT}"
    api_up = False
    try:
        r = httpx.get(f"{local}/api/health", timeout=5.0)
        api_up = r.status_code == 200
        line(OK if api_up else BAD, "local API", f"{local}/api/health -> {r.status_code}")
    except Exception:      # noqa: BLE001
        fail("local API", f"nothing listening on {local} -- start it with run.bat "
                          f"in another terminal")

    if not config.PUBLIC_API_URL:
        fail("PUBLIC_API_URL", "not set -- n8n Cloud cannot reach 127.0.0.1")
    else:
        line(OK, "public tunnel", config.PUBLIC_API_URL)
        try:
            r = httpx.get(f"{config.PUBLIC_API_URL}/api/health", timeout=20.0,
                          headers={"X-Saathi-Secret": config.INTERNAL_SECRET})
            if r.status_code == 200 and r.json().get("ok"):
                line(OK, "tunnel reaches this API", "GET /api/health ok")
            elif r.status_code in (502, 503, 504, 530):
                hint = ("the API is running, so this URL belongs to a cloudflared "
                        "run that has since stopped -- restart the tunnel and put "
                        "the NEW url in .env"
                        if api_up else
                        "cloudflared is up but the API is not -- start run.bat")
                fail("tunnel reaches this API", f"{r.status_code}: {hint}")
            else:
                fail("tunnel reaches this API", f"{r.status_code}: {r.text[:120]}")
        except Exception as exc:      # noqa: BLE001
            fail("tunnel reaches this API",
                 f"{type(exc).__name__} -- cloudflared is not running, or the "
                 f"hostname in .env is dead")

    if not config.N8N_API_KEY:
        line(SKIP, "N8N_API_KEY", "not set -- import the 3 workflow JSONs by hand")
        return
    line(OK, "n8n base url", config.N8N_BASE_URL)
    try:
        r = httpx.get(f"{config.N8N_BASE_URL}/api/v1/workflows",
                      headers={"X-N8N-API-KEY": config.N8N_API_KEY}, timeout=20.0)
        if r.status_code < 400:
            names = [w.get("name") for w in (r.json().get("data") or [])]
            line(OK, "GET /api/v1/workflows", f"{len(names)}: {', '.join(names) or '(none)'}")
        else:
            fail("GET /api/v1/workflows", f"{r.status_code}: {r.text[:150]}")
    except Exception as exc:      # noqa: BLE001
        fail("GET /api/v1/workflows", str(exc)[:200])


# ============================================================ main
def main() -> int:
    which = [a.lower() for a in sys.argv[1:]] or [
        "cognee", "sarvam", "openai", "gemini", "nvidia", "n8n"]
    print(f"\nchecking from {ROOT}")
    print(f".env found: {(ROOT / '.env').exists()}")
    print(f"flags: {config.adapters()}")

    if "cognee" in which:
        check_cognee()
    if "sarvam" in which:
        check_sarvam()
    if "openai" in which:
        check_openai()
    if "gemini" in which:
        check_gemini()
    if "nvidia" in which or "nim" in which:
        check_nvidia()
    if "n8n" in which:
        check_n8n()

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        print()
        return 1
    print("all checked integrations responded\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
