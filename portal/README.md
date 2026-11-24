# Voice ID portal

Public overview page for the Voice ID booth game: what it is and why, how to play,
the AI opponents, scoring with worked examples, architecture and installation links.

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

Brand: Red Hat palette only (red #ee0000/#a60000, greys, white/black; teal #147878
means human, purple #5e40be means AI), Red Hat Display/Text embedded from
`public/fonts` (SIL OFL, see `public/fonts/OFL.txt`), official Red Hat standard
icons in `public/icons` (CC BY 4.0, see `public/icons/ICON-LICENSE`).
