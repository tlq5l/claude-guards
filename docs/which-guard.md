# Which guard stops which mistake

This kit has three hooks and one skill. Each one catches a different kind of mistake. Use this page to find the one that matches the mistake you are worried about.

> **Not a security boundary.** These guards are not a security boundary. A determined command can get around the text checks; they are a guard against accidents. Keep your other safeguards (permission settings, code review, account limits) in place. These guards do not replace them.

## The table

| Guard | Mistake it stops | What it does | What it leaves alone |
|---|---|---|---|
| [git-guard](../guards/git-guard/README.md) | Git commands that would throw away work: a force push that drops commits on the remote branch, or a `git reset --hard` that overwrites untracked files | Asks you to Proceed or Cancel, and shows what would be lost | Silent when nothing would be lost, for example a force push after a plain rebase of the same patch |
| [purchase-guard](../guards/purchase-guard/README.md) | Shell commands that spend money, such as `vercel domains buy` or a non-GET `curl` to a Stripe payment endpoint such as `/v1/payment_intents` | Refuses the command until you reply yes in chat | Ordinary commands of the same tool, and Stripe calls made with a test key (`sk_test_`) |
| [pipefail-guard](../guards/pipefail-guard/README.md) | A failing test that looks like a pass because a pipe hides its exit code, such as `pytest \| tail` | Denies the command and shows the rerun with `set -o pipefail; ` in front | Commands that are not tests, and tests that already set `set -o pipefail` |
| [streamline](../skills/streamline/README.md) (skill) | Finished work with repeated text or steps, needless parts, wasted effort, or the wrong level of detail | Reviews the work you name (or the session's changes) and applies small fixes that keep the meaning | Blocks nothing. It is a skill, not a hook |

## What each one does not cover

**git-guard**

- Other ways to lose work: `rm -rf`, editing files by hand, deleting a repo folder, or any git command it does not know.
- Git commands whose path or branch name comes from a variable or command output are not fully checked.

**purchase-guard**

- Purchases made through MCP tools or other apps. It reads only Bash commands.
- A script or program that buys inside it (`./buy.sh`, `python buy.py`), and browser checkouts.
- Paid size or tier changes, such as `fly scale`.

**pipefail-guard**

- Test commands it does not recognize, such as `./run-checks`, or tests run inside a script file it never sees.
- Whether the tests pass. It only makes a failure visible.

**streamline**

- Bugs and errors. Use a separate bug review for those.
- Checking the result by running your tests, so a missed check is possible.

As the table says, none of these guards is a security boundary. They guard against accidents and against agents that are misled, not against a command that is set on getting around them.

Back to the [README](../README.md).
