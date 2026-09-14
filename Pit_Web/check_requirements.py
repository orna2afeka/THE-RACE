r"""
check_requirements.py — are the required packages already installed?
=====================================================================
    python check_requirements.py requirements_web.txt

Exit 0 and say so when every requirement in the file is satisfied by what is
actually installed; exit 1 and name what is missing or wrong otherwise. Used by
run_web.bat to skip a `pip install` on every launch once the environment
already matches.

Reads installed package METADATA only — no network, so it is fast and works on
an offline pit LAN. And it reads the versions FROM the requirements file, so
unlike a hardcoded module list it cannot drift out of step with it.

Adapted from Pit_Dashboard/check_requirements.py in the race-day repo, with one
addition: that version understood `name==version` pins only, and this folder's
requirements file uses `>=` for the web packages (fastapi, uvicorn, pydantic,
websockets). A checker that silently ignored those would report "already match"
on an environment with no fastapi at all.

WHY THIS IS ITS OWN FILE AND NOT AN INLINE `python -c "..."` IN THE BATCH SCRIPT
Taken verbatim from the original's hard-won lesson. It used to be a ~700
character semicolon-chained one-liner inside the batch file's double quotes. It
worked for months, then began throwing "SyntaxError: unterminated string
literal" — not from any Python change, but because CMD.EXE mangled the string
before Python ever saw it:

  * `^` is CMD's own escape character. Even inside double quotes it consumes
    itself and the next character, so a regex class like `[^\s;#]` silently
    lost its `^` and became an inverted, wrong pattern that Python accepted
    without complaint.
  * `!` is delayed-expansion syntax once a script has run `setlocal
    enabledelayedexpansion` (run_web.bat does). A single bare `!` made CMD
    delete everything up to the next quote, which is what produced the visible
    SyntaxError.

Neither is fixed for good by `^^`-escaping: the next person to type a `!` or
`^` in that line reintroduces it, silently, until someone runs it. Real source
in a real .py file has no such landmines.
"""

import re
import sys
from importlib.metadata import distributions

# name, operator, version — e.g. "pandas==2.2.1" or "fastapi>=0.115".
REQ_RE = re.compile(r"([A-Za-z0-9_.\-]+)\s*(==|>=)\s*([^\s;#]+)")


def _norm(name):
    """PyPI treats "-" and "_" as the same character, and is case-insensitive."""
    return name.lower().replace("_", "-")


def _parts(version):
    """A version as a tuple of ints for comparison, ignoring any suffix.

    Good enough for the pins here (plain X.Y.Z). A version this cannot parse
    compares as (0,), which fails the >= test and triggers a reinstall — the
    safe direction: a needless install beats a missing package at the track.
    """
    out = []
    for chunk in str(version).split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: %s <requirements-file>" % sys.argv[0])
    req_path = sys.argv[1]

    # .get("Name"), NOT ["Name"]. Subscripting the metadata raises
    # "DeprecationWarning: Implicit None on return values is deprecated and
    # will raise KeyErrors" on Python 3.12 — noise in the launcher output now,
    # and a hard failure on the Python after next. A package with no Name in
    # its metadata is skipped rather than crashing the check.
    have = {}
    for d in distributions():
        name = d.metadata.get("Name")
        if name:
            have[_norm(name)] = d.version

    try:
        with open(req_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print("   [X] cannot read %s: %s" % (req_path, exc))
        sys.exit(1)

    reqs = [m for m in (REQ_RE.match(line.split("#")[0].strip())
                        for line in lines) if m]
    if not reqs:
        print("   [X] no requirements parsed from %s" % req_path)
        sys.exit(1)

    problems = []
    for m in reqs:
        name, op, want = m.group(1), m.group(2), m.group(3)
        installed = have.get(_norm(name))
        if installed is None:
            problems.append("%s (missing)" % name)
        elif op == "==" and installed != want:
            problems.append("%s (have %s, need %s)" % (name, installed, want))
        elif op == ">=" and _parts(installed) < _parts(want):
            problems.append("%s (have %s, need >=%s)" % (name, installed, want))

    if problems:
        print("   [i] Need install  : %s" % ", ".join(problems))
        sys.exit(1)
    print("   [OK] Packages     : all %d requirements satisfied" % len(reqs))
    sys.exit(0)


if __name__ == "__main__":
    main()
