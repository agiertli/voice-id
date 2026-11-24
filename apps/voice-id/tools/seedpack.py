#!/usr/bin/env python3
"""Export / import Voice ID seed packs (clips + speaker profiles) via the admin API.

Seed packs contain voice recordings of real people. Share them privately -
never commit them to git or post them publicly. This tool refuses to write a pack
inside a git working tree unless --force is given.

  seedpack.py export --game URL --token T [--out DIR]          (default DIR: ~/voice-id-audio/seedpacks/)
  seedpack.py import --game URL --token T --file PACK [--mode skip|replace]

Offline (no server needed) - convert between a pack and the samples/ folder layout
used by load_samples.py (ai/<lang>/<model>-<speaker>-<desc>.<ext>, natural/<lang>/<speaker>-<desc>.<ext>,
speakers/<name>.<ext>, manifest.json):

  seedpack.py unpack --file PACK [--out DIR] [--force]          (default DIR: ~/voice-id-audio/samples/)
  seedpack.py pack   --dir DIR   [--out DIR] [--force]          (default out: ~/voice-id-audio/seedpacks/)
                     [--alias OLD=NEW ...] [--allow-ai-only NAME ...]

Speaker identity: one person = one speaker key = their lowercase first name (jakub, not
jveverka / kuba). Every command prints a speaker integrity report. `pack` REFUSES (exit 4)
to build a pack containing an AI-clone speaker without real recordings, because the game
could never link that clone to the real person. Fix it with --alias OLD=NEW (rewrite a
speaker key while packing, e.g. --alias jveverka=jakub) or, for a synthetic voice with no
real person behind it, --allow-ai-only NAME (or "ai_only": true on the speaker in
manifest.json). Known Coqui XTTS stock voices (claribel, daisy, ...) are recognised.

The token can also come from $VOICEID_ADMIN_TOKEN. Stdlib only. TLS verification is
off by default (self-signed demo routes); pass --verify-tls to enable it.
"""
import argparse
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

DEFAULT_OUT = Path.home() / "voice-id-audio" / "seedpacks"
DEFAULT_SAMPLES = Path.home() / "voice-id-audio" / "samples"
TIMEOUT = 1800  # large packs over slow links


def git_toplevel(path):
    """Return the git working tree containing path (or its nearest existing parent), else None."""
    p = Path(path).expanduser().resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        out = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def git_ignored(path):
    """True if path is git-ignored in the repo that contains it (e.g. samples/)."""
    top = git_toplevel(path)
    if not top:
        return False
    p = str(Path(path).expanduser().resolve())
    try:
        return subprocess.run(["git", "-C", top, "check-ignore", "-q", p],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def refuse_in_git(target, force, allow_ignored=False, what="Seed packs"):
    """Return an exit code (3) if target is inside a git working tree, else None."""
    top = git_toplevel(target)
    if not top or force or (allow_ignored and git_ignored(target)):
        return None
    hint = " and not git-ignored" if allow_ignored else ""
    print(f"REFUSING: {target} is inside the git working tree {top}{hint}.\n"
          f"{what} contain real voices and must never be committed. Choose a directory outside the repo"
          f"{' (or a git-ignored one such as samples/)' if allow_ignored else ''}, or pass --force.",
          file=sys.stderr)
    return 3


# --- Offline pack/unpack (folder layout of load_samples.py) ---

SEEDPACK_FORMAT = "voice-id-seedpack"
SEEDPACK_VERSION = 1
MAX_ENTRY = 50 * 1024 * 1024          # server per-clip limit
MAX_PHOTO = 5 * 1024 * 1024           # server photo limit
MAX_ENTRIES = 20000
AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".webm"}
# Same mapping as load_samples.py so packed clips match existing game metadata.
AI_MODELS = {"fish", "xtts", "moss", "qwen", "higgs", "coqui"}
MODEL_DISPLAY_NAMES = {
    "fish": "Fish Audio S2 Pro",
    "xtts": "Coqui XTTS v2",
    "moss": "MOSS-TTS-Nano",
    "qwen": "Qwen3-TTS",
    "higgs": "Higgs Audio v3",
    "coqui": "Coqui XTTS v2",
}
SOURCE_TO_MODEL = {"Fish Audio S2 Pro": "fish", "Coqui XTTS v2": "xtts", "MOSS-TTS-Nano": "moss",
                   "Qwen3-TTS": "qwen", "Higgs Audio v3": "higgs"}
AUDIO_EXT = {"audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
             "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/webm": ".webm"}
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
LANG_RE = re.compile(r"[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})?")


# --- Speaker identity integrity (keep in sync with speaker_integrity() in src/app.py) ---

XTTS_STOCK_VOICES = frozenset("""
aaron abrahan adde alexandra alison alma ana andrew annmarie asya badr baldur barbora brenda
camilla chandra claribel craig daisy damien damjan dionisio eugenio ferran filip gilberto gitta
gracie henriette ige ilkin kazuhiko kumar lidiya lilya ludvig luis maja marcos narelle nova
rosemary royston sofia suad szofi tammie tammy tanja torcull uta viktor vjollca wulf xavier
zacharie zofija
""".split())


def _fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(ch for ch in s if ch.isalnum() and not unicodedata.combining(ch))


def _levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _alias_reason(key, other, other_display, loose=False):
    a, b = _fold(key), _fold(other)
    if not a or not b:
        return None
    if a == b:
        return "same name without accents"
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        return "prefix match"
    if _levenshtein(a, b) <= max(1, min(len(a), len(b)) // 4):
        return "similar spelling"
    for tok in (other_display or "").split():
        t = _fold(tok)
        if len(t) >= 3 and t != b and (t in a or a in t):
            return f"matches display name '{other_display}'"
    if loose and len(a) >= 5 and a[0] == b[0] and len(a) > len(b) and not a.startswith(b):
        return "same initial, surname-style key"
    return None


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def speaker_integrity(speakers):
    """speakers: {key: {human, ai, sources:set, ai_only:bool, display:str}} -> report dict."""
    def is_stock(k, v):
        return k in XTTS_STOCK_VOICES and v["sources"] and all("xtts" in (s or "").lower() for s in v["sources"])

    def reason(x, y, loose=False):
        return (_alias_reason(x, y, speakers[y]["display"], loose)
                or _alias_reason(y, x, speakers[x]["display"], loose))

    warnings, stock, flagged, covered = [], [], [], set()
    for k in sorted(speakers):
        v = speakers[k]
        if not (v["ai"] and not v["human"]):
            continue
        if v["ai_only"]:
            flagged.append(k)
        elif is_stock(k, v):
            stock.append(k)
        else:
            sugg = [{"speaker": o, "reason": r} for o in sorted(speakers)
                    if o != k and speakers[o]["human"] and not speakers[o]["ai"] for r in [reason(o, k, True)] if r]
            covered.update(s["speaker"] for s in sugg)
            msg = f"{_plural(v['ai'], 'AI clip')}, no real recordings."
            msg += (" Possibly the same person as " + ", ".join(f"{s['speaker']} ({s['reason']})" for s in sugg)
                    + " - merge." if sugg else f" Merge the person's real recordings into '{k}', or mark it AI-only voice.")
            warnings.append({"speaker": k, "kind": "ai_without_human", "human_clips": 0, "ai_clips": v["ai"],
                             "message": msg, "suggestions": sugg})
    for k in sorted(speakers):
        v = speakers[k]
        if not (v["human"] and not v["ai"]) or k in covered:
            continue
        # AI-only voices that are resolved (stock / flagged) are not real people -> no alias targets
        sugg = [{"speaker": o, "reason": r} for o in sorted(speakers)
                if o != k and speakers[o]["ai"] and o not in stock and o not in flagged
                for r in [reason(k, o)] if r]
        if sugg:
            warnings.append({
                "speaker": k, "kind": "human_without_ai_match", "human_clips": v["human"], "ai_clips": 0,
                "message": f"{_plural(v['human'], 'real recording')}, no AI clips. Possibly the same person as "
                           + ", ".join(f"{s['speaker']} ({s['reason']})" for s in sugg) + ".",
                "suggestions": sugg})
    return {"ok": not warnings, "warnings": warnings, "stock_voices": stock, "ai_only": flagged}


def integrity_from_clips(clips, speaker_meta=None, extra_ai_only=()):
    """clips: [{is_ai, speaker, source}], speaker_meta: {key: {display_name, ai_only}}."""
    speaker_meta = speaker_meta or {}
    sp = {}
    for c in clips:
        k = (c.get("speaker") or "").strip().lower()
        if not k:
            continue
        v = sp.setdefault(k, {"human": 0, "ai": 0, "sources": set(), "ai_only": False, "display": ""})
        if c.get("is_ai"):
            v["ai"] += 1
            v["sources"].add(c.get("source") or "")
        else:
            v["human"] += 1
    extra = {n.strip().lower() for n in extra_ai_only}
    for k, v in sp.items():
        m = speaker_meta.get(k) or {}
        v["display"] = m.get("display_name") or ""
        v["ai_only"] = bool(m.get("ai_only")) or k in extra
    return speaker_integrity(sp)


def print_integrity(report, out=sys.stdout):
    """Human-readable speaker integrity report. Returns the number of warnings."""
    ws = report.get("warnings") or []
    print("\nSpeaker integrity check (one person = one speaker key, lowercase first name):", file=out)
    if report.get("stock_voices"):
        print(f"  ok  XTTS stock voices (no real person): {', '.join(report['stock_voices'])}", file=out)
    if report.get("ai_only"):
        print(f"  ok  marked AI-only voice: {', '.join(report['ai_only'])}", file=out)
    if not ws:
        print("  ok  every AI clone is linked to a real person's recordings.", file=out)
        return 0
    for w in ws:
        print(f"  WARNING {w['speaker']}: {w['message']}", file=out)
    print("  Resolve before the booth opens: merge aliases (admin > Speakers > Merge, or pack --alias OLD=NEW)\n"
          "  or mark synthetic voices AI-only (admin checkbox, or pack --allow-ai-only NAME).", file=out)
    return len(ws)


def sniff_audio(data):
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    return None


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def slug(s, keep_dash=True, maxlen=80):
    """Filesystem-safe name component (unicode letters kept). No path separators, no leading dots."""
    pat = r"[^\w.-]+" if keep_dash else r"[^\w.]+"
    out = re.sub(pat, "_", (s or "").strip()).strip("._")
    if not keep_dash:
        out = out.replace("-", "_")
    return out[:maxlen]


def parse_filename(filename, is_ai):
    """Identical to load_samples.parse_filename."""
    parts = Path(filename).stem.split("-", 2)
    source = speaker = ""
    if is_ai and len(parts) >= 2:
        if parts[0].lower() in AI_MODELS:
            source, speaker = parts[0], parts[1]
        else:
            source, speaker = "unknown", parts[0]
    elif not is_ai and len(parts) >= 1:
        speaker = parts[0]
    return MODEL_DISPLAY_NAMES.get(source, source), speaker


def layout_path(clip, ctype):
    """Local relative path in the samples/ layout for a manifest clip entry."""
    cat = "ai" if clip.get("is_ai") else "natural"
    lang = clip.get("language") or "xx"
    lang = lang if LANG_RE.fullmatch(lang) else "xx"
    ext = AUDIO_EXT.get(ctype, ".wav")
    fn = (clip.get("filename") or "").replace("\\", "/")
    parts = fn.split("/")
    # Reuse the manifest filename when it already follows <cat>/<lang>/<name>.<ext>
    # (AI: <known model>-<speaker>-..., so load_samples.py parses it the same way)
    if len(parts) == 3 and parts[0] == cat and parts[1] == lang:
        base = slug(parts[2])
        stem, e = os.path.splitext(base)
        bits = stem.split("-")
        follows = cat == "natural" or (len(bits) >= 2 and bits[0].lower() in AI_MODELS)
        if stem and e.lower() in AUDIO_EXTENSIONS and follows:
            return f"{cat}/{lang}/{base}"
    # Otherwise derive {model}-{speaker}-{desc} / {speaker}-{desc} (no dashes inside fields)
    speaker = slug(clip.get("speaker"), keep_dash=False) or "unknown"
    desc = slug(Path(parts[-1]).stem, keep_dash=True) or (clip.get("sha256") or "clip")[:8]
    if cat == "ai":
        src = clip.get("source") or ""
        model = SOURCE_TO_MODEL.get(src) or slug(src, keep_dash=False).lower() or "unknown"
        return f"ai/{lang}/{model}-{speaker}-{desc}{ext}"
    return f"natural/{lang}/{speaker}-{desc}{ext}"


def inside(base, rel):
    """Resolve rel under base; raise if it would escape base."""
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"unsafe path {rel!r}")
    return target


def read_capped(zf, name, limit):
    with zf.open(name) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{name}: exceeds {limit} bytes")
    return data


def write_file(target, data, force):
    """-> 'written' | 'identical' | 'conflict' | 'overwritten'."""
    if target.exists():
        if hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(data).digest():
            return "identical"
        if not force:
            return "conflict"
        status = "overwritten"
    else:
        status = "written"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return status


def cmd_unpack(args):
    pack = Path(args.file).expanduser()
    out = Path(args.out).expanduser()
    rc = refuse_in_git(out, args.force, allow_ignored=True, what="Sample folders")
    if rc:
        return rc
    if not zipfile.is_zipfile(pack):
        print(f"Not a zip file: {pack}", file=sys.stderr)
        return 2
    counts = {"written": 0, "identical": 0, "conflict": 0, "overwritten": 0, "invalid": 0}
    errors = []
    with zipfile.ZipFile(pack) as zf:
        names = set(zf.namelist())
        if len(names) > MAX_ENTRIES + 1:
            print("Too many entries in pack", file=sys.stderr)
            return 2
        try:
            manifest = json.loads(read_capped(zf, "manifest.json", 20 * 1024 * 1024))
        except KeyError:
            print("manifest.json missing - not a Voice ID seed pack", file=sys.stderr)
            return 2
        if manifest.get("format") != SEEDPACK_FORMAT or manifest.get("version") != SEEDPACK_VERSION:
            print(f"Unsupported pack (format={manifest.get('format')!r}, version={manifest.get('version')!r})",
                  file=sys.stderr)
            return 2
        out.mkdir(parents=True, exist_ok=True)
        base = out.resolve()
        used = set()
        clips_out, speakers_out = [], []
        for c in manifest.get("clips", []):
            entry = dict(c)
            try:
                if c.get("file") not in names:
                    raise ValueError(f"{c.get('filename')}: entry missing in zip")
                data = read_capped(zf, c["file"], MAX_ENTRY)
                if hashlib.sha256(data).hexdigest() != c.get("sha256") or len(data) != c.get("bytes"):
                    raise ValueError(f"{c.get('filename')}: sha256/size mismatch")
                ctype = sniff_audio(data)
                if not ctype:
                    raise ValueError(f"{c.get('filename')}: not audio")
                rel = layout_path(c, ctype)
                if rel in used:  # two clips mapping to one name: disambiguate deterministically
                    stem, ext = os.path.splitext(rel)
                    rel = f"{stem}_{c['sha256'][:8]}{ext}"
                used.add(rel)
                status = write_file(inside(base, rel), data, args.force)
                counts[status] += 1
                if status == "conflict":
                    errors.append(f"exists with different content (use --force): {rel}")
                entry["local_path"] = rel
            except (ValueError, KeyError) as e:
                counts["invalid"] += 1
                errors.append(str(e))
                entry["local_path"] = None
            clips_out.append(entry)
        for sp in manifest.get("speakers", []):
            entry = dict(sp, local_path=None)
            pf = sp.get("photo_file")
            if pf:
                try:
                    if pf not in names:
                        raise ValueError(f"speaker {sp.get('name')}: photo missing in zip")
                    data = read_capped(zf, pf, MAX_PHOTO)
                    itype = sniff_image(data)
                    name = slug(sp.get("name"))
                    if not itype or not name:
                        raise ValueError(f"speaker {sp.get('name')}: invalid photo or name")
                    rel = f"speakers/{name}{IMAGE_EXT[itype]}"
                    status = write_file(inside(base, rel), data, args.force)
                    counts[status] += 1
                    if status == "conflict":
                        errors.append(f"exists with different content (use --force): {rel}")
                    entry["local_path"] = rel
                except (ValueError, KeyError) as e:
                    counts["invalid"] += 1
                    errors.append(str(e))
            speakers_out.append(entry)
        manifest_out = dict(manifest, clips=clips_out, speakers=speakers_out,
                            unpacked_from=pack.name, unpacked_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        (base / "manifest.json").write_text(json.dumps(manifest_out, indent=2, ensure_ascii=False))
    print(f"Unpacked {pack.name} -> {base}")
    print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    for e in errors[:50]:
        print(f"  - {e}")
    print("Contains real voice recordings: keep this folder private and out of git.")
    return 1 if counts["invalid"] or counts["conflict"] else 0


def cmd_pack(args):
    src = Path(args.dir).expanduser().resolve()
    out = Path(args.out).expanduser()
    if not src.is_dir():
        print(f"No such directory: {src}", file=sys.stderr)
        return 2
    rc = refuse_in_git(out, args.force)
    if rc:
        return rc
    aliases = {}
    for a in args.alias or []:
        old, sep, new = a.partition("=")
        old, new = old.strip().lower(), new.strip().lower()
        if not sep or not old or not new or old == new or "/" in new or "\\" in new or len(new) > 64:
            print(f"Invalid --alias {a!r}: use OLD=NEW, e.g. --alias jveverka=jakub", file=sys.stderr)
            return 2
        aliases[old] = new
    alias = lambda k: aliases.get((k or "").strip().lower(), (k or "").strip().lower())  # noqa: E731
    meta, spk_meta = {}, {}
    mpath = src / "manifest.json"
    if mpath.is_file():
        m = json.loads(mpath.read_text())
        for c in m.get("clips", []):
            if c.get("local_path"):
                meta[c["local_path"]] = c
        aliased = []
        for sp in m.get("speakers", []):
            if isinstance(sp.get("name"), str):
                key = alias(sp["name"])
                if key != sp["name"].strip().lower():
                    aliased.append(dict(sp, name=key))
                else:
                    spk_meta[key] = dict(sp, name=key)
        for sp in aliased:  # target keeps its own display name/photo; gaps filled from the alias
            cur = spk_meta.setdefault(sp["name"], {"name": sp["name"]})
            for k, v in sp.items():
                if v and not cur.get(k):
                    cur[k] = v
        print(f"Using metadata from {mpath} ({len(meta)} clips, {len(spk_meta)} speakers)")
    else:
        print("No manifest.json - deriving metadata from the path convention (like load_samples.py)")

    clips, errors, files = [], [], []
    for cat in ("ai", "natural"):
        cdir = src / cat
        if not cdir.is_dir():
            continue
        for lang_dir in sorted(p for p in cdir.iterdir() if p.is_dir()):
            for f in sorted(lang_dir.iterdir()):
                if f.is_file() and not f.is_symlink() and f.suffix.lower() in AUDIO_EXTENSIONS:
                    files.append((cat, lang_dir.name, f))
    if len(files) > MAX_ENTRIES:
        print(f"Too many files ({len(files)} > {MAX_ENTRIES})", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"voice-id-seedpack-{time.strftime('%Y%m%d-%H%M')}.seedpack.zip"
    part = dest.with_name(dest.name + ".part")
    speakers = []
    renamed = {}
    allow = {n.strip().lower() for n in (args.allow_ai_only or [])}
    with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for i, (cat, lang, f) in enumerate(files):
            rel = f.relative_to(src).as_posix()
            if f.stat().st_size > MAX_ENTRY:
                errors.append(f"{rel}: larger than 50 MB, skipped")
                continue
            data = f.read_bytes()
            ctype = sniff_audio(data)
            if not ctype:
                errors.append(f"{rel}: not a supported audio file, skipped")
                continue
            m = meta.get(rel)
            if m:
                entry = {k: m.get(k) for k in ("filename", "is_ai", "language", "source", "speaker")}
            else:
                is_ai = cat == "ai"
                source, speaker = parse_filename(f.name, is_ai)
                entry = {"filename": rel, "is_ai": int(is_ai), "language": lang,
                         "source": source, "speaker": speaker}
            entry["is_ai"] = int(bool(entry["is_ai"]))
            entry["source"] = entry.get("source") or ""
            entry["speaker"] = entry.get("speaker") or ""
            if entry["speaker"].strip().lower() in aliases:
                entry["speaker"] = aliases[entry["speaker"].strip().lower()]
                renamed[entry["speaker"]] = renamed.get(entry["speaker"], 0) + 1
            if not LANG_RE.fullmatch(entry.get("language") or ""):
                errors.append(f"{rel}: invalid language {entry.get('language')!r}, skipped")
                continue
            arc = f"clips/{i:05d}_{slug(entry['filename'].replace('/', '__'))[-120:] or 'clip'}"
            zf.writestr(arc, data)
            entry.update(content_type=ctype, sha256=hashlib.sha256(data).hexdigest(),
                         bytes=len(data), file=arc)
            clips.append(entry)
        # speakers: manifest entries + any speakers/<name>.<ext> files
        photos = {}
        sdir = src / "speakers"
        if sdir.is_dir():
            for f in sorted(sdir.iterdir()):
                if f.is_file() and not f.is_symlink() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    stem = f.stem.strip().lower()
                    if stem in aliases and aliases[stem] in photos:
                        continue  # the target's own photo wins
                    photos[alias(stem)] = f
        for sp in spk_meta.values():
            lp = sp.get("local_path")
            if lp:
                try:
                    p = inside(src, lp)
                    for k in [k for k, v in photos.items() if v.resolve() == p]:
                        del photos[k]  # file is claimed by this manifest speaker, not by its stem
                    photos[sp["name"]] = p
                except ValueError:
                    errors.append(f"speaker {sp['name']}: unsafe photo path {lp!r}")
        for j, name in enumerate(sorted(set(spk_meta) | set(photos))):
            if not name or len(name) > 64 or "/" in name or "\\" in name:
                errors.append(f"speaker {name!r}: invalid name, skipped")
                continue
            entry = {"name": name, "display_name": (spk_meta.get(name) or {}).get("display_name"),
                     "photo_file": None, "photo_type": None,
                     "ai_only": bool((spk_meta.get(name) or {}).get("ai_only")) or name in allow}
            p = photos.get(name)
            if p and p.is_file():
                data = p.read_bytes()
                itype = sniff_image(data) if len(data) <= MAX_PHOTO else None
                if itype:
                    entry["photo_file"] = f"speakers/{j:04d}_{slug(name, maxlen=60) or 'speaker'}{IMAGE_EXT[itype]}"
                    entry["photo_type"] = itype
                    zf.writestr(entry["photo_file"], data)
                else:
                    errors.append(f"speaker {name}: photo is not JPEG/PNG/WebP or > 5 MB, skipped")
            if entry["display_name"] or entry["photo_file"] or entry["ai_only"]:
                speakers.append(entry)
        for name in sorted(allow - {s["name"] for s in speakers}):
            speakers.append({"name": name, "display_name": None, "photo_file": None, "photo_type": None,
                             "ai_only": True})
        zf.writestr("manifest.json", json.dumps({
            "format": SEEDPACK_FORMAT, "version": SEEDPACK_VERSION,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "app_version": None, "packed_from": "folder",
            "clips": clips, "speakers": speakers,
        }, indent=2, ensure_ascii=False))
    for old, new in aliases.items():
        print(f"Alias {old} -> {new}")
    if renamed:
        print("  rewritten clips: " + ", ".join(f"{k}={v}" for k, v in sorted(renamed.items())))
    report = integrity_from_clips(clips, {s["name"]: s for s in speakers})
    print_integrity(report)
    blocking = [w["speaker"] for w in report["warnings"] if w["kind"] == "ai_without_human"]
    if blocking:
        part.unlink(missing_ok=True)
        print(f"\nREFUSING to pack: AI clone speaker(s) without real recordings in this pack: {', '.join(blocking)}.\n"
              "The game would show these clones as unrelated to any real person. Either add/alias the person's\n"
              "real recordings (--alias SURNAME=firstname) or, if it is a synthetic voice with no real person,\n"
              f"pass --allow-ai-only {blocking[0]} (repeatable). No pack was written.", file=sys.stderr)
        return 4
    os.replace(part, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    print(f"\nPacked {len(clips)} clips, {len(speakers)} speaker profiles -> {dest}")
    for e in errors[:50]:
        print(f"  - {e}")
    print("Contains real voice recordings: share privately, never commit or publish.")
    return 1 if errors else 0


def ssl_ctx(verify):
    return None if verify else ssl._create_unverified_context()


def http_error(e):
    body = e.read()[:500].decode("utf-8", "replace")
    try:
        return e.code, json.loads(body)
    except ValueError:
        return e.code, {"error": body}


def cmd_export(args):
    out_dir = Path(args.out).expanduser()
    rc = refuse_in_git(out_dir, args.force)
    if rc:
        return rc
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        ireq = urllib.request.Request(args.game.rstrip("/") + "/api/admin/speakers/integrity",
                                      headers={"Authorization": f"Bearer {args.token}"})
        with urllib.request.urlopen(ireq, timeout=60, context=ssl_ctx(args.verify_tls)) as resp:
            if print_integrity(json.loads(resp.read())):
                print("  -> Exporting anyway; the receiving booth will see the same warnings.\n")
    except (urllib.error.URLError, OSError, ValueError):
        print("(Speaker integrity report unavailable on this server version.)")
    req = urllib.request.Request(args.game.rstrip("/") + "/api/admin/seedpack",
                                 headers={"Authorization": f"Bearer {args.token}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_ctx(args.verify_tls)) as resp:
            m = re.search(r'filename="?([^";]+)"?', resp.headers.get("Content-Disposition", ""))
            name = os.path.basename(m.group(1)) if m else f"voice-id-seedpack-{time.strftime('%Y%m%d-%H%M')}.seedpack.zip"
            dest = out_dir / name
            part = dest.with_suffix(dest.suffix + ".part")
            size = 0
            with open(part, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
            part.rename(dest)
    except urllib.error.HTTPError as e:
        code, body = http_error(e)
        print(f"Export failed: HTTP {code}: {body.get('error', body)}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"Export failed: {getattr(e, 'reason', e)}", file=sys.stderr)
        return 1
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    print(f"Saved {dest} ({size / 1e6:.1f} MB in {time.time() - t0:.0f}s)")
    print("Contains real voice recordings: share privately, never commit or publish.")
    return 0


def cmd_import(args):
    pack = Path(args.file).expanduser()
    if not pack.is_file():
        print(f"No such file: {pack}", file=sys.stderr)
        return 2
    boundary = "----seedpack" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\n{args.mode}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"seedpack\"; filename=\"{pack.name}\"\r\n"
            f"Content-Type: application/zip\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = pack.stat().st_size

    def body():  # stream the file, never load the whole pack into memory
        yield head
        with open(pack, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        yield tail

    req = urllib.request.Request(args.game.rstrip("/") + "/api/admin/seedpack", data=body(), method="POST",
                                 headers={"Authorization": f"Bearer {args.token}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}",
                                          "Content-Length": str(len(head) + size + len(tail))})
    print(f"Uploading {pack.name} ({size / 1e6:.1f} MB), mode={args.mode} ...", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_ctx(args.verify_tls)) as resp:
            code, result = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        code, result = http_error(e)
    except (urllib.error.URLError, OSError) as e:
        print(f"Import failed: {getattr(e, 'reason', e)}", file=sys.stderr)
        return 1
    print(f"HTTP {code} - {result.get('status', result.get('error', '?'))}")
    for section in ("clips", "speakers"):
        if isinstance(result.get(section), dict):
            print(f"  {section:<9} " + "  ".join(f"{k}={v}" for k, v in result[section].items()))
    errs = result.get("errors") or []
    for e in errs[:50]:
        print(f"  - {e}")
    if len(errs) > 50:
        print(f"  ... and {len(errs) - 50} more")
    if isinstance(result.get("integrity"), dict):
        if print_integrity(result["integrity"]):
            print("  -> Fix these in the game admin (/admin#speakers) before the booth opens.")
    elif code == 200:
        print("\n(Server too old to report speaker integrity - check /admin#speakers manually.)")
    return 0 if code == 200 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "import"):
        sp = sub.add_parser(name)
        sp.add_argument("--game", required=True, help="Voice ID base URL")
        sp.add_argument("--token", default=os.environ.get("VOICEID_ADMIN_TOKEN", ""),
                        help="admin token (or $VOICEID_ADMIN_TOKEN)")
        sp.add_argument("--verify-tls", action="store_true")
        if name == "export":
            sp.add_argument("--out", default=str(DEFAULT_OUT), help=f"output dir (default {DEFAULT_OUT})")
            sp.add_argument("--force", action="store_true", help="allow writing inside a git working tree")
        else:
            sp.add_argument("--file", required=True, help="seed pack zip")
            sp.add_argument("--mode", choices=["skip", "replace"], default="skip")
    sp = sub.add_parser("unpack", help="offline: pack -> samples/ folder layout")
    sp.add_argument("--file", required=True, help="seed pack zip")
    sp.add_argument("--out", default=str(DEFAULT_SAMPLES), help=f"target folder (default {DEFAULT_SAMPLES})")
    sp.add_argument("--force", action="store_true",
                    help="allow a non-ignored git working tree and overwrite differing files")
    sp = sub.add_parser("pack", help="offline: samples/ folder layout -> pack")
    sp.add_argument("--dir", required=True, help="folder with ai/ natural/ [speakers/] [manifest.json]")
    sp.add_argument("--alias", action="append", metavar="OLD=NEW",
                    help="rewrite speaker key OLD to NEW while packing (repeatable), e.g. jveverka=jakub")
    sp.add_argument("--allow-ai-only", action="append", metavar="NAME",
                    help="speaker NAME is a synthetic voice with no real person (repeatable)")
    sp.add_argument("--out", default=str(DEFAULT_OUT), help=f"output dir (default {DEFAULT_OUT})")
    sp.add_argument("--force", action="store_true", help="allow writing inside a git working tree")
    args = ap.parse_args(argv)
    if args.cmd == "unpack":
        return cmd_unpack(args)
    if args.cmd == "pack":
        return cmd_pack(args)
    if not args.token:
        ap.error("--token or $VOICEID_ADMIN_TOKEN required")
    return cmd_export(args) if args.cmd == "export" else cmd_import(args)


if __name__ == "__main__":
    sys.exit(main())
