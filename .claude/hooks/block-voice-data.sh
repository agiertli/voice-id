#!/usr/bin/env bash
# Claude Code PreToolUse guard — RULE ZERO (see AGENTS.md):
# voice recordings, clones, seed packs and databases must NEVER enter git or leave via gh.
# Exit 2 = block the tool call (message on stderr is shown to Claude). Exit 0 = allow.
# Reads the hook JSON on stdin. Requires jq.
set -uo pipefail

INPUT="$(cat)"
TOOL="$(jq -r '.tool_name // empty' <<<"$INPUT")"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

EXT_RE='\.(wav|mp3|m4a|flac|ogg|oga|opus|webm|aac|aif|aiff|wma|amr|3gp|mka|caf|mp4|db|db-wal|db-shm|sqlite|sqlite3|backup|age)$|\.seedpack\.zip$'

block() {
  echo "BLOCKED by Rule Zero guard (.claude/hooks/block-voice-data.sh): $1" >&2
  echo "Voice audio, clones, seed packs and databases must never go to git or GitHub. Keep them in ~/voice-id-audio/ and share seed packs privately." >&2
  exit 2
}

# Returns 0 if the file at path $1 is audio/db by extension or content.
is_sensitive() {
  local f="$1" lower head
  lower="$(printf '%s' "$f" | tr '[:upper:]' '[:lower:]')"
  [[ "$lower" =~ $EXT_RE ]] && return 0
  [[ -f "$f" ]] || return 1
  head="$(head -c 12 -- "$f" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  case "$head" in
    52494646????????57415645*) return 0 ;;   # RIFF....WAVE
    494433*|fffb*|fff3*|fff2*) return 0 ;;    # ID3 / MPEG frame
    664c6143*) return 0 ;;                     # fLaC
    4f676753*) return 0 ;;                     # OggS
    1a45dfa3*) return 0 ;;                     # EBML (WebM/Matroska)
    ????????66747970*) return 0 ;;             # ....ftyp (MP4/M4A)
    53514c69746520666f726d61*) return 0 ;;     # "SQLite forma"
  esac
  return 1
}

check_list() {   # stdin: newline-separated paths (relative to $1)
  local base="$1" p hits=""
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    if is_sensitive "$base/$p" || is_sensitive "$p"; then hits+=" $p"; fi
  done
  [[ -n "$hits" ]] && block "sensitive files would be committed/pushed:$hits"
  return 0
}

# ---------------- Write / Edit / NotebookEdit ----------------
if [[ "$TOOL" == "Write" || "$TOOL" == "Edit" || "$TOOL" == "NotebookEdit" ]]; then
  FP="$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$INPUT")"
  [[ -z "$FP" ]] && exit 0
  case "$FP" in "$REPO"/*) ;; *) exit 0 ;; esac          # only inside this repo
  lower="$(printf '%s' "$FP" | tr '[:upper:]' '[:lower:]')"
  [[ "$lower" =~ $EXT_RE ]] && block "refusing to write an audio/database file inside the repo: $FP"
  exit 0
fi

[[ "$TOOL" == "Bash" ]] || exit 0
CMD="$(jq -r '.tool_input.command // empty' <<<"$INPUT")"
[[ -z "$CMD" ]] && exit 0

# Strip heredoc bodies and quoted strings so prose (commit messages, docs written via
# python/cat heredocs) is not mistaken for flags. File checks below use git itself, not text.
CMD_RAW="$CMD"
CMD="$(python3 - "$CMD_RAW" <<'PY'
import re, sys
s = sys.argv[1]
s = re.sub(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", "<<HEREDOC", s, flags=re.S | re.M)
s = re.sub(r"'[^']*'", "''", s)
s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
print(s)
PY
)"
[[ -z "$CMD" ]] && CMD="$CMD_RAW"

# Fast path: nothing git/gh related -> allow.
grep -qE '(^|[;&|(` ]|\$\()(git|gh)( |$)' <<<"$CMD" || exit 0

# Never weaken the guards.
grep -qE 'git[^;&|]*(commit|push|merge|rebase|am|cherry-pick)[^;&|]*--no-verify' <<<"$CMD" && block "--no-verify bypasses the audio pre-commit hook"
grep -qE 'git[^;&|]*commit[^;&|]*( -[a-zA-Z]*n[a-zA-Z]*( |$))' <<<"$CMD" && block "'git commit -n' bypasses the audio pre-commit hook"
grep -qE 'git +-c +core\.hooksPath' <<<"$CMD" && block "overriding core.hooksPath disables the audio guard"
if grep -qE 'git +config[^;&|]*core\.hooksPath' <<<"$CMD"; then
  grep -qE 'core\.hooksPath +\.githooks( |$|;|&)' <<<"$CMD" || block "core.hooksPath must stay '.githooks'"
fi
grep -qE 'git[^;&|]* add[^;&|]*( -f| --force| -[a-zA-Z]*f[a-zA-Z]*( |$))' <<<"$CMD" && block "'git add --force' can stage ignored audio/database files"
grep -qE 'git +lfs' <<<"$CMD" && block "git lfs is not used here; audio must not be tracked at all"

# gh release / gh gist with sensitive files.
if grep -qE 'gh +(release +(create|upload)|gist +(create|edit))' <<<"$CMD"; then
  for tok in $CMD_RAW; do
    t="${tok%\"}"; t="${t#\"}"; t="${t%\'}"; t="${t#\'}"
    if [[ -e "$t" ]] && is_sensitive "$t"; then block "gh would upload a sensitive file: $t"; fi
    lt="$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')"; [[ "$lt" =~ $EXT_RE ]] && block "gh would upload a sensitive file: $t"
  done
fi

# Determine the git working dir of the command (cd X && git ... / git -C X ...).
WD="$REPO"
cdir="$(grep -oE '(^|[;&] *)cd +[^;&|]+' <<<"$CMD" | tail -1 | sed -E 's/^[;& ]*cd +//; s/ +$//')"
[[ -n "$cdir" ]] && { cdir="${cdir/#\~/$HOME}"; [[ -d "$cdir" ]] && WD="$(cd "$cdir" && pwd)"; }
cflag="$(grep -oE 'git +-C +[^ ;&|]+' <<<"$CMD" | tail -1 | awk '{print $3}')"
[[ -n "$cflag" ]] && { cflag="${cflag/#\~/$HOME}"; [[ -d "$cflag" ]] && WD="$(cd "$cflag" && pwd)"; }
TOP="$(git -C "$WD" rev-parse --show-toplevel 2>/dev/null)" || exit 0

# git add: what would be staged.
if grep -qE 'git[^;&|]* add( |$)' <<<"$CMD"; then
  ARGS="$(grep -oE 'git[^;&|]* add[^;&|]*' <<<"$CMD" | head -1 | sed -E 's/.* add//')"
  # shellcheck disable=SC2086
  check_list "$WD" < <(cd "$WD" && git add --dry-run $ARGS 2>/dev/null | sed -E "s/^add '(.*)'$/\1/")
  check_list "$TOP" < <(cd "$WD" && git diff --cached --name-only)
fi

# git commit: staged files (+ tracked modifications for -a/--all).
if grep -qE 'git[^;&|]* commit( |$)' <<<"$CMD"; then
  check_list "$TOP" < <(cd "$TOP" && git diff --cached --name-only)
  if grep -qE 'commit[^;&|]*( -[a-zA-Z]*a| --all)' <<<"$CMD"; then check_list "$TOP" < <(cd "$TOP" && git diff --name-only); fi
fi

# git stash (could later be applied/pushed via a branch): check what it would capture.
if grep -qE 'git[^;&|]* stash( |$)' <<<"$CMD"; then
  check_list "$TOP" < <(cd "$TOP" && { git diff --cached --name-only; git diff --name-only; })
  if grep -qE 'stash[^;&|]*(-u|--include-untracked|-a|--all)' <<<"$CMD"; then check_list "$TOP" < <(cd "$TOP" && git ls-files --others --exclude-standard); fi
fi

# git push: every file touched by commits not yet on the remote.
if grep -qE 'git[^;&|]* push( |$)' <<<"$CMD"; then
  RANGE_BASE="$(cd "$TOP" && git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo origin/main)"
  check_list "$TOP" < <(cd "$TOP" && git log --name-only --format= "$RANGE_BASE..HEAD" 2>/dev/null | sort -u)
  # explicit refspec like <sha>:main
  for ref in $(grep -oE '[0-9a-f]{7,40}:[A-Za-z0-9/_.-]+' <<<"$CMD"); do
    sha="${ref%%:*}"; check_list "$TOP" < <(cd "$TOP" && git log --name-only --format= "origin/main..$sha" 2>/dev/null | sort -u)
  done
fi

exit 0
