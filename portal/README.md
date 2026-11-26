# Voice ID portal

Public overview page for the Voice ID booth game: what it is and why, an animated
round in the hero, how to play, the AI opponents, scoring with worked examples,
architecture and installation links.

```bash
npm ci
npm run dev      # http://localhost:5173
npm run build    # static output in dist/
```

Deployed by `.github/workflows/portal.yml` to GitHub Pages on every push to `main`
that touches `portal/`. The site is public:

- never add cluster URLs, tokens or passwords;
- **never add real voice recordings** (no audio files at all unless the owner
  explicitly approves a synthetic placeholder).

`src/scoring.js` mirrors the scoring constants in
`apps/voice-id/src/app.py` (scoring is server-side) and `docs/customization/scoring.md`; update
all three together.

`src/components/RoundDemo.jsx` is the animated hero: a scripted mock of one solo round
(progress dots, mystery speaker, play, AI/Human buttons, reveal, points caption). It
mirrors `#screen-game` in `apps/voice-id/src/templates/index.html` and its English
strings in `helm/voice-id/i18n.json`; when the game screen or wording changes, update
the mock in the same change. Its speaker ("Jana") is fictitious on purpose: never put a
real speaker's name, photo or voice in it. It pauses on demand and shows a static reveal
under `prefers-reduced-motion`.

Brand: Red Hat palette only (red #ee0000/#a60000, greys, white/black; teal #147878
means human, purple #5e40be means AI; the hero mock also uses the game's right/wrong
green #3d7317, the same owner-approved exception as in the game), Red Hat Display/Text embedded from
`public/fonts` (SIL OFL, see `public/fonts/OFL.txt`), official Red Hat standard
icons in `public/icons` (CC BY 4.0, see `public/icons/ICON-LICENSE`).
