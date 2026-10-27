# streamline (Claude Code skill)

## What it does

Asks Claude to review something you just made or changed (code, a document,
a prompt, a plan, a config, and similar) and make it leaner. It looks for
repeated text or work, parts nobody needs, wasted steps, and the wrong level
of detail. It then applies small fixes, one at a time, and reports what it
changed and what it left alone.

Use it by typing `/streamline`, optionally with a target, for example
`/streamline the README`. It also starts on phrases such as "tighten this",
"clean this up" or "trim the fat".

## What it blocks or changes

- It blocks nothing. It is a skill, not a hook, so it does not stop any
  command or edit by itself.
- It edits the file or change you point it at. It keeps the meaning: it
  should not change facts, promises or the tone you chose. If a cut would
  change meaning, it leaves that part alone and tells you.
- Deletions go to the system Trash, not `rm`.

## How to install

Copy the folder `skills/streamline` (the folder that holds `SKILL.md`) to one
of these places:

- For all your projects: `~/.claude/skills/streamline/`
- For one project only: `<your-project>/.claude/skills/streamline/`

No `settings.json` change is needed. Start a new Claude Code session, then
type `/streamline`.

## What it does NOT cover

- It does not look for bugs or errors. Use a separate bug review for that.
- It does not check the result by running your tests. It tells Claude to run
  the cheapest real check, so a missed check is possible.
- It uses `git diff` and `git status` when the folder is a git repo. Outside
  git, it works from the files Claude wrote in the session.
- Deleting needs `trash` (macOS) or `trash-put` from trash-cli (Linux). If
  neither is installed, Claude should ask you before it deletes anything.
- It does not work on a file you have not named unless the session shows a
  clear target. In that case it asks you one question.
