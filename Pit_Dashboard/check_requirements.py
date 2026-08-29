r"""
check_requirements.py — are the pinned packages already installed?
====================================================================
    python check_requirements.py requirements_pit.txt

Exit 0 and print "already match" when every `name==version` pin in the file
is satisfied by what's actually installed; exit 1 and print what's missing
otherwise. Used by run_pit.bat to skip a `pip install` on every single launch
once the environment already matches.

WHY THIS IS ITS OWN FILE AND NOT AN INLINE `python -c "..."` IN THE BATCH SCRIPT
It used to be exactly that: one ~700-character semicolon-chained one-liner
sitting inside run_pit.bat's double quotes. It happened to work for months and
then started throwing "SyntaxError: unterminated string literal (detected at
line 1)" — not from a real Python syntax error (this file's own logic never
changed), but because CMD.EXE mangled the string BEFORE Python ever saw it:

  * `^` is CMD's own escape character. Even inside double quotes, it consumes
    itself and whatever follows it, so the regex character class `[^\s;#]`
    silently lost its `^` and became `[\s;#]` — an inverted, wrong regex that
    Python would have accepted without complaint, matching nothing like what
    was intended.
  * `!` is delayed-expansion syntax when a script has run `setlocal
    enabledelayedexpansion` (run_pit.bat does, for other reasons). With
    exactly one bare `!` in the line and no second one to close it, CMD
    deleted everything from that `!` up to the next quote boundary — which is
    what actually produced the visible SyntaxError: the deleted span happened
    to eat a closing `'` and a `(`, leaving a genuinely unbalanced string for
    Python to choke on.

Neither of those is a Python bug, and neither is something `^^`-escaping or
toggling delayed expansion around one line fixes for good — the next person
who edits that one-liner and types a `!` or `^` for an unrelated reason
reintroduces the exact same failure, silently, until someone runs it. Real
source code in a real .py file has no such landmines, so the check lives here.
"""

import re
import sys
from importlib.metadata import distributions

PIN_RE = re.compile(r"([A-Za-z0-9_.\-]+)==([^\s;#]+)")


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <requirements-file>")
    req_path = sys.argv[1]

    have = {(d.metadata["Name"] or "").lower().replace("_", "-"): d.version
            for d in distributions()}

    with open(req_path, encoding="utf-8") as fh:
        pins = [m for m in (PIN_RE.match(line.split("#")[0].strip())
                            for line in fh) if m]

    missing = [m.group(1) for m in pins
              if have.get(m.group(1).lower().replace("_", "-")) != m.group(2)]

    if missing:
        print(f"   [i] Need install  : {', '.join(missing)}")
        sys.exit(1)
    print(f"   [OK] Packages     : already match {req_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
