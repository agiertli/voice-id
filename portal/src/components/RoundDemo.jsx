import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { POINTS_BASE, speedBonus } from '../scoring.js'

// A faithful-enough mock of one solo round of the booth game
// (apps/voice-id/src/templates/index.html, #screen-game). Pure presentation,
// driven by a scripted timeline. The speaker is fictitious on purpose: no real
// colleague's name, photo or voice ever appears on this public page.

const ANSWER_AT = 2.4 // seconds after the first press of play
const POINTS = POINTS_BASE + speedBonus(ANSWER_AT)

const BASE = {
  results: [true, false],  // clips 1 and 2 already answered
  current: 2,              // zero-based: clip 3 of 5
  playing: false,
  enabled: false,          // AI / Human buttons
  selected: null,          // 'ai' | 'human'
  revealed: false,
  hint: 'Press play, then choose',
  cursor: 'rest',          // rest | play | ai | next | off
  click: 0,
  bubble: null,
  caption: 'Clip 3 of 5. Nobody knows yet whose voice this is.',
}

const FRAMES = [
  { d: 1300, set: {} },
  { d: 900, set: { cursor: 'play' } },
  { d: 500, set: { click: 1, playing: true, enabled: true, hint: 'Make your guess!', caption: 'The clock starts on the first press of play.' } },
  { d: 1100, set: { cursor: 'rest' } },
  { d: 1500, set: { bubble: 'Hmm… a bit too smooth?' } },
  { d: 800, set: { bubble: null, cursor: 'ai' } },
  { d: 450, set: { click: 2, selected: 'ai', enabled: false, playing: false } },
  {
    d: 4200,
    set: {
      revealed: true, results: [true, false, true], cursor: 'rest',
      caption: `Correct after ${ANSWER_AT} s: ${POINTS_BASE} + ${POINTS - POINTS_BASE} speed points = ${POINTS}.`,
    },
  },
  { d: 900, set: { cursor: 'next' } },
  { d: 500, set: { click: 3 } },
]

export const prefersReducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

function useTimeline(autoplay) {
  const [state, setState] = useState(BASE)
  const [pos, setPos] = useState(0)
  const [playing, setPlaying] = useState(autoplay)

  useEffect(() => {
    if (!playing) return
    if (pos >= FRAMES.length) {
      const t = setTimeout(() => { setState(BASE); setPos(0) }, 400)
      return () => clearTimeout(t)
    }
    const f = FRAMES[pos]
    setState((s) => ({ ...s, ...f.set }))
    const t = setTimeout(() => setPos((p) => p + 1), f.d)
    return () => clearTimeout(t)
  }, [playing, pos])

  return { state, playing, toggle: () => setPlaying((p) => !p) }
}

// Static frame for reduced motion: the reveal, which tells the whole story at once.
const REVEALED = { ...BASE, ...Object.assign({}, ...FRAMES.slice(0, 8).map((f) => f.set)), cursor: 'off', bubble: null }

const Check = () => <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
const Cross = () => <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>

const IcoAi = () => <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" /><path d="M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" /></svg>
const IcoUser = () => <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7z" /></svg>

const BARS = Array.from({ length: 28 }, (_, i) => 0.35 + 0.65 * Math.abs(Math.sin(i * 1.7) * Math.cos(i * 0.6)))

export default function RoundDemo() {
  const reduced = prefersReducedMotion()
  const tl = useTimeline(!reduced)
  const s = reduced && !tl.playing ? REVEALED : tl.state

  const root = useRef(null)
  const refs = { play: useRef(null), ai: useRef(null), next: useRef(null) }
  const [pos, setPos] = useState({ x: '90%', y: '52%' })

  useLayoutEffect(() => {
    const place = () => {
      const box = root.current?.getBoundingClientRect()
      const target = refs[s.cursor]?.current
      if (!box) return
      if (!target) { setPos({ x: '90%', y: '52%' }); return }
      const r = target.getBoundingClientRect()
      setPos({ x: `${r.left - box.left + r.width * 0.55}px`, y: `${r.top - box.top + r.height * 0.6}px` })
    }
    place()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [s.cursor, s.revealed]) // eslint-disable-line react-hooks/exhaustive-deps

  const label = s.revealed
    ? 'Game screen: the player answered AI-generated. Correct! It was an AI clone of Jana, made with Fish Audio S2 Pro.'
    : 'Game screen: clip 3 of 5, a mystery voice. Buttons: AI-generated and Human voice.'

  return (
    <figure className="demo">
      <div className="demo-frame">
        <div className="demo-game" ref={root} role="img" aria-label={label}>
          <div className="dg-bar">
            <span className="dg-quit">✕ Quit</span>
            <div className="dg-progress">
              {[0, 1, 2, 3, 4].map((i) => {
                const r = s.results[i]
                const cls = r === undefined ? (i === s.current && !s.revealed ? 'active' : '') : r ? 'correct' : 'wrong'
                return <span key={i} className={`dg-dot ${cls}`}>{r === undefined ? i + 1 : r ? <Check /> : <Cross />}</span>
              })}
            </div>
          </div>

          <div className={`dg-card ${s.revealed ? 'reveal-correct' : ''}`}>
            <div className="dg-meta">Clip 3 of 5</div>
            <div className="dg-identity">
              {s.revealed
                ? <span className="dg-avatar ai" key="j">J</span>
                : <span className="dg-avatar mystery" key="q">?</span>}
            </div>

            {!s.revealed ? (
              <>
                <div className={`dg-wave ${s.playing ? 'playing' : ''}`} aria-hidden="true">
                  {BARS.map((h, i) => <i key={i} style={{ '--h': h, animationDelay: `${(i % 7) * -0.13}s` }} />)}
                </div>
                <span ref={refs.play} className="dg-play" aria-hidden="true">
                  {s.playing
                    ? <svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" /><rect x="14" y="4" width="4" height="16" /></svg>
                    : <svg viewBox="0 0 24 24"><polygon points="6,3 20,12 6,21" /></svg>}
                </span>
                <div className="dg-hint">{s.hint}</div>
                <div className="dg-choices">
                  <span ref={refs.ai} className={`dg-choice ai ${s.selected === 'ai' ? 'selected' : ''} ${!s.enabled && s.selected !== 'ai' ? 'disabled' : ''}`}><IcoAi /> AI-generated</span>
                  <span className={`dg-choice human ${!s.enabled ? 'disabled' : ''}`}><IcoUser /> Human voice</span>
                </div>
              </>
            ) : (
              <div className="dg-reveal">
                <div className="dg-verdict"><Check /> Correct!</div>
                <div className="dg-who">AI-generated: a clone of Jana</div>
                <div className="dg-sub">Model: Fish Audio S2 Pro</div>
                <span ref={refs.next} className="dg-next">Next clip</span>
              </div>
            )}
          </div>

          {s.bubble && <div className="dg-bubble" key={s.bubble}>{s.bubble}</div>}

          <svg
            className={`dg-cursor ${s.click ? 'click' : ''}`}
            key={`c${s.click}`}
            style={{ left: pos.x, top: pos.y, opacity: s.cursor === 'off' ? 0 : 1 }}
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path d="M3 2l7.5 19 2.6-7.9L21 10.5z" fill="#fff" stroke="#151515" strokeWidth="1.4" strokeLinejoin="round" />
          </svg>
        </div>
      </div>
      <figcaption className="demo-caption">
        <span aria-live="off">{s.caption}</span>
        <button type="button" className="demo-toggle" onClick={tl.toggle}>{tl.playing ? 'Pause' : 'Play'} the round</button>
      </figcaption>
    </figure>
  )
}
