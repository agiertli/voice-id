import io
import json
import logging
import os
import sqlite3
import subprocess
import time
import uuid
import warnings
from pathlib import Path

import imageio_ffmpeg
from flask import Flask, Response, g, jsonify, render_template, request

from providers import FishSpeechProvider, XTTSProvider

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# Trim leading/trailing silence (keeps 0.15s padding). Human clips that start with
# a second of silence are an instant giveaway next to TTS output — for players and the detector.
TRIM_SILENCE_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,"
    "areverse"
)


def convert_to_wav(input_bytes, sample_rate=22050, trim_silence=False):
    cmd = [FFMPEG, "-i", "pipe:0", "-ac", "1", "-ar", str(sample_rate), "-sample_fmt", "s16"]
    if trim_silence:
        cmd += ["-af", TRIM_SILENCE_FILTER]
    cmd += ["-f", "wav", "pipe:1"]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode()[-200:]}")
    return proc.stdout

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/voice-data"))
DB_PATH = DATA_DIR / "studio.db"
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "cs")

providers = {}
voice_provider_refs = {}

PROMPTS = {
    "cs": [
        "Umělá inteligence mění způsob, jakým vyvíjíme a nasazujeme software. Dnes dokážeme pomocí velkých jazykových modelů generovat kód, analyzovat dokumentaci a automatizovat testování. Tyto modely se neustále zlepšují a jejich schopnosti rostou každým měsícem. Otázkou zůstává, jak zajistit kvalitu a bezpečnost kódu, který tyto systémy produkují. Lidský dohled je stále nezbytný, ale hranice mezi tím, co zvládne stroj a co člověk, se každým dnem posouvá.",
        "Kontejnery a Kubernetes tvoří základ moderní cloudové infrastruktury. Red Hat OpenShift přidává podnikové funkce jako bezpečnost, monitoring a správu životního cyklu aplikací. Díky operátorům můžeme automatizovat nasazení složitých systémů, které by dříve vyžadovaly ruční konfiguraci. Hybridní cloud umožňuje firmám provozovat stejné aplikace v datovém centru i ve veřejném cloudu. Tato flexibilita je klíčová pro organizace, které potřebují splnit regulatorní požadavky a zároveň využívat výhody cloudových služeb.",
        "Open source komunita pohání inovace rychleji než jakákoliv firma sama. Tisíce vývojářů po celém světě přispívají do projektů jako Linux, Kubernetes nebo PyTorch. Spolupráce napříč firmami a zeměmi vytváří software, který je transparentní, bezpečný a neustále se zlepšuje. Red Hat staví svůj obchodní model na tom, že přináší podporu a stabilitu do open source projektů pro podnikové zákazníky. Otevřené standardy zajišťují, že technologie zůstane dostupná všem, nejen velkým korporacím.",
        "Velké jazykové modely dokážou generovat text, kód i zvuk, ale stále potřebují lidský dohled. Klonování hlasu je jednou z nejpůsobivějších aplikací moderní umělé inteligence. Stačí několik sekund nahrávky vašeho hlasu a model dokáže vytvořit syntetický hlas, který zní téměř identicky. Tato technologie má obrovský potenciál v přístupnosti, lokalizaci a zábavním průmyslu. Zároveň ale přináší nové výzvy v oblasti bezpečnosti a ověřování identity, které musíme jako společnost řešit.",
        "Automatizace s nástrojem Ansible šetří čas a snižuje riziko lidských chyb při správě serverů a síťových zařízení. Místo ručního přihlašování na každý server můžeme definovat požadovaný stav v jednoduchých souborech a nechat Ansible, aby zajistil, že všechny systémy odpovídají tomuto stavu. Díky tomu dokážeme spravovat stovky nebo tisíce serverů stejně snadno jako jeden jediný. Automatizace je klíčovým pilířem moderního IT a pomáhá týmům soustředit se na inovace místo opakující se manuální práce.",
    ],
    "en": [
        "Artificial intelligence is transforming how we develop and deploy software in the modern enterprise. Large language models can now generate code, analyze documentation, and automate testing pipelines with remarkable accuracy. These models continue to improve month after month, pushing the boundaries of what machines can accomplish. The key question remains how to ensure the quality and security of AI-generated code in production environments. Human oversight is still essential, but the line between machine capability and human expertise shifts every single day.",
        "Containers and Kubernetes form the foundation of modern cloud-native infrastructure around the world. Red Hat OpenShift adds enterprise features like built-in security, comprehensive monitoring, and full application lifecycle management on top of Kubernetes. With operators, we can automate the deployment of complex distributed systems that previously required extensive manual configuration and deep expertise. Hybrid cloud enables organizations to run the same applications in their private data center and in any public cloud provider seamlessly.",
        "The open source community drives innovation faster than any single company ever could on its own. Thousands of developers across the globe contribute to projects like Linux, Kubernetes, and PyTorch every day. This cross-company and cross-border collaboration produces software that is transparent, secure, and continuously improving. Red Hat builds its business model on bringing enterprise support and long-term stability to open source projects. Open standards ensure that technology remains accessible to everyone, not just large corporations with massive budgets.",
        "Large language models can generate text, code, and even audio, but they still require careful human oversight and validation. Voice cloning is one of the most impressive applications of modern artificial intelligence today. Just a few seconds of your voice recording is enough for a model to create a synthetic voice that sounds nearly identical to you. This technology has enormous potential in accessibility, localization, entertainment, and education. At the same time, it raises new challenges around security, identity verification, and ethical use that we must address as a society.",
        "Automation with tools like Ansible saves time and dramatically reduces the risk of human error when managing servers, network devices, and cloud infrastructure. Instead of manually logging into each server, we define the desired state in simple declarative files and let Ansible ensure all systems match that configuration. This approach lets us manage hundreds or thousands of servers just as easily as a single one. Automation is a key pillar of modern IT operations and helps teams focus their energy on innovation rather than repetitive manual work.",
    ],
}


# One-shot sentences for human clips (Voice ID game + detector training).
# Same texts as apps/deepfake-detector/generate_synthetic_data.py, so human and AI
# clips say identical sentences — content must not give away which is which.
SENTENCES = {
    "cs": [
        "Kontejnerizace aplikací přináší flexibilitu a škálovatelnost.",
        "OpenShift je podniková platforma pro správu kontejnerů.",
        "Automatizace je klíčem k úspěšnému nasazení v cloudu.",
        "Bezpečnost je naší nejvyšší prioritou při vývoji software.",
        "Umělá inteligence transformuje způsob, jakým pracujeme.",
        "Hybridní cloud umožňuje kombinovat veřejné a privátní prostředí.",
        "DevOps praktiky zrychlují dodávku software zákazníkům.",
        "Kubernetes orchestruje kontejnery v produkčním prostředí.",
        "Strojové učení pomáhá analyzovat velké objemy dat.",
        "Red Hat Enterprise Linux je stabilní základ pro podnikové systémy.",
        "Otevřený zdrojový kód je hnacím motorem inovací.",
        "Mikroslužby umožňují nezávislý vývoj a nasazení komponent.",
        "Správa konfigurace je důležitá pro konzistentní prostředí.",
        "Monitorování systémů pomáhá předcházet výpadkům.",
        "Infrastruktura jako kód zajišťuje reprodukovatelnost nasazení.",
    ],
    "sk": [
        "Kontajnerizácia aplikácií prináša flexibilitu a škálovateľnosť.",
        "OpenShift je podniková platforma na správu kontajnerov.",
        "Automatizácia je kľúčom k úspešnému nasadeniu v cloude.",
        "Bezpečnosť je našou najvyššou prioritou pri vývoji softvéru.",
        "Umelá inteligencia transformuje spôsob, akým pracujeme.",
        "Hybridný cloud umožňuje kombinovať verejné a privátne prostredie.",
        "DevOps praktiky zrýchľujú dodávku softvéru zákazníkom.",
        "Kubernetes orchestruje kontajnery v produkčnom prostredí.",
        "Strojové učenie pomáha analyzovať veľké objemy dát.",
        "Red Hat Enterprise Linux je stabilný základ pre podnikové systémy.",
        "Otvorený zdrojový kód je hnacím motorom inovácií.",
        "Mikroslužby umožňujú nezávislý vývoj a nasadenie komponentov.",
        "Správa konfigurácie je dôležitá pre konzistentné prostredie.",
        "Monitorovanie systémov pomáha predchádzať výpadkom.",
        "Infraštruktúra ako kód zabezpečuje reprodukovateľnosť nasadenia.",
    ],
    "en": [
        "Container technology brings flexibility and scalability to modern applications.",
        "OpenShift is an enterprise platform for managing containerized workloads.",
        "Automation is the key to successful cloud deployment strategies.",
        "Security remains our highest priority in software development.",
        "Artificial intelligence is transforming the way we work every day.",
        "Hybrid cloud enables combining public and private environments seamlessly.",
        "DevOps practices accelerate software delivery to end customers.",
        "Kubernetes orchestrates containers in production environments at scale.",
        "Machine learning helps analyze large volumes of data efficiently.",
        "Red Hat Enterprise Linux provides a stable foundation for enterprise systems.",
        "Open source code drives innovation across the technology industry.",
        "Microservices enable independent development and deployment of components.",
        "Configuration management ensures consistent and reliable environments.",
        "System monitoring helps prevent outages and performance degradation.",
        "Infrastructure as code ensures reproducible and auditable deployments.",
    ],
}
SENTENCE_LANGS = list(SENTENCES.keys())
SENTENCE_SAMPLE_RATE = 44100
MAX_SENTENCE_BYTES = 10 * 1024 * 1024

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS voices (
            voice_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            reference_audio BLOB NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clips (
            clip_id TEXT PRIMARY KEY,
            voice_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            provider_name TEXT NOT NULL,
            text TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'cs',
            audio_data BLOB NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (voice_id) REFERENCES voices(voice_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS recordings (
            recording_id TEXT PRIMARY KEY,
            speaker TEXT NOT NULL COLLATE NOCASE,
            language TEXT NOT NULL,
            sentence_idx INTEGER NOT NULL,
            text TEXT NOT NULL,
            voice_id TEXT,
            audio_data BLOB NOT NULL,
            duration_s REAL,
            created_at REAL NOT NULL,
            UNIQUE (speaker, language, sentence_idx),
            FOREIGN KEY (voice_id) REFERENCES voices(voice_id) ON DELETE SET NULL
        );
    """)
    db.commit()
    db.close()


def restore_provider_refs():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT voice_id, name, reference_audio FROM voices").fetchall()
    db.close()

    for row in rows:
        voice_id = row["voice_id"]
        wav_bytes = row["reference_audio"]
        name = row["name"]
        refs = {}
        for p in providers.values():
            ref = p.register_voice(wav_bytes, f"{name}-{voice_id}")
            if ref:
                refs[p.id] = ref
                logger.info("Restored voice %s (%s) with %s", voice_id, name, p.name)
        voice_provider_refs[voice_id] = refs

    if rows:
        logger.info("Restored provider refs for %d voice(s)", len(rows))


def init_providers():
    xtts_url = os.environ.get("XTTS_URL", "")
    fish_url = os.environ.get("FISH_URL", "")

    if xtts_url:
        p = XTTSProvider(xtts_url)
        providers[p.id] = p
        logger.info("Provider registered: %s at %s", p.name, xtts_url)

    if fish_url:
        p = FishSpeechProvider(fish_url)
        providers[p.id] = p
        logger.info("Provider registered: %s at %s", p.name, fish_url)

    if not providers:
        logger.warning("No TTS providers configured. Set XTTS_URL and/or FISH_URL.")

    restore_provider_refs()


def new_id():
    return str(uuid.uuid4())[:8]


@app.route("/")
def index():
    return render_template("index.html", default_lang=DEFAULT_LANG)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Voice Clone Studio API",
        "version": "2.1.0",
        "description": "TTS voice cloning demo — record your voice, generate speech with multiple AI models, and record one-shot sentences as human clips. Audio stored in SQLite.",
    },
    "paths": {
        "/api/health": {
            "get": {
                "summary": "Health check",
                "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object", "properties": {"status": {"type": "string"}}}}}}},
            }
        },
        "/api/prompts": {
            "get": {
                "summary": "Get reading prompts",
                "parameters": [{"name": "lang", "in": "query", "schema": {"type": "string", "enum": ["cs", "en"], "default": "cs"}, "description": "Language code"}],
                "responses": {"200": {"description": "List of prompt texts", "content": {"application/json": {"schema": {"type": "array", "items": {"type": "string"}}}}}},
            }
        },
        "/api/providers": {
            "get": {
                "summary": "List configured TTS providers",
                "responses": {"200": {"description": "Provider list", "content": {"application/json": {"schema": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "url": {"type": "string"}}}}}}}},
            }
        },
        "/api/voices": {
            "get": {
                "summary": "List registered voices",
                "responses": {"200": {"description": "Voice list", "content": {"application/json": {"schema": {"type": "array", "items": {"type": "object", "properties": {"voice_id": {"type": "string"}, "name": {"type": "string"}}}}}}}},
            },
            "post": {
                "summary": "Register a new voice from audio recording",
                "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["audio", "name"], "properties": {"audio": {"type": "string", "format": "binary", "description": "Audio file (webm/wav/mp4)"}, "name": {"type": "string", "description": "Speaker name"}}}}}},
                "responses": {
                    "200": {"description": "Voice registered", "content": {"application/json": {"schema": {"type": "object", "properties": {"voice_id": {"type": "string"}, "name": {"type": "string"}, "providers": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}}}}}}}},
                    "400": {"description": "Invalid input"},
                },
            },
        },
        "/api/voices/{voice_id}": {
            "put": {
                "summary": "Replace reference audio for an existing voice",
                "parameters": [{"name": "voice_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["audio"], "properties": {"audio": {"type": "string", "format": "binary", "description": "New audio file (webm/wav/mp4)"}}}}}},
                "responses": {
                    "200": {"description": "Voice updated"},
                    "400": {"description": "Invalid input"},
                    "404": {"description": "Voice not found"},
                },
            },
            "delete": {
                "summary": "Delete a voice and all its clips",
                "parameters": [{"name": "voice_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "Voice deleted", "content": {"application/json": {"schema": {"type": "object", "properties": {"status": {"type": "string"}, "clips_deleted": {"type": "integer"}}}}}},
                    "404": {"description": "Voice not found"},
                },
            },
        },
        "/api/voices/{voice_id}/audio": {
            "get": {
                "summary": "Download original voice reference audio",
                "parameters": [{"name": "voice_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "WAV audio", "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}},
                    "404": {"description": "Voice not found"},
                },
            }
        },
        "/api/generate": {
            "post": {
                "summary": "Generate speech from text using a cloned voice",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "required": ["voice_id", "text", "provider_id"], "properties": {"voice_id": {"type": "string"}, "text": {"type": "string"}, "provider_id": {"type": "string", "description": "xtts or fish"}, "language": {"type": "string", "default": "cs"}}}}}},
                "responses": {
                    "200": {"description": "Clip generated", "content": {"application/json": {"schema": {"type": "object", "properties": {"clip_id": {"type": "string"}, "voice_id": {"type": "string"}, "provider_id": {"type": "string"}, "provider_name": {"type": "string"}, "text": {"type": "string"}, "language": {"type": "string"}}}}}},
                    "400": {"description": "Invalid input"},
                    "404": {"description": "Voice not found"},
                    "500": {"description": "Generation failed"},
                },
            }
        },
        "/api/clips": {
            "get": {
                "summary": "List generated clips",
                "parameters": [{"name": "voice_id", "in": "query", "schema": {"type": "string"}, "description": "Filter by voice ID"}],
                "responses": {"200": {"description": "Clip list"}},
            }
        },
        "/api/clips/import": {
            "post": {
                "summary": "Import an existing audio clip (for migration)",
                "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["audio", "voice_id", "provider_id", "provider_name", "text"], "properties": {"audio": {"type": "string", "format": "binary", "description": "WAV audio file"}, "voice_id": {"type": "string"}, "provider_id": {"type": "string"}, "provider_name": {"type": "string"}, "text": {"type": "string"}, "language": {"type": "string", "default": "cs"}, "created_at": {"type": "number", "description": "Original Unix timestamp"}}}}}},
                "responses": {
                    "200": {"description": "Clip imported", "content": {"application/json": {"schema": {"type": "object", "properties": {"clip_id": {"type": "string"}, "status": {"type": "string"}}}}}},
                    "400": {"description": "Invalid input"},
                    "404": {"description": "Voice not found"},
                },
            }
        },
        "/api/clips/{clip_id}/audio": {
            "get": {
                "summary": "Download generated audio clip",
                "parameters": [{"name": "clip_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "WAV audio", "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}}},
            }
        },
        "/api/clips/{clip_id}": {
            "delete": {
                "summary": "Delete a generated clip",
                "parameters": [{"name": "clip_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "Clip deleted", "content": {"application/json": {"schema": {"type": "object", "properties": {"status": {"type": "string"}}}}}},
                    "404": {"description": "Clip not found"},
                },
            }
        },
        "/api/sentences": {
            "get": {
                "summary": "List one-shot sentences to read (human clips)",
                "parameters": [{"name": "lang", "in": "query", "schema": {"type": "string", "enum": ["cs", "sk", "en"], "default": "cs"}}],
                "responses": {
                    "200": {"description": "Sentences", "content": {"application/json": {"schema": {"type": "array", "items": {"type": "object", "properties": {"idx": {"type": "integer"}, "text": {"type": "string"}}}}}}},
                    "400": {"description": "Unsupported language"},
                },
            }
        },
        "/api/recordings": {
            "get": {
                "summary": "List sentence recordings (metadata only)",
                "parameters": [
                    {"name": "speaker", "in": "query", "schema": {"type": "string"}, "description": "Filter by speaker (case-insensitive)"},
                    {"name": "language", "in": "query", "schema": {"type": "string", "enum": ["cs", "sk", "en"]}},
                    {"name": "voice_id", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Recording list", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Recording"}}}}}},
            },
            "post": {
                "summary": "Save a one-shot sentence recording (replaces previous take of the same speaker/language/sentence)",
                "description": "Audio is converted to mono 16-bit 44.1 kHz WAV with leading/trailing silence trimmed.",
                "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["audio", "speaker", "language", "sentence_idx"], "properties": {
                    "audio": {"type": "string", "format": "binary", "description": "Audio file (webm/wav/mp4), max 10 MB"},
                    "speaker": {"type": "string"},
                    "language": {"type": "string", "enum": ["cs", "sk", "en"]},
                    "sentence_idx": {"type": "integer", "description": "Index from /api/sentences"},
                    "voice_id": {"type": "string", "description": "Optional link to a cloned voice (pairs real and AI clips)"},
                }}}}},
                "responses": {
                    "200": {"description": "Recording saved", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Recording"}}}},
                    "400": {"description": "Invalid input or empty recording"},
                },
            },
        },
        "/api/recordings/{recording_id}/audio": {
            "get": {
                "summary": "Download a sentence recording",
                "parameters": [{"name": "recording_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "WAV audio", "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}}, "404": {"description": "Not found"}},
            }
        },
        "/api/recordings/{recording_id}": {
            "delete": {
                "summary": "Delete a sentence recording",
                "parameters": [{"name": "recording_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Deleted"}, "404": {"description": "Not found"}},
            }
        },
    },
    "components": {
        "schemas": {
            "Recording": {"type": "object", "properties": {
                "recording_id": {"type": "string"}, "speaker": {"type": "string"}, "language": {"type": "string"},
                "sentence_idx": {"type": "integer"}, "text": {"type": "string"}, "voice_id": {"type": "string", "nullable": True},
                "duration_s": {"type": "number"}, "created_at": {"type": "number"},
            }},
        },
    },
}


@app.route("/api/openapi.json")
def openapi_spec():
    return jsonify(OPENAPI_SPEC)


@app.route("/api/docs")
def swagger_ui():
    return """<!DOCTYPE html>
<html><head>
<title>Voice Clone Studio — API Docs</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head><body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:'/api/openapi.json',dom_id:'#swagger-ui'})</script>
</body></html>"""


@app.route("/api/prompts")
def api_prompts():
    lang = request.args.get("lang", DEFAULT_LANG)
    return jsonify(PROMPTS.get(lang, PROMPTS["cs"]))


@app.route("/api/providers")
def api_providers():
    return jsonify([{"id": p.id, "name": p.name, "url": p.url} for p in providers.values()])


@app.route("/api/voices", methods=["POST"])
def create_voice():
    audio_file = request.files.get("audio")
    name = request.form.get("name", "").strip()

    if not audio_file:
        return jsonify({"error": "audio file required"}), 400
    if not name:
        return jsonify({"error": "name required"}), 400

    raw_bytes = audio_file.read()

    try:
        wav_bytes = convert_to_wav(raw_bytes)
    except Exception:
        logger.exception("Audio conversion failed")
        return jsonify({"error": "Failed to process audio. Try a different recording."}), 400

    voice_id = new_id()

    provider_results = []
    refs = {}
    for p in providers.values():
        ref = p.register_voice(wav_bytes, f"{name}-{voice_id}")
        if ref:
            refs[p.id] = ref
            provider_results.append({"id": p.id, "status": "ok"})
            logger.info("Voice %s registered with %s", voice_id, p.name)
        else:
            provider_results.append({"id": p.id, "status": "failed"})
            logger.warning("Voice %s failed to register with %s", voice_id, p.name)

    voice_provider_refs[voice_id] = refs

    db = get_db()
    db.execute(
        "INSERT INTO voices (voice_id, name, reference_audio, created_at) VALUES (?, ?, ?, ?)",
        (voice_id, name, wav_bytes, time.time()),
    )
    db.commit()

    return jsonify({"voice_id": voice_id, "name": name, "providers": provider_results})


@app.route("/api/voices/<voice_id>", methods=["PUT"])
def replace_voice(voice_id):
    db = get_db()
    row = db.execute("SELECT voice_id, name FROM voices WHERE voice_id = ?", (voice_id,)).fetchone()
    if not row:
        return jsonify({"error": "voice not found"}), 404

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "audio file required"}), 400

    raw_bytes = audio_file.read()
    try:
        wav_bytes = convert_to_wav(raw_bytes)
    except Exception:
        logger.exception("Audio conversion failed")
        return jsonify({"error": "Failed to process audio."}), 400

    name = row["name"]

    provider_results = []
    refs = {}
    for p in providers.values():
        ref = p.register_voice(wav_bytes, f"{name}-{voice_id}")
        if ref:
            refs[p.id] = ref
            provider_results.append({"id": p.id, "status": "ok"})
        else:
            provider_results.append({"id": p.id, "status": "failed"})

    voice_provider_refs[voice_id] = refs

    db.execute(
        "UPDATE voices SET reference_audio = ? WHERE voice_id = ?",
        (wav_bytes, voice_id),
    )
    db.commit()
    logger.info("Replaced reference audio for voice %s (%s)", voice_id, name)

    return jsonify({"voice_id": voice_id, "name": name, "providers": provider_results})


@app.route("/api/voices")
def list_voices():
    db = get_db()
    rows = db.execute("SELECT voice_id, name FROM voices ORDER BY created_at").fetchall()
    return jsonify([{"voice_id": r["voice_id"], "name": r["name"]} for r in rows])


@app.route("/api/voices/<voice_id>/audio")
def serve_voice_audio(voice_id):
    db = get_db()
    row = db.execute("SELECT reference_audio, name FROM voices WHERE voice_id = ?", (voice_id,)).fetchone()
    if not row:
        return jsonify({"error": "voice not found"}), 404

    return Response(
        row["reference_audio"],
        mimetype="audio/wav",
        headers={
            "Content-Length": str(len(row["reference_audio"])),
            "Content-Disposition": f'attachment; filename="{row["name"]}-reference.wav"',
        },
    )


@app.route("/api/voices/<voice_id>", methods=["DELETE"])
def delete_voice(voice_id):
    db = get_db()
    row = db.execute("SELECT voice_id FROM voices WHERE voice_id = ?", (voice_id,)).fetchone()
    if not row:
        return jsonify({"error": "voice not found"}), 404

    clips_deleted = db.execute("SELECT COUNT(*) FROM clips WHERE voice_id = ?", (voice_id,)).fetchone()[0]
    db.execute("DELETE FROM voices WHERE voice_id = ?", (voice_id,))
    db.commit()

    voice_provider_refs.pop(voice_id, None)
    logger.info("Deleted voice %s and %d clip(s)", voice_id, clips_deleted)

    return jsonify({"status": "deleted", "clips_deleted": clips_deleted})


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    voice_id = data.get("voice_id", "")
    text = data.get("text", "").strip()
    provider_id = data.get("provider_id", "")
    language = data.get("language", DEFAULT_LANG)

    db = get_db()
    row = db.execute("SELECT voice_id FROM voices WHERE voice_id = ?", (voice_id,)).fetchone()
    if not row:
        return jsonify({"error": "voice not found"}), 404
    if not text:
        return jsonify({"error": "text required"}), 400
    if not provider_id or provider_id not in providers:
        return jsonify({"error": f"provider '{provider_id}' not available"}), 400

    provider = providers[provider_id]
    refs = voice_provider_refs.get(voice_id, {})
    voice_ref = refs.get(provider_id)
    if not voice_ref:
        return jsonify({"error": f"voice not registered with {provider.name}"}), 400

    logger.info("Generating: provider=%s voice=%s text='%s'", provider.name, voice_id, text[:60])

    wav_bytes = provider.generate(text, voice_ref, language)
    if not wav_bytes:
        return jsonify({"error": "Generation failed. Try again or use a different provider."}), 500

    clip_id = new_id()
    created_at = time.time()

    clip_meta = {
        "clip_id": clip_id,
        "voice_id": voice_id,
        "provider_id": provider_id,
        "provider_name": provider.name,
        "text": text,
        "language": language,
        "created_at": created_at,
    }

    db.execute(
        "INSERT INTO clips (clip_id, voice_id, provider_id, provider_name, text, language, audio_data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (clip_id, voice_id, provider_id, provider.name, text, language, wav_bytes, created_at),
    )
    db.commit()

    logger.info("Generated clip %s (%d bytes)", clip_id, len(wav_bytes))
    return jsonify(clip_meta)


@app.route("/api/clips/import", methods=["POST"])
def import_clip():
    audio_file = request.files.get("audio")
    voice_id = request.form.get("voice_id", "").strip()
    provider_id = request.form.get("provider_id", "").strip()
    provider_name = request.form.get("provider_name", "").strip()
    text = request.form.get("text", "").strip()
    language = request.form.get("language", DEFAULT_LANG).strip()
    created_at = request.form.get("created_at", "")

    if not audio_file:
        return jsonify({"error": "audio file required"}), 400
    if not voice_id:
        return jsonify({"error": "voice_id required"}), 400
    if not provider_id:
        return jsonify({"error": "provider_id required"}), 400
    if not provider_name:
        return jsonify({"error": "provider_name required"}), 400
    if not text:
        return jsonify({"error": "text required"}), 400

    db = get_db()
    row = db.execute("SELECT voice_id FROM voices WHERE voice_id = ?", (voice_id,)).fetchone()
    if not row:
        return jsonify({"error": "voice not found"}), 404

    wav_bytes = audio_file.read()
    clip_id = new_id()

    try:
        ts = float(created_at) if created_at else time.time()
    except ValueError:
        ts = time.time()

    db.execute(
        "INSERT INTO clips (clip_id, voice_id, provider_id, provider_name, text, language, audio_data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (clip_id, voice_id, provider_id, provider_name, text, language, wav_bytes, ts),
    )
    db.commit()

    logger.info("Imported clip %s for voice %s (%d bytes)", clip_id, voice_id, len(wav_bytes))
    return jsonify({"clip_id": clip_id, "status": "imported"})


@app.route("/api/clips")
def list_clips():
    voice_id = request.args.get("voice_id")
    db = get_db()

    if voice_id:
        rows = db.execute(
            "SELECT clip_id, voice_id, provider_id, provider_name, text, language, created_at FROM clips WHERE voice_id = ? ORDER BY created_at DESC",
            (voice_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT clip_id, voice_id, provider_id, provider_name, text, language, created_at FROM clips ORDER BY created_at DESC"
        ).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/clips/<clip_id>/audio")
def serve_clip(clip_id):
    db = get_db()
    row = db.execute("SELECT audio_data FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()
    if not row:
        return jsonify({"error": "clip not found"}), 404

    data = row["audio_data"]
    return Response(data, mimetype="audio/wav", headers={"Content-Length": str(len(data))})


@app.route("/api/clips/<clip_id>", methods=["DELETE"])
def delete_clip(clip_id):
    db = get_db()
    row = db.execute("SELECT clip_id FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()
    if not row:
        return jsonify({"error": "clip not found"}), 404

    db.execute("DELETE FROM clips WHERE clip_id = ?", (clip_id,))
    db.commit()
    logger.info("Deleted clip %s", clip_id)

    return jsonify({"status": "deleted"})


# --- One-shot sentence recordings (human clips) ---

def _fix_wav_header(wav_bytes):
    """ffmpeg writing WAV to a pipe can't seek back, so RIFF/data sizes are bogus (0xFFFFFFFF).
    Rewrite them so browsers, the game and training tools read the right length.
    Returns (fixed_bytes, duration_seconds)."""
    import struct
    b = bytearray(wav_bytes)
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, byte_rate = 12, None
    while pos + 8 <= len(b):
        cid, size = bytes(b[pos:pos + 4]), struct.unpack("<I", b[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            byte_rate = struct.unpack("<I", b[pos + 16:pos + 20])[0]
        if cid == b"data":
            data_len = len(b) - (pos + 8)
            struct.pack_into("<I", b, pos + 4, data_len)
            struct.pack_into("<I", b, 4, len(b) - 8)
            return bytes(b), round(data_len / float(byte_rate), 2) if byte_rate else 0.0
        pos += 8 + size + (size & 1)
    raise ValueError("no data chunk")


@app.route("/api/sentences")
def api_sentences():
    lang = request.args.get("lang", DEFAULT_LANG)
    if lang not in SENTENCES:
        return jsonify({"error": f"unsupported language '{lang}', use one of {SENTENCE_LANGS}"}), 400
    return jsonify([{"idx": i, "text": t} for i, t in enumerate(SENTENCES[lang])])


@app.route("/api/recordings", methods=["POST"])
def create_recording():
    audio_file = request.files.get("audio")
    speaker = request.form.get("speaker", "").strip()
    language = request.form.get("language", "").strip()
    voice_id = request.form.get("voice_id", "").strip() or None

    if not audio_file:
        return jsonify({"error": "audio file required"}), 400
    if not speaker:
        return jsonify({"error": "speaker required"}), 400
    if language not in SENTENCES:
        return jsonify({"error": f"language must be one of {SENTENCE_LANGS}"}), 400
    try:
        idx = int(request.form.get("sentence_idx", ""))
    except ValueError:
        return jsonify({"error": "sentence_idx must be an integer"}), 400
    if not 0 <= idx < len(SENTENCES[language]):
        return jsonify({"error": "sentence_idx out of range"}), 400

    raw = audio_file.read()
    if len(raw) > MAX_SENTENCE_BYTES:
        return jsonify({"error": "recording too large"}), 400

    db = get_db()
    if voice_id and not db.execute("SELECT 1 FROM voices WHERE voice_id = ?", (voice_id,)).fetchone():
        voice_id = None

    try:
        wav_bytes = convert_to_wav(raw, sample_rate=SENTENCE_SAMPLE_RATE, trim_silence=True)
    except Exception:
        logger.exception("Audio conversion failed")
        return jsonify({"error": "Failed to process audio. Try a different recording."}), 400

    try:
        wav_bytes, duration = _fix_wav_header(wav_bytes)
    except ValueError:
        logger.exception("Bad WAV from ffmpeg")
        return jsonify({"error": "Failed to process audio."}), 400
    if duration < 0.5:
        return jsonify({"error": "Recording is empty or too quiet. Try again."}), 400

    recording_id = new_id()
    text = SENTENCES[language][idx]
    created_at = time.time()
    # Re-recording the same sentence replaces the previous take (no duplicates in the game).
    db.execute("DELETE FROM recordings WHERE speaker = ? AND language = ? AND sentence_idx = ?",
               (speaker, language, idx))
    db.execute(
        "INSERT INTO recordings (recording_id, speaker, language, sentence_idx, text, voice_id, audio_data, duration_s, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (recording_id, speaker, language, idx, text, voice_id, wav_bytes, duration, created_at),
    )
    db.commit()
    logger.info("Recording %s: speaker=%s lang=%s idx=%d %.1fs", recording_id, speaker, language, idx, duration)

    return jsonify({"recording_id": recording_id, "speaker": speaker, "language": language,
                    "sentence_idx": idx, "text": text, "voice_id": voice_id,
                    "duration_s": duration, "created_at": created_at})


@app.route("/api/recordings")
def list_recordings():
    q = "SELECT recording_id, speaker, language, sentence_idx, text, voice_id, duration_s, created_at FROM recordings"
    where, args = [], []
    for col in ("speaker", "language", "voice_id"):
        v = request.args.get(col, "").strip()
        if v:
            where.append(f"{col} = ?")
            args.append(v)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY speaker, language, sentence_idx"
    return jsonify([dict(r) for r in get_db().execute(q, args).fetchall()])


@app.route("/api/recordings/<recording_id>/audio")
def serve_recording(recording_id):
    row = get_db().execute("SELECT audio_data, speaker, language, sentence_idx FROM recordings WHERE recording_id = ?",
                           (recording_id,)).fetchone()
    if not row:
        return jsonify({"error": "recording not found"}), 404
    data = row["audio_data"]
    fname = f'{row["speaker"]}-{row["language"]}-{row["sentence_idx"]:02d}.wav'
    return Response(data, mimetype="audio/wav", headers={
        "Content-Length": str(len(data)),
        "Content-Disposition": f'inline; filename="{fname}"',
    })


@app.route("/api/recordings/<recording_id>", methods=["DELETE"])
def delete_recording(recording_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM recordings WHERE recording_id = ?", (recording_id,)).fetchone():
        return jsonify({"error": "recording not found"}), 404
    db.execute("DELETE FROM recordings WHERE recording_id = ?", (recording_id,))
    db.commit()
    logger.info("Deleted recording %s", recording_id)
    return jsonify({"status": "deleted"})


init_db()

with app.app_context():
    init_providers()
