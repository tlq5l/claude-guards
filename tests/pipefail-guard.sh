#!/usr/bin/env bash
# Clean-install test for guards/pipefail-guard.
# Drives the real Claude Code CLI (Haiku) in a throwaway git repo that gets only
# this item installed. Prints PASS/FAIL per check; exits 1 on any FAIL.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$ROOT/guards/pipefail-guard/pipefail-guard.py"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/pipefail-guard-test.XXXXXX")"
FAILS=0
CHECKS=0

pass() { CHECKS=$((CHECKS + 1)); echo "PASS $1"; }
fail() { CHECKS=$((CHECKS + 1)); FAILS=$((FAILS + 1)); echo "FAIL $1"; }

# make_repo DIR hook|none : fresh git repo with two test files, optionally with the hook wired in.
make_repo() {
  local dir="$1" wiring="$2"
  mkdir -p "$dir/.claude"
  (cd "$dir" && git init -q .)
  cat > "$dir/test_math.py" <<'PY'
import sys
if 1 + 1 != 3:
    print("FAILED: 1 + 1 is not 3")
    sys.exit(1)
print("OK")
PY
  printf 'import sys\nprint("OK")\nsys.exit(0)\n' > "$dir/test_ok.py"
  if [ "$wiring" = "hook" ]; then
    # the hook's own output is copied to hook.out, so a test can see what the hook decided
    printf '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"python3 %s | tee -a %s"}]}]}}\n' "$HOOK" "$dir/hook.out" > "$dir/.claude/settings.json"
  fi
}

# submitted_commands OUT_FILE: every Bash command the model submitted (before any hook rewrite)
submitted_commands() {
  grep '"tool_use"' "$1" | grep '"name":"Bash"'
}

# run_claude DIR MODE OUT_FILE PROMPT [ALLOW_BASH]
run_claude() {
  local dir="$1" mode="$2" out="$3" prompt="$4" allow="${5:-}"
  (cd "$dir" && claude -p --model haiku --setting-sources project --max-turns 6 \
    --permission-mode "$mode" ${allow:+--allowedTools Bash} --output-format stream-json --verbose "$prompt") > "$out" 2>&1
}

# no_missing_tools OUT_FILE : the run must not hit missing-command or missing-file errors.
no_missing_tools() { ! grep -qE "command not found|No such file or directory" "$1"; }

sha() { shasum -a 256 "$1" | cut -d' ' -f1; }

RISK_PROMPT='Run exactly one shell command in the current directory, then stop. The command is: python3 test_math.py | tee run.log ; echo EXIT=$? > exit.txt'
OK_PROMPT='Run exactly one shell command in the current directory, then stop. The command is: set -o pipefail; python3 test_ok.py | tee ok.log ; echo EXIT=$? > exit.txt'

echo "Work folder: $WORK"
echo "Hook under test: $HOOK"

# 1) CONTROL: no hook. The risky pipe must really hide the failure, or the other checks prove nothing.
C="$WORK/control"; make_repo "$C" none
run_claude "$C" bypassPermissions "$WORK/control.jsonl" "$RISK_PROMPT"
if [ "$(cat "$C/exit.txt" 2>/dev/null)" = "EXIT=0" ] && grep -q "FAILED" "$C/run.log" 2>/dev/null; then
  pass "CONTROL without hook: failing test reports EXIT=0 through the pipe (risk is real)"
else
  fail "CONTROL without hook: expected EXIT=0 with FAILED in run.log; exit.txt=[$(cat "$C/exit.txt" 2>/dev/null)] run.log=[$(cat "$C/run.log" 2>/dev/null)]"
fi
if no_missing_tools "$WORK/control.jsonl"; then pass "CONTROL: no missing-tool or file-not-found errors"; else fail "CONTROL: missing-tool or file-not-found error in run output"; fi

# 2) BLOCKED in default mode: the hook must deny the piped run and give the pipefail rerun.
B="$WORK/blocked-deny"; make_repo "$B" hook
B_SHA="$(sha "$B/test_math.py")"
run_claude "$B" default "$WORK/blocked-deny.jsonl" "$RISK_PROMPT" allow
if grep -q "Piping hides the test" "$WORK/blocked-deny.jsonl"; then
  pass "BLOCKED default mode: hook denied the piped test run with a pipefail rerun"
else
  fail "BLOCKED default mode: no deny message found in run output"
fi
if grep -q '"permissionDecision": "deny"' "$B/hook.out" 2>/dev/null; then
  pass "BLOCKED default mode: the hook itself printed the deny decision"
else
  fail "BLOCKED default mode: no deny decision in $B/hook.out"
fi
if [ "$(cat "$B/exit.txt" 2>/dev/null)" != "EXIT=0" ]; then
  pass "BLOCKED default mode: no run reported a false EXIT=0"
else
  fail "BLOCKED default mode: a run still reported EXIT=0 for the failing test"
fi
if [ "$(sha "$B/test_math.py")" = "$B_SHA" ] && [ -f "$B/test_ok.py" ]; then
  pass "BLOCKED default mode: throwaway test files intact"
else
  fail "BLOCKED default mode: throwaway test files changed or missing"
fi

# 3) REWRITE under bypassPermissions: the hook adds pipefail itself, so the failing exit code shows.
R="$WORK/blocked-rewrite"; make_repo "$R" hook
R_SHA="$(sha "$R/test_math.py")"
run_claude "$R" bypassPermissions "$WORK/blocked-rewrite.jsonl" "$RISK_PROMPT"
if submitted_commands "$WORK/blocked-rewrite.jsonl" | grep -q 'python3 test_math.py'; then
  pass "REWRITE bypassPermissions: the model submitted the test command"
else
  fail "REWRITE bypassPermissions: no test command was submitted; see $WORK/blocked-rewrite.jsonl"
fi
if submitted_commands "$WORK/blocked-rewrite.jsonl" | grep -q pipefail; then
  fail "REWRITE bypassPermissions: the model itself wrote pipefail, so EXIT=1 would prove nothing"
else
  pass "REWRITE bypassPermissions: the submitted command had no pipefail (the hook must add it)"
fi
if grep -q 'updatedInput' "$R/hook.out" 2>/dev/null && grep -q 'set -o pipefail; python3 test_math.py' "$R/hook.out"; then
  pass "REWRITE bypassPermissions: the hook output rewrote the command with set -o pipefail"
else
  fail "REWRITE bypassPermissions: no pipefail rewrite in $R/hook.out"
fi
if [ "$(cat "$R/exit.txt" 2>/dev/null)" = "EXIT=1" ]; then
  pass "REWRITE bypassPermissions: the failing test shows EXIT=1 through the pipe"
else
  fail "REWRITE bypassPermissions: expected EXIT=1; exit.txt=[$(cat "$R/exit.txt" 2>/dev/null)]"
fi
if [ "$(sha "$R/test_math.py")" = "$R_SHA" ]; then
  pass "REWRITE bypassPermissions: throwaway test file intact"
else
  fail "REWRITE bypassPermissions: throwaway test file changed"
fi

# 4) ALLOWED: an ordinary test run that already sets pipefail goes through untouched.
A="$WORK/allowed"; make_repo "$A" hook
run_claude "$A" bypassPermissions "$WORK/allowed.jsonl" "$OK_PROMPT"
if [ "$(cat "$A/exit.txt" 2>/dev/null)" = "EXIT=0" ] && grep -q "OK" "$A/ok.log" 2>/dev/null; then
  pass "ALLOWED with hook: pipefail test ran and passed (EXIT=0)"
else
  fail "ALLOWED with hook: expected EXIT=0 and OK in ok.log; exit.txt=[$(cat "$A/exit.txt" 2>/dev/null)]"
fi
if grep -q "Piping hides the test" "$WORK/allowed.jsonl"; then
  fail "ALLOWED with hook: hook denied an allowed command"
else
  pass "ALLOWED with hook: no deny message for the allowed command"
fi
if [ -s "$A/hook.out" ]; then
  fail "ALLOWED with hook: the hook printed a decision for an allowed command: $(cat "$A/hook.out")"
else
  pass "ALLOWED with hook: the hook printed nothing for the allowed command"
fi

# 5) Skills: this item ships a hook only, so there is no skill run to make.
echo "SKIP skills: item has no skill folder"

echo "Checks run: $CHECKS, failed: $FAILS"
if [ "$FAILS" -gt 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
