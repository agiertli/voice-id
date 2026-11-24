// Mirror of the server-side scoring in apps/voice-id/src/app.py
// (POINTS_BASE, POINTS_SPEED_MAX, SPEED_WINDOW_SEC, OPPONENT_BONUS;
// prize = correct >= PRIZE_THRESHOLD).
// Keep in sync with docs/customization/scoring.md.

export const POINTS_BASE = 100
export const POINTS_SPEED_MAX = 200
export const SPEED_WINDOW_SEC = 10
export const GAME_SIZE = 5
export const PRIZE_THRESHOLD = 4

export const OPPONENTS = [
  { id: 'qwen', name: 'Teddy', level: 'Easy', stars: 1, bonus: 50, model: 'Qwen2-Audio-7B', method: 'Listens to the raw audio', input: 'Audio', accuracy: 24.4,
    note: 'Says “human” to almost everything. Beat it by trusting your doubts.' },
  { id: 'gemma', name: 'Iris', level: 'Medium', stars: 2, bonus: 100, model: 'Gemma-4-12B (FP8)', method: 'Reads a spectrogram image of the clip', input: 'Image', accuracy: 59.3,
    note: 'The only opponent that looks at a picture of the sound. Paranoid: sees AI in real voices.' },
  { id: 'gary-base', name: 'Gary', level: 'Hard', stars: 3, bonus: 150, model: 'Wav2Vec2 deepfake detector', method: 'Classifies the audio waveform', input: 'Audio', accuracy: 67.4,
    note: 'A real deepfake detector, trained on commercial TTS. Never heard Fish S2 Pro.' },
  { id: 'gary-finetuned', name: 'Gary 2.0', level: 'Boss', stars: 5, bonus: 250, model: 'Wav2Vec2, fine-tuned on the booth voices', method: 'Classifies the audio waveform', input: 'Audio', accuracy: 91.9,
    note: 'Trained on these very voices. Rarely accuses a real person. Good luck.' },
]

// Same expression as the game: Math.max(0, Math.round(200 * (1 - t / 10)))
export const speedBonus = (t) => Math.max(0, Math.round(POINTS_SPEED_MAX * (1 - t / SPEED_WINDOW_SEC)))
export const clipPoints = (correct, t) => (correct ? POINTS_BASE + speedBonus(t) : 0)

// rounds: [{ correct: bool, t: seconds }]
export function gamePoints(rounds, opponent = null, opponentCorrect = 0) {
  const base = rounds.reduce((s, r) => s + clipPoints(r.correct, r.t), 0)
  const correct = rounds.filter((r) => r.correct).length
  let bonus = 0
  let outcome = null
  if (opponent) {
    const win = correct > opponentCorrect
    const tie = correct === opponentCorrect
    const perfect = correct === rounds.length
    outcome = win ? 'win' : tie ? (perfect ? 'perfect tie' : 'tie') : 'loss'
    if (win || (tie && perfect)) bonus = opponent.bonus
  }
  return { base, bonus, total: base + bonus, correct, outcome, prize: correct >= PRIZE_THRESHOLD }
}
