import requests
import json
import base64
import os
import glob

API = 'http://localhost:8080'

print('Loading studio speakers...')
speakers = requests.get(f'{API}/studio_speakers', timeout=120).json()
names = list(speakers.keys())
print(f'Available: {len(names)} speakers')

used = {'damien black', 'filip traverse', 'barbora macphail', 'sofia hellen', 'ana florence', 'marcos rudaski'}
new_presets = {}
for n in names:
    if n.lower() not in used:
        new_presets[n] = speakers[n]
    if len(new_presets) >= 4:
        break
print(f'New preset speakers: {list(new_presets.keys())}')

# Clone speakers — upload reference audio from /tmp/clone_refs/
clone_speakers = {}
clone_ref_dir = '/tmp/clone_refs'
os.makedirs(clone_ref_dir, exist_ok=True)

# Check for existing reference files
for name in ['anton', 'miska']:
    refs = glob.glob(f'{clone_ref_dir}/{name}*')
    if refs:
        ref_path = refs[0]
        print(f'Found clone ref for {name}: {ref_path}')
        try:
            with open(ref_path, 'rb') as f:
                r = requests.post(f'{API}/clone_speaker', files={'wav_file': (f'{name}.wav', f, 'audio/wav')}, timeout=60)
            if r.status_code == 200:
                clone_speakers[name] = r.json()
                print(f'  Clone speaker {name} loaded OK')
            else:
                print(f'  Clone upload failed: {r.status_code}')
        except Exception as e:
            print(f'  Clone error: {e}')
    else:
        print(f'No clone ref for {name} in {clone_ref_dir}')

preset_names = list(new_presets.keys())

tasks = [
    # English - new preset voices
    {'text': 'Automation frees time for innovation', 'lang': 'en', 'outname': 'automation', 'speaker': preset_names[0], 'type': 'preset'},
    {'text': 'Cloud solutions increase efficiency', 'lang': 'en', 'outname': 'cloud', 'speaker': preset_names[1], 'type': 'preset'},
    {'text': 'Security is priority in cloud environment', 'lang': 'en', 'outname': 'security', 'speaker': preset_names[2], 'type': 'preset'},
    # Czech - new preset voices
    {'text': 'Umela inteligence transformuje byznys', 'lang': 'cs', 'outname': 'inteligence', 'speaker': preset_names[3], 'type': 'preset'},
    {'text': 'Cloudova reseni zvysuji efektivitu', 'lang': 'cs', 'outname': 'efektivita', 'speaker': preset_names[0], 'type': 'preset'},
    # English - clones
    {'text': 'Red Hat leads in open source innovation', 'lang': 'en', 'outname': 'redhat', 'speaker': 'anton', 'type': 'clone'},
    {'text': 'Containerization is the future of cloud', 'lang': 'en', 'outname': 'container', 'speaker': 'miska', 'type': 'clone'},
    # Czech - clones
    {'text': 'DevOps spojuje vyvojare a operace', 'lang': 'cs', 'outname': 'devops2', 'speaker': 'anton', 'type': 'clone'},
    {'text': 'OpenShift je skvelý produkt', 'lang': 'cs', 'outname': 'openshift2', 'speaker': 'miska', 'type': 'clone'},
]

results = []

for task in tasks:
    speaker = task['speaker']
    stype = task['type']

    if stype == 'clone' and speaker not in clone_speakers:
        print(f'Skipping {task["outname"]} - no clone ref for {speaker}')
        continue

    if stype == 'preset':
        if speaker not in speakers:
            print(f'Skipping {task["outname"]} - speaker {speaker} not found')
            continue
        embedding = speakers[speaker]
        short_name = speaker.split()[0].lower()
    else:
        embedding = clone_speakers[speaker]
        short_name = speaker

    fname = f'xtts-{short_name}-{task["outname"]}.wav'
    print(f'Generating {fname} [{task["lang"]}]: {task["text"]}')

    try:
        payload = {
            'text': task['text'],
            'language': task['lang'],
            'speaker_embedding': embedding['speaker_embedding'],
            'gpt_cond_latent': embedding['gpt_cond_latent'],
        }
        r = requests.post(f'{API}/tts', json=payload, timeout=120)

        if r.status_code == 200:
            raw = r.content
            # Response is base64-encoded WAV in JSON string
            if raw[0:1] == b'"':
                raw = json.loads(raw)
                padding = 4 - len(raw) % 4 if len(raw) % 4 else 0
                raw = base64.b64decode(raw + '=' * padding)

            outpath = f'/tmp/{fname}'
            with open(outpath, 'wb') as f:
                f.write(raw)
            print(f'  OK: {len(raw)} bytes')
            results.append(fname)
        else:
            print(f'  FAILED: {r.status_code} {r.text[:200]}')
    except Exception as e:
        print(f'  ERROR: {e}')

print(f'\nGenerated {len(results)}/{len(tasks)}:')
for f in results:
    print(f'  /tmp/{f}')
