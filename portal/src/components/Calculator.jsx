import { useState } from 'react'
import { OPPONENTS, GAME_SIZE, PRIZE_THRESHOLD, clipPoints, gamePoints, speedBonus } from '../scoring.js'

const START = [
  { correct: true, t: 2 },
  { correct: true, t: 3 },
  { correct: false, t: 5 },
  { correct: true, t: 4 },
  { correct: true, t: 6 },
]

export default function Calculator() {
  const [rounds, setRounds] = useState(START)
  const [oppId, setOppId] = useState('')
  const [oppCorrect, setOppCorrect] = useState(3)
  const opp = OPPONENTS.find((o) => o.id === oppId) || null
  const r = gamePoints(rounds, opp, oppCorrect)

  const set = (i, patch) => setRounds(rounds.map((x, j) => (j === i ? { ...x, ...patch } : x)))

  return (
    <div className="calc">
      <div className="calc-in">
        <div className="field">
          <span className="label">Mode</span>
          <div className="seg" role="group" aria-label="Mode and opponent">
            <button aria-pressed={!opp} onClick={() => setOppId('')}>Solo</button>
            {OPPONENTS.map((o) => (
              <button key={o.id} aria-pressed={oppId === o.id} onClick={() => setOppId(o.id)}>{o.name}</button>
            ))}
          </div>
        </div>
        {rounds.map((x, i) => (
          <div className="round" key={i}>
            <span className="rn">Clip {i + 1}</span>
            <div className="seg tiny" role="group" aria-label={`Clip ${i + 1} answer`}>
              <button aria-pressed={x.correct} onClick={() => set(i, { correct: true })}>Right</button>
              <button aria-pressed={!x.correct} onClick={() => set(i, { correct: false })}>Wrong</button>
            </div>
            <label className="slider">
              <span className="sr">Clip {i + 1} answer time</span>
              <input type="range" min="0" max="15" step="0.5" value={x.t} onChange={(e) => set(i, { t: +e.target.value })} />
              <span className="t">{x.t.toFixed(1)} s</span>
            </label>
            <span className="pts">{clipPoints(x.correct, x.t)}</span>
          </div>
        ))}
        {opp && (
          <div className="field">
            <label className="label" htmlFor="oppc">{opp.name} got {oppCorrect} of {GAME_SIZE} right</label>
            <input id="oppc" type="range" min="0" max={GAME_SIZE} step="1" value={oppCorrect} onChange={(e) => setOppCorrect(+e.target.value)} />
          </div>
        )}
      </div>
      <div className="calc-out" aria-live="polite">
        <div className="row"><span>{r.correct} correct × 100</span><span>{r.correct * 100}</span></div>
        <div className="row"><span>Speed bonus</span><span>+{rounds.reduce((s, x) => s + (x.correct ? speedBonus(x.t) : 0), 0)}</span></div>
        {opp && <div className="row"><span>Opponent bonus ({r.outcome})</span><span>+{r.bonus}</span></div>}
        <div className="total">{r.total.toLocaleString('en')} <small>points</small></div>
        <div className={`verdict ${r.prize ? 'won' : ''}`}>{r.prize ? `Prize: ${r.correct} of ${GAME_SIZE} correct` : `No prize: needs ${PRIZE_THRESHOLD} of ${GAME_SIZE} correct`}</div>
        {opp && !r.bonus && <p className="small">No bonus: {r.outcome === 'tie' ? 'a tie earns the bonus only with a perfect game.' : `you need more correct answers than ${opp.name}.`}</p>}
      </div>
    </div>
  )
}
