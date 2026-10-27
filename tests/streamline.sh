#!/usr/bin/env bash
# End-to-end test for the streamline skill.
#
# Drives real Claude Code (claude -p, Haiku) in a throwaway folder under $TMPDIR
# that gets only this item installed (.claude/skills/streamline/). The user-level
# settings are skipped with --setting-sources project, so the owner's own
# guards cannot mask a failure.
#
# Prints PASS or FAIL lines and exits 1 on any FAIL.
# Hook runs (control, blocked, allowed) do not apply: this item ships no hook.
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ITEM="$HERE/skills/streamline"
FAILS=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILS=$((FAILS + 1)); }

command -v claude >/dev/null 2>&1 || { fail "claude CLI is not on PATH"; exit 1; }
[ -f "$ITEM/SKILL.md" ] || { fail "item has no SKILL.md at $ITEM"; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/streamline-test.XXXXXX")"
REPO="$WORK/repo"
RUN="$WORK/run.jsonl"
mkdir -p "$REPO/.claude/skills"
cp -R "$ITEM" "$REPO/.claude/skills/streamline"

# Throwaway document with padding, a repeat, a hedge, and three facts that must survive.
cat > "$REPO/notes.md" <<'EOF'
# Deploy notes

Run `npm run build` before you ship. Run `npm run build` before you ship.

The app listens on port 8080 in production.

Note: remember that the app listens on port 8080 in production, which is the production port 8080.

Maybe we might possibly want to consider, if it seems useful, adding a note about logging, though this is not strictly needed at this time.

Never delete the `data/` folder; it holds the customer exports.
EOF
BEFORE_BYTES=$(wc -c < "$REPO/notes.md")

echo "NOTE hook runs (control, blocked, allowed): not applicable, this item ships no hook"
echo "NOTE work folder: $WORK"
echo "NOTE user-level settings are not fully isolated: login needs the default config folder, so the owner's user-level skills and memory may still load in the run"

# Skill run: real Claude Code, Haiku, project settings only, capped turns.
PROMPT='Use the streamline skill on notes.md in this folder. Tighten it without changing what it says or does. Then give the Changed, Left alone and Checked report.'
( cd "$REPO" && claude -p "$PROMPT" --model haiku --setting-sources project --max-turns 6 \
    --permission-mode bypassPermissions --output-format stream-json --verbose ) \
  > "$RUN" 2> "$WORK/stderr.txt"
RC=$?

[ "$RC" -eq 0 ] && pass "claude run exited 0" || fail "claude run exited $RC (stderr: $WORK/stderr.txt)"
grep -q '"type":"result"' "$RUN" && pass "run produced a final result" || fail "run produced no final result"

# The init event lists the owner's machine paths and user-level skills (the login
# only works from the default config folder, so those still load). Scan everything
# else: messages, tool inputs and results are what the run actually did.
SCAN="$WORK/scan.jsonl"
grep -v '"subtype":"init"' "$RUN" > "$SCAN"

grep -E '"name":"(Skill|Read)"' "$RUN" | grep -q 'streamline' \
  && pass "run loaded the streamline skill" || fail "run never loaded the streamline skill"

if grep -oE '"name":"Skill","input":\{"skill":"[^"]*"' "$RUN" | grep -qv '"skill":"streamline"'; then
  fail "run called a skill other than streamline"
else
  pass "run called no skill other than streamline"
fi

AFTER_BYTES=$(wc -c < "$REPO/notes.md")
if [ -f "$REPO/notes.md" ] && [ "$AFTER_BYTES" -lt "$BEFORE_BYTES" ]; then
  pass "notes.md got shorter ($BEFORE_BYTES -> $AFTER_BYTES bytes)"
else
  fail "notes.md not shorter or missing ($BEFORE_BYTES -> ${AFTER_BYTES:-gone} bytes)"
fi

# Each fact must still say what it said: the positive wording stays, and no negation is put next to it.
# A bare substring match would pass "Skip npm run build" or "production is NOT on 8080", so check the meaning.
NOTES="$REPO/notes.md"
if grep -qE 'npm run build' "$NOTES" && grep -qiE 'before (you )?ship' "$NOTES" \
  && ! grep -qiE '(skip|don.t|do not|never|not needed).{0,40}npm run build' "$NOTES"; then
  pass "kept fact: run npm run build before shipping"
else
  fail "lost or flipped fact: run npm run build before shipping"
fi
# One sentence must say production runs on 8080 and name no other port (a 9090 sentence does not count).
PROD_OK=$(python3 - "$NOTES" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
ports = lambda s: set(re.findall(r"(?<!\d)\d{4,5}(?!\d)", s)) - {"8080"}
ok = any(re.search("production", s, re.I) and re.search(r"(?<!\d)8080(?!\d)", s) and not ports(s)
         for s in re.split(r"[.;\n]", text))
print("yes" if ok else "no")
PY
)
if [ "$PROD_OK" = yes ] && ! grep -qiE '\bnot\b.{0,30}8080|8080.{0,30}\bnot\b' "$NOTES"; then
  pass "kept fact: the app listens on port 8080 in production"
else
  fail "lost or flipped fact: the app listens on port 8080 in production"
fi
if grep -qiE 'never delete.{0,40}data/' "$NOTES"; then
  pass "kept fact: never delete the data/ folder"
else
  fail "lost or flipped fact: never delete the data/ folder"
fi

if grep -qiE 'command not found|No such file or directory|ENOENT|not installed' "$SCAN"; then
  fail "run hit a missing tool or file-not-found error"
else
  pass "no missing tool or file-not-found errors in run"
fi

echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; exit 0; fi
echo "RESULT FAIL ($FAILS failing checks)"
exit 1
