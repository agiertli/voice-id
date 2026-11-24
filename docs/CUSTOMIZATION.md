# Customize and operate the game

Start with the task you need. Game settings live in
`apps/voice-id/helm/voice-id/values.yaml` (or your own copy passed with `-f`).
Helm-only changes need no image rebuild. Always upgrade with `-f <values>` and your
`adminToken` override; a bare `helm upgrade` reuses old values.

- [Scoring](customization/scoring.md): points formula, speed table, worked solo and
  battle examples, prize vs leaderboard rank, ties, replays, configuration knobs and
  known limitations.
- [AI opponents](customization/ai-opponents.md): Teddy, Iris, Gary, Gary 2.0; how each
  decides, where it runs, enabling/disabling, retraining.
- [Seeding](SEEDING.md): getting, importing, exporting, unpacking and sharing seed
  packs; the "I want to... use..." table.
- [Add clips and speakers](customization/clips-and-speakers.md): seed packs, the clip
  API, Clone Studio sentence recordings, the import tool, speaker names and photos,
  detector training exports.
- [Languages and UI text](customization/languages.md): editing `i18n.json`, default
  language, UI vs clip language, adding a language.
- [Booth operations](customization/booth-operations.md): game, TV and admin screens,
  admin password, known risks, daily checklist, resets.
- [Seed packs, backups and restore](customization/backups.md): what to export, where to
  keep it, how a new team gets the clips, disaster recovery.

**Audio never goes to Git.** Recordings, seed packs, backups and exports stay in the
cluster and in `~/voice-id-audio/` outside the repository.

For installation and troubleshooting use [the runbook](INSTALLATION.md); for having an
agent do it, [agent installation](AGENT-INSTALLATION.md); for background,
[architecture](ARCHITECTURE.md).
