// Static architecture diagram (HTML/CSS, no images). Mirrors docs/ARCHITECTURE.md.
const Box = ({ title, sub, hw, kind = '' }) => (
  <div className={`node ${kind}`}>
    <b>{title}</b>
    {sub && <span>{sub}</span>}
    {hw && <em className={hw.startsWith('GPU') ? 'gpu' : 'cpu'}>{hw}</em>}
  </div>
)

export default function Architecture() {
  return (
    <div className="arch" role="img" aria-label="Architecture: booth browsers reach the voice-id game, which calls four opponents served by Red Hat OpenShift AI (two on GPU with vLLM, two on CPU with OpenVINO Model Server); Voice Clone Studio uses XTTS and Fish Speech to make clips that are imported into the game.">
      <div className="zone booth">
        <h4>Booth</h4>
        <Box title="Game" sub="player laptop or tablet" />
        <Box title="/tv leaderboard" sub="booth monitor, every 10 s" />
        <Box title="/admin" sub="staff, password" />
        <Box title="Voice Clone Studio" sub="speakers record and clone" />
      </div>
      <div className="arrow" aria-hidden="true">→</div>
      <div className="zone cluster">
        <h4>OpenShift · namespace voice-id</h4>
        <div className="row-nodes">
          <Box title="voice-id" sub="Flask game, SQLite on PVC" hw="CPU" kind="core" />
        </div>
        <p className="lane">Opponents (battle mode) · Red Hat OpenShift AI · Teddy and Iris from profile standard</p>
        <div className="grid4">
          <Box title="Teddy" sub="Qwen2-Audio-7B · raw audio · vLLM" hw="GPU · L4" kind="opp-node" />
          <Box title="Iris" sub="Gemma-4-12B · spectrogram · vLLM" hw="GPU · L4" kind="opp-node" />
          <Box title="Gary" sub="Wav2Vec2 base · OpenVINO Model Server" hw="CPU · 4 cores" kind="opp-node" />
          <Box title="Gary 2.0" sub="Wav2Vec2 fine-tuned · OpenVINO Model Server" hw="CPU · 4 cores" kind="opp-node" />
        </div>
        <p className="lane">Voice cloning · profile full only</p>
        <div className="grid2">
          <Box title="XTTS v2" sub="Coqui, ~5 s per clip" hw="GPU · L4" kind="tts" />
          <Box title="Fish Speech S2 Pro" sub="best quality, 4 min warm-up" hw="GPU · L4" kind="tts" />
        </div>
        <p className="small muted">One platform for generative and predictive AI: OpenShift AI serves Teddy and Iris as KServe LLMInferenceServices on vLLM (GPU), and Gary and Gary 2.0 as KServe InferenceServices on the built-in OpenVINO Model Server (CPU), behind a small adapter that prepares the audio. Voice Clone Studio runs in the same namespace. Clips move from Clone Studio into the game with an import tool, never through Git.</p>
      </div>
    </div>
  )
}
