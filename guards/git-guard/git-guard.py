#!/usr/bin/env python3
"""PreToolUse(Bash): before a git command that can lose work, show what it would
lose and ask Proceed / Cancel. When nothing would be lost it stays silent.
Unattended helpers turn an ask into a denial, so every ask says what to do instead.

Asks (permissionDecision "ask") for:
- git push with --force / -f / --force-with-lease / --force-if-includes / --mirror
  or a `+refspec`: the commits on the remote branch (local remote-tracking ref, else
  `ls-remote`; no fetch) that the push would drop. A commit with the same patch as a
  local one doesn't count. A commit that only shares a subject with a local commit
  still counts, marked "may be a rebased copy": a rebase that resolved conflicts
  keeps the subject, and a different commit can share it too. --mirror also asks when
  git-guard's refs/git-guard/* backups exist, because --mirror would publish them. No
  tracking ref to compare with (never fetched, URL remote, glob refspec, `$var` names):
  it asks and says it cannot tell.
- git reset --hard [ref]: uncommitted changes to tracked files, staged or not (files
  and line counts), are saved to refs/git-guard/dirty-<sha> and only warn (ask only if
  saving fails; restore with git stash apply --index <ref>). Commits that would end up on no
  branch, tag or remote-tracking ref are saved to refs/git-guard/reset-<sha> and only
  warn (ask only if saving fails). Untracked files that the target commit tracks are
  an ask: reset overwrites them without a prompt. A branch or tag the line makes earlier
  (git branch backup, git tag x, switch -c) holds the commit it points at; a branch the
  line moves away holds nothing old.
- git clean with -f: the same flags with -n, listing what would go (a `$var` or
  `$( )` path: it asks and says it cannot tell).
- git worktree remove --force <path or name>: uncommitted and untracked (not
  ignored) files there. Never for commits: the branch outlives the worktree. A
  worktree that `git worktree add` made earlier in the same line is skipped, like a
  mktemp folder. An agent's worktree (session scratchpad, .claude/worktrees/agent-*)
  gets its files saved to refs/git-guard/<folder>-<sha> and only warns.
- git push -f to a URL: compares against the URL's live tips (ls-remote).
Warns only (systemMessage to the user, additionalContext to the agent, no
permission decision, so the command still goes through the normal permission
check) for git stash drop / clear / pop / push, a detached worktree HEAD with
commits on no ref, and every branch deletion (branch -D, push --delete, push
:branch): the count of commits on no other branch, tag or upstream (a branch
deleted both locally and on the remote in one line counts as held by neither).
A deleted branch whose content is already on the main branch (a squash merge)
is not warned about; merge detection never makes anything ask.

Commands are found in every shell segment (; && || | newline, ( ), `sh -c`,
`$( )` and backticks), heredoc bodies of non-shell commands are skipped, and the
directory follows the payload's cwd, `cd X` / `pushd X`, `git -C X`, GIT_DIR /
GIT_WORK_TREE, $PWD and simple `VAR=value` / `for v in a b; do` variables (a `cd`
to a missing folder followed by `;` leaves the folder as it was). HEAD and branch
tips follow an earlier `git switch` / `git checkout` (also -c / -b), `git branch
[-f] <name> [<start>]`, `git tag [-f] <name> [<commit>]` and `git reset --hard` in the line. git is
only read (GIT_OPTIONAL_LOCKS=0, `clean -n`), each call with a 3 s timeout, except the
refs/git-guard/* refs it writes to keep commits and files it would otherwise ask about
(GIT_GUARD_DRY=1 skips those writes, for replays against real repos).

Fail open: a crash or a git error (missing folder, not a repository, bad ref)
allows the command and appends a line to git-guard.log next to this file
(GIT_GUARD_LOG overrides the path). Every ask and warn is logged there too.
A line with no "git" and no risky subcommand word exits before anything but
json and sys is imported (this runs on every Bash call).
"""
import json
import sys

WORDS = ("push", "reset", "clean", "worktree", "stash", "branch")


def main():
    try:
        data = json.load(sys.stdin)
        command = data["tool_input"]["command"]
    except Exception:
        return
    if not isinstance(command, str) or "git" not in command or not any(w in command for w in WORDS):
        return
    slow_path(data, command)


# ---------------------------------------------------------------- slow path

def slow_path(data, command):
    global os, re, subprocess, time
    import os
    import re
    import subprocess
    import time
    cwd = data.get("cwd")
    if not (isinstance(cwd, str) and cwd):
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = None
    try:
        text = strip_heredocs(command)
        guard = Guard(text, line_deletes(text))
        guard.walk(text, cwd, {}, 0)
        out = guard.output()
    except Exception as e:  # fail open
        log("crash", cwd, "%s: %s" % (type(e).__name__, e))
        return
    if out:
        print(json.dumps(out))


def log(kind, cwd, reason):
    path = os.environ.get("GIT_GUARD_LOG") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         "git-guard.log")
    line = "%s\t%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), kind, cwd or "-",
                                 " ".join(str(reason).split())[:300])
    try:
        with open(path, "a") as f:
            f.write(line)
    except OSError:
        pass


class GitError(Exception):
    """git failed or the folder is missing: the command would fail too, so allow."""


# A variable set from $(mktemp ...) expands to this; paths holding it are fresh folders.
FRESH = "\x02mktemp"
# Worktrees agents make: Claude Code's per-session scratchpad and its isolated agent worktrees
# (<repo>/.claude/worktrees/agent-*). Removing one with uncommitted files saves them to a ref first.
AGENT_WT = __import__("re").compile(r"^/private/tmp/claude-\d+/[^/]+/[^/]+/scratchpad/|/\.claude/worktrees/agent-[^/]+/?$")


class Unsure(Exception):
    """The hook can't tell what the command touches. warn: the command only deletes
    branches, so this becomes a warning, never an ask."""
    warn = False


# what an unattended agent should do instead (every ask ends with one of these)
DO_UNSURE = ("Spell out the literal name or path (no variables), fetch first if the remote copy is "
             "missing, or skip it and report it to the user.")
DO_PUSH = ("To keep them, fetch and rebase onto (or merge) %s first, or push to a new branch name; "
           "if you can't, don't push and report it to the user.")
DO_MIRROR = "Push the branches you mean by name instead of --mirror, or skip it and report it to the user."
DO_RESET_FILES = ("Commit or stash them first (git stash push), or leave them and report it to the user.")
DO_RESET_COMMITS = ("Keep them first in the same command (git branch backup-<name> && git reset --hard ...), "
                    "or skip it and report it to the user.")
DO_CLEAN = "Move them to a backup folder first (mv <path> <backup folder>), or leave them and report it to the user."
DO_WORKTREE = ("Commit, stash or move these %d files out first, or leave the worktree and report it to the user.")


SHOW = 5  # commit subjects / files listed per finding


def git(cwd, gopts, *args, ok_codes=(0,), timeout=3, env=None):
    if cwd is None:
        raise Unsure("the folder it runs in uses a variable or command output")
    if FRESH in cwd:
        raise GitError("folder made by mktemp in this command: it holds only what the command made")
    if not os.path.isdir(cwd):
        raise GitError("no folder " + cwd)
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GIT_TERMINAL_PROMPT="0",
               GIT_PAGER="cat", **(env or {}))
    try:
        r = subprocess.run(["git", *gopts, *args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(str(e))
    if r.returncode not in ok_codes:
        raise GitError("git %s: %s" % (" ".join(args[:3]), r.stderr.strip()[:200]))
    return r.stdout


def lines(text):
    return [x for x in text.split("\n") if x.strip()]


def verify(cwd, g, ref):
    """The commit a ref names, or None."""
    out = git(cwd, g, "rev-parse", "-q", "--verify", ref + "^{commit}", ok_codes=(0, 1))
    return out.strip() or None


def commits(cwd, g, rev_args):
    """(count, up to SHOW 'hash subject' lines) for a rev-list range."""
    n = int(git(cwd, g, "rev-list", "--count", *rev_args).strip() or 0)
    if not n:
        return 0, []
    return n, lines(git(cwd, g, "log", "-n", str(SHOW), "--format=%h %s", *rev_args))


def holders(branches=(), remotes=(), tags=(), extra=()):
    """rev-list args: not on any branch, remote-tracking ref or tag except the named ones
    (short names: `x`, `origin/x`; each --exclude applies to the next --branches /
    --remotes / --tags), nor on the extra commits."""
    return ["--not", *["--exclude=" + b for b in branches], "--branches",
            *["--exclude=" + r for r in remotes], "--remotes",
            *["--exclude=" + t for t in tags], "--tags", *extra]


def config(cwd, g, key):
    return git(cwd, g, "config", "--get", key, ok_codes=(0, 1)).strip() or None


def more(total, shown):
    return " (+%d more)" % (total - shown) if total > shown else ""


def commit_list(n, subjects):
    return "; ".join(subjects) + more(n, len(subjects))


def plural(n, word):
    if n == 1:
        return "1 " + word
    return "%d %s" % (n, word[:-1] + "ies" if word.endswith("y") else word + "s")


def unsure(word):
    return "$" in word or "`" in word


def is_long(a, full, least):
    """Is a (without any =value) git's unique prefix of the long option `full`?"""
    a = a.split("=", 1)[0]
    return len(a) >= least and full.startswith(a)


# ------------------------------------------------------------- the checks

def parse_push(args):
    """The flags of `git push` args, the remote word (None: the default) and the refspecs."""
    f = dict(force=False, delete=False, mirror=False, every=False, dry=False, prune=False)
    pos, repo = [], None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("--"):
            if (is_long(a, "--force", 5) or is_long(a, "--force-with-lease", 5)
                    or is_long(a, "--force-if-includes", 5)):
                f["force"] = True
            elif is_long(a, "--delete", 4):
                f["delete"] = True
            elif is_long(a, "--mirror", 4):
                f["mirror"] = True
            elif is_long(a, "--all", 4) or a == "--branches":
                f["every"] = True
            elif is_long(a, "--dry-run", 4):
                f["dry"] = True
            elif is_long(a, "--prune", 5):
                f["prune"] = True
            elif a.startswith("--repo="):
                repo = a[len("--repo="):]
            elif a in ("--repo", "--receive-pack", "--exec", "--push-option"):
                if a == "--repo" and i + 1 < len(args):
                    repo = args[i + 1]
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch == "f":
                    f["force"] = True
                elif ch == "d":
                    f["delete"] = True
                elif ch == "n":
                    f["dry"] = True
                elif ch == "o":
                    if k == len(a) - 2:
                        i += 1
                    break
        else:
            pos.append(a)
        i += 1
    # like git: a <repository> argument beats --repo
    return f, (pos[0] if pos else repo), pos[1:]


def push_forces(f, specs):
    """Can this push overwrite (not just delete) a remote branch? Only then may it ask."""
    if f["mirror"]:
        return True
    if f["delete"]:
        return False  # every refspec names a branch to delete
    if not specs:
        return f["force"]  # the default push (or --all) with --force
    return any(s.startswith("+") and not s.startswith("+:") or f["force"] and not s.startswith(":")
               for s in specs)


def check_push(cwd, g, args, deletes):
    f, remote, specs = parse_push(args)
    risky = (f["force"] or f["delete"] or f["mirror"] or any(s.startswith(("+", ":")) for s in specs)
             or f["prune"] and any("*" in s for s in specs))  # --prune deletes what a pattern no longer matches
    if f["dry"] or not risky:
        return None
    try:
        return push_findings(cwd, g, f, remote, specs, deletes)
    except Unsure as e:
        e.warn = not push_forces(f, specs)
        raise


def is_url(remote):
    """A remote given as a URL (https://, ssh://, file://, user@host:path): its branch tips come
    from `ls-remote`, like a named remote never fetched. Bare paths still count as unsure."""
    import re
    return "://" in remote or re.match(r"^[\w.-]+@[\w.-]+:", remote) is not None


def push_findings(cwd, g, f, remote, specs, deletes):
    """[(level, message)]: "ask" for commits an overwrite drops, "warn" for deletions."""
    force, delete, mirror, every = f["force"], f["delete"], f["mirror"], f["every"]
    if remote is not None and unsure(remote):
        raise Unsure("the remote is a variable (%s)" % remote)
    if remote is None:
        cur = current_branch(cwd, g)
        remote = (cur and config(cwd, g, "branch.%s.remote" % cur)) or "origin"
    if remote not in lines(git(cwd, g, "remote")) and (mirror or every or not is_url(remote)):
        raise Unsure("it pushes to %s, which is not a named remote with tracking refs to compare" % remote)
    found = []
    if mirror:
        n, subs = commits(cwd, g, ["--remotes=" + remote, "--not", "--branches", "--tags"])
        if n:
            found.append(("ask", "--mirror drops %s from %s that no local branch or tag has: %s. %s"
                          % (plural(n, "commit"), remote, commit_list(n, subs), DO_MIRROR)))
        backups = lines(git(cwd, g, "for-each-ref", "--format=%(refname)", "refs/git-guard"))
        if backups:  # --mirror pushes every ref, so the backups git-guard saved would be published too
            found.append(("ask", "--mirror would also publish %s to %s: %s. %s" % (
                plural(len(backups), "git-guard backup ref"), remote, ", ".join(backups[:SHOW]) + more(len(backups), SHOW),
                DO_MIRROR)))
        return found
    if every:
        for b in lines(git(cwd, g, "for-each-ref", "--format=%(refname:short)", "refs/heads")):
            if force and verify(cwd, g, "refs/remotes/%s/%s" % (remote, b)):
                n, subs = dropped(cwd, g, local_tip(cwd, g, b), "refs/remotes/%s/%s" % (remote, b))
                if n:
                    found.append(("ask", "%s/%s loses %s: %s. %s" % (
                        remote, b, plural(n, "commit"), commit_list(n, subs), DO_PUSH % (remote + "/" + b))))
        return found
    if not specs:
        cur = current_branch(cwd, g)
        if not cur:
            return None  # detached HEAD with no refspec: git refuses
        merge = config(cwd, g, "branch.%s.merge" % cur)
        same = config(cwd, g, "branch.%s.remote" % cur) == remote
        dst = merge[len("refs/heads/"):] if same and merge and merge.startswith("refs/heads/") else cur
        specs = ["HEAD:" + dst]
    for spec in specs:
        if unsure(spec):
            raise Unsure("the refspec is a variable (%s)" % spec)
        plus = spec.startswith("+")
        body = spec[1:] if plus else spec
        if "*" in body:
            raise Unsure("the refspec %s is a pattern" % spec)
        src, dst = body.split(":", 1) if ":" in body else (body, body)
        gone = delete or src == ""
        if not (force or plus or gone):
            continue
        if dst == "HEAD":
            dst = current_branch(cwd, g) or ""
        if dst.startswith("refs/") and not dst.startswith("refs/heads/"):
            continue  # tags and other refs: not branches
        dst = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
        if not dst:
            continue
        tracking = "refs/remotes/%s/%s" % (remote, dst)
        tip = remote_tip(cwd, g, remote, dst, tracking)
        if tip is None:
            continue  # no such branch on the remote: nothing to drop
        if gone:
            # warn only; a local branch this line also deletes holds nothing afterwards
            local = [dst] if dst in deletes["branches"] else []
            n, subs = commits(cwd, g, [tip] + holders(local, [remote + "/" + dst]))
            if not n:
                continue
            if already_merged(cwd, g, remote, tip):
                log("quiet", cwd, "git push: deleting %s/%s: content already on the main branch" % (remote, dst))
                continue
            found.append(("warn", "git push deletes %s/%s (tip %s) with %s on no other branch, tag or "
                          "upstream: %s" % (remote, dst, tip[:9], plural(n, "commit"), commit_list(n, subs))))
        else:
            src = local_tip(cwd, g, src or "HEAD")
            if not verify(cwd, g, src):
                continue  # git will fail on the bad source
            n, subs = dropped(cwd, g, src, tip)
            if n:
                found.append(("ask", "git push force push drops %s now on %s/%s: %s. %s" % (
                    plural(n, "commit"), remote, dst, commit_list(n, subs), DO_PUSH % (remote + "/" + dst))))
    return found


def dropped(cwd, g, src, tip):
    """(count, 'hash subject' lines) of commits on tip that pushing src over it drops.
    A commit whose patch src already has doesn't count (a plain rebase). A commit that only
    shares a subject with one of src's commits still counts, marked "may be a rebased copy":
    a rebase that resolved conflicts changes the patch but keeps the subject, and a different
    commit can share a subject too. If telling copies apart fails or times out, all count."""
    n, subs = commits(cwd, g, [tip, "^" + src])
    if not n:
        return 0, []
    try:
        rows = lines(git(cwd, g, "log", "--cherry-pick", "--right-only", "--format=%h %s",
                         "%s...%s" % (src, tip)))
        if git(cwd, g, "merge-base", src, tip, ok_codes=(0, 1)).strip():
            mine = set(git(cwd, g, "log", "--format=%s", src, "^" + tip).split("\n"))
            rows = [r + " (may be a rebased copy)" if r.partition(" ")[2] in mine else r for r in rows]
    except GitError:
        return n, subs
    return len(rows), rows[:SHOW]


def already_merged(cwd, g, remote, commit):
    """Is all of commit's content already on the main branch (as after a squash merge)?
    True when merging it into the main branch would leave that branch's tree unchanged."""
    try:
        own = verify(cwd, g, commit)
        for base in ("refs/remotes/%s/HEAD" % remote, "refs/remotes/%s/main" % remote,
                     "refs/remotes/%s/master" % remote, "refs/heads/main", "refs/heads/master"):
            at = verify(cwd, g, base)
            if at and at != own:  # deleting main itself: nothing to compare with
                out = lines(git(cwd, g, "merge-tree", "--write-tree", base, commit, ok_codes=(0, 1)))
                return bool(out) and out[0] == git(cwd, g, "rev-parse", base + "^{tree}").strip()
    except GitError:
        pass
    return False


# Per hook run: where a switch, checkout or `reset --hard` earlier in the line left HEAD
# (top folder -> (commit, branch or None)), branches and tags it made or moved
# ((top, name) -> commit), the remote branch lists already asked for, and the network deadline.
HEADS = {}
MOVED = {}
TAGS = {}
REMOTE_HEADS = {}
NET_SECONDS = 6
NET_DEADLINE = []


def toplevel(cwd, g):
    return git(cwd, g, "rev-parse", "--show-toplevel", ok_codes=(0, 128)).strip() or None


def head_state(cwd, g):
    """(commit, branch) HEAD is at once the earlier commands in the line ran; commit is
    "HEAD" when none of them moved it."""
    st = HEADS.get(toplevel(cwd, g)) if HEADS else None
    if st:
        return st
    out = git(cwd, g, "symbolic-ref", "-q", "--short", "HEAD", ok_codes=(0, 1)).strip()
    return "HEAD", out or None


def current_branch(cwd, g):
    return head_state(cwd, g)[1]


def local_tip(cwd, g, src):
    """src as the line left it: HEAD, @, HEAD~2 and branches moved by an earlier switch,
    checkout or `reset --hard` in this line name the commit they moved to."""
    if not HEADS and not MOVED:
        return src
    top = toplevel(cwd, g)
    commit, branch = HEADS.get(top, ("HEAD", None))
    for name in ("HEAD", "@"):
        if src == name or src.startswith((name + "~", name + "^")):
            return commit + src[len(name):]
    name = src[len("refs/heads/"):] if src.startswith("refs/heads/") else src
    return MOVED.get((top, name), src)


def track_switch(cwd, g, sub, args):
    """Note where `git switch` / `git checkout` of a branch or commit (not of files) leaves HEAD."""
    if "--" in args:
        return  # checkout of files
    new, reset, detach, pos = None, False, False, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-b", "-B", "-c", "-C", "--orphan"):
            if a == "--orphan" or i + 1 >= len(args):
                return  # an unborn branch: nothing to compare with
            new, reset = args[i + 1], a in ("-B", "-C")
            i += 1
        elif a == "--detach" or (sub == "switch" and a == "-d"):
            detach = True
        elif not a.startswith("-"):
            pos.append(a)
        i += 1
    if len(pos) > 1 or any(unsure(w) for w in pos + [new or ""]):
        return
    top = toplevel(cwd, g)
    if not top:
        return
    if new:
        if not reset and verify(cwd, g, "refs/heads/" + new):
            return  # git refuses: the branch exists
        commit = verify(cwd, g, local_tip(cwd, g, pos[0] if pos else "HEAD"))
        if commit:
            HEADS[top] = (commit, new)
            MOVED[(top, new)] = commit
        return
    if not pos:
        return
    x = pos[0]
    if not detach and verify(cwd, g, "refs/heads/" + x):
        HEADS[top] = (MOVED.get((top, x)) or verify(cwd, g, "refs/heads/" + x), x)
    elif not detach and verify(cwd, g, "refs/remotes/origin/" + x):  # git makes x from origin/x
        HEADS[top] = (verify(cwd, g, "refs/remotes/origin/" + x), x)
    else:
        commit = verify(cwd, g, local_tip(cwd, g, x))
        if commit:
            HEADS[top] = (commit, None)  # a commit: detached; a file name verifies to nothing


def track_branch(cwd, g, args):
    """Note where `git branch [-f] <name> [<start>]` (create or move, nothing else) leaves name."""
    pos, force = [], False
    for a in args:
        if a.startswith("--"):
            force = force or is_long(a, "--force", 4)
            if not (is_long(a, "--force", 4) or is_long(a, "--quiet", 3) or is_long(a, "--no-track", 4)
                    or a == "--track" or a.startswith("--track=")):
                return  # --list, --delete, --move, --copy, --set-upstream-to and the rest
        elif a.startswith("-") and len(a) > 1:
            if a.strip("-fqt"):
                return
            force = force or "f" in a
        else:
            pos.append(a)
    if not 1 <= len(pos) <= 2 or any(unsure(w) for w in pos):
        return
    if pos[0] == current_branch(cwd, g) or not force and verify(cwd, g, "refs/heads/" + pos[0]):
        return  # git refuses: the checked-out branch, or an existing one without -f
    top = toplevel(cwd, g)
    commit = top and verify(cwd, g, local_tip(cwd, g, pos[1] if len(pos) > 1 else "HEAD"))
    if commit:
        MOVED[(top, pos[0])] = commit


def track_tag(cwd, g, args):
    """Note where `git tag [-a] [-f] [-m msg] <name> [<commit>]` (create or move, nothing else) leaves name."""
    pos, force, i = [], False, 0
    while i < len(args):
        a = args[i]
        if a in ("-m", "-F", "-u", "--message", "--file", "--local-user", "--cleanup"):
            i += 2
            continue
        if a.startswith("--"):
            force = force or is_long(a, "--force", 3)
            if not (is_long(a, "--force", 3) or a.split("=", 1)[0] in (
                    "--annotate", "--sign", "--no-sign", "--message", "--file", "--local-user", "--cleanup",
                    "--create-reflog", "--edit", "--no-edit")):
                return  # --list, --delete, --verify, --contains and the rest
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch in "mFu":  # takes a value: the rest of the word, or the next word
                    i += k == len(a) - 2
                    break
                if ch not in "asfe":
                    return
                force = force or ch == "f"
        else:
            pos.append(a)
        i += 1
    if not 1 <= len(pos) <= 2 or any(unsure(w) for w in pos):
        return
    if not force and verify(cwd, g, "refs/tags/" + pos[0]):
        return  # git refuses: the tag exists
    top = toplevel(cwd, g)
    commit = top and verify(cwd, g, local_tip(cwd, g, pos[1] if len(pos) > 1 else "HEAD"))
    if commit:
        TAGS[(top, pos[0])] = commit


def remote_tip(cwd, g, remote, branch, tracking):
    """The commit the remote branch is at: the remote-tracking ref, else `git ls-remote --heads`
    (read-only, no fetch; once per remote, 6 s in all). None when the remote has no such branch."""
    tip = verify(cwd, g, tracking)
    if tip:
        return tip
    key = (toplevel(cwd, g), remote)
    if key not in REMOTE_HEADS:
        if not NET_DEADLINE:
            NET_DEADLINE.append(time.monotonic() + NET_SECONDS)
        left = NET_DEADLINE[0] - time.monotonic()
        if left < 0.5:
            raise Unsure("there is no local copy of %s/%s and there was no time left to ask the remote"
                         % (remote, branch))
        try:
            out = git(cwd, g, "ls-remote", "--heads", remote, timeout=left)
        except GitError as e:
            raise Unsure("there is no local copy of %s/%s and asking the remote failed (%s)"
                         % (remote, branch, str(e)[:80]))
        REMOTE_HEADS[key] = {ref[len("refs/heads/"):]: sha for sha, ref in
                             (row.split("\t", 1) for row in lines(out)) if ref.startswith("refs/heads/")}
    sha = REMOTE_HEADS[key].get(branch)
    if sha is None:
        return None
    if not verify(cwd, g, sha):
        raise Unsure("%s/%s is at %s, a commit this clone has never fetched" % (remote, branch, sha[:9]))
    return sha


def uncommitted(cwd, g):
    """'N files (+a -r lines): f (+x -y), ...' for tracked changes against HEAD, or ''. Staged changes
    count too: a file staged and then restored in the working tree differs from HEAD only in the index."""
    counts = {}
    for args in (("diff", "HEAD"), ("diff", "--cached", "HEAD")):
        for row in lines(git(cwd, g, *args, "--numstat", "--no-renames")):
            add, rem, path = row.split("\t", 2)
            counts.setdefault(path, (add, rem))
    rows = [(path, int(add) if add.isdigit() else 0, int(rem) if rem.isdigit() else 0, not add.isdigit())
            for path, (add, rem) in counts.items()]
    if not rows:
        return ""
    shown = ", ".join("%s (%s)" % (p, "binary" if b else "+%d -%d" % (a, r)) for p, a, r, b in rows[:SHOW])
    return "%s (+%d -%d lines): %s%s" % (plural(len(rows), "file"), sum(r[1] for r in rows),
                                        sum(r[2] for r in rows), shown, more(len(rows), SHOW))


def untracked_under(cwd, g, to):
    """Untracked files, ignored ones included, anywhere in the repository (not only under cwd), that
    `reset --hard <to>` would overwrite or delete without a prompt. git deletes an untracked path that is in
    the way of a tracked one: the same path, a file where the target has a directory (or the reverse), or a
    file inside a path the target tracks as a file."""
    top = toplevel(cwd, g) or cwd  # ls-files from a subfolder lists only that subfolder
    tracked = set(lines(git(top, g, "ls-tree", "-r", "--full-tree", "--name-only", to)))
    dirs = set()
    for t in tracked:
        parts = t.split("/")
        dirs.update("/".join(parts[:k]) for k in range(1, len(parts)))
    clash = []
    for p in lines(git(top, g, "ls-files", "--others", "--full-name")):
        parts = p.split("/")
        ancestors = {"/".join(parts[:k]) for k in range(1, len(parts))}
        if p in tracked or p in dirs or ancestors & tracked:
            clash.append(p)
    return clash


def check_reset(cwd, g, args):
    if not any(a.startswith("--") and is_long(a, "--hard", 4) for a in args):
        return None
    if "--" in args:
        if args[args.index("--") + 1:]:
            return None  # paths with --hard: git refuses
        args = args[:args.index("--")]
    pos = [a for a in args if not a.startswith("-")]
    target = pos[0] if pos else "HEAD"
    if unsure(target):
        raise Unsure("the target is a variable (%s)" % target)
    found = []
    changes = uncommitted(cwd, g)
    if changes:
        # Save them and only warn: a yes to a prompt once lost files with no copy left.
        ref = save_dirty(cwd, g)
        if ref:
            found.append(("warn", "git reset --hard: git-guard saved uncommitted changes in %s at %s "
                          "(restore: git stash apply --index %s)" % (changes, ref, ref)))
        else:
            found.append("discards uncommitted changes in %s. %s" % (changes, DO_RESET_FILES))
    to = verify(cwd, g, local_tip(cwd, g, target))
    if to:
        top = toplevel(cwd, g)
        head, branch = head_state(cwd, g)
        # branches and tags made or moved earlier in the line hold where they point now,
        # not their old tips; the branch being reset holds nothing
        made = [b for t, b in MOVED if t == top and b != branch]
        tags = [t for tp, t in TAGS if tp == top]
        extra = [MOVED[(top, b)] for b in made] + [TAGS[(top, t)] for t in tags]
        n, subs = commits(cwd, g, [head, "^" + to] + holders(([branch] if branch else []) + made, (), tags, extra))
        if top:
            HEADS[top] = (to, branch)
            if branch:
                MOVED[(top, branch)] = to
        clash = untracked_under(cwd, g, to)
        if clash:
            found.append("would overwrite %s that git does not track: %s. %s" % (
                plural(len(clash), "untracked file"), ", ".join(clash[:SHOW]) + more(len(clash), SHOW), DO_CLEAN))
        if n:
            # The commits are all behind HEAD, so one ref keeps them: save it and only warn,
            # so a throwaway test commit doesn't stop the run with a prompt.
            ref = save_reset(cwd, g, head)
            if ref:
                found.append(("warn", "git reset --hard: git-guard saved %s that would end up on no branch at %s "
                              "(restore: git branch <name> %s): %s" % (plural(n, "commit"), ref, ref, commit_list(n, subs))))
            else:
                found.append("leaves %s on no branch, tag or remote: %s. %s"
                             % (plural(n, "commit"), commit_list(n, subs), DO_RESET_COMMITS))
    return found


def save_dirty(cwd, g):
    """Keeps the tracked uncommitted changes (index and files) as a stash commit at
    refs/git-guard/dirty-<sha>, touching neither; None on failure."""
    try:
        sha = git(cwd, g, "stash", "create", "git-guard: uncommitted changes before reset --hard",
                  env=GUARD_ID).strip()
        if not sha:
            return None
        ref = "refs/git-guard/dirty-%s" % sha[:8]
        if not os.environ.get("GIT_GUARD_DRY"):
            git(cwd, g, "update-ref", ref, sha)
        log("saved", cwd, "uncommitted changes before reset --hard kept at %s" % ref)
        return ref
    except (GitError, Unsure):
        return None


def save_reset(cwd, g, head):
    """Keeps head (and so every commit a reset leaves behind) at refs/git-guard/reset-<sha>; None on failure."""
    try:
        sha = verify(cwd, g, head)
        if not sha:
            return None
        ref = "refs/git-guard/reset-%s" % sha[:8]
        if not os.environ.get("GIT_GUARD_DRY"):
            git(cwd, g, "update-ref", ref, sha)
        log("saved", cwd, "commits before reset --hard kept at %s" % ref)
        return ref
    except (GitError, Unsure):
        return None


def check_clean(cwd, g, args):
    force = False
    dry = ["-n"]
    blind = None  # a path argument the hook can't read
    value = False  # the next argument is the pattern of -e / --exclude
    for a in args:
        if value:
            dry.append(a)
            value = False
            continue
        if a == "--":
            dry.append(a)
            continue
        if a.startswith("--"):
            value = a == "--exclude"
            if is_long(a, "--force", 4):
                force = True
            elif is_long(a, "--dry-run", 4) or is_long(a, "--interactive", 4):
                return None
            elif not is_long(a, "--quiet", 4):
                dry.append(a)
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:].split("e", 1)[0]  # after -e comes its pattern, not more flags
            if "n" in letters or "i" in letters:
                return None
            force = force or "f" in letters
            value = a.endswith("e") and len(letters) == len(a) - 2  # a bare -e: the pattern is next
            rest = "-" + letters.replace("q", "") + a[1 + len(letters):]
            if rest != "-":
                dry.append(rest)
        else:
            blind = blind or (a if unsure(a) else None)
            dry.append(a)
    if not force:
        return None
    if blind:
        raise Unsure("the path %s is a variable or command output" % blind)
    gone = [x[len("Would remove "):] for x in lines(git(cwd, g, "clean", *dry)) if x.startswith("Would remove ")]
    if not gone:
        return None
    return ["deletes %s for good: %s%s. %s" % (plural(len(gone), "untracked path"), ", ".join(gone[:SHOW]),
                                               more(len(gone), SHOW), DO_CLEAN)]


def worktree_path(cwd, p):
    """p as a folder (when the hook can tell), else p itself, to compare add and remove paths."""
    p = os.path.expanduser(p)
    return os.path.normpath(os.path.join(cwd or "", p)) if cwd or os.path.isabs(p) else p


def worktree_by_name(cwd, g, name):
    """The worktree path git means by `name` when it matches the trailing parts of exactly
    one worktree's path (git tries this before reading name as a path)."""
    tail = "/" + name.strip("/")
    hits = [w[len("worktree "):] for w in lines(git(cwd, g, "worktree", "list", "--porcelain"))
            if w.startswith("worktree ") and w.endswith(tail)]
    return hits[0] if len(hits) == 1 else None


GUARD_ID = {"GIT_AUTHOR_NAME": "git-guard", "GIT_AUTHOR_EMAIL": "git-guard",
            "GIT_COMMITTER_NAME": "git-guard", "GIT_COMMITTER_EMAIL": "git-guard"}


def save_worktree(path, g):
    """Commits path's uncommitted and untracked (not ignored) files through a throwaway index,
    keeps the commit at refs/git-guard/<folder>-<sha>, and returns that ref (None on any failure)."""
    import tempfile
    fd, idx = tempfile.mkstemp(prefix="git-guard-index.")
    os.close(fd); os.unlink(idx)
    try:
        env = {"GIT_INDEX_FILE": idx}
        git(path, g, "read-tree", "HEAD", env=env)
        git(path, g, "add", "-A", timeout=8, env=env)  # the hook itself is killed at 15 s
        tree = git(path, g, "write-tree", env=env).strip()
        # A nested repo or submodule is saved only as a pointer to a commit that may live
        # inside the folder being deleted, so its work isn't really kept: ask instead.
        links = lambda t: {x for x in lines(git(path, g, "ls-tree", "-r", t)) if x.startswith("160000 ")}
        if links(tree) - links("HEAD") or any(x[:1] in "+U" for x in lines(git(path, g, "submodule", "status"))):
            return None
        sha = git(path, g, "commit-tree", tree, "-p", "HEAD", "-m", "git-guard: uncommitted files of %s before "
                  "worktree remove --force" % path, env=dict(env, **GUARD_ID)).strip()
        ref = "refs/git-guard/%s-%s" % (os.path.basename(path.rstrip("/")), sha[:8])
        if not os.environ.get("GIT_GUARD_DRY"):
            git(path, g, "update-ref", ref, sha)
        log("saved", path, "uncommitted files kept at %s" % ref)
        return ref
    except (GitError, Unsure):
        return None
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def check_worktree(cwd, g, args, added):
    """[(level, message)]: "ask" for uncommitted and untracked (not ignored) files only;
    a branch outlives its worktree, so its commits never ask."""
    if not args or args[0] != "remove":
        return None
    rest = args[1:]
    force = any(is_long(a, "--force", 3) if a.startswith("--") else a.startswith("-") and "f" in a for a in rest)
    pos = [a for a in rest if not a.startswith("-")]
    if not force or not pos:
        return None
    if worktree_path(cwd, pos[0]) in added:
        log("added-here", cwd, "git worktree remove %s: added earlier in the same command"
            % pos[0].replace(FRESH, "$(mktemp)"))
        return None
    if unsure(pos[0]):
        raise Unsure("the worktree path is a variable (%s)" % pos[0])
    if cwd is None:
        raise Unsure("the folder it runs in uses a variable or command output")
    path = os.path.normpath(os.path.join(cwd, os.path.expanduser(pos[0])))
    if not os.path.isabs(os.path.expanduser(pos[0])) and FRESH not in cwd and os.path.isdir(cwd):
        path = worktree_by_name(cwd, g, pos[0]) or path
    found = []
    # git() rejects mktemp and missing folders
    status = lines(git(path, g, "status", "--porcelain", "--untracked-files=normal"))
    # Losing these loses nothing: a deleted tracked file is still in git, and removing an
    # untracked symlink (e.g. node_modules linked to the main checkout) keeps its target.
    status = [s for s in status if set(s[:2]) != {" ", "D"} and set(s[:2]) != {"D"}
              and not (s.startswith("??") and os.path.islink(os.path.join(path, s[3:].rstrip("/"))))]
    saved = status and AGENT_WT.search(os.path.realpath(path)) and save_worktree(path, g)
    if saved:
        found.append(("warn", "git worktree remove --force: an agent worktree, so git-guard saved its %s to "
                      "%s first (get them back in a new folder: git worktree add <folder> %s)" % (plural(len(status), "uncommitted path"), saved, saved)))
    elif status:
        paths = [s[3:] for s in status]
        untracked = sum(s.startswith("??") for s in status)
        found.append(("ask", "git worktree remove --force deletes %s (%d untracked, %d changed tracked): %s%s. %s" % (
            plural(len(paths), "uncommitted path"), untracked, len(paths) - untracked,
            ", ".join(paths[:SHOW]), more(len(paths), SHOW), DO_WORKTREE % len(paths))))
    if current_branch(path, g) is None:
        n, subs = commits(path, g, ["HEAD"] + holders())
        if n:
            found.append(("warn", "git worktree remove: its detached HEAD has %s on no branch, tag or remote, "
                          "lost with the worktree (git branch <name> %s keeps them): %s"
                          % (plural(n, "commit"), subs[0].split()[0], commit_list(n, subs))))
    return found


def check_stash(cwd, g, args):
    if args and not args[0].startswith("-"):
        sub, rest = args[0], args[1:]
    else:
        sub, rest = "push", args
    if sub in ("push", "save"):
        untracked = any(a in ("-u", "--include-untracked", "-a", "--all") for a in rest)
        paths = stash_paths(rest) if sub == "push" else []
        if any(unsure(p) for p in paths):
            paths = []  # can't tell which paths: count them all
        st = [s for s in lines(git(cwd, g, "status", "--porcelain", "--", *paths))
              if untracked or not s.startswith("??")]
        if not st:
            return None
        return ["git stash moves %s out of the working tree (get them back with git stash pop)"
                % plural(len(st), "changed path")]
    if sub not in ("drop", "clear", "pop"):
        return None
    entries = lines(git(cwd, g, "stash", "list"))
    if not entries:
        return None
    if sub == "clear":
        return ["git stash clear deletes all %s: %s" % (plural(len(entries), "stash entry"),
                                                        "; ".join(entries[:SHOW]))]
    ref = next((a for a in rest if not a.startswith("-")), "stash@{0}")
    if unsure(ref):
        raise Unsure("the stash entry is a variable")
    if ref.isdigit():
        ref = "stash@{%s}" % ref
    entry = next((e for e in entries if e.startswith(ref + ":")), None)
    if entry is None:
        return None
    if sub == "pop":
        return ["git stash pop applies and then drops %s" % entry]
    return ["git stash drop deletes %s (only its hash can bring it back)" % entry]


def stash_paths(args):
    """The pathspecs of `git stash push` args (all after --; -m takes a value); none: all paths."""
    paths, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return paths + args[i + 1:]
        if a in ("-m", "--message", "--pathspec-from-file"):
            i += 1
        elif not a.startswith("-"):
            paths.append(a)
        i += 1
    return paths


def check_branch(cwd, g, args, deletes):
    delete = force = remote = False
    names = []
    for a in args:
        if a.startswith("--"):
            if is_long(a, "--delete", 5):
                delete = True
            elif is_long(a, "--force", 4):
                force = True
            elif is_long(a, "--remotes", 5):
                remote = True
        elif a.startswith("-") and len(a) > 1:
            force = force or "D" in a or "f" in a
            delete = delete or "D" in a or "d" in a
            remote = remote or "r" in a
        else:
            names.append(a)
    if not (delete and force):
        return None
    found = []
    for name in names:
        if unsure(name):
            raise Unsure("the branch name is a variable (%s)" % name)
        ref = ("refs/remotes/" if remote else "refs/heads/") + name
        tip = verify(cwd, g, ref)
        if not tip:
            continue
        # a remote branch this line also deletes holds nothing afterwards
        hits = [] if remote else [r or "origin" for r, b in deletes["pushes"] if b == name]
        if remote:
            n, subs = commits(cwd, g, [ref] + holders((), [name]))
        else:
            n, subs = commits(cwd, g, [ref] + holders([name], ["%s/%s" % (r, name) for r in hits]))
        if not n:
            continue
        if already_merged(cwd, g, (hits or ["origin"])[0], ref):
            log("quiet", cwd, "git branch -D %s: content already on the main branch" % name)
            continue
        found.append("git branch -D %s (tip %s) deletes the only ref to %s: %s"
                     % (name, tip[:9], plural(n, "commit"), commit_list(n, subs)))
    return found


# push and branch also take the line's deletes; worktree the paths added earlier in the line.
# push and worktree return (level, message) pairs; the rest plain messages at their kind's level.
CHECKS = {"push": check_push, "reset": check_reset, "clean": check_clean, "worktree": check_worktree,
          "stash": check_stash, "branch": check_branch}
WARN_KINDS = ("stash", "branch")


# ------------------------------------------------------------- shell walk

PREFIXES = ("sudo", "env", "nohup", "command", "exec", "nice", "caffeinate", "timeout", "gtimeout", "time",
            "builtin", "noglob", "arch", "watch")
TIMEOUT_FLAGS = ("-s", "-k", "--signal", "--kill-after")
WRAPPER_FLAGS = {"env": ("-u", "--unset", "-C", "--chdir"), "sudo": ("-u", "-g"), "nice": ("-n",),
                 "timeout": TIMEOUT_FLAGS, "gtimeout": TIMEOUT_FLAGS, "caffeinate": ("-t", "-w"),
                 "arch": ("-e", "-d"), "watch": ("-n", "--interval")}
SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
GIT_VALUE_OPTS = ("-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path")


def is_name_char(ch):
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch == "_"


def is_assignment(w):
    eq = w.find("=")
    return eq > 0 and not ("0" <= w[0] <= "9") and all(is_name_char(ch) for ch in w[:eq])


VAR = None  # compiled on the slow path


def expand(word, vars_):
    """word with known $name / ${name} / leading ~ replaced; unknown ones stay (so `$` marks unsure)."""
    global VAR
    if VAR is None:
        VAR = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
    if word == "~" or word.startswith("~/"):
        word = os.path.expanduser("~") + word[1:]
    if "$" not in word:
        return word

    def sub(m):
        whole = m.group(0) or ""
        name = m.group(1) or m.group(2)
        val = vars_.get(name)
        if val is not None:
            return str(val)
        if name in ("HOME", "USER"):
            return os.environ.get(name) or whole
        if name == "TMPDIR":  # Claude Code's Bash tool sets its own TMPDIR
            own = "/tmp/claude-%d" % os.getuid()
            return own if os.path.isdir(own) else (os.environ.get("TMPDIR") or whole).rstrip("/")
        return whole
    return VAR.sub(sub, word)


def plain_words(seg):
    """A command's words without its redirections."""
    words, skip = [], False
    for w in seg:
        if skip:
            skip = False
        elif w.startswith(REDIR):
            skip = True
        else:
            words.append(w)
    return words


def line_deletes(text, out=None):
    """The raw name words of every branch the line deletes: local ones (`git branch -D`)
    as "branches", remote ones (`git push --delete` / `:name`) as "pushes" (remote word or
    None, name). A branch deleted in both places is held by neither ref afterwards."""
    out = out or {"branches": [], "pushes": []}
    for seg in shell_segments(text):
        if seg[0] == M_SUB:
            line_deletes(seg[1], out)
            continue
        seg = plain_words(seg)
        if "git" not in seg:
            continue
        rest = seg[seg.index("git") + 1:]
        if "branch" in rest:
            args = rest[rest.index("branch") + 1:]
            flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
            longs = [a for a in args if a.startswith("--")]
            if "D" in flags or (("d" in flags or "--delete" in longs) and ("f" in flags or "--force" in longs)):
                out["branches"] += [a for a in args if not a.startswith("-")]
        elif "push" in rest:
            f, remote, specs = parse_push(rest[rest.index("push") + 1:])
            names = specs if f["delete"] else [s[1:] for s in specs if s.startswith(":")]
            out["pushes"] += [(remote, n[len("refs/heads/"):] if n.startswith("refs/heads/") else n)
                              for n in names]
    return out


KEYWORDS = ("do", "then", "else", "{", "!", "if", "elif", "while", "until")
LOOPS = ("for", "while", "until", "select")


def loop_end(items, i):
    """Index of the `done` that closes the loop whose body starts at items[i]."""
    level = 1
    for j in range(i, len(items)):
        w = items[j]
        if isinstance(w, str):
            continue
        if w[0] in LOOPS:
            level += 1
        elif w[0] == "done":
            level -= 1
            if level == 0:
                return j
    return len(items)


class Guard:
    def __init__(self, text, deletes):
        self.text = text
        self.deletes = deletes
        self.added = set()  # worktree paths a `git worktree add` earlier in the line made
        self.asks, self.warns = [], []

    def walk(self, text, cwd, vars_, depth):
        items = []  # a marker string, (M_SUB, text), or the words of one command without redirections
        for seg in shell_segments(text):
            if seg[0] == M_SUB:
                items.append((M_SUB, seg[1]))
                continue
            if seg[0].startswith("\x00"):
                items.append(seg[0])
                continue
            words = plain_words(seg)
            while words and words[0] in KEYWORDS:
                words = words[1:]
            if words:
                items.append(words)
        self.run(items, cwd, vars_, depth)

    def run(self, items, cwd, vars_, depth):
        """Follow the commands in order; a `for v in a b` loop runs its body once per value.
        Returns the folder at the end."""
        stack = []
        i = 0
        while i < len(items):
            words = items[i]
            i += 1
            vars_["PWD"] = cwd  # None (unknown folder) leaves $PWD unexpanded, so unsure
            if isinstance(words, tuple):  # a $( ) or backtick substitution: a subshell
                if depth < 3:
                    self.walk(words[1], cwd, dict(vars_), depth + 1)
                continue
            if isinstance(words, str):
                if words == M_OPEN:
                    stack.append(cwd)
                elif words == M_CLOSE and stack:
                    cwd = stack.pop()
                continue
            if words[0] == "for" and len(words) >= 3 and words[2] == "in":
                end = loop_end(items, i)
                vals = [expand(w, vars_) for w in words[3:]]
                if vals and all(not unsure(v) and not any(c in v for c in "*?[") for v in vals):
                    for v in vals[:20]:
                        cwd = self.run(items[i:end], cwd, dict(vars_, **{words[1]: v}), depth)
                else:
                    cwd = self.run(items[i:end], cwd, dict(vars_, **{words[1]: None}), depth)
                i = end + 1
                continue
            if words[0] == "done":
                continue
            new = self.command(words, cwd, vars_, depth)
            if new != cwd and self.cd_fails(words, new) and (i >= len(items) or items[i] == M_SEQ):
                new = cwd  # `cd missing; next`: next runs where the line already was
            cwd = new
        return cwd

    def cd_fails(self, words, path):
        """Does this cd surely fail: its folder is missing and nothing else in the line names it
        (so no mkdir, clone or worktree add made it)? Behind && or || the hook keeps the
        missing folder, so what follows fails open."""
        return (words[0] in ("cd", "pushd") and path is not None and FRESH not in path
                and not os.path.exists(path) and self.text.count(os.path.basename(path)) < 2)

    def command(self, words, cwd, vars_, depth):
        """Look at one simple command; return the folder after it."""
        if words[0] == "export":
            words = words[1:]
        if words and all(is_assignment(w) for w in words):
            for w in words:
                name, val = w.split("=", 1)
                v = expand(val, vars_)
                vars_[name] = FRESH if val.startswith("$(mktemp") else None if unsure(v) else v
            return cwd
        env = {}  # GIT_DIR / GIT_WORK_TREE set for this one command
        while words and is_assignment(words[0]):
            env.update([words[0].split("=", 1)])
            words = words[1:]
        while words and words[0] in PREFIXES:
            head, words = words[0], words[1:]
            while words and (words[0].startswith("-") or (head == "env" and is_assignment(words[0]))):
                if is_assignment(words[0]):
                    env.update([words[0].split("=", 1)])
                takes = words[0] in WRAPPER_FLAGS.get(head, ())
                words = words[1 + takes:]
            if head in ("timeout", "gtimeout") and words:
                words = words[1:]
        if not words:
            return cwd
        name = os.path.basename(expand(words[0], vars_))
        if name in ("cd", "pushd"):
            target = words[1] if len(words) > 1 else "~"
            if target == "-":
                return None
            t = expand(target, vars_)
            if unsure(t) or cwd is None and not os.path.isabs(t):
                return None
            return os.path.normpath(os.path.join(cwd or "/", t))
        if name == "popd":
            return None
        if name in SHELLS and depth < 3:
            # -c, also inside a cluster such as -lc or -xc
            flag = next((k for k, w in enumerate(words[1:-1], 1)
                         if w.startswith("-") and not w.startswith("--") and "c" in w), None)
            if flag is not None:
                self.walk(words[flag + 1], cwd, dict(vars_), depth + 1)
            return cwd
        if name == "eval" and depth < 3:
            self.walk(" ".join(words[1:]), cwd, dict(vars_), depth + 1)
            return cwd
        if name == "git":
            g = []
            for var, opt in (("GIT_DIR", "--git-dir"), ("GIT_WORK_TREE", "--work-tree")):
                if var in env:
                    g += [opt, expand(env[var], vars_)]
            self.git_command(words, cwd, vars_, g)
        return cwd

    def git_command(self, words, cwd, vars_, g):
        words = [expand(w, vars_) for w in words]
        i = 1
        while i < len(words) and words[i].startswith("-"):
            w = words[i]
            if w == "-C" and i + 1 < len(words):
                d = words[i + 1]
                cwd = None if unsure(d) or cwd is None else os.path.normpath(os.path.join(cwd, d))
                i += 2
            elif w in GIT_VALUE_OPTS and i + 1 < len(words):
                g += [w, words[i + 1]]
                i += 2
            else:
                g.append(w)
                i += 1
        if i >= len(words):
            return
        sub, args = words[i], words[i + 1:]
        if sub in ("switch", "checkout", "branch", "tag"):
            try:
                if sub == "branch":
                    track_branch(cwd, g, args)
                elif sub == "tag":
                    track_tag(cwd, g, args)
                else:
                    track_switch(cwd, g, sub, args)
            except (GitError, Unsure):
                pass  # nothing is allowed on this: the checks after it read the repo themselves
            if sub != "branch":
                return
        if sub == "worktree" and args[:1] == ["add"]:
            self.note_worktree_add(cwd, args[1:])
            return
        if sub not in CHECKS:
            return
        level = "warn" if sub in WARN_KINDS else "ask"
        kind = "worktree-remove" if sub == "worktree" else sub
        deletes = {"branches": {expand(w, vars_) for w in self.deletes["branches"]},
                   "pushes": [(r and expand(r, vars_), expand(b, vars_)) for r, b in self.deletes["pushes"]]}
        try:
            if sub == "worktree":
                found = check_worktree(cwd, g, args, self.added)
            elif sub in ("push", "branch"):
                found = CHECKS[sub](cwd, g, args, deletes)
            else:
                found = CHECKS[sub](cwd, g, args)
        except Unsure as e:
            log("unsure", cwd, "git %s: %s" % (sub, e))
            if level == "ask" and not e.warn:  # the ask checks only raise Unsure once they have seen the risky form
                self.asks.append((kind, cwd, "git %s: cannot tell what it would lose, because %s. %s"
                                  % (sub, e, DO_UNSURE)))
            elif e.warn:  # a branch deletion: say so, but never ask
                self.warns.append((kind, cwd, "git %s: cannot tell how many commits the deletion drops, "
                                   "because %s." % (sub, e)))
            return
        except GitError as e:
            log("git-error", cwd, "git %s: %s" % (sub, e))
            return
        prefix = "" if level == "warn" else "git %s " % (" ".join(words[i:i + 2]) if sub == "worktree" else sub)
        tagged = [f if isinstance(f, tuple) else (level, prefix + f) for f in found or ()]
        for lv in ("ask", "warn"):
            msgs = [m for x, m in tagged if x == lv]
            if msgs:
                (self.asks if lv == "ask" else self.warns).append((kind, cwd, "; ".join(msgs)))

    def note_worktree_add(self, cwd, args):
        """Remember the path of `git worktree add [opts] <path> [commit]`: a later
        `remove --force` of it in the same line deletes only what the line made."""
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-b", "-B", "--reason"):
                i += 2
            elif a.startswith("-"):
                i += 1
            else:
                # git refuses to add over an existing worktree, so only a path that is not
                # there yet (or can't be known now) is sure to hold only what the line made
                p = worktree_path(cwd, a)
                if unsure(p) or FRESH in p or not os.path.exists(p) or (os.path.isdir(p) and not os.listdir(p)):
                    self.added.add(p)
                return

    def output(self):
        for kind, cwd, msg in self.asks:
            log("ask:" + kind, cwd, msg)
        for kind, cwd, msg in self.warns:
            log("warn:" + kind, cwd, msg)
        if self.asks:
            body = [m for _, _, m in self.asks] + [m for _, _, m in self.warns]
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "git-guard: this would lose work. " + " | ".join(body),
                },
                "systemMessage": "git-guard: asking before a git command that would lose work.",
            }
        if self.warns:
            text = "git-guard warning: " + " | ".join(m for _, _, m in self.warns)
            return {
                "systemMessage": text,
                "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text},
            }
        return None


# ---------------------------------------- shell-command splitter
# shell_segments marks && / || and reads `name=(...)` arrays as unknown.
# Self-contained on purpose: hooks don't import each other.

HEREDOC_RE = r"<<-?[ \t]*(?P<q>['\"]?)(?P<tag>\w+)(?P=q)"
SHELLISH_RE = (r"\b(?:sh|bash|zsh|dash|ksh|ssh|su|eval|source|exec|sudo|env|xargs|docker|podman|kubectl|"
               r"tmux|screen|script|parallel|nohup|watch|timeout|osascript)\b|/dev/(?:stdin|fd)")
SHELL_PIPE_RE = r"\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b|\beval\b|\bsource\b"
M_SEQ, M_AND, M_OR, M_OPEN, M_CLOSE, M_SUB = "\x00;", "\x00&&", "\x00||", "\x00(", "\x00)", "\x00$"
REDIR = "\x01"


def strip_heredocs(command):
    """Drop the bodies of heredocs fed to anything but a shell."""
    heredoc, shellish, shell_pipe = re.compile(HEREDOC_RE), re.compile(SHELLISH_RE), re.compile(SHELL_PIPE_RE)
    lines_ = command.split("\n")
    out, i = [], 0
    while i < len(lines_):
        line = lines_[i]
        out.append(line)
        i += 1
        m = heredoc.search(line)
        if not m:
            continue
        end = next((j for j in range(i, len(lines_)) if lines_[j].strip() == m["tag"]), None)
        if end is None:
            continue
        if not shellish.search(line[:m.start()]) and not shell_pipe.search(line[m.end():]):
            i = end  # drop the body, keep the delimiter line
    return "\n".join(out)


def scan_subst(text, i):
    """(index after the `)` closing a `$(` whose contents start at i, whether it closed)."""
    depth, n = 1, len(text)
    while i < n:
        c = text[i]
        i += 1
        if c == "\\":
            i += 1
        elif c == "'":
            j = text.find("'", i)
            if j < 0:
                return n, False
            i = j + 1
        elif c == '"':
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i, True
    return n, False


def scan_tick(text, i):
    """(index after the backtick closing one whose contents start at i, whether it closed)."""
    n = len(text)
    while i < n:
        c = text[i]
        i += 1
        if c == "\\":
            i += 1
        elif c == "`":
            return i, True
    return n, False


def shell_segments(text):
    """Split shell text into simple commands (lists of words), the way the shell
    would: quotes and backslashes joined into words; `#` comments dropped; a `$( )`
    or backtick substitution kept whole inside its word; redirections turned into
    REDIR items. Between commands comes a one-item list holding a marker (M_OPEN /
    M_CLOSE for ( ), M_AND / M_OR for && ||, M_SEQ for any other separator). Before a command comes [M_SUB, text]
    for each substitution in it. An unclosed quote or substitution runs to the end."""
    segments, words, subs = [], [], []

    def flush():
        segments.extend([M_SUB, s] for s in subs)
        if words:
            segments.append(words)
        subs.clear()
    word, in_word = "", False
    quote = ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        i += 1
        if c == "\x00" or c == "\x01":
            continue
        if quote == "'":
            if c == "'":
                quote = ""
            else:
                word += c
        elif c == "`" or (c == "$" and i < n and text[i] == "("):
            if c == "`":
                end, closed = scan_tick(text, i)
                inner = text[i:end - 1] if closed else text[i:end]
                word += "`" + inner + ("`" if closed else "")
            else:
                end, closed = scan_subst(text, i + 1)
                inner = text[i + 1:end - 1] if closed else text[i + 1:end]
                word += "$(" + inner + (")" if closed else "")
            subs.append(inner)
            in_word = True
            i = end
        elif quote == '"':
            if c == '"':
                quote = ""
            elif c == "\\" and i < n and text[i] in "$`\"\\\n":
                if text[i] != "\n":
                    word += text[i]
                i += 1
            else:
                word += c
        elif c == "\\":
            if i < n:
                if text[i] != "\n":
                    word += text[i]
                    in_word = True
                i += 1
        elif c in "'\"":
            quote, in_word = c, True
        elif c == "#" and not in_word:
            while i < n and text[i] != "\n":
                i += 1
        elif c in "<>" or (c == "&" and i < n and text[i] == ">"):
            if in_word and word != "" and all(ch in "0123456789" for ch in word):
                pass  # `2>file`: the 2 is a file descriptor, not a word
            elif in_word:
                words.append(word)
            word, in_word = "", False
            # step over the rest of the operator: &> &>> >> >| >& <<< <<- << <& <>
            if c == "&":
                i += 2 if text.startswith(">>", i) else 1
            elif c == ">":
                i += i < n and text[i] in ">|&"
            elif text.startswith("<<<", i - 1) or text.startswith("<<-", i - 1):
                i += 2
            elif i < n and text[i] in "<&>":
                i += 1
            words.append(REDIR)
        elif c == "(" and in_word and word.endswith("=") and is_assignment(word):
            i, _ = scan_subst(text, i)  # `name=(a b)`: an array the hook doesn't follow
            word += "$(array)"
        elif c in " \t;&|()\n":
            if in_word:
                words.append(word)
            word, in_word = "", False
            if c not in " \t":
                flush()
                words = []
                mark = M_OPEN if c == "(" else M_CLOSE if c == ")" else M_SEQ
                if c in "&|" and i < n and (text[i] == c or c + text[i] == "|&"):
                    mark = M_AND if c + text[i] == "&&" else M_OR if c + text[i] == "||" else M_SEQ
                    i += 1  # && || |&
                segments.append([mark])
        else:
            word += c
            in_word = True
    if in_word:
        words.append(word)
    flush()
    return segments


main()
