"""
real_deobfuscator.py
---------------------
A REAL deobfuscator: instead of guessing what obfuscated Lua code does by
pattern-matching text (which breaks on anything non-trivial), this module
actually EXECUTES the code inside a restricted, timed-out sandbox (real
Lua 5.4 interpreter) with a debug-hook based variable tracer attached.

Because the code is truly executed, every trick (string.char + arithmetic,
table.concat + reverse, nested load()/loadstring(), XOR loops, whatever)
resolves itself automatically -- there is nothing to "detect", the VM does
the work.

Safety:
  - No io, os.execute, os.remove, require, package, dofile, loadfile-from-
    arbitrary-path exposed to the sandboxed script.
  - Roblox / executor globals (game, workspace, Instance, task, Drawing,
    hookfunction, getgenv, request, writefile, ...) are stubbed as harmless
    no-ops so scripts written for executors don't immediately error out on
    an undefined global -- they just don't actually touch a real game.
  - Hard wall-clock timeout via `timeout(1)` subprocess wrapper, since a
    malicious/obfuscated script could contain an infinite loop.
  - Hard output size cap.

Public API:
    deobfuscate(source: str) -> (result_text: str, stats: dict)

`result_text` is the ORIGINAL source with inline annotations showing the
real, executed value of every local variable assignment the tracer
observed, plus a "Program output" section showing everything the script
actually print()/warn()'d.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

RUNLUA_BIN = os.environ.get("RUNLUA_BIN", "/opt/deobfuscator/runlua")
SANDBOX_RUNNER = os.environ.get(
    "SANDBOX_RUNNER", "/opt/deobfuscator/sandbox_runner.lua"
)

EXEC_TIMEOUT_SECONDS = 5
MAX_TRACE_BYTES = 2_000_000  # cap how much trace output we'll parse


class ExecutionResult:
    __slots__ = ("var_events", "output_events", "fatal", "timed_out")

    def __init__(self):
        self.var_events = []      # list of (line:int, name:str, type:str, value:str)
        self.output_events = []   # list of (stream:str, value:str)
        self.fatal = None         # error message, if the run failed outright
        self.timed_out = False


def _unescape_trace_field(s: str) -> str:
    return s.replace("\\p", "|").replace("\\n", "\n").replace("\\r", "\r").replace("\\\\", "\\")


def run_in_sandbox(source: str) -> ExecutionResult:
    """Actually executes the Lua source in the sandbox and captures the trace."""
    result = ExecutionResult()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".lua", delete=False, encoding="utf-8"
    ) as f:
        f.write(source)
        target_path = f.name

    stderr_chunks = []

    try:
        proc = subprocess.Popen(
            [RUNLUA_BIN, SANDBOX_RUNNER, target_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _, stderr_bytes = proc.communicate(timeout=EXEC_TIMEOUT_SECONDS)
            stderr_chunks.append(stderr_bytes or b"")
        except subprocess.TimeoutExpired:
            # Grab whatever the child already wrote to the pipe BEFORE we
            # kill it, so partial progress (everything traced up to the
            # infinite loop / heavy computation) still makes it into the
            # report instead of being silently discarded.
            proc.kill()
            try:
                _, leftover = proc.communicate(timeout=2)
                stderr_chunks.append(leftover or b"")
            except Exception:
                pass
            result.timed_out = True
            result.fatal = (
                f"Execution exceeded {EXEC_TIMEOUT_SECONDS}s "
                "(infinite loop or extremely heavy computation)"
            )

        stderr = b"".join(stderr_chunks)[:MAX_TRACE_BYTES].decode(
            "utf-8", errors="replace"
        )

        for line in stderr.splitlines():
            if line.startswith("TRACE_VAR|"):
                parts = line.split("|", 4)
                if len(parts) == 5:
                    _, lineno, name, typ, value = parts
                    try:
                        lineno_i = int(lineno)
                    except ValueError:
                        continue
                    result.var_events.append(
                        (lineno_i, name, typ, _unescape_trace_field(value))
                    )
            elif line.startswith("TRACE_OUT|"):
                parts = line.split("|", 2)
                if len(parts) == 3:
                    _, stream, value = parts
                    result.output_events.append(
                        (stream, _unescape_trace_field(value))
                    )
            elif line.startswith("FATAL|"):
                result.fatal = line[len("FATAL|"):]

    except Exception as e:
        result.fatal = f"Sandbox invocation error: {e}"
    finally:
        try:
            os.unlink(target_path)
        except OSError:
            pass

    return result


# ------------------------------------------------------------------
# Map traced values back onto source lines as inline annotations
# ------------------------------------------------------------------

DECL_RE = re.compile(
    r"""^\s*
    (?:local\s+)?
    (?P<names>[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)
    \s*=\s*(?!=)
    """,
    re.X,
)


def _format_value(typ: str, value: str) -> str:
    if typ == "string":
        return '"' + value.replace('"', '\\"').replace("\n", "\\n") + '"'
    return value


def _statement_end_line(lines: list[str], start_idx: int) -> int:
    """
    Given 0-based start_idx pointing at a line that opens a `local X = ...`
    statement, find the 0-based index of the line where that statement's
    brackets/braces/parens balance back out to zero (i.e. where the
    expression actually finishes). Falls back to start_idx if it never
    balances within a reasonable window (plain single-line statement).
    """
    depth = 0
    quote = None
    i = start_idx
    limit = min(len(lines), start_idx + 200)

    while i < limit:
        line = lines[i]
        j = 0
        while j < len(line):
            c = line[j]
            if quote:
                if c == "\\":
                    j += 2
                    continue
                if c == quote:
                    quote = None
                j += 1
                continue
            if c in "\"'":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            j += 1
        if depth <= 0 and i > start_idx:
            return i
        if depth <= 0 and i == start_idx and "=" in line:
            # single-line statement that already balances
            # (only return early if it's not clearly continuing)
            pass
        i += 1

    return i - 1 if i > start_idx else start_idx


def annotate_source(source: str, exec_result: ExecutionResult) -> str:
    """
    Walk the ORIGINAL source. For every line that STARTS a variable
    declaration/assignment, locate the full statement span (it may run
    across many lines -- obfuscators love wrapping decoders in nested
    calls), then attach the real, traced runtime value of that variable
    as a same-line comment on the LAST line of that span. Obfuscated code
    often re-executes the same line number across loop iterations -- we
    keep the final/most-representative value seen for it.
    """
    # Build: (line_number, name) -> last seen (type, value), keeping the
    # LAST occurrence in trace order (later overwrites earlier).
    last_value_at_line: dict[tuple[int, str], tuple[str, str]] = {}
    for lineno, name, typ, value in exec_result.var_events:
        last_value_at_line[(lineno, name)] = (typ, value)

    # Also build a global "last value ever seen for this name, at or before
    # a given line" index, used as a fallback when the exact declaration
    # line never got hit directly (e.g. value only visible one frame up).
    last_value_by_name: dict[str, tuple[int, str, str]] = {}
    for lineno, name, typ, value in exec_result.var_events:
        prev = last_value_by_name.get(name)
        if prev is None or lineno >= prev[0]:
            last_value_by_name[name] = (lineno, typ, value)

    lines = source.splitlines()
    n = len(lines)
    annotations_by_line: dict[int, list[str]] = {}

    idx = 0
    while idx < n:
        line = lines[idx]
        m = DECL_RE.match(line)
        if m:
            names = [x.strip() for x in m.group("names").split(",")]
            end_idx = _statement_end_line(lines, idx)
            found = []
            for name in names:
                value_pair = None
                # Search every line within the statement span, PLUS a small
                # lookahead past it -- Lua 5.4's debug info sometimes marks
                # a local as "visible" starting at the line of the NEXT
                # statement rather than the closing line of its own
                # (multi-line) initializer.
                for probe in range(idx + 1, end_idx + 6):
                    key = (probe, name)
                    if key in last_value_at_line:
                        value_pair = last_value_at_line[key]
                if value_pair and value_pair[0] in ("string", "number", "boolean", "nil"):
                    found.append(f"{name} = {_format_value(*value_pair)}")
            if found:
                annotations_by_line.setdefault(end_idx, []).extend(found)
            idx = end_idx + 1
            continue
        idx += 1

    out_lines = []
    for i, line in enumerate(lines):
        if i in annotations_by_line:
            out_lines.append(
                line.rstrip() + "  --[[ RUNTIME: " + "; ".join(annotations_by_line[i]) + " ]]"
            )
        else:
            out_lines.append(line)

    return "\n".join(out_lines)


def build_output_section(exec_result: ExecutionResult) -> str:
    if not exec_result.output_events:
        return ""
    parts = ["-- ============================================================",
             "-- PROGRAM OUTPUT (actually executed, real values)",
             "-- ============================================================"]
    for stream, value in exec_result.output_events:
        prefix = "[print]" if stream == "print" else "[warn]"
        parts.append(f"-- {prefix} {value}")
    return "\n".join(parts) + "\n\n"


def deobfuscate(source: str):
    """
    Drop-in replacement for the old static-analysis deobfuscate().
    Returns (result_text, stats) same shape the rest of the bot expects.
    """
    stats = {
        "rounds": 1,               # a single real execution replaces N "rounds" of guessing
        "changes": 0,
        "functions": 0,
        "constants": 0,
        "engine": "real-lua-5.4-sandbox",
    }

    exec_result = run_in_sandbox(source)

    stats["constants"] = len({
        (name) for (_, name, typ, _) in exec_result.var_events
        if typ in ("string", "number", "boolean")
    })
    stats["functions"] = len({
        name for (_, name, typ, _) in exec_result.var_events if typ == "function"
    })
    stats["changes"] = len(exec_result.var_events) + len(exec_result.output_events)

    annotated = annotate_source(source, exec_result)
    output_section = build_output_section(exec_result)

    header = ""
    if exec_result.fatal:
        header = (
            "-- ============================================================\n"
            "-- EXECUTION NOTE\n"
            "-- ============================================================\n"
            f"-- {exec_result.fatal}\n"
            "-- Everything traced BEFORE the failure point below is still\n"
            "-- real, executed data -- not a guess.\n\n"
        )

    result = header + output_section + annotated + "\n"
    return result, stats
