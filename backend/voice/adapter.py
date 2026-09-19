"""Speech in and out.

Default is 'browser': the server does no speech at all and the page uses the
Web Speech API. That path needs no key and no network, which is why it is the
default. Sarvam is the real integration behind the same contract.

Model choice matters here. `saarika` TRANSCRIBES; `saaras` transcribes and
TRANSLATES TO ENGLISH. We want transcription, because the intent router reads
Hinglish and because "aapne bataya tha…" quotes the merchant's own words back
to them. Translation throws both away. So saarika is the default, saaras is the
fallback, and scripts/check_integrations.py reports which the account accepts.
"""
from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402

STT_URL = "https://api.sarvam.ai/speech-to-text"
STT_TRANSLATE_URL = "https://api.sarvam.ai/speech-to-text-translate"
TTS_URL = "https://api.sarvam.ai/text-to-speech"


class Voice(ABC):
    name = "abstract"
    server_side = False

    @abstractmethod
    def transcribe(self, audio: bytes, language: str | None = None) -> dict: ...

    @abstractmethod
    def speak(self, text: str, language: str | None = None) -> dict: ...


class BrowserVoice(Voice):
    """No-op server side: the page does STT and TTS itself."""
    name = "browser"
    server_side = False

    def transcribe(self, audio: bytes, language: str | None = None) -> dict:
        return {"ok": False, "handled_by": "browser",
                "note": "the page uses webkitSpeechRecognition"}

    def speak(self, text: str, language: str | None = None) -> dict:
        return {"ok": False, "handled_by": "browser", "text": text,
                "note": "the page uses speechSynthesis"}


class SarvamVoice(Voice):
    name = "sarvam"
    server_side = True

    def __init__(self):
        self._stt_model = config.SARVAM_STT_MODEL
        self._translate = "saaras" in self._stt_model

    # ------------------------------------------------------------------ STT
    def _post_stt(self, audio: bytes, model: str, language: str):
        import httpx
        url = STT_TRANSLATE_URL if "saaras" in model else STT_URL
        data = {"model": model}
        if "saaras" not in model:
            data["language_code"] = language
        return httpx.post(url,
                          headers={"api-subscription-key": config.SARVAM_API_KEY},
                          files={"file": ("audio.wav", audio, "audio/wav")},
                          data=data, timeout=config.SARVAM_TIMEOUT)

    def transcribe(self, audio: bytes, language: str | None = None) -> dict:
        lang = language or config.SARVAM_LANGUAGE
        t0 = time.time()
        for model in (self._stt_model, config.SARVAM_STT_FALLBACK):
            if not model:
                continue
            try:
                r = self._post_stt(audio, model, lang)
                if r.status_code >= 400:
                    # a wrong model id is a 4xx: try the fallback rather than
                    # dropping the merchant to browser speech
                    continue
                body = r.json()
                text = body.get("transcript") or body.get("text") or ""
                return {"ok": bool(text), "text": text, "engine": "sarvam",
                        "model": model,
                        "translated": "saaras" in model,
                        "language": body.get("language_code", lang),
                        "confidence": body.get("confidence"),
                        "ms": int((time.time() - t0) * 1000)}
            except Exception:      # noqa: BLE001
                continue
        # fall to the browser path within the timeout, with no visible error
        return {"ok": False, "engine": "sarvam", "error": "stt_failed",
                "fallback": "browser"}

    # ------------------------------------------------------------------ TTS
    def speak(self, text: str, language: str | None = None) -> dict:
        import httpx
        lang = language or config.SARVAM_LANGUAGE
        try:
            r = httpx.post(TTS_URL,
                           headers={"api-subscription-key": config.SARVAM_API_KEY},
                           json={"inputs": [text], "target_language_code": lang,
                                 "speaker": config.SARVAM_SPEAKER,
                                 "model": config.SARVAM_TTS_MODEL},
                           timeout=config.SARVAM_TIMEOUT)
            if r.status_code >= 400:
                return {"ok": False, "engine": "sarvam",
                        "error": f"http_{r.status_code}", "detail": r.text[:200],
                        "fallback": "browser", "text": text}
            body = r.json()
            audios = body.get("audios") or []
            return {"ok": bool(audios), "audio_b64": audios[0] if audios else None,
                    "engine": "sarvam", "model": config.SARVAM_TTS_MODEL,
                    "speaker": config.SARVAM_SPEAKER}
        except Exception as exc:                      # noqa: BLE001
            return {"ok": False, "engine": "sarvam", "error": type(exc).__name__,
                    "fallback": "browser", "text": text}


def get_voice() -> Voice:
    if config.USE_REAL_SARVAM and config.SARVAM_API_KEY:
        return SarvamVoice()
    return BrowserVoice()
