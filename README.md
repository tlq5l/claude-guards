# claude-guards

Free guards for Claude Code: three hooks that check Bash commands before they run (git data loss, purchases, test failures hidden by a pipe), plus a `streamline` skill that trims finished work.

This kit was written and agent-tested with Claude Code.

## Items

- [git-guard](guards/git-guard/README.md): a hook that asks before a git command loses work.
- [purchase-guard](guards/purchase-guard/README.md): a hook that refuses shell commands that spend money until you approve in chat.
- [pipefail-guard](guards/pipefail-guard/README.md): a hook that stops test commands from hiding failures behind a pipe.
- [streamline](skills/streamline/README.md): a skill that reviews finished work and makes it leaner.

## git-guard

A hook that stops a git command before it can throw away your work. It runs before every Bash command. When the command is a risky git command, it looks at what would be lost and asks you to Proceed or Cancel. When nothing would be lost, it stays silent.

### What it stops

It asks before these commands, and only when they would lose something:

- `git push --force` and `--mirror`: shows the commits on the remote branch that the push would drop. A commit that only shares a subject with a local commit still asks. `--mirror` also asks when git-guard's `refs/git-guard/*` backups exist, since `--mirror` would publish them. A force push after a plain rebase (same patch, new hash) stays silent.
- `git reset --hard`: asks when the reset would delete or overwrite untracked files, ignored ones included, that the target commit tracks or that stand in the way of its paths, because git does this without a prompt. The check covers the whole repository, also when `git` runs from a subfolder.

It only warns, without asking, for:

- `git reset --hard` when there are staged uncommitted changes: it saves them to a hidden ref (`refs/git-guard/...`) and warns.

### What it does NOT cover

- Other ways to lose work: `rm -rf`, editing files by hand, deleting a repo folder, or any command that is not a git command the hook knows.
- Git commands that use a variable or command output in the path or branch name are not fully checked.
- Other permission modes. `tests/hook-cases.sh` runs in the default mode only.
- It is not a security boundary. A determined command can get around the text checks; it is a guard against accidents.

## purchase-guard

A hook that stops shell commands which spend money. The command is refused until you reply yes in chat.

### What it stops

It runs before every Bash command, and refuses these:

- `buy` or `purchase` subcommands, such as `vercel domains buy`.
- Non-GET `curl` calls to these Stripe endpoints: `/v1/charges`, `/v1/payment_intents`, `/v1/subscriptions`, `/v1/payouts`, `/v1/transfers`, `/v1/checkout/sessions`, `/v1/payment_links` and `/v1/invoices/<id>/pay`. Other Stripe endpoints, such as `/v1/customers`, are not checked. A Stripe test key (`sk_test_`, given with `-u`) is allowed; a live key is not.
- Non-GET `curl` calls to a URL whose path ends in `/purchase`, on any host.

A `curl` call with `-G` or `--get` sends its `-d` data as a GET query, so it is allowed unless `-X` sets another method. For example, `curl -G -d limit=3 https://api.stripe.com/v1/payment_intents` is allowed.

A help-only call is allowed: a call with `--help` or `-h` before any `--` (for example `vercel domains buy --help`). This does not apply to HTTP tools such as `curl`.

It looks inside `sh -c` and inside double-quoted `$(...)`. Single-quoted text is not run. Ordinary commands of the same tool go through (for example `vercel domains ls`).

**How approval works:**

1. The command is refused with a code such as `purchase check 3994d7`. Claude asks you in chat.
2. You reply yes. A reply such as "no, wait" does not approve.
3. Claude reruns the same command with `PURCHASE_APPROVED=1 ` in front. Each approval lets one rerun through.

### What it does NOT cover

- Purchases made through MCP tools or other apps. The hook reads only Bash commands.
- A script or program that buys inside (`./buy.sh`, `python buy.py`).
- Browser checkouts.
- Commands whose words are built from variables (`v=buy; tool $v`), `ssh host '<cmd>'`, `find -exec`, `gh api` billing calls.
- Paid size or tier changes such as `fly scale` or instance tiers.
- Unknown CLIs. Only the patterns listed above are recognized.
- curl attached form options (`--form=...`, `--form-string`) are not caught.
- Agents that turn the sandbox off and forge a line in their own transcript. This guards against agents that are misled, not ones that work around it.
- A command the parser cannot read is allowed through.
- The approval test uses a made-up transcript. It does not show that Claude Code still writes the `origin.kind` field set to `"human"` that the approval reads. If a later version changes that field, approvals never pass, so purchases stay blocked.

## pipefail-guard

A hook that stops test commands from hiding failures behind a pipe.

### What it stops

When Claude runs a test command through a pipe (for example `pytest | tail` or `python3 test_math.py | tee run.log`) without `set -o pipefail`, the exit code is the last command's code. A failing test can then look like a pass.

The hook reads each Bash command before it runs and denies a test command that is piped without `pipefail`. Commands that are not tests, and tests that already set `set -o pipefail`, are allowed.

It classifies the program each pipe stage runs, after skipping wrappers such as `timeout` and `sudo -u`. A test name inside a URL or an API path does not make a command a test (for example `curl https://example.com/pytest | head`). A quoted test file still counts (`'test_math.py'`).

It recognises `npm test`, `pytest`, `python -m pytest` and `test_*.py` files.

If the command reads a stage's status after the pipeline with `${PIPESTATUS[0]}` in bash, that clears the failure only for the stage it names, and only from the pipeline that finished last.

### What it does NOT cover

- Test commands it does not recognise, such as `./run-checks` or a Makefile target with another name.
- Tests run inside a script file that the hook never sees, or through `eval`.
- Whether the tests pass. It only makes the failure visible.
- A pipe used as an if or while condition (`if pytest | cat; then`) is not caught.
- It does not skip wrapper option values, so `uv run --project app pytest | cat` is not caught.
- It may treat `npm run build -- --mode test | cat` as a test run.
- Shells other than Bash-style `sh`, `bash`, `zsh`, `dash` and `ksh`.
- Claude Code tools other than Bash.
- Windows. Written and tested on macOS with Python 3.

It reads shell text with a small parser; it does not run the command, so it can miss an unusual quoting or nesting pattern.

## streamline (skill)

A Claude Code skill that asks Claude to review something you just made or changed (code, a document, a prompt, a plan, a config, and similar) and make it leaner. It looks for repeated text or work, parts nobody needs, wasted steps, and the wrong level of detail. It then applies small fixes, one at a time, and reports what it changed and what it left alone.

### What it stops

It blocks nothing. It is a skill, not a hook. It edits the file or change you point it at, and keeps the meaning: it should not change facts, promises or the tone you chose. If a cut would change meaning, it leaves that part alone and tells you. Deletions go to the system Trash, not `rm`.

### What it does NOT cover

- It does not look for bugs or errors. Use a separate bug review for that.
- It does not check the result by running your tests. It tells Claude to run the cheapest real check, so a missed check is possible.
- It uses `git diff` and `git status` when the folder is a git repo. Outside git, it works from the files Claude wrote in the session.
- Deleting needs `trash` (macOS) or `trash-put` from trash-cli (Linux). If neither is installed, Claude should ask you before it deletes anything.
- It does not work on a file you have not named unless the session shows a clear target. In that case it asks you one question.

## Install

Each guard needs `python3` (standard library only). git-guard also needs `git`. The guards are tested on macOS.

1. Copy the three scripts to a folder such as `~/.claude/hooks/`:

   ```bash
   mkdir -p ~/.claude/hooks
   cp guards/git-guard/git-guard.py guards/purchase-guard/purchase-guard.py guards/pipefail-guard/pipefail-guard.py ~/.claude/hooks/
   ```

2. Add the hooks to `~/.claude/settings.json` (all projects), or to a project's `.claude/settings.json` (one project). If the file already has a `PreToolUse` list, add the three commands to the existing `Bash` entry:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             { "type": "command", "command": "python3 \"$HOME/.claude/hooks/git-guard.py\"" },
             { "type": "command", "command": "python3 \"$HOME/.claude/hooks/purchase-guard.py\"" },
             { "type": "command", "command": "python3 \"$HOME/.claude/hooks/pipefail-guard.py\"" }
           ]
         }
       ]
     }
   }
   ```

   You can keep only the guards you want. Each one is a separate entry.

3. Optional, for the streamline skill: copy the folder `skills/streamline` (the folder that holds `SKILL.md`) to `~/.claude/skills/streamline/`. No `settings.json` change is needed.

4. Start a new Claude Code session so the settings and skill load.

## Tests

Run these from the repo root.

- `bash tests/hook-cases.sh`: checks the three hook scripts directly, with no model calls. It needs only `python3` and `git`. It prints PASS or FAIL per check and exits 1 on any FAIL.
- `bash tests/git-guard.sh`, `bash tests/pipefail-guard.sh`, `bash tests/purchase-guard.sh`, `bash tests/streamline.sh`: end-to-end runs that drive the real Claude Code CLI on Haiku in throwaway folders. They need the `claude` CLI, signed in, and make real (short) model calls, so they cost usage. Each prints PASS or FAIL per check and exits 1 on any FAIL.

## License

MIT. See [LICENSE](LICENSE).
