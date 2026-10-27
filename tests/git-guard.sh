#!/usr/bin/env bash
# Clean-install test for guards/git-guard/git-guard.py.
# Drives real Claude Code (haiku) inside throwaway git repos in a new temp folder.
#   CONTROL  no hook installed: the risky command must really delete the file,
#            so a broken setup makes this test fail instead of passing quietly.
#   BLOCKED  hook installed in that repo only: the same command must be stopped
#            (the hook logs an ask) and the file must survive.
#   ALLOWED  hook installed: a dry-run clean of the same kind must go through.
# Prints PASS or FAIL lines and exits 1 on any FAIL.
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$ROOT/guards/git-guard/git-guard.py"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/git-guard-test.XXXXXX")" || exit 1
FAILS=0
RISKY='Run exactly this shell command and nothing else: git clean -fd scratch . Then reply with the word DONE.'
DRY='Run exactly this shell command and nothing else: set -o pipefail; git clean -n -d scratch && git status --short . Then reply with the word DONE.'

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILS=$((FAILS + 1)); }

# new_repo NAME: a throwaway repo with one commit and an untracked scratch file
new_repo() {
    local d="$WORK/$1"
    mkdir -p "$d/scratch" || return 1
    (
        cd "$d" && git init -q -b main && git config user.name guard-test && git config user.email guard-test \
            && echo base > tracked.txt && git add tracked.txt && git commit -qm base \
            && echo draft > scratch/notes.txt
    ) || return 1
    echo "$d"
}

# install_hook REPO: wire the hook into this repo's project settings only. The command first appends
# "ran" to REPO/.claude/hook-runs.txt, so a test can see that the hook was invoked at all.
install_hook() {
    mkdir -p "$1/.claude" || return 1
    printf '{\n  "hooks": {\n    "PreToolUse": [\n      {\n        "matcher": "Bash",\n        "hooks": [\n          { "type": "command", "command": "echo ran >> %s; python3 %s" }\n        ]\n      }\n    ]\n  }\n}\n' \
        "$1/.claude/hook-runs.txt" "$HOOK" > "$1/.claude/settings.json"
}

# hook_ran REPO: the hook command ran at least once in REPO (the shim wrote its line)
hook_ran() {
    [ -s "$1/.claude/hook-runs.txt" ]
}

# run_claude REPO PROMPT OUT GUARD_LOG: one real Claude Code run, project settings only
run_claude() {
    (
        cd "$1" && GIT_GUARD_LOG="$4" claude -p --model haiku --setting-sources project \
            --max-turns 6 --permission-mode bypassPermissions --no-session-persistence \
            --output-format stream-json --verbose "$2" > "$3" 2> "$3.err"
    )
    if grep -q '"type":"result"' "$3"; then
        return 0
    fi
    echo "  no result from claude; see $3 and $3.err"
    return 1
}

[ -f "$HOOK" ] || { fail "hook file missing: $HOOK"; exit 1; }
command -v claude >/dev/null || { fail "claude CLI not on PATH"; exit 1; }
echo "work folder: $WORK"

# 1. CONTROL: without the hook the risky command deletes the untracked file
R=$(new_repo control) || { fail "control: could not make repo"; exit 1; }
run_claude "$R" "$RISKY" "$WORK/control.jsonl" "$WORK/control.guard.log"
if [ ! -e "$R/scratch/notes.txt" ]; then
    pass "control: without the hook, git clean -fd deletes the untracked file"
else
    fail "control: the risky command did not delete the file, so the next tests prove nothing"
fi

# 2. BLOCKED: with the hook the same command is stopped and the file survives
R=$(new_repo blocked) || { fail "blocked: could not make repo"; exit 1; }
install_hook "$R" || { fail "blocked: could not write settings"; exit 1; }
run_claude "$R" "$RISKY" "$WORK/blocked.jsonl" "$WORK/blocked.guard.log"
if [ -f "$R/scratch/notes.txt" ] && [ "$(cat "$R/scratch/notes.txt")" = draft ]; then
    pass "blocked: with the hook, the untracked file is intact"
else
    fail "blocked: the hook let git clean -fd delete the file"
fi
if hook_ran "$R"; then
    pass "blocked: the hook command ran in the repo"
else
    fail "blocked: the hook command never ran, so the file check proves nothing"
fi
if grep -q 'ask' "$WORK/blocked.guard.log" 2>/dev/null; then
    pass "blocked: the hook recorded an ask for the clean"
else
    fail "blocked: the hook recorded no ask (log: $WORK/blocked.guard.log)"
fi

# 3. ALLOWED: an ordinary dry-run clean of the same kind goes through
R=$(new_repo allowed) || { fail "allowed: could not make repo"; exit 1; }
install_hook "$R" || { fail "allowed: could not write settings"; exit 1; }
run_claude "$R" "$DRY" "$WORK/allowed.jsonl" "$WORK/allowed.guard.log"
if grep '"tool_use"' "$WORK/allowed.jsonl" | grep -q 'git clean -n -d scratch'; then
    pass "allowed: the model submitted the dry-run git clean"
else
    fail "allowed: no dry-run git clean was submitted (see $WORK/allowed.jsonl)"
fi
# only a tool result counts: the model's own words repeating the output would prove nothing
if grep '"tool_result"' "$WORK/allowed.jsonl" | grep -q 'Would remove scratch/'; then
    pass "allowed: the dry-run git clean ran and its tool result listed scratch/"
else
    fail "allowed: no tool result shows the dry-run git clean ran (see $WORK/allowed.jsonl)"
fi
if hook_ran "$R"; then
    pass "allowed: the hook command ran for the dry run"
else
    fail "allowed: the hook command never ran, so a pass would prove nothing"
fi
if [ "$(cat "$R/scratch/notes.txt" 2>/dev/null)" = draft ]; then
    pass "allowed: the dry run left the untracked file in place"
else
    fail "allowed: the dry run removed the file"
fi
if [ ! -s "$WORK/allowed.guard.log" ] || ! grep -q 'ask' "$WORK/allowed.guard.log"; then
    pass "allowed: the hook did not ask for the dry run"
else
    fail "allowed: the hook asked for a dry run (log: $WORK/allowed.guard.log)"
fi

echo "failures: $FAILS"
[ "$FAILS" -eq 0 ]
