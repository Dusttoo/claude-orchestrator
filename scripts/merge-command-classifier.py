#!/usr/bin/env python3
"""Conservatively classify hook shell payloads that may execute `gh pr merge`.

This is deliberately a bounded classifier, not a shell interpreter. It proves a
small set of literal forms safe/classifiable and blocks ambiguity, malformed
merge-like executable forms, dynamic command names, and resource exhaustion.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

SCHEMA = "merge-command-classifier/v2"
MAX_INPUT = 65_536
MAX_TOKENS = 4_096
MAX_DEPTH = 8
BOUNDARIES = {
    ";",
    ";;",
    ";&",
    ";;&",
    "\n",
    "&&",
    "||",
    "|",
    "|&",
    "&",
    "(",
    ")",
    "{",
    "}",
}
CONTROL_BOUNDARIES = {"if", "then", "elif", "else", "while", "until", "do"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$"
)
PR_URL = re.compile(
    r"^https://github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99}))"
    r"/pull/([1-9][0-9]*)/?$"
)
MERGE_LIKE = re.compile(r"(?:^|[\s;&|()`{}])gh\s+pr\s+merge(?:[\s;&|()`{}]|$)")


class LimitError(Exception):
    pass


@dataclass
class Token:
    kind: str
    value: str
    literal: bool = True


@dataclass
class Merge:
    pr: str
    repo: str
    strategy: str
    base: str
    head: str


@dataclass
class Result:
    decision: str = "allow"
    reason: str = "proven-data"
    merges: list[Merge] = field(default_factory=list)

    def block(self, reason: str) -> None:
        self.decision = "block"
        self.reason = reason


class Lexer:
    def __init__(self, text: str, depth: int):
        if len(text.encode("utf-8")) > MAX_INPUT:
            raise LimitError("input-limit")
        self.text = text
        self.depth = depth
        self.i = 0
        self.tokens: list[Token] = []
        self.substitutions: list[str] = []
        self.malformed_substitution = False
        self.malformed_quote = False

    def emit(self, token: Token) -> None:
        self.tokens.append(token)
        if len(self.tokens) > MAX_TOKENS:
            raise LimitError("token-limit")

    def balanced_substitution(self, start: int) -> tuple[str, int] | None:
        depth = 1
        quote = ""
        escaped = False
        j = start
        while j < len(self.text):
            ch = self.text[j]
            if escaped:
                escaped = False
            elif ch == "\\" and quote != "'":
                escaped = True
            elif quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif self.text.startswith("$(", j):
                depth += 1
                if depth > MAX_DEPTH:
                    raise LimitError("depth-limit")
                j += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return self.text[start:j], j + 1
            j += 1
        return None

    def backtick(self, start: int) -> tuple[str, int] | None:
        escaped = False
        j = start
        while j < len(self.text):
            ch = self.text[j]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "`":
                return self.text[start:j], j + 1
            j += 1
        return None

    def lex(self) -> tuple[list[Token], list[str], bool, bool]:
        operators = (
            ";;&",
            "&&",
            "||",
            "|&",
            ";;",
            ";&",
            "<<",
            ">>",
            ";",
            "|",
            "&",
            "(",
            ")",
            "{",
            "}",
            "<",
            ">",
        )
        while self.i < len(self.text):
            ch = self.text[self.i]
            if ch in " \t\r":
                self.i += 1
                continue
            if ch == "\n":
                self.emit(Token("op", "\n"))
                self.i += 1
                continue
            if ch == "#":
                while self.i < len(self.text) and self.text[self.i] != "\n":
                    self.i += 1
                continue
            op = next(
                (item for item in operators if self.text.startswith(item, self.i)), None
            )
            if op:
                self.emit(Token("op", op))
                self.i += len(op)
                continue

            value: list[str] = []
            literal = True
            quote = ""
            while self.i < len(self.text):
                ch = self.text[self.i]
                if not quote and (
                    ch in " \t\r\n"
                    or any(self.text.startswith(item, self.i) for item in operators)
                ):
                    break
                if not quote and ch == "#" and not value:
                    break
                if ch == "\\" and quote != "'":
                    self.i += 1
                    if self.i >= len(self.text):
                        self.malformed_quote = True
                        break
                    value.append(self.text[self.i])
                    self.i += 1
                    continue
                if ch in "'\"":
                    if not quote:
                        quote = ch
                        self.i += 1
                        continue
                    if quote == ch:
                        quote = ""
                        self.i += 1
                        continue
                if quote != "'" and self.text.startswith("$(", self.i):
                    found = self.balanced_substitution(self.i + 2)
                    literal = False
                    if found is None:
                        fragment = self.text[self.i + 2 :]
                        self.substitutions.append(fragment)
                        self.malformed_substitution = True
                        self.i = len(self.text)
                        break
                    fragment, end = found
                    self.substitutions.append(fragment)
                    self.i = end
                    continue
                if quote != "'" and ch == "`":
                    found = self.backtick(self.i + 1)
                    literal = False
                    if found is None:
                        self.substitutions.append(self.text[self.i + 1 :])
                        self.malformed_substitution = True
                        self.i = len(self.text)
                        break
                    fragment, end = found
                    self.substitutions.append(fragment)
                    self.i = end
                    continue
                if quote != "'" and ch == "$":
                    literal = False
                value.append(ch)
                self.i += 1
            if quote:
                self.malformed_quote = True
            if value or literal is False:
                self.emit(Token("word", "".join(value), literal))
            elif self.i < len(self.text) and self.text[self.i] == "#":
                continue
            else:
                self.i += 1
        return (
            self.tokens,
            self.substitutions,
            self.malformed_substitution,
            self.malformed_quote,
        )


def basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def merge_like(values: Iterable[Token] | str) -> bool:
    text = (
        values if isinstance(values, str) else " ".join(token.value for token in values)
    )
    return bool(MERGE_LIKE.search(text))


def parse_merge(args: list[Token]) -> tuple[Merge | None, str | None]:
    strategy = "default"
    strategy_seen = False
    base = ""
    base_seen = False
    head = ""
    head_seen = False
    selector = ""
    repo = ""
    repo_seen = False
    data_value = {"--body", "--body-file", "--subject", "--author-email"}
    boolean = {"--admin", "--auto", "--disable-auto", "--delete-branch"}
    i = 0
    while i < len(args):
        token = args[i]
        value = token.value
        if value in BOUNDARIES:
            break
        if not token.literal and value.startswith("-"):
            return None, "dynamic-merge-argument"
        if value in ("--merge", "-m", "--squash", "-s", "--rebase", "-r"):
            normalized = {
                "--merge": "merge",
                "-m": "merge",
                "--squash": "squash",
                "-s": "squash",
                "--rebase": "rebase",
                "-r": "rebase",
            }[value]
            if strategy_seen:
                return None, "conflicting-strategy"
            strategy, strategy_seen = normalized, True
        elif value in ("--base", "-B"):
            if base_seen:
                return None, "duplicate-base"
            i += 1
            if (
                i >= len(args)
                or args[i].kind != "word"
                or not args[i].literal
                or not args[i].value
            ):
                return None, "missing-base"
            base = args[i].value
            base_seen = True
        elif value.startswith("--base="):
            if base_seen:
                return None, "duplicate-base"
            base = value[len("--base=") :]
            if not token.literal or not base:
                return None, "missing-base"
            base_seen = True
        elif value.startswith("-B") and value != "-B":
            if base_seen:
                return None, "duplicate-base"
            base = value[2:]
            if not token.literal or not base:
                return None, "missing-base"
            base_seen = True
        elif value == "--match-head-commit":
            i += 1
            if (
                head_seen
                or i >= len(args)
                or args[i].kind != "word"
                or not args[i].literal
            ):
                return None, "invalid-head-pin"
            head = args[i].value
            head_seen = True
            if not SHA.fullmatch(head):
                return None, "invalid-head-pin"
        elif value.startswith("--match-head-commit="):
            if head_seen or not token.literal:
                return None, "invalid-head-pin"
            head = value.split("=", 1)[1]
            head_seen = True
            if not SHA.fullmatch(head):
                return None, "invalid-head-pin"
        elif value == "--repo":
            if repo_seen:
                return None, "duplicate-repository"
            i += 1
            if (
                i >= len(args)
                or args[i].kind != "word"
                or not args[i].literal
                or not REPOSITORY.fullmatch(args[i].value)
            ):
                return None, "invalid-repository"
            repo = args[i].value
            repo_seen = True
        elif value.startswith("--repo="):
            if repo_seen or not token.literal:
                return (
                    None,
                    "duplicate-repository" if repo_seen else "invalid-repository",
                )
            repo = value.split("=", 1)[1]
            if not REPOSITORY.fullmatch(repo):
                return None, "invalid-repository"
            repo_seen = True
        elif value in data_value:
            i += 1
            if i >= len(args) or args[i].kind != "word":
                return None, "missing-option-value"
        elif any(value.startswith(flag + "=") for flag in data_value):
            if value.endswith("="):
                return None, "missing-option-value"
        elif value in boolean:
            pass
        elif value.startswith("-"):
            return None, "unknown-merge-flag"
        else:
            if selector:
                return None, "multiple-selectors"
            if not token.literal:
                return None, "dynamic-selector"
            match = PR_URL.fullmatch(value)
            if value.isdigit() and value != "0":
                selector = str(int(value))
            elif match:
                if repo_seen:
                    return None, "duplicate-repository"
                repo = match.group(1)
                repo_seen = True
                selector = str(int(match.group(2)))
            else:
                return None, "unsupported-selector"
        i += 1
    if not selector:
        return None, "missing-selector"
    return Merge(selector, repo, strategy, base, head), None


def command_starts(tokens: list[Token]) -> list[int]:
    starts: list[int] = []
    at_start = True
    for i, token in enumerate(tokens):
        if token.kind == "op":
            if token.value in BOUNDARIES:
                at_start = True
            continue
        if at_start:
            starts.append(i)
            at_start = False
        if token.value in CONTROL_BOUNDARIES:
            at_start = True
    return starts


def inspect_simple(tokens: list[Token], start: int, result: Result, depth: int) -> None:
    i = start
    while (
        i < len(tokens)
        and tokens[i].kind == "word"
        and ASSIGNMENT.match(tokens[i].value)
    ):
        i += 1
    if i >= len(tokens) or tokens[i].kind != "word":
        return
    if not tokens[i].literal:
        result.block("dynamic-command")
        return
    if any(char in tokens[i].value for char in "*?["):
        result.block("dynamic-command")
        return

    # Transparent literal wrappers.
    while i < len(tokens) and basename(tokens[i].value) in {"command", "exec", "env"}:
        wrapper = basename(tokens[i].value)
        i += 1
        if wrapper == "command" and i < len(tokens) and tokens[i].value in {"-v", "-V"}:
            return
        while i < len(tokens) and tokens[i].kind == "word":
            value = tokens[i].value
            if value == "--":
                i += 1
                break
            if wrapper == "env" and (
                ASSIGNMENT.match(value)
                or value in {"-i", "--ignore-environment", "--null"}
            ):
                i += 1
                continue
            if wrapper == "env" and value in {"-u", "--unset", "-C", "--chdir"}:
                i += 2
                continue
            if wrapper == "env" and (
                value.startswith("--unset=") or value.startswith("--chdir=")
            ):
                i += 1
                continue
            if wrapper == "command" and value == "-p":
                i += 1
                continue
            if wrapper == "exec" and value in {"-c", "-l"}:
                i += 1
                continue
            if wrapper == "exec" and value == "-a":
                i += 2
                continue
            if value.startswith("-"):
                result.block("unsupported-wrapper-option")
                return
            break
        if i >= len(tokens) or tokens[i].kind != "word" or not tokens[i].literal:
            result.block("unresolved-wrapper")
            return

    if any(char in tokens[i].value for char in "*?["):
        result.block("dynamic-command")
        return

    executable = basename(tokens[i].value)
    segment: list[Token] = []
    j = i
    while j < len(tokens) and not (
        tokens[j].kind == "op" and tokens[j].value in BOUNDARIES
    ):
        segment.append(tokens[j])
        j += 1

    if executable in {"builtin", "eval", "source", ".", "xargs"}:
        result.block("unsupported-dispatch")
        return

    if executable in SHELLS:
        # Interpreter dispatch is intentionally outside the proven-safe grammar.
        # The sanctioned merge wrapper invokes no shell through this hook path.
        result.block("unsupported-shell-dispatch")
        return

    if executable != "gh":
        if executable in CONTROL_BOUNDARIES | {
            "case",
            "for",
            "select",
            "done",
            "fi",
            "esac",
            "in",
        }:
            return
        if executable not in {"echo", "printf", "git"} and merge_like(segment[1:]):
            result.block("unsupported-merge-wrapper")
        return
    if len(segment) < 3 or any(not token.literal for token in segment[:3]):
        if merge_like(segment):
            result.block("malformed-merge-command")
        return
    if segment[1].value != "pr" or segment[2].value != "merge":
        return
    merge, error = parse_merge(segment[3:])
    if error:
        result.block(error)
    elif merge:
        result.merges.append(merge)


def classify(text: str, depth: int = 0) -> Result:
    result = Result()
    if depth > MAX_DEPTH:
        result.block("depth-limit")
        return result
    # Bash removes an unquoted backslash-newline before tokenization. Applying
    # the normalization globally is conservative for single-quoted data and
    # prevents command words from being split around the lexer boundary.
    text = text.replace("\\\n", "")
    try:
        tokens, substitutions, malformed_sub, malformed_quote = Lexer(text, depth).lex()
    except LimitError as exc:
        result.block(str(exc))
        return result

    if substitutions:
        result.block("unsupported-command-substitution")

    # Heredocs/here-strings and process substitution are deliberately excluded
    # from the safe subset. Ordinary literal file redirections remain supported.
    for index, token in enumerate(tokens):
        if token.kind == "op" and token.value == "<<":
            result.block("unsupported-heredoc")
        if (
            token.kind == "op"
            and token.value in {"<", ">"}
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "op"
            and tokens[index + 1].value == "("
        ):
            result.block("unsupported-process-substitution")

    # Brace expansion can construct a command name before execution (for
    # example g{h,x}). Braces are not needed by the supported safe grammar.
    if any(token.kind == "op" and token.value in {"{", "}"} for token in tokens):
        result.block("unsupported-brace-expansion")
    if any(token.kind == "op" and token.value in {"(", ")"} for token in tokens):
        result.block("unsupported-grouping")

    # Redirections after a literal command are supported. A leading redirect
    # (including after assignment prefixes or an explicit file descriptor) is
    # excluded because its operand precedes the executable in the token stream.
    at_start = True
    assignment_prefix = True
    for index, token in enumerate(tokens):
        if token.kind == "op" and token.value in BOUNDARIES:
            at_start = True
            assignment_prefix = True
            continue
        if not at_start:
            continue
        if token.kind == "op" and token.value in {"<", ">", "<<", ">>"}:
            result.block("unsupported-leading-redirection")
            at_start = False
            continue
        if token.kind == "word" and assignment_prefix and ASSIGNMENT.match(token.value):
            continue
        if (
            token.kind == "word"
            and token.value.isdigit()
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "op"
            and tokens[index + 1].value in {"<", ">", "<<", ">>"}
        ):
            result.block("unsupported-leading-redirection")
        assignment_prefix = False
        at_start = False
        if token.kind == "word" and token.value in CONTROL_BOUNDARIES:
            at_start = True
            assignment_prefix = True

    for start in command_starts(tokens):
        inspect_simple(tokens, start, result, depth)

    if malformed_sub:
        result.block("malformed-substitution")
    if malformed_quote and result.merges:
        result.block("malformed-merge-quote")
    if len(result.merges) > 1:
        result.block("multiple-merges")
    if result.decision != "block" and len(result.merges) == 1:
        result.decision = "merge"
        result.reason = "literal-merge"
    return result


def safe_field(value: str) -> str:
    return value if "\n" not in value and "\r" not in value else ""


def emit(result: Result) -> None:
    merge = result.merges[0] if len(result.merges) == 1 else Merge("", "", "", "", "")
    fields = (
        ("schema", SCHEMA),
        ("decision", result.decision),
        ("reason", result.reason),
        ("pr", merge.pr),
        ("repo", merge.repo),
        ("strategy", merge.strategy),
        ("base", merge.base),
        ("head", merge.head),
    )
    sys.stdout.write("".join(f"{key}={safe_field(value)}\n" for key, value in fields))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--command",
        action="store_true",
        help="stdin is a raw command instead of hook JSON",
    )
    args = parser.parse_args()
    raw = sys.stdin.read(MAX_INPUT + 1)
    if len(raw.encode("utf-8")) > MAX_INPUT:
        emit(Result("block", "input-limit"))
        return 0
    if args.command:
        emit(classify(raw))
        return 0

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=unique_object)
        tool = payload["tool_name"]
        command = payload["tool_input"]["command"]
        if not isinstance(tool, str) or not isinstance(command, str):
            raise TypeError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        emit(Result("block", "invalid-hook-json"))
        return 0
    if tool != "Bash":
        emit(Result("allow", "non-bash-tool"))
    else:
        emit(classify(command))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
