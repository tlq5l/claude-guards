#!/usr/bin/env python3
"""PreToolUse(Bash): deny test pipes only when a checked repair is available."""
import itertools
import json
import re
import sys

PREFIX = "set -o pipefail; "
REASON = "Piping hides the test's exit code. Rerun as: "
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
TEXT_COMMANDS = {"echo", "printf", "grep", "rg", "sed", "awk", "cat", "tee", "head", "tail",
                 "ls", "find", "git", "which", "pip", "pip3", "ps", "pgrep", "pkill", "wc", "sort", "uniq", "cut", "tr", "jq"}
TOKEN = re.compile(r"(?P<space>[ \t\r]+|\\\n)|"
                   r"(?P<comment>\#[^\n]*)|(?P<op>\d*[<>]&[\d-]+|&>>?|&&|\|\||\|&|[;|&(){}\n])|"
                   r"(?P<word>(?:\\.|\$\{[^}]*\}|[^\s;|&(){}'\"`\\])+)|(?P<backtick>`)")
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1")
STATUS = re.compile(r"\$\{(pipestatus|PIPESTATUS)\[([^\]}]+)\]\}")
ENV_WORD = re.compile(r"[A-Za-z_]\w*=")
WRAPPERS = {"env", "time", "timeout", "gtimeout", "nice", "nohup", "command", "exec", "sudo", "doas",
            "caffeinate", "stdbuf", "unbuffer", "xargs", "setsid"}
WRAPPER_VALUE_FLAGS = {"-s", "-k", "-n", "-u", "-C", "-g", "--signal", "--kill-after"}
PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?")
TEST_SCRIPT = re.compile(r"(?:test_.+|.+[-_]test)\.py")
TEST_SHELL_SCRIPT = re.compile(r".*tests?.*\.sh")
GRADLE_TEST = re.compile(r"(?:[\w-]*:)*test\w*")
PACKAGE_EDITS = {"add", "i", "install", "ls", "list", "why", "remove", "uninstall", "rm"}
RUNS_OTHER = {"exec", "dlx", "x"}


def basename(word):
    return word.rsplit("/", 1)[-1]


def after_flags(args):
    """The words from the first one that isn't a flag."""
    return args[next((i for i, a in enumerate(args) if not a.startswith("-")), len(args)):]


def program_at(words):
    """Index of the program a simple command runs. Skips VAR=value words and wrappers (env, timeout 60,
    sudo -u me, nice -n 5), so only the program itself is classified."""
    i = 0
    while i < len(words):
        if ENV_WORD.match(words[i]):
            i += 1
        elif basename(words[i]) in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or ENV_WORD.match(words[i])
                                      or re.fullmatch(r"[\d.]+[smhd]?", words[i])):
                i += 2 if words[i] in WRAPPER_VALUE_FLAGS else 1
        else:
            return i
    return None


def is_test(words):
    """Does this simple command run a test? Only the program it runs is classified. Its arguments count
    only where it hands the work on: npx vitest, pnpm exec vitest, uv run pytest, python3 -m pytest."""
    at = program_at(words)
    if at is None:
        return False
    name, args = basename(words[at]), words[at + 1:]
    if name in TEXT_COMMANDS:
        return False
    positional = [a for a in args if not a.startswith("-")]
    first = positional[0] if positional else ""
    if name in {"npx", "bunx", "pnpx"}:
        return is_test(after_flags(args))
    if name in {"npm", "pnpm", "yarn", "bun"}:
        cut = next((i for i, a in enumerate(args) if a in RUNS_OTHER), len(args))
        own = [a for a in args[:cut] if not a.startswith("-")]
        if own[:1] and own[0] in PACKAGE_EDITS:
            return False
        if cut < len(args):
            return is_test(after_flags(args[cut + 1:]))
        return own[:1] == ["t"] or any(a == "test" or a.startswith("test:") for a in own)
    if name in {"uv", "pipx", "poetry"}:
        return first == "run" and is_test(after_flags(args[args.index("run") + 1:]))
    if PYTHON.fullmatch(name):
        rest = python_script(args)
        if rest[:2] == ["-m", "pip"]:
            return False
        if rest[:1] == ["-m"] and len(rest) > 1:
            return rest[1] == "unittest" or is_test(rest[1:])
        return bool(rest) and TEST_SCRIPT.fullmatch(basename(rest[0])) is not None
    if name in {"vitest", "jest", "pytest"}:
        return not any(arg in {"--version", "-V", "--help", "-h"} for arg in args)
    if name == "claude":
        return args[:2] == ["plugin", "test"]
    if TEST_SHELL_SCRIPT.fullmatch(name):
        return True
    if name in {"node", "tsx"}:
        return "--test" in args or bool(first) and re.fullmatch(r".+\.(?:test|spec)\.[cm]?[jt]sx?", basename(first)) is not None
    if name in SHELLS:
        return bool(first) and TEST_SHELL_SCRIPT.fullmatch(basename(first)) is not None
    if name in {"go", "cargo", "playwright", "make", "deno", "vp", "swift", "mvn"}:
        return first == "test" or name == "cargo" and positional[:2] == ["nextest", "run"]
    if name in {"gradle", "gradlew"}:
        return any(GRADLE_TEST.fullmatch(arg) for arg in positional)
    if name == "xcodebuild":
        return any(arg in {"test", "test-without-building"} for arg in positional)
    return False


def python_script(args):
    """The words from a python command's -m or script name on, after its own options (-u, -X dev, -W x)."""
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] not in ("-m", "-c", "-"):
        i += 2 if args[i] in ("-W", "-X") else 1
    return args[i:]


def option_state(words, enabled):
    if words[:1] in (["setopt"], ["unsetopt"]):
        return words[0] == "setopt" if "pipefail" in words[1:] else enabled
    if words[:1] != ["set"]:
        return enabled
    for index, word in enumerate(words[1:], 1):
        if re.fullmatch(r"[-+][A-Za-z]*o", word) and words[index + 1:index + 2] == ["pipefail"]:
            enabled = word[0] == "-"
    return enabled


def heredocs(command):
    """Locate bodies as data, except shell input or unquoted expansions."""
    lines = command.splitlines(keepends=True)
    regions, offset, index = {}, 0, 0
    while index < len(lines):
        line = lines[index]
        offset += len(line)
        index += 1
        match = HEREDOC.search(line)
        if not match:
            continue
        end = next((j for j in range(index, len(lines)) if lines[j].strip() == match[2]), None)
        if end is None:
            continue
        body = "".join(lines[index:end])
        shell = re.search(r"(?:^|[;|&]\s*)\s*(?:bash|sh|zsh|dash|ksh)\b", line)
        finish = offset + len(body) + len(lines[end])
        regions[offset] = (finish, body, "shell" if shell else "data" if match[1] else "expand")
        offset, index = finish, end + 1
    return regions


def quoted_end(text, start):
    quote, index = text[start], start + 1
    while index < len(text):
        if text[index] == quote:
            return index + 1
        if quote == '"' and text[index] == "\\":
            index += 2
        elif quote == '"' and text.startswith("$(", index):
            index = substitution_end(text, index + 2)
        else:
            index += 1
    raise ValueError("unclosed quote")


def substitution_end(text, index):
    depth = 1
    while index < len(text):
        if text[index] in "'\"":
            index = quoted_end(text, index)
            continue
        if text[index] == "\\":
            index += 2
            continue
        depth += (text[index] == "(") - (text[index] == ")")
        index += 1
        if not depth:
            return index
    raise ValueError("unclosed substitution")


def expansions(text, offset):
    """Expose code substitutions in double quotes and unquoted heredocs."""
    index = 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text.startswith("$(", index):
            start = index + 2
            cursor = substitution_end(text, start)
            yield "subshell", text[start:cursor - 1], offset + start
            index = cursor
        elif text[index] == "`":
            end = text.find("`", index + 1)
            if end < 0:
                raise ValueError("unclosed backtick")
            yield "subshell", text[index + 1:end], offset + index + 1
            index = end + 1
        else:
            index += 1


def tokens(command, offset=0):
    regions = heredocs(command)
    index = 0
    while index < len(command):
        if index in regions:
            end, body, mode = regions[index]
            if mode == "shell":
                yield "shell", body, offset + index
            elif mode == "expand":
                yield from expansions(body, offset + index)
            index = end
            continue
        if command[index] in "'\"":
            end = quoted_end(command, index)
            yield "quote", command[index:end], offset + index
            index = end
            continue
        match = TOKEN.match(command, index)
        if not match:
            raise ValueError("unparsed shell text")
        kind, value = match.lastgroup, match[0]
        if kind not in ("space", "comment"):
            if kind == "backtick":
                end = command.find("`", index + 1)
                if end < 0:
                    raise ValueError("unclosed backtick")
                yield "subshell", command[index + 1:end], offset + index + 1
                index = end + 1
                continue
            yield kind, value, offset + index
        index = match.end()


_pipelines = itertools.count(1)


def status_stages(text):
    """Stages that `${PIPESTATUS[k]}` (bash, counts from 0) or `${pipestatus[k]}` (zsh, counts from 1) reads.
    None means every stage (`[@]`, `[*]`). A variable index reads nothing."""
    out = []
    for name, index in STATUS.findall(text):
        if index in ("@", "*"):
            out.append(None)
        elif index.isdigit():
            out.append(int(index) - (1 if name == "pipestatus" else 0))
    return out


def read_status(pending, text, done):
    """Drop the pending failures whose status `text` reads: only stages of the pipeline that finished last
    (`done`), and only the stage the index names. Printing a stage that isn't the test's leaves it pending."""
    reads = status_stages(text)
    return [p for p in pending if not (p[1] == done and any(r is None or r == p[2] for r in reads))]


def scan(parts, enabled=False, closing=None):
    """Each shell frame owns its options and pending, unchecked pipeline failures."""
    words, start, grouped, has_tests = [], None, False, False
    pending, nested, controls = [], [], []  # pending: (position, pipeline id, stage number) of unchecked test stages
    pipeline, skip = False, False
    cur, stage, done = next(_pipelines), 0, None  # this pipeline, its stage count, the last one that finished
    for kind, value, position in parts:
        if kind == "op" and value in ("(", "{"):
            child_test, failures, child_options = scan(parts, enabled, ")" if value == "(" else "}")
            nested.extend(failures)
            grouped |= child_test
            start = position if start is None else start
            if value == "{":
                enabled = child_options
            continue
        if kind in ("shell", "subshell") or kind == "quote":
            shell_at = next((i for i, word in enumerate(words) if basename(word) in SHELLS), None)
            shell_code = kind == "shell" or (kind == "quote" and shell_at is not None and words and re.fullmatch(r"-[A-Za-z]*c", words[-1]))
            if kind == "quote" and not shell_code:
                if value[0] == '"':
                    for _, body, body_start in expansions(value[1:-1], position + 1):
                        _, failures, _ = scan(tokens(body, body_start), enabled)
                        nested.extend(failures)
                    pending = read_status(pending, value[1:-1], done)
                words.append(value[1:-1])  # the literal word: 'test_math.py' is test_math.py
                continue
            body, body_start = (value[1:-1], position + 1) if kind == "quote" else (value, position)
            initial = False if shell_code else enabled
            if shell_code and shell_at is not None:
                initial = option_state(["set"] + words[shell_at + 1:], False)
            child_test, failures, _ = scan(tokens(body, body_start), initial)
            nested.extend(failures)
            grouped |= child_test
            start = position if start is None else start
            words.append("")
            continue
        if kind == "word" or kind == "op" and value not in ("|", "|&", ";", "\n", "&&", "||", "&", ")", "}"):
            pending = read_status(pending, value, done)
            if start is None:
                start = position
            words.append(value)
            continue
        if words[:1] in (["for"], ["if"], ["while"], ["until"]):
            controls.append(enabled)
            words = []
        elif words[:1] in (["done"], ["fi"]):
            enabled = controls.pop() if controls else enabled
            words = words[1:]
        elif words[:1] in (["do"], ["then"], ["else"]):
            # These words are syntax; any following command starts after them.
            keyword = words.pop(0)
            start += len(keyword) + 1
            if keyword == "else" and controls:
                enabled = controls[-1]
        test = grouped or is_test(words)
        has_tests |= test
        if value in ("|", "|&"):
            if test and not enabled:
                pending.append((start, cur, stage))
            pipeline, stage = True, stage + 1
        else:
            if not pipeline and value != "&" and not skip:
                enabled = option_state(words, enabled)
            skip = value == "&&" and (skip or words[:1] == ["false"])
            pipeline = False
            done, cur, stage = cur, next(_pipelines), 0
        words, start, grouped = [], None, False
        if value == closing:
            return has_tests, nested + [p[0] for p in pending], enabled
    if closing:
        raise ValueError("unclosed shell group")
    return has_tests or grouped or is_test(words), nested + [p[0] for p in pending], enabled


def suggested_fix(command):
    """Only deny when the exact proposed command passes this same analysis."""
    try:
        failures = scan(tokens(command))[1]
        if not failures:
            return None
        fixed = PREFIX + command
        if not scan(tokens(fixed))[1]:
            return fixed
        for position in sorted(set(failures), reverse=True):
            command = command[:position] + PREFIX + command[position:]
        fixed = PREFIX + command
        return None if scan(tokens(fixed))[1] else fixed
    except Exception:
        return None


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name", "Bash") != "Bash":
            return
        command = data["tool_input"]["command"]
        fixed = suggested_fix(command) if isinstance(command, str) else None
        if fixed is not None and data.get("permission_mode") == "bypassPermissions":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "allow",
                "permissionDecisionReason": "pipefail-guard: added 'set -o pipefail; ' so the test's exit code shows",
                "updatedInput": {**data["tool_input"], "command": fixed},
            }}))
        elif fixed is not None:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": REASON + fixed,
            }}))
    except Exception:
        return


if __name__ == "__main__":
    main()
