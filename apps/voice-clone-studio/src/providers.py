import base64
import io
import logging

import requests

logger = logging.getLogger(__name__)


class TTSProvider:
    id = ""
    name = ""

    def __init__(self, url):
        self.url = url.rstrip("/")

    def register_voice(self, wav_bytes, speaker_name):
        raise NotImplementedError

    def generate(self, text, voice_ref, language):
        raise NotImplementedError


class XTTSProvider(TTSProvider):
    id = "xtts"
    name = "XTTS v2"

    def register_voice(self, wav_bytes, speaker_name):
        try:
            resp = requests.post(
                f"{self.url}/clone_speaker",
                files={"wav_file": ("reference.wav", io.BytesIO(wav_bytes), "audio/wav")},
                timeout=30,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "speaker_embedding": data["speaker_embedding"],
                "gpt_cond_latent": data["gpt_cond_latent"],
            }
        except Exception:
            logger.exception("XTTS register_voice failed")
            return None

    def generate(self, text, voice_ref, language):
        try:
            payload = {
                "text": text,
                "language": language,
                "speaker_embedding": voice_ref["speaker_embedding"],
                "gpt_cond_latent": voice_ref["gpt_cond_latent"],
            }
            resp = requests.post(
                f"{self.url}/tts",
                json=payload,
                timeout=60,
                verify=False,
            )
            resp.raise_for_status()
            raw = resp.text.strip().strip('"')
            return base64.b64decode(raw)
        except Exception:
            logger.exception("XTTS generate failed")
            return None


class FishSpeechProvider(TTSProvider):
    id = "fish"
    name = "Fish Speech S2 Pro"

    def register_voice(self, wav_bytes, speaker_name):
        try:
            resp = requests.post(
                f"{self.url}/v1/references/add",
                files={"audio": ("reference.wav", io.BytesIO(wav_bytes), "audio/wav")},
                data={"id": speaker_name, "text": "Toto je ukázka mého hlasu pro klonování."},
                timeout=30,
                verify=False,
            )
            if resp.status_code == 409:
                return {"reference_id": speaker_name}
            if resp.status_code != 200:
                logger.error("Fish register_voice HTTP %d: %s", resp.status_code, resp.text[:500])
                return None
            return {"reference_id": speaker_name}
        except Exception:
            logger.exception("Fish register_voice failed")
            return None

    def generate(self, text, voice_ref, language):
        try:
            payload = {
                "text": text,
                "reference_id": voice_ref["reference_id"],
                "format": "wav",
            }
            resp = requests.post(
                f"{self.url}/v1/tts",
                json=payload,
                timeout=180,
                verify=False,
            )
            resp.raise_for_status()
            return resp.content
        except Exception:
            logger.exception("Fish generate failed")
            return None
