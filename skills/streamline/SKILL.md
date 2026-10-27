---
name: streamline
description: Review something that was just made or changed, whatever it is (code, a document, a prompt or AGENTS.md, a skill, a spreadsheet, a plan, a config, an email, a design, a workflow), for repeats, needless parts, wasted effort and wrong level of detail, then apply the fixes. The general version of Claude Code's simplify skill, which covers code only. Use when the user types /streamline (optionally with a target, e.g. "/streamline the README"), or says "tighten this", "clean this up", "trim the fat", "make this leaner", "is there anything redundant here", or asks for a quality pass on non-code work. Quality only: it does not hunt for errors or change what the thing does or says; use a bug review (for example, a code review) for bugs.
argument-hint: [what to streamline]
---

# Streamline

Take work that already does its job and make it leaner without changing what it does or says. Find the problems, then fix them. Don't stop at a list.

## 1. Pick the target

In this order:

1. What the user named in the arguments (a file, a section, "the email draft", "the plan above").
2. What changed in this session: `git diff` (plus `git status` for new files) if this is a git repo, otherwise the files you wrote or edited, otherwise the last thing you produced in chat.
3. If none of those gives a clear target, ask one short question. Don't guess a whole project.

State the target in one line before you start. Read all of it, not a skim: you can't spot a repeat you haven't seen.

## 2. Look through four lenses

Ask each lens of the target. Only keep a finding you can point to (a line, a cell, a paragraph, a step) and fix concretely.

- **Reuse (say it once).** Is something here said, built or computed twice? Is there already a thing in the project, the document set or the toolbox that does this? Replace the copy with a pointer to the original. Check the surroundings before you decide something is new.
- **Simplify (earn its place).** What can be removed without the result getting worse? Look for unused parts, hedges, scaffolding left from drafting, options nobody asked for, layers that only pass things along, and steps that undo each other. A part that exists "just in case" is a candidate. If you can't say what breaks without it, cut it.
- **Efficiency (cost to the user of it).** Where does the reader, the machine or the person running it spend effort for nothing? Examples: a repeated lookup, a long preamble before the point, a manual step that could be one command, a loop doing work already done, a table nobody reads. Fix the ones that matter, skip trivial ones.
- **Altitude (right level of detail).** Is each part written at the right level? Too low: step-by-step detail for something the reader already knows, special cases hard-coded where a general rule would do, a fix aimed at one symptom. Too high: vague advice where the reader needs the exact step, a concept with no example. Also check placement: does it live in the right file, section, layer or document?

## 3. Fix, with limits

- Apply each fix yourself. Keep each one small and separate so the user can undo one without losing the rest.
- **Preserve meaning.** The result must do and say the same thing. If a cut changes behaviour, facts, tone the user chose, or a promise to someone else, it is not a simplification: leave it and mention it.
- Don't add anything: no new features, tests, files or docs. The only new things allowed are ones that replace more than they add.
- Don't touch what you weren't asked about, even if it is also messy. Mention it in one line instead.
- When two lenses disagree (cutting a repeat makes a part harder to follow), keep the version a first-time reader understands.
- For deletions of files or data, move them to the system Trash, never `rm`: `trash <path>` on macOS, or `trash-put <path>` from the trash-cli package on Linux.
- Do this yourself in this session. Don't start other agents for it.

## 4. Check it still works

Run the cheapest real check for the kind of thing it is: tests, build or lint for code; re-render or re-open for a document, page or video; re-run a formula or script on the same input; re-read a plan or prompt against the original goal. If a check fails after your change, undo that change.

## 5. Report

Keep it short:

- **Changed:** one line per fix, naming the lens (Reuse, Simplify, Efficiency, Altitude) and where. Include before and after size if it is easy to count (lines, words, steps).
- **Left alone:** things that looked removable but could change meaning, with the reason, so the user decides.
- **Checked:** what you ran and the result. If you couldn't check, say so.

If you found nothing worth changing, say that plainly. Don't invent fixes to look useful.
