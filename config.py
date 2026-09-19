"""Single source of truth for every flag and threshold.

Defaults are ALL FAKE on purpose: a fresh clone runs the entire demo offline,
with no API keys and no network. Flip a flag only after the matching adapter
check in scripts/check_integrations.py passes.

This is the file to put on screen when a judge asks about production readiness.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# .env loading
#
# Nothing else reads this file, so without it every flag below would silently
# stay off and you would demo the fallbacks believing they were the real
# adapters. Deliberately dependency-free: no python-dotenv import to go stale.
# Real environment variables always win over the file.
# --------------------------------------------------------------------------
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env(ROOT / ".env")

DB_PATH = Path(os.getenv("SAATHI_DB", ROOT / "data" / "saathi.db"))
SEED_DB_PATH = DB_PATH.with_name(DB_PATH.stem + ".seed.db")
WEB_DIR = ROOT / "frontend"


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# --------------------------------------------------------------------------
# adapter selection -- every one defaults to the fake implementation
# --------------------------------------------------------------------------
USE_REAL_COGNEE = _flag("SAATHI_REAL_COGNEE")
USE_REAL_SARVAM = _flag("SAATHI_REAL_SARVAM")
USE_REAL_N8N = _flag("SAATHI_REAL_N8N")
USE_REAL_LLM = _flag("SAATHI_REAL_LLM")

# --------------------------------------------------------------------------
# Cognee -- managed SaaS tenant, REST. The tenant runs the extraction model
# and embeddings itself, so there is no model credential to supply here.
# --------------------------------------------------------------------------
COGNEE_API_KEY = os.getenv("COGNEE_API_KEY", "")
COGNEE_BASE_URL = os.getenv("COGNEE_BASE_URL", "").rstrip("/")
COGNEE_TENANT_ID = os.getenv("COGNEE_TENANT_ID", "")
COGNEE_DATASET = os.getenv("COGNEE_DATASET", "paytm_hack")
COGNEE_SEARCH_TYPE = os.getenv("COGNEE_SEARCH_TYPE", "CHUNKS")
COGNEE_TOP_K = int(_num("COGNEE_TOP_K", 15))
COGNEE_TIMEOUT = _num("COGNEE_TIMEOUT", 20.0)

# Retrieval sits on the merchant's critical path; ingestion does not. Cognee
# enrichment NEVER changes a number -- every figure comes from the SQLite
# ledger -- so a slow search must degrade the provenance note, not the answer.
# A merchant waiting 20s for a spoken reply is a broken product; a merchant
# waiting 4s with `store: sqlite (cognee timed out)` in the evidence is not.
COGNEE_RETRIEVAL_TIMEOUT = _num("COGNEE_RETRIEVAL_TIMEOUT", 2.5)
COGNEE_CACHE_TTL = _num("COGNEE_CACHE_TTL", 900.0)

# --------------------------------------------------------------------------
# Sarvam
#
# saarika TRANSCRIBES; saaras transcribes AND TRANSLATES TO ENGLISH. We want
# transcription: the intent router reads Hinglish, and "aapne bataya tha"
# attribution quotes the merchant's own words back to them. Translation would
# throw both away, so saarika is the default and saaras is the fallback.
# scripts/check_integrations.py probes the candidates and reports what the
# account actually accepts.
# --------------------------------------------------------------------------
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saarika:v2.5")
SARVAM_STT_FALLBACK = os.getenv("SARVAM_STT_FALLBACK", "saaras:v3")
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_SPEAKER = os.getenv("SARVAM_SPEAKER", "priya")
SARVAM_LANGUAGE = os.getenv("SARVAM_LANGUAGE", "hi-IN")
SARVAM_TIMEOUT = _num("SARVAM_TIMEOUT", 8.0)

# --------------------------------------------------------------------------
# n8n
#
# n8n Cloud runs on their servers and cannot reach 127.0.0.1, so the workflows
# call back through a public tunnel to this API. PUBLIC_API_URL is that tunnel.
# --------------------------------------------------------------------------
N8N_BASE_URL = os.getenv("N8N_BASE_URL", "http://localhost:5678").rstrip("/")
N8N_API_KEY = os.getenv("N8N_API_KEY", "")
N8N_WEBHOOK_SECRET = os.getenv("N8N_WEBHOOK_SECRET", "saathi-demo-secret")
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "").rstrip("/")
INTERNAL_SECRET = os.getenv("SAATHI_INTERNAL_SECRET", "saathi-demo-secret")
# cloudflared rewrites every tunneled request to this host. The API uses the
# marker to require the n8n secret on the entire tunnel surface.
TUNNEL_HOST_HEADER = "n8n-callback.saathi.internal"

# --------------------------------------------------------------------------
# LLM -- Gemini plans evidence retrieval and phrases the grounded answer.
# Python still owns arithmetic, tool execution, action construction and safety.
# --------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
# The reasoning model: it plans, calls tools and decides when it has enough.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
# The speaking model, used only to turn finished evidence into spoken Hinglish.
# Separately configurable so an Indic-tuned model can take the voice without
# touching the reasoning, which is where structured-output reliability matters.
SPEECH_PROVIDER = os.getenv("SAATHI_SPEECH_PROVIDER", "same").lower()
SPEECH_MODEL = os.getenv("SAATHI_SPEECH_MODEL", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
LLM_PROVIDER = os.getenv("SAATHI_LLM_PROVIDER", "auto").lower()
PORT = int(_num("SAATHI_PORT", 8000))
SITE_PASSWORD = os.getenv("SAATHI_SITE_PASSWORD", "")  # Optional hosted demo access

# --------------------------------------------------------------------------
# privacy -- enforced in graph/privacy.py, not by convention
# --------------------------------------------------------------------------
MIN_COHORT_SIZE = int(_num("MIN_COHORT_SIZE", 5))

# --------------------------------------------------------------------------
# evidence strength -- a DIFFERENT question from privacy
#
# MIN_COHORT_SIZE asks "are there enough merchants that nobody is identifiable".
# This asks "has this play been tried enough times to be worth quoting". They
# are not the same: a cohort of 7 merchants can still yield a single attempt of
# some action, and "worked 0 of 1 (0%)" is not a finding -- it is one shop's
# bad afternoon reported as a rate.
#
# Below this, an option is carried as context but is never the recommendation
# and never has its success rate spoken as a cohort pattern.
# --------------------------------------------------------------------------
MIN_ATTEMPTS_TO_RECOMMEND = int(_num("MIN_ATTEMPTS_TO_RECOMMEND", 3))

# --------------------------------------------------------------------------
# similarity -- a product decision that belongs on a slide, not in the env
#
# 75% of the score is MEASURED from the ledger; 25% is what the merchant
# declared on a form. That ratio is the point. Category used to be 35% against
# a 0.85 threshold, which made the label a gate: a different label capped the
# score at 0.65 and no amount of genuine similarity could get past it.
# Now a mislabelled shop loses 0.10 and can still find its real peers, while a
# correctly labelled shop that trades nothing like you no longer qualifies.
# --------------------------------------------------------------------------
SIMILARITY_WEIGHTS = {
    "hour_shape": 0.30,        # when money arrives across the day
    "weekday_shape": 0.15,     # and across the week
    "ticket": 0.20,            # observed rupees per payment, absolute
    "scale": 0.10,             # observed payments per day, absolute
    "locality": 0.15,          # declared
    "category": 0.10,          # declared -- a prior, not a gate
}
SIMILARITY_THRESHOLD = _num("SIMILARITY_THRESHOLD", 0.82)   # tight cohort
EXTENDED_THRESHOLD = _num("EXTENDED_THRESHOLD", 0.70)       # wider ring, ops only
COHORT_CAP = 20

# --------------------------------------------------------------------------
# proactive monitoring
# --------------------------------------------------------------------------
ALERT_DEVIATION_PCT = _num("ALERT_DEVIATION_PCT", 12.0)
ALERT_MIN_SUSTAINED_DAYS = int(_num("ALERT_MIN_SUSTAINED_DAYS", 3))
ALERT_SUPPRESSION_HOURS = int(_num("ALERT_SUPPRESSION_HOURS", 24))

# --------------------------------------------------------------------------
# demo tuning
# --------------------------------------------------------------------------
SIM_CLOCK_SECONDS_PER_DAY = int(_num("SIM_CLOCK_SECONDS_PER_DAY", 20))
DEMO_MERCHANT = os.getenv("DEMO_MERCHANT", "M001")
COLDSTART_MERCHANT = os.getenv("COLDSTART_MERCHANT", "M999")
PEER_MERCHANT = os.getenv("PEER_MERCHANT", "M008")   # used for the learning proof

# --------------------------------------------------------------------------
# conversational fact TTLs, in hours
# --------------------------------------------------------------------------
INPUT_TTL_HOURS = {
    "stock_estimate": 12,
    "upcoming_expense": 24 * 30,
    "closure": 24 * 7,
    "event": 24 * 7,
    "supplier_delay": 24 * 7,
    "capacity": 24 * 30,
}

CATEGORIES = ["food_stall", "kirana", "salon", "pharmacy", "mobile_accessories"]
LOCALITIES = [
    ("Sector 62", "college_area"),
    ("Sector 18", "office_park"),
    ("Sector 44", "residential_colony"),
    ("Atta Market", "market_street"),
]


def cognee_ready() -> bool:
    return bool(COGNEE_API_KEY and COGNEE_BASE_URL)


def sarvam_ready() -> bool:
    return bool(SARVAM_API_KEY)


def openai_ready() -> bool:
    return bool(OPENAI_API_KEY)


def gemini_ready() -> bool:
    return bool(GEMINI_API_KEY)


def nvidia_ready() -> bool:
    return bool(NVIDIA_API_KEY)


def llm_ready() -> bool:
    return openai_ready() or gemini_ready() or nvidia_ready()


def llm_provider_label() -> str:
    if LLM_PROVIDER == "openai":
        return "openai" if openai_ready() else "offline"
    if LLM_PROVIDER == "gemini":
        return "gemini" if gemini_ready() else "template"
    if LLM_PROVIDER == "nvidia":
        return "nvidia-nim" if nvidia_ready() else "template"
    if LLM_PROVIDER == "nvidia-first":
        if nvidia_ready() and gemini_ready():
            return "nvidia+gemini-fallback"
        return "nvidia-nim" if nvidia_ready() else ("gemini" if gemini_ready() else "template")
    ready = [n for n, r in (("openai", openai_ready()), ("gemini", gemini_ready()),
                            ("nvidia-nim", nvidia_ready())) if r]
    if not ready:
        return "offline"
    return ready[0] if len(ready) == 1 else f"{ready[0]}+{'/'.join(ready[1:])}-fallback"


def adapters() -> dict:
    """What is actually running. Rendered in the /ops header and shown to judges."""
    return {
        "graph": "cognee" if (USE_REAL_COGNEE and cognee_ready()) else "sqlite",
        "voice": "sarvam" if (USE_REAL_SARVAM and sarvam_ready()) else "browser",
        "orchestrator": "n8n" if USE_REAL_N8N else "state_machine",
        "reasoner": llm_provider_label() if USE_REAL_LLM else "template",
        "campaign_api": "simulated",
    }


# --------------------------------------------------------------------------
# Agent loop. The model decides what to look up; these are the only limits.
# --------------------------------------------------------------------------
AGENT_MAX_ROUNDS = int(_num("SAATHI_AGENT_MAX_ROUNDS", 4))
AGENT_MAX_CALLS = int(_num("SAATHI_AGENT_MAX_CALLS", 10))
AGENT_BUDGET_SECONDS = _num("SAATHI_AGENT_BUDGET_SECONDS", 9.0)
AGENT_REPAIR_ATTEMPTS = int(_num("SAATHI_AGENT_REPAIR_ATTEMPTS", 2))
CONVERSATION_TURNS = int(_num("SAATHI_CONVERSATION_TURNS", 10))
