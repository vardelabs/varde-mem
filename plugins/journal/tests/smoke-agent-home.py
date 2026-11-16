#!/usr/bin/env python3
"""Smoke test: agent_home resolves no home from an empty or relative passwd
home field, so no caller builds a path under the cwd."""
import importlib.util
import os
import sys
import types
from pathlib import Path

AGENT_HOME = Path(__file__).resolve().parent.parent / "hooks" / "agent_home.py"
OVERRIDE_ENV = "JOURNAL_SMOKE_AGENT_HOME_OVERRIDE"

failures = []


def load_with_passwd_home(pw_dir):
    spec = importlib.util.spec_from_file_location("agent_home_under_test", AGENT_HOME)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.pwd = types.SimpleNamespace(getpwuid=lambda _uid: types.SimpleNamespace(pw_dir=pw_dir))
    return mod


def expect(label, got, want):
    if got == want:
        print(f"  PASS {label}")
    else:
        failures.append(label)
        print(f"  ** FAIL {label}: got {got!r}, want {want!r}")


def main():
    if os.name == "nt":
        print("SKIP: the passwd lookup is POSIX only")
        return 0
    os.environ.pop(OVERRIDE_ENV, None)
    for pw_dir in ("", "relative/home"):
        mod = load_with_passwd_home(pw_dir)
        expect(f"passwd home {pw_dir!r}: trusted_home is None", mod.trusted_home(), None)
        mod = load_with_passwd_home(pw_dir)
        expect(f"passwd home {pw_dir!r}: env_override_or_home is None",
               mod.env_override_or_home(OVERRIDE_ENV, ".config", "x.json"), None)
    mod = load_with_passwd_home("/home/agent")
    expect("absolute passwd home is returned", mod.trusted_home(), "/home/agent")
    mod = load_with_passwd_home("/home/agent")
    expect("absolute passwd home gives the home-relative path",
           mod.env_override_or_home(OVERRIDE_ENV, ".config", "x.json"),
           "/home/agent/.config/x.json")
    if failures:
        print(f"FAILED ({len(failures)})")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
