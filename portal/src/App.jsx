import { useState } from 'react'
import { OPPONENTS, POINTS_BASE, POINTS_SPEED_MAX, SPEED_WINDOW_SEC, GAME_SIZE, PRIZE_THRESHOLD, speedBonus, gamePoints } from './scoring.js'
import { REPO, doc } from './links.js'
import Calculator from './components/Calculator.jsx'
import Architecture from './components/Architecture.jsx'

const Icon = ({ name, label }) => <img className="icon" src={`./icons/${name}.svg`} alt={label || ''} aria-hidden={label ? undefined : 'true'} />

const NAV = [
  ['why', 'Why'],
  ['play', 'How to play'],
  ['opponents', 'Opponents'],
  ['scoring', 'Scoring'],
  ['architecture', 'Architecture'],
  ['install', 'Install'],
]

function Logo() {
  const [ok, setOk] = useState(true)
  return ok
    ? <img className="brand-logo" src="./logo-compact.png" alt="Voice ID: AI or Alive?" onError={() => setOk(false)} />
    : <span className="brand-text">Voice ID <b>AI or Alive?</b></span>
}

function HeroLogo() {
  const [ok, setOk] = useState(true)
  return ok ? <img className="hero-logo" src="./logo.png" alt="" onError={() => setOk(false)} /> : null
}

function Stars({ n }) {
  return <span className="stars" aria-label={`${n} of 5 stars`}>{'★'.repeat(n)}<span className="off">{'★'.repeat(5 - n)}</span></span>
}

const SPEED_ROWS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

const SOLO = [
  { who: 'Anna', desc: '4/5 correct, answers at 2 s', rounds: [2, 2, 2, 2].map((t) => ({ correct: true, t })).concat([{ correct: false, t: 2 }]) },
  { who: 'Ben', desc: '4/5 correct, listens twice: 12 s', rounds: [12, 12, 12, 12].map((t) => ({ correct: true, t })).concat([{ correct: false, t: 12 }]) },
  { who: 'Cyril', desc: '5/5 correct, steady 6 s', rounds: [6, 6, 6, 6, 6].map((t) => ({ correct: true, t })) },
  { who: 'Dana', desc: '3/5 correct, very fast: 1 s', rounds: [1, 1, 1].map((t) => ({ correct: true, t })).concat([{ correct: false, t: 1 }, { correct: false, t: 1 }]) },
]

const FOUR_AT_3 = [3, 3, 3, 3].map((t) => ({ correct: true, t })).concat([{ correct: false, t: 3 }])
const BATTLES = [
  { opp: OPPONENTS[0], oppCorrect: 1 },
  { opp: OPPONENTS[1], oppCorrect: 3 },
  { opp: OPPONENTS[2], oppCorrect: 3 },
  { opp: OPPONENTS[3], oppCorrect: 4 },
  { opp: OPPONENTS[3], oppCorrect: 5 },
]

export default function App() {
  return (
    <>
      <a href="#main" className="skip">Skip to content</a>
      <header className="topbar">
        <div className="wrap bar">
          <a className="brand" href="#top" aria-label="Voice ID home"><Logo /></a>
          <nav className="topnav" aria-label="Sections">
            {NAV.map(([id, label]) => <a key={id} href={`#${id}`}>{label}</a>)}
          </nav>
        </div>
      </header>

      <main id="main">
        <section className="hero" id="top">
          <div className="wrap hero-grid">
            <div>
              <p className="eyebrow">A Red Hat booth game on OpenShift AI</p>
              <h1>Can you still tell a real voice from its clone?</h1>
              <p className="lede">
                Listen to five short clips. Some are real people, some are AI clones of the same people saying the same sentences.
                Decide <span className="human">natural voice</span> or <span className="ai">AI generated</span>, as fast as you can,
                then try to beat an AI detector at its own game.
              </p>
              <div className="actions">
                <a className="btn primary" href="#play">How to play</a>
                <a className="btn" href="#scoring">How scoring works</a>
                <a className="btn" href="#install">Run it at your event</a>
              </div>
            </div>
            <HeroLogo />
          </div>
        </section>

        <section id="why" className="section">
          <div className="wrap">
            <h2>Why this game exists</h2>
            <p className="lede">Thirty seconds of someone’s voice is enough to clone it. Most people find out how convincing that is only when it is too late.</p>
            <div className="cards three">
              <div className="card">
                <Icon name="conversation" />
                <h3>Trust is the attack surface</h3>
                <p>“It’s me, call me back on this number.” Voice is how we confirm a colleague, a family member or a manager on the phone. A cloned voice makes that check worthless. Players feel this on their own ears, in a minute.</p>
              </div>
              <div className="card">
                <Icon name="shield" />
                <h3>Detection is a specialist job</h3>
                <p>A big multimodal model staring at a spectrogram is a poor detector. A small classifier trained on the right voices does far better, and only for the voices it knows. The four opponents show exactly that gradient.</p>
              </div>
              <div className="card">
                <Icon name="server-stack" />
                <h3>All of it on your own platform</h3>
                <p>Cloning, the LLM opponents and the detectors run on OpenShift and OpenShift AI: generative models on vLLM with NVIDIA L4 GPUs, the detectors on OpenVINO Model Server on plain CPUs. No voice leaves the cluster. That is the sovereignty story, told through a game.</p>
              </div>
            </div>
          </div>
        </section>

        <section id="play" className="section tint">
          <div className="wrap">
            <h2>How to play</h2>
            <ol className="steps">
              <li><Icon name="user" /><div><h3>Pick a nickname</h3><p>Type a nickname (and an email if you want to be reachable for a prize). Typing the same nickname later continues as you: only your best game counts.</p></div></li>
              <li><Icon name="target" /><div><h3>Solo or battle</h3><p><b>Solo:</b> just you and the clips. <b>Battle:</b> choose an AI opponent who labels the same five clips. Its answer is revealed after yours.</p></div></li>
              <li><Icon name="clock" /><div><h3>Press play, then decide</h3><p>The clock starts on your first press of play. You may answer before the clip ends. Every correct answer is worth {POINTS_BASE} points plus up to {POINTS_SPEED_MAX} for speed.</p></div></li>
              <li><Icon name="magnifying-glass" /><div><h3>See the truth</h3><p>After each guess you learn whether it was a real person or which model cloned whom. In battle mode you also see the opponent’s reasoning.</p></div></li>
              <li><Icon name="bar-graph" /><div><h3>Prize and leaderboard</h3><p>Get <b>{PRIZE_THRESHOLD} of {GAME_SIZE}</b> right for a prize. Your points decide your place on the booth leaderboard.</p></div></li>
            </ol>
            <p className="small muted">How many of the five are AI is random (one to four), so counting won’t give the last answer away. Every game includes at least one AI clone of a real person you also hear for real, and at most one other AI voice. Clips are in English, Czech and Slovak, whatever language the screen is in.</p>
          </div>
        </section>

        <section id="opponents" className="section">
          <div className="wrap">
            <h2>The AI opponents</h2>
            <p className="lede">Four detectors, from hopeless to frightening. Beat one and you earn its bonus. Only Iris uses spectrograms; the others work on the audio itself.</p>
            <div className="cards four">
              {OPPONENTS.map((o) => (
                <div className={`card opp ${o.id}`} key={o.id}>
                  <div className="opp-head">
                    <Icon name={o.input === 'Image' ? 'ai-model' : (o.id.startsWith('gary') ? 'deployed-model' : 'ai-chatbot-happy')} />
                    <div><h3>{o.name}</h3><span className="level">{o.level}</span> <Stars n={o.stars} /></div>
                  </div>
                  <dl>
                    <dt>Model</dt><dd>{o.model}</dd>
                    <dt>Method</dt><dd>{o.method}</dd>
                    <dt>Accuracy</dt><dd><span className="acc"><i style={{ width: `${o.accuracy}%` }} /></span> {o.accuracy}%</dd>
                    <dt>Bonus if beaten</dt><dd><b>+{o.bonus}</b> points</dd>
                  </dl>
                  <p className="small">{o.note}</p>
                </div>
              ))}
            </div>
            <p className="small muted">Accuracy measured on the game’s own clip set, not deepfake detection in general. All four are served by Red Hat OpenShift AI: Teddy and Iris on vLLM with one NVIDIA L4 GPU each, both Garys on OpenVINO Model Server on CPU (about 1 s per clip).</p>
          </div>
        </section>

        <section id="scoring" className="section tint">
          <div className="wrap">
            <h2>Scoring, exactly</h2>
            <p className="lede">Two numbers come out of every game. They answer different questions.</p>
            <div className="cards two">
              <div className="card">
                <h3>Correct answers decide the prize</h3>
                <p>Counted by the server. <b>{PRIZE_THRESHOLD} or more out of {GAME_SIZE}</b> wins a prize, in solo and in battle. Speed, points and the opponent do not matter.</p>
              </div>
              <div className="card">
                <h3>Points decide the leaderboard</h3>
                <p>Correct answers weighted by speed, plus a bonus for beating your opponent. The board shows each nickname’s best game, sorted by points, then correct answers.</p>
              </div>
            </div>

            <h3 className="sub">The formula</h3>
            <pre className="formula">{`for each clip    t = seconds from your first press of play to your answer
correct answer   ${POINTS_BASE} + max(0, round(${POINTS_SPEED_MAX} × (1 − t / ${SPEED_WINDOW_SEC})))
wrong answer     0
battle bonus     + Teddy 50 | Iris 100 | Gary 150 | Gary 2.0 250
                   if you got more right than the opponent,
                   or tied with a perfect ${GAME_SIZE}/${GAME_SIZE}`}</pre>

            <div className="split">
              <div>
                <h3 className="sub">Points for one correct answer</h3>
                <table className="tbl speed">
                  <thead><tr><th>Answer after</th><th>Speed bonus</th><th>Points</th></tr></thead>
                  <tbody>
                    {SPEED_ROWS.map((t) => (
                      <tr key={t}><td>{t === 10 ? '10 s or more' : `${t} s`}</td><td>{speedBonus(t)}</td><td><b>{POINTS_BASE + speedBonus(t)}</b></td></tr>
                    ))}
                    <tr className="muted"><td>Wrong answer</td><td>0</td><td>0</td></tr>
                  </tbody>
                </table>
                <p className="small muted">Clips are about three seconds long, and the clock includes listening. Waiting for the end of each clip costs roughly 60 points per clip. Wrong answers never subtract.</p>
              </div>
              <div>
                <h3 className="sub">Worked examples: solo</h3>
                <table className="tbl">
                  <thead><tr><th>Player</th><th>Game</th><th>Points</th><th>Prize</th></tr></thead>
                  <tbody>
                    {SOLO.map((p) => { const r = gamePoints(p.rounds); return (
                      <tr key={p.who}><td>{p.who}</td><td>{p.desc}</td><td><b>{r.total.toLocaleString('en')}</b></td><td>{r.prize ? 'Yes' : 'No'}</td></tr>
                    ) })}
                  </tbody>
                </table>
                <p className="small">Leaderboard: Anna, Cyril, Dana, Ben. Anna beats perfect Cyril by being faster; Dana outranks Ben but wins no prize.</p>

                <h3 className="sub">Worked examples: battle</h3>
                <p className="small">You get 4/5 right at 3 s each: {gamePoints(FOUR_AT_3).base} points before any bonus. The opponent’s score is a count of correct answers, not points.</p>
                <table className="tbl">
                  <thead><tr><th>Opponent</th><th>Its score</th><th>Result</th><th>Points</th></tr></thead>
                  <tbody>
                    {BATTLES.map((b, i) => { const r = gamePoints(FOUR_AT_3, b.opp, b.oppCorrect); return (
                      <tr key={i}><td>{b.opp.name}</td><td>{b.oppCorrect}/5</td><td>{r.outcome}{r.bonus ? ` +${r.bonus}` : ''}</td><td><b>{r.total.toLocaleString('en')}</b></td></tr>
                    ) })}
                  </tbody>
                </table>
                <p className="small">A perfect 5/5 at 3 s against Gary 2.0 that also scores 5/5 counts as a perfect tie: 1,200 + 250 = <b>1,450</b>.</p>
              </div>
            </div>

            <h3 className="sub">Try it</h3>
            <Calculator />

            <div className="cards three facts">
              <div className="card"><h3>Replays</h3><p>Play as often as you like. Only your best game counts; points are never summed.</p></div>
              <div className="card"><h3>Ties</h3><p>Equal points: more correct answers first. Still equal: no defined order, so agree a tie-break before the prize ceremony.</p></div>
              <div className="card"><h3>Your rank</h3><p>Ranked by each player's best game, the same way as the leaderboard. Equal points share a rank.</p></div>
            </div>
            <p className="small muted">Limits: 1,500 points solo, 1,750 against Gary 2.0. The event owner can change the number of clips and the prize threshold. Points are computed on the game server; see the <a href={doc('docs/customization/scoring.md#known-limitations')}>known limitations</a> and the <a href={doc('docs/customization/scoring.md')}>full scoring rules</a>.</p>
          </div>
        </section>

        <section id="architecture" className="section">
          <div className="wrap">
            <h2>Architecture</h2>
            <p className="lede">One small web app, four opponents, two voice-cloning engines, one namespace. Up to four NVIDIA L4 GPUs (two for a standard booth, none for minimal), everything else on CPU.</p>
            <Architecture />
            <ul className="facts-list">
              <li><b>The game never calls the TTS engines.</b> Clips are made beforehand with Voice Clone Studio and stored in the game’s database.</li>
              <li><b>Opponents answer before round one.</b> Every opponent's answer for every clip is pre-computed in the background and cached, so a battle starts almost instantly. Answers stay sealed until the player has guessed.</li>
              <li><b>Audio stays on the cluster.</b> No recordings in Git, in this page or in any public place. Teams receive clips as a private seed pack.</li>
            </ul>
            <a className="btn small" href={doc('docs/ARCHITECTURE.md')}>Architecture details</a>
          </div>
        </section>

        <section id="install" className="section tint">
          <div className="wrap">
            <h2>Run it at your event</h2>
            <p className="lede">Pick a profile, run one script or let an agent drive. Voice cloning is optional: the game only plays pre-recorded clips.</p>
            <div className="tbl-scroll"><table className="tbl profiles">
              <thead><tr><th>Profile</th><th>GPUs</th><th>Opponents</th><th>Voice cloning</th><th>GPU cost*</th></tr></thead>
              <tbody>
                <tr><td><b>minimal</b></td><td>0</td><td>Gary, Gary 2.0</td><td>no</td><td>none</td></tr>
                <tr><td><b>standard</b> (recommended)</td><td>2</td><td>all four</td><td>no</td><td>≈ $2/h</td></tr>
                <tr><td><b>full</b></td><td>4</td><td>all four</td><td>Clone Studio, XTTS, Fish</td><td>≈ $4/h</td></tr>
              </tbody>
            </table></div>
            <p className="small muted">* About $1 per hour per g6.2xlarge (one NVIDIA L4) on demand; approximate, check current AWS pricing. Use full while collecting voices; dropping to standard afterwards is the owner’s call.</p>
            <ol className="steps compact">
              <li><Icon name="gpu" /><div><h3>Get a cluster</h3><p>OpenShift on AWS with OpenShift AI 3 (KServe, built-in OpenVINO Model Server) and the NVIDIA GPU Operator, plus quota for the profile’s g6.2xlarge GPU nodes (0, 2 or 4). Ask the owner for the detector model files (OpenVINO IR, no voice data).</p></div></li>
              <li><Icon name="support" /><div><h3>Run the installer, or let the agent drive</h3><p>Already logged in with a dedicated kubeconfig: <code>./install.sh --profile standard</code> (try <code>--dry-run</code> first). Or open Claude Code or Codex in the repository and start with the skill:</p>
                <pre className="cmd">{`export KUBECONFIG=~/.kube/voice-id-<event>.kubeconfig
/voice-id-operations Install profile standard on this cluster.
Event: <name>. Default language: cs.
Admin password: <your choice>.
Seed pack: ~/voice-id-audio/seedpacks/<file>.seedpack.zip
OpenVINO IR directory: ~/voice-id-audio/ovms/repo`}</pre></div></li>
              <li><Icon name="storage" /><div><h3>Load the clips</h3><p>Ask the game owner for the private seed pack and import it on the admin page, then press <b>Warm now</b> and wait until every opponent shows all clips cached: battles then start in about a second. The repository contains no audio, on purpose: voices are personal data. <a href={doc('docs/SEEDING.md')}>Seeding guide</a>.</p></div></li>
              <li><Icon name="check" /><div><h3>Check the booth</h3><p>Play a solo game and a battle on the booth laptop, put <code>/tv</code> on the monitor, change the admin password, reset scores.</p></div></li>
            </ol>
            <div className="actions">
              <a className="btn primary" href={doc('docs/INSTALLATION.md')}>Installation runbook</a>
              <a className="btn" href={doc('docs/AGENT-INSTALLATION.md')}>Install with an agent</a>
              <a className="btn" href={doc('docs/SEEDING.md')}>Seeding the clips</a>
              <a className="btn" href={doc('docs/CUSTOMIZATION.md')}>Customize and operate</a>
            </div>
            <p className="small muted">A custom conference demo built on Red Hat OpenShift and OpenShift AI with community models; not a Red Hat supported solution as a whole.</p>
          </div>
        </section>
      </main>

      <footer className="foot">
        <div className="wrap">
          <p>Voice ID: AI or Alive? A Red Hat booth game. Created by Anton Giertli. <a href={REPO}>Source and docs on GitHub</a>.</p>
          <p className="small">Icons: Red Hat standard icons by Red Hat, from <a href="https://github.com/RedHat-UX/red-hat-icons">RedHat-UX/red-hat-icons</a>, used unmodified under <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> (<a href="./icons/ICON-LICENSE">license</a>). Fonts: Red Hat Display and Red Hat Text, <a href="./fonts/OFL.txt">SIL Open Font License</a>. Red Hat and the Red Hat logo are trademarks of Red Hat, Inc.</p>
        </div>
      </footer>
    </>
  )
}
