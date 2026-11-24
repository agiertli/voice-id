# Languages and UI text

Two separate things are called "language" in this game:

| | Where it lives | Change needs |
|---|---|---|
| **UI language** (buttons, rules, opponent cards, quotes) | `apps/voice-id/helm/voice-id/i18n.json` → ConfigMap `voice-id-config` → `/api/i18n` | Helm upgrade only |
| **Clip language** (what the voices speak) | `language` column of each clip | Uploading clips |

Games draw clips from **all** languages in the database, independent of the UI
language. A Czech-speaking visitor using the Czech UI can still get English and
Slovak clips. To run a single-language event, load only that language's clips.

## UI languages shipped

`en`, `cs`, `sk`, each with the same set of keys (137 at the time of writing). The
language switcher (EN / CS / SK) is in the game header; the choice is remembered in
the browser (`localStorage`). A new browser starts with `game.defaultLang`:

```yaml
game:
  defaultLang: "cs"     # en | cs | sk
```

Missing keys fall back to English, then to the key name itself.

## Edit text

1. Edit `apps/voice-id/helm/voice-id/i18n.json`. Keep the JSON valid and the same
   keys in every language.
2. Placeholders are replaced at runtime: `{game_size}`, `{prize_threshold}`,
   `{name}`, `{score}`, `{current}`, `{total}`, `{n}`, `{speaker}`. Keep them.
3. Validate and apply:

```bash
python3 -m json.tool apps/voice-id/helm/voice-id/i18n.json >/dev/null && echo valid
helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id \
  -f apps/voice-id/helm/voice-id/values.yaml          # plus your adminToken override
oc rollout restart deploy/voice-id -n voice-id
```

The ConfigMap change alone does not restart the pod. The app reads `i18n.json` on
every `/api/i18n` request, so the projected file update (up to about a minute
after the upgrade) is picked up without a restart; the rollout restart makes it
immediate. Players need to reload the page.

Useful keys: `rule_prize`/`prize_won`/`no_prize` (prize wording), `rule_clips`
(mentions the clip languages; update it if you load other languages),
`opponent_<id>_name|label|bio|model|method` and `difficulty_*` (opponent cards).
Opponent ids use underscores in keys: `gary_base`, `gary_finetuned`.

## Add a UI language

1. Copy the `en` block in `i18n.json` to a new code (for example `de`) and
   translate every value.
2. The language buttons are hard-coded in
   `apps/voice-id/src/templates/index.html` (`<button class="lang-btn" data-lang="...">`).
   A new button needs an HTML change and a **new image** (owned by the app
   developers). Without it, the new language is reachable only through
   `game.defaultLang`.
3. Opponent and difficulty labels on `/tv` are hard-coded English in `tv.html`.

## Add clips in a new spoken language

1. Only Fish S2 Pro and XTTS v2 produced usable Czech voice clones among nine
   tested engines; check engine support for the target language first. XTTS v2 has
   no Slovak.
2. Record human sentences and generate AI versions of the **same** sentences.
   Clone Studio's sentence mode offers only `cs`, `sk` and `en`
   (`SENTENCES` in `apps/voice-clone-studio/src/app.py`); a new language there
   needs a code change.
3. Upload with the clip's language code ([clips and speakers](clips-and-speakers.md)).
4. Expect the opponents' accuracy to change: Gary 2.0 was trained on cs/sk/en voices.
