#!/usr/bin/env bash
# Hook-level checks for the three guards. Each case runs the real hook script as its own process, with the
# JSON that Claude Code sends on stdin, and checks the decision it prints. No model calls, so these run fast
# and cost nothing. The approval case is narrow on purpose: under `claude -p` the owner's chat reply can
# never reach the hook (helper runs can't pass it), so approve-once and deny-on-reuse can only be reached
# with a made-up transcript here.
# Prints PASS/FAIL per check; exits 1 on any FAIL.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/hook-cases.XXXXXX")" || exit 1
export ROOT WORK
python3 - <<'PY'
import json, os, re, subprocess, sys

ROOT, WORK = os.environ["ROOT"], os.environ["WORK"]
PURCHASE = os.path.join(ROOT, "guards/purchase-guard/purchase-guard.py")
PIPEFAIL = os.path.join(ROOT, "guards/pipefail-guard/pipefail-guard.py")
GIT = os.path.join(ROOT, "guards/git-guard/git-guard.py")
fails, checks = [], 0


def check(name, got, want):
    global checks
    checks += 1
    if got == want:
        print("PASS %s" % name)
    else:
        fails.append(name)
        print("FAIL %s: got %r, want %r" % (name, got, want))


def run(hook, command, cwd=WORK, mode="default", state=None, transcript="/nonexistent", extra_env=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd, "session_id": "s1",
               "transcript_path": transcript, "permission_mode": mode}
    env = dict(os.environ, PURCHASE_GUARD_STATE=state or os.path.join(WORK, "state.jsonl"),
               GIT_GUARD_LOG=os.path.join(WORK, "git-guard.log"))
    env.update(extra_env or {})
    p = subprocess.run(["python3", hook], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)
    if p.returncode:
        return "CRASH: " + p.stderr[-200:]
    return p.stdout.strip()


def decision(out):
    """allow when the hook printed nothing; else its permissionDecision (deny, or allow with an updatedInput rewrite)."""
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


def git_decision(out):
    if not out:
        return "silent"
    data = json.loads(out)
    decided = data.get("hookSpecificOutput", {}).get("permissionDecision")
    if decided:
        return decided  # ask
    return "warn" if "systemMessage" in data else "silent"  # a warning only comes with a systemMessage


def state_path(name):
    return os.path.join(WORK, "state-%s.jsonl" % name)


# ---- purchase-guard: classification
cases = [
    ("purchase: single-quoted substitution is text", "echo '$(vercel domains buy x.com)'", "allow"),
    ("purchase: double-quoted substitution runs", 'echo "$(vercel domains buy x.com)"', "deny"),
    ("purchase: --request=POST to Stripe payment_intents", "curl --request=POST https://api.stripe.com/v1/payment_intents", "deny"),
    ("purchase: sk_test word in a header is not a test key",
     "curl -X POST https://api.stripe.com/v1/payment_intents -d a=1 -H 'X-Note: sk_test_1'", "deny"),
    ("purchase: sk_test key in -u is test mode",
     "curl -X POST https://api.stripe.com/v1/payment_intents -u sk_test_1: -d a=1", "allow"),
    ("purchase: live key still denied", "curl -X POST https://api.stripe.com/v1/charges -u sk_live_x: -d a=1", "deny"),
    ("purchase: sh -c with a purchase inside", "sh -c 'vercel domains buy x.com'", "deny"),
    ("purchase: ordinary domains ls", "vercel domains ls", "allow"),
    ("purchase: quoted commit message is text", "git commit -m 'buy example.com later'", "allow"),
    ("purchase: sk_test key in --user= is test mode",
     "curl -X POST https://api.stripe.com/v1/payment_intents --user=sk_test_1: -d a=1", "allow"),
    ("purchase: attached -usk_test key is test mode",
     "curl -X POST https://api.stripe.com/v1/payment_intents -usk_test_1: -d a=1", "allow"),
    ("purchase: sk_test key does not clear a non-Stripe purchase",
     "curl -X POST https://example.com/purchase -u sk_test_1: -d a=1", "deny"),
    ("purchase: help-only call is allowed", "vercel domains buy --help", "allow"),
    ("purchase: -h help-only call is allowed", "vercel domains buy x.com -h", "allow"),
    ("purchase: --help after -- is not help", "vercel domains buy x.com -- --help", "deny"),
    ("purchase: curl -d -h sends -h as data, so it is denied", "curl -d -h https://api.stripe.com/v1/payment_intents", "deny"),
    ("purchase: curl -G with -d is a GET and allowed",
     "curl -G -d limit=3 https://api.stripe.com/v1/payment_intents", "allow"),
    ("purchase: curl --get with -d is a GET and allowed",
     "curl --get -d limit=3 https://api.stripe.com/v1/payment_intents", "allow"),
    ("purchase: curl -sG cluster with -d is a GET and allowed",
     "curl -sG -d limit=3 https://api.stripe.com/v1/payment_intents", "allow"),
    ("purchase: curl -G with -X POST is still denied",
     "curl -G -X POST -d limit=3 https://api.stripe.com/v1/payment_intents", "deny"),
]
for i, (name, command, want) in enumerate(cases):
    check(name, decision(run(PURCHASE, command, state=state_path("c%d" % i))), want)

# repeated purchase in one command: one approval must not cover both
out = run(PURCHASE, "vercel domains buy x.com; vercel domains buy x.com", state=state_path("dup"))
check("purchase: a repeated purchase is denied", decision(out), "deny")
check("purchase: the repeat says to split the command", "its own command" in out, True)

# approval flow (narrow, see header): the owner's first reply after the ask must be a yes, used once
st = state_path("approval")
first = run(PURCHASE, "vercel domains buy x.com", state=st)
m = re.search(r"purchase check [0-9a-f]{6}", first)
code = m.group(0) if m else "purchase check ??????"
transcript = os.path.join(WORK, "owner.jsonl")


def write_transcript(path, reply):
    rows = [
        {"type": "assistant", "timestamp": "2099-01-01T00:00:01Z",
         "message": {"content": [{"type": "text", "text": "Ask: vercel domains buy x.com costs money. %s" % code}]}},
        {"type": "user", "origin": {"kind": "human"}, "timestamp": "2099-01-01T00:00:02Z", "message": {"content": reply}},
    ]
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


write_transcript(transcript, "yes")
cmd = "PURCHASE_APPROVED=1 vercel domains buy x.com"
check("approval: a yes after the ask lets one rerun through", decision(run(PURCHASE, cmd, state=st, transcript=transcript)), "allow")
check("approval: the same approval does not pass a second run", decision(run(PURCHASE, cmd, state=st, transcript=transcript)), "deny")

st3 = state_path("approval-repeat")
run(PURCHASE, "vercel domains buy x.com", state=st3)  # asks for the code, then the owner says yes
check("approval: one approval does not cover a repeat of the purchase in the same command",
      decision(run(PURCHASE, "PURCHASE_APPROVED=1 vercel domains buy x.com; vercel domains buy x.com",
                   state=st3, transcript=transcript)), "deny")

st2 = state_path("approval-no")
run(PURCHASE, "vercel domains buy x.com", state=st2)  # same command, so the same code as above
transcript2 = os.path.join(WORK, "owner-no.jsonl")
write_transcript(transcript2, "no, wait")
check("approval: a no reply does not approve", decision(run(PURCHASE, cmd, state=st2, transcript=transcript2)), "deny")

# ---- pipefail-guard
pipe_cases = [
    ("pipefail: PIPESTATUS of tee does not clear the test", "python3 test_math.py | tee x; echo ${PIPESTATUS[1]}", "deny"),
    ("pipefail: PIPESTATUS of the test clears it", "python3 test_math.py | tee x; echo ${PIPESTATUS[0]}", "allow"),
    ("pipefail: a later command's PIPESTATUS does not clear it", "python3 test_math.py | tee x; ls; echo ${PIPESTATUS[0]}", "deny"),
    ("pipefail: quoted test path is still a test", "python3 'test_math.py' | cat", "deny"),
    ("pipefail: a URL ending in /pytest is not a test", "curl https://example.com/pytest | head", "allow"),
    ("pipefail: a gh path ending in /pytest is not a test", "gh api repos/x/pytest | head", "allow"),
    ("pipefail: npm test through a pipe is denied", "npm test | cat", "deny"),
    ("pipefail: pipefail already set is allowed", "set -o pipefail; npm test | cat", "allow"),
    ("pipefail: timeout with a flag still finds the test", "timeout -s KILL 60 pytest | tee x", "deny"),
    ("pipefail: sudo -u still finds the test", "sudo -u me pytest | tee x", "deny"),
    ("pipefail: python -u -m pytest still finds the test", "python3 -u -m pytest | cat", "deny"),
    ("pipefail: python -X dev test file still finds the test", "python3 -X dev test_math.py | cat", "deny"),
]
for name, command, want in pipe_cases:
    check(name, decision(run(PIPEFAIL, command, mode="default")), want)

# ---- git-guard: reset, mirror, subject-only force push
def sh(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("git %s failed: %s" % (" ".join(args), r.stderr))
    return r.stdout


def fresh(name):
    d = os.path.join(WORK, name)
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "t")
    sh(d, "config", "user.email", "t@t")
    return d


def write(d, path, text):
    with open(os.path.join(d, path), "w") as f:
        f.write(text)


def commit(d, path, text, subject):
    write(d, path, text)
    sh(d, "add", path)
    sh(d, "commit", "-qm", subject)


d = fresh("staged")
commit(d, "tracked.txt", "base\n", "base")
write(d, "tracked.txt", "changed\n")
sh(d, "add", "tracked.txt")
write(d, "tracked.txt", "base\n")
check("git: reset with only staged changes warns (saves them)", git_decision(run(GIT, "git reset --hard", cwd=d)), "warn")

d = fresh("untracked")
commit(d, "tracked.txt", "base\n", "base")
sh(d, "checkout", "-q", "-b", "other")
commit(d, "newfile.txt", "from other\n", "other")
sh(d, "checkout", "-q", "main")
write(d, "newfile.txt", "mine, untracked\n")
check("git: reset that would overwrite an untracked file asks", git_decision(run(GIT, "git reset --hard other", cwd=d)), "ask")

d = fresh("untracked-ancestor")
commit(d, "tracked.txt", "base\n", "base")
sh(d, "checkout", "-q", "-b", "other")
os.makedirs(os.path.join(d, "sub"))
commit(d, "sub/inner.txt", "from other\n", "other")
sh(d, "checkout", "-q", "main")
write(d, "sub", "my untracked file, where other has a directory\n")
check("git: reset that needs an untracked file out of a directory path asks",
      git_decision(run(GIT, "git reset --hard other", cwd=d)), "ask")

d = fresh("untracked-descendant")
commit(d, "tracked.txt", "base\n", "base")
sh(d, "checkout", "-q", "-b", "other")
commit(d, "name", "from other, a file\n", "other")
sh(d, "checkout", "-q", "main")
os.makedirs(os.path.join(d, "name"))
write(d, "name/mine.txt", "my untracked file under a path other tracks as a file\n")
check("git: reset that would delete an untracked file under a tracked file path asks",
      git_decision(run(GIT, "git reset --hard other", cwd=d)), "ask")

d = fresh("subfolder-clash")
os.makedirs(os.path.join(d, "sub"))
commit(d, "tracked.txt", "base\n", "base")
commit(d, "sub/keep.txt", "keep\n", "sub folder")
commit(d, ".gitignore", "secret.env\n", "ignore secret.env")
sh(d, "checkout", "-q", "-b", "other")
write(d, "secret.env", "from other\n")
sh(d, "add", "-f", "secret.env")
commit(d, "notes.txt", "from other\n", "other tracks notes and the ignored file")
sh(d, "checkout", "-q", "main")
write(d, "secret.env", "my ignored copy\n")
write(d, "notes.txt", "my untracked copy, outside the subfolder\n")
check("git: from a subfolder, reset that would overwrite an ignored file and an untracked file elsewhere asks",
      git_decision(run(GIT, "git reset --hard other", cwd=os.path.join(d, "sub"))), "ask")

d = fresh("subfolder-quiet")
os.makedirs(os.path.join(d, "sub"))
commit(d, "tracked.txt", "base\n", "base")
commit(d, "sub/keep.txt", "keep\n", "sub folder")
commit(d, ".gitignore", "secret.env\n", "ignore secret.env")
write(d, "secret.env", "my ignored file, nothing tracks it\n")
check("git: from a subfolder, reset with nothing to lose stays silent despite an ignored file elsewhere",
      git_decision(run(GIT, "git reset --hard HEAD", cwd=os.path.join(d, "sub"))), "silent")

d = fresh("mirror")
commit(d, "a.txt", "1\n", "one")
remote = os.path.join(WORK, "mirror-remote.git")
subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
sh(d, "remote", "add", "origin", remote)
sh(d, "push", "-q", "origin", "main")
sh(d, "update-ref", "refs/git-guard/dirty-cafe0000", sh(d, "rev-parse", "HEAD").strip())
check("git: --mirror asks when git-guard backups would be published", git_decision(run(GIT, "git push --mirror origin", cwd=d)), "ask")
sh(d, "update-ref", "-d", "refs/git-guard/dirty-cafe0000")
check("git: --mirror with no backups stays silent", git_decision(run(GIT, "git push --mirror origin", cwd=d)), "silent")

d = fresh("subject")
commit(d, "a.txt", "1\n", "base")
remote = os.path.join(WORK, "subject-remote.git")
subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
sh(d, "remote", "add", "origin", remote)
sh(d, "push", "-q", "origin", "main")
commit(d, "a.txt", "remote version\n", "fix")
sh(d, "push", "-q", "origin", "main")
sh(d, "reset", "-q", "--hard", "HEAD~1")
commit(d, "b.txt", "local version\n", "fix")
check("git: force push that drops a different commit with the same subject asks",
      git_decision(run(GIT, "git push --force origin main", cwd=d)), "ask")

d = fresh("rebase")
commit(d, "a.txt", "1\n", "base")
remote = os.path.join(WORK, "rebase-remote.git")
subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
sh(d, "remote", "add", "origin", remote)
sh(d, "push", "-q", "origin", "main")
commit(d, "b.txt", "same\n", "feature")
sh(d, "push", "-q", "origin", "main")
sh(d, "reset", "-q", "--hard", "HEAD~1")
commit(d, "b.txt", "same\n", "feature")
check("git: force push after a plain rebase stays silent", git_decision(run(GIT, "git push --force origin main", cwd=d)), "silent")

d = fresh("scan-timeout")
commit(d, "tracked.txt", "base\n", "base")
sh(d, "checkout", "-q", "-b", "other")
commit(d, "newfile.txt", "from other\n", "other")
sh(d, "checkout", "-q", "main")
write(d, "newfile.txt", "mine, untracked\n")
slow = os.path.join(WORK, "slow-git")
os.makedirs(slow)
real_git = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
with open(os.path.join(slow, "git"), "w") as f:  # stalls only the untracked-file scan, like a huge ignored folder
    f.write('#!/bin/sh\nfor a in "$@"; do [ "$a" = --others ] && sleep 20; done\nexec %s "$@"\n' % real_git)
os.chmod(os.path.join(slow, "git"), 0o755)
check("git: reset whose untracked-file scan times out asks, not allows",
      git_decision(run(GIT, "git reset --hard other", cwd=d, extra_env={"PATH": slow + os.pathsep + os.environ["PATH"]})), "ask")

print("checks: %d, failed: %d" % (checks, len(fails)))
sys.exit(1 if fails else 0)
PY
status=$?
echo "temp folder kept for inspection: $WORK"
if [ "$status" -ne 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
