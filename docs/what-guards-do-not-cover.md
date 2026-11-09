# What the guards do not cover

This page lists what each guard leaves alone. The lists are copied from the "What it does NOT cover" sections of the [main README](../README.md). To find which guard fits which mistake, read [which-guard.md](which-guard.md).

> **Not a security boundary.** These guards are not a security boundary. A determined command can get around the text checks; they are a guard against accidents. Keep your other safeguards (permission settings, code review, account limits) in place. These guards do not replace them.

## git-guard

- Other ways to lose work: `rm -rf`, editing files by hand, deleting a repo folder, or any command that is not a git command the hook knows.
- Git commands that use a variable or command output in the path or branch name are not fully checked.
- Other permission modes. `tests/hook-cases.sh` runs in the default mode only.
- It is not a security boundary. A determined command can get around the text checks; it is a guard against accidents.

## purchase-guard

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

Back to the [README](../README.md).
