#!/usr/bin/env python3
"""One-shot migration of an auto-memory dir into the homes a tree declares.

Each memory file's frontmatter `type:` maps to a rule kind through the config's
`route_kinds`. A file whose body is one paragraph that fits on one rule line is
recorded with `vardemem rule <kind> "<text>"`. Every other file is reported for
the operator to place. The migration deletes nothing and edits no memory file,
and a rerun records nothing new, because `vardemem rule` answers "Already
recorded" for an entry it holds.

Without `--apply` it only prints what it would do.

Usage: migrate.py [--apply] [<memory-dir>]   (default: the scanner's memory dir)
Exit 0 when every file was placed or reported, 2 on a usage or config error.
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent.parent / "hooks" / "enforce-memory-routing.py"
RULE_CHARS = 280
# A list item or a heading is its own fact, so joining the lines would merge facts.
TYPE_LINE = re.compile(r"^\s*type:", re.MULTILINE)
STRUCTURED_LINE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#)", re.MULTILINE)
EXIT_ROUTED = 4
RULE_TIMEOUT_SECONDS = 30


def load_module(name: str, path: Path):
    """The hook and the scanner are scripts, not a package, so they load by path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load {path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body_of(text: str) -> str:
    if not text.startswith("---"):
        return text.strip()
    end = text.find("\n---", 3)
    if end == -1:
        return text.strip()
    _, _, body = text[end + 4:].partition("\n")
    return body.strip()


def frontmatter_of(text: str) -> str:
    end = text.find("\n---", 3) if text.startswith("---") else -1
    return text[3:end] if end != -1 else ""


def one_line(body: str) -> str | None:
    """The body as one rule line, or None when it needs a person to split it."""
    if not body or re.search(r"\n\s*\n", body) or STRUCTURED_LINE.search(body):
        return None
    line = " ".join(body.split())
    return line if len(line) <= RULE_CHARS else None


def plan(hook, cfg: dict, path: Path):
    """(kind, text, None) for a file it can record, else (None, None, why)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, None, f"unreadable: {e}"
    # The hook's matcher takes the first `type:` line, which may sit inside a
    # block scalar; an advisory can live with that, a write to a home cannot.
    if len(TYPE_LINE.findall(frontmatter_of(text))) > 1:
        return None, None, "frontmatter has more than one `type:` line"
    ftype = hook.frontmatter_type(text)
    kind = hook.route_kinds(cfg).get(ftype)
    if not kind:
        return None, None, f"type `{ftype or 'none'}` has no route_kinds entry"
    line = one_line(body_of(text))
    if line is None:
        return None, None, f"body is not one paragraph of at most {RULE_CHARS} characters"
    return kind, line, None


def vardemem_answer(cmd: list, *args: str):
    """(returncode, last line said), or (None, why) when vardemem did not run."""
    try:
        proc = subprocess.run([*cmd, *args], capture_output=True, text=True,
                              timeout=RULE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"vardemem failed: {e}"
    said = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
    return proc.returncode, (said[-1] if said else f"exit {proc.returncode}")


def routable(cmd: list, kind: str):
    """(True, None) when `kind` has one home in this tree, else (False, why)."""
    code, said = vardemem_answer(cmd, "route", kind)
    if code != 0:
        return False, said
    try:
        written_here = json.loads(said).get("written_here")
    except (ValueError, AttributeError):
        return False, f"unreadable route answer: {said}"
    return (True, None) if written_here else (False, f"its home is in a repo: {said}")


def record(cmd: list, kind: str, line: str):
    """(placed, message) from `vardemem rule`."""
    code, said = vardemem_answer(cmd, "rule", kind, line)
    if code == 0:
        return True, said
    if code == EXIT_ROUTED:
        return False, f"its home is in a repo: {said}"
    return False, said


def migrate_file(hook, cfg: dict, cmd: list, path: Path, apply: bool):
    """(placed, report line) for one memory file."""
    kind, line, why = plan(hook, cfg, path)
    if kind and line and not apply:
        ok, why = routable(cmd, kind)
        if ok:
            return True, f"would put {path.name} as {kind}: {line}"
    elif kind and line:
        placed, why = record(cmd, kind, line)
        if placed:
            return True, f"placed    {path.name} as {kind}: {why}"
    return False, f"unplaced  {path.name}: {why}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate an auto-memory dir into declared homes.")
    parser.add_argument("memory_dir", nargs="?")
    parser.add_argument("--apply", action="store_true", help="record; without it, only print the plan")
    args = parser.parse_args()

    hook = load_module("enforce_memory_routing", HOOK_PATH)
    cfg = hook.load_config()
    if not cfg or not hook.route_kinds(cfg):
        print(f"No usable route_kinds in {hook.CONFIG_PATH}.", file=sys.stderr)
        return 2
    cmd = hook.vardemem_cmd(cfg)
    if not cmd:
        print("vardemem not found: set \"vardemem\" in the config or put it on PATH.", file=sys.stderr)
        return 2
    if args.memory_dir:
        mem = Path(args.memory_dir)
    else:
        mem = load_module("scan", HERE / "scan.py").default_memory_dir()
    if not mem.is_dir():
        print(f"{mem} is not a directory.", file=sys.stderr)
        return 2

    unplaced = 0
    for path in sorted(mem.glob("*.md")):
        if path.name in hook.SKIP_NAMES:
            continue
        placed, report = migrate_file(hook, cfg, cmd, path, args.apply)
        unplaced += not placed
        print(report)
    print(f"{unplaced} file(s) left for the operator. Nothing was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
