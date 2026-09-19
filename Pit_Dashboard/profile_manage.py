"""
profile_manage.py — add, edit and remove speed profiles
========================================================
The file work behind the builder's "Manage profiles" dialog, with NO Streamlit
in it so it can be checked headlessly:

    python Pit_Dashboard/profile_manage.py        # self-check in a temp folder

WHAT A PROFILE IS, ON DISK
    profiles/<key>.csv       the curve. The car loads every CSV in the folder at
                             startup, and <key> is the string the pit sends it.
    constants.py             PROFILE_MATRIX: label, Wh/lap estimate and target
                             time per key. What the pit dashboard reads. The
                             builder's Save button rewrites it.
    profiles/profiles.json   the builder's working copy: unsaved changes to the
                             matrix, plus which lap each built curve came from.

NEW CURVES ARE SCALED, NOT INVENTED
A new profile, or a changed target time, is generated from the base profile
(profiles/<DEFAULT_STRATEGY_KEY>.csv — see BASELINE_PATH) by
tools/generate_profiles.py's own solver: corners stay at or under the baseline
apex, straights are scaled, and braking and acceleration never exceed the
baseline's. Since that base is itself built from a lap the car drove, an added
profile is that lap re-paced. Building one from a real lap in the builder is
still better, and replaces it the same way.

KEYS ARE NEVER RENAMED
The key is the filename, the string the car acknowledges, and what every
recorded lap's `active_strategy` points at. Renaming one would orphan its
measured energy and leave the car holding a name the pit no longer sends. So
editing changes the label and the curve, never the key.

NOTHING IS DELETED OUTRIGHT
Every replaced or removed CSV is copied to profiles/_backup/ first (*.bak, which
git ignores), so a slip in the dialog is one file copy away from undone.
"""

import ast
import datetime
import importlib.util
import json
import os
import re
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import profile_build as pb                                  # noqa: E402
import speed_profile                                        # noqa: E402
from constants import DEFAULT_STRATEGY_KEY                  # noqa: E402

PROFILE_DIR = os.path.join(_REPO_ROOT, "profiles")
BACKUP_DIR = os.path.join(PROFILE_DIR, "_backup")
# What a new or retargeted profile is scaled FROM. It used to be 210s.xlsx, the
# desk model, which meant every curve the builder produced inherited a 92 km/h
# main straight the car has never reached. It is now the committed base profile
# itself — which is built from a lap the car drove (tools/build_dor_profiles.py)
# — so an added strategy is a real lap re-paced, not a model re-scaled.
#
# It follows DEFAULT_STRATEGY_KEY on purpose: rebuild the base from a newer lap
# and everything scaled afterwards comes off that newer lap too.
BASELINE_PATH = os.path.join(PROFILE_DIR, f"{DEFAULT_STRATEGY_KEY}.csv")
CONSTANTS_PATH = os.path.join(_HERE, "constants.py")
MATRIX_BEGIN = "# >>> PROFILE MATRIX >>>"
MATRIX_END = "# <<< PROFILE MATRIX <<<"

# The base profile cannot be removed. It is the car's startup default, the
# corner-cap reference for every lap built in the builder, and the baseline
# validate_profile() compares against — removing it breaks all three.
PROTECTED_KEYS = {DEFAULT_STRATEGY_KEY}

# Lowercase so a key means the same file on the pit laptop (Windows, case-
# insensitive) and on the Pi (Linux, case-sensitive).
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Outside this the solver either cannot reach the target or produces a lap
# nobody would drive. The base is 280 s, and the fixed corners mean the solver
# cannot get under about 208 s however hard it scales the straights.
MIN_TARGET_S = 150.0
MAX_TARGET_S = 330.0
# Two targets closer than this put two columns on top of each other in the
# builder, and nearest_key() would file laps between them by a coin toss.
MIN_TARGET_SPACING_S = 1.0
# How close the solved lap must land to what was asked for.
TARGET_TOLERANCE_S = 0.5


def _generator():
    """tools/generate_profiles.py, loaded by path.

    By path because tools/ is not a package and this runs from Pit_Dashboard/.
    Imported rather than copied: one solver means an added profile and the
    committed ones can never be scaled by two slightly different rules. Only
    that script's main() is retired — see its docstring.
    """
    path = os.path.join(_REPO_ROOT, "tools", "generate_profiles.py")
    spec = importlib.util.spec_from_file_location("generate_profiles", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# The saved matrix — PROFILE_MATRIX in constants.py
# --------------------------------------------------------------------------- #
# Read from the FILE, never from the imported constants module: Streamlit does
# not re-import, so the module would still hold whatever was saved before the
# app started, and "unsaved changes" would be judged against a stale copy.
def normalise_entry(meta):
    """One matrix row in canonical form, so equal rows compare equal."""
    energy = meta.get("energy_wh")
    target = meta.get("target_s")
    return {"label": str(meta.get("label") or "").strip(),
            "energy_wh": None if energy is None else round(float(energy), 1),
            "target_s": None if target is None else round(float(target), 1)}


def _split_constants(text):
    """(before, block, after) around the markers. Raises if they are not there."""
    begin, end = text.find(MATRIX_BEGIN), text.find(MATRIX_END)
    if begin < 0 or end < 0 or end < begin or text.count(MATRIX_BEGIN) != 1 \
            or text.count(MATRIX_END) != 1:
        raise ValueError(f"constants.py must contain exactly one '{MATRIX_BEGIN}' "
                         f"line followed by one '{MATRIX_END}' line")
    block_start = text.index("\n", begin) + 1
    return text[:block_start], text[block_start:end], text[end:]


def _parse_block(block):
    tree = ast.parse(block)
    if (len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign)
            or [getattr(t, "id", None) for t in tree.body[0].targets] != ["PROFILE_MATRIX"]):
        raise ValueError("the PROFILE MATRIX block must hold exactly one "
                         "`PROFILE_MATRIX = {...}` assignment")
    value = ast.literal_eval(tree.body[0].value)
    if not isinstance(value, dict):
        raise ValueError("PROFILE_MATRIX must be a dict")
    return {str(k): normalise_entry(v) for k, v in value.items()}


def read_saved_matrix(path=CONSTANTS_PATH):
    """{key: {label, energy_wh, target_s}} as currently written in constants.py."""
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    return _parse_block(_split_constants(text)[1])


def render_matrix(matrix, newline="\n"):
    """The block's Python source: one line per profile, fastest first."""
    def num(x):
        return "None" if x is None else repr(float(x))

    rows = sorted(matrix.items(),
                  key=lambda kv: (kv[1].get("target_s") is None,
                                  kv[1].get("target_s") or 0.0, kv[0]))
    # json.dumps gives a double-quoted string that is also a valid Python
    # literal, whatever quotes or accents the label contains.
    lines = ["PROFILE_MATRIX = {"]
    for key, meta in rows:
        m = normalise_entry(meta)
        lines.append(f'    {json.dumps(key)}: {{"label": '
                     f'{json.dumps(m["label"], ensure_ascii=False)}, '
                     f'"energy_wh": {num(m["energy_wh"])}, '
                     f'"target_s": {num(m["target_s"])}}},')
    lines.append("}")
    return newline.join(lines) + newline


def matrix_diff(saved, draft):
    """[(kind, key, detail)] — what Save would change. Empty when in sync."""
    out = []
    for key in sorted(set(saved) | set(draft)):
        a, b = saved.get(key), draft.get(key)
        if a is None:
            out.append(("added", key, b))
        elif b is None:
            out.append(("removed", key, a))
        else:
            a, b = normalise_entry(a), normalise_entry(b)
            changed = {f: (a[f], b[f]) for f in a if a[f] != b[f]}
            if changed:
                out.append(("changed", key, changed))
    return out


def write_saved_matrix(matrix, path=CONSTANTS_PATH, backup_dir=BACKUP_DIR):
    """Rewrite the PROFILE MATRIX block in constants.py, and nothing else.

    Checked before it lands: the new file must parse as Python and the block
    must read back as exactly `matrix`. Keeps the file's own line endings, and
    a copy of the old file goes to profiles/_backup/.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    newline = "\r\n" if "\r\n" in text else "\n"
    before, _old, after = _split_constants(text)
    block = render_matrix(matrix, newline)
    new_text = before + block + after

    ast.parse(new_text)                                   # whole file still Python
    expected = {k: normalise_entry(v) for k, v in matrix.items()}
    if _parse_block(block) != expected:
        raise ValueError("the rendered matrix does not read back as what was saved")

    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, os.path.join(backup_dir, f"constants.{stamp}.py.bak"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def suggest_key(label, target_s):
    """'Eco Push', 205 -> 'eco_push_205s'. Same shape as the built-in keys."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_") or "profile"
    if not slug[0].isalpha():
        slug = "p_" + slug
    return f"{slug[:32]}_{int(round(float(target_s)))}s"


def key_problem(key, existing_keys):
    """Why `key` cannot be a new profile, or None."""
    if not KEY_RE.match(key or ""):
        return ("use 2–40 lowercase letters, digits and underscores, "
                "starting with a letter")
    if key in {k.lower() for k in existing_keys}:
        return f"`{key}` already exists"
    return None


def target_problem(target_s, other_targets):
    """Why `target_s` cannot be used, or None. `other_targets`: {key: seconds}."""
    t = float(target_s)
    if not MIN_TARGET_S <= t <= MAX_TARGET_S:
        return f"target must be between {MIN_TARGET_S:.0f} and {MAX_TARGET_S:.0f} s"
    for key, other in other_targets.items():
        if other is not None and abs(float(other) - t) < MIN_TARGET_SPACING_S:
            return (f"`{key}` already targets {float(other):.1f} s — keep targets "
                    f"at least {MIN_TARGET_SPACING_S:.0f} s apart")
    return None


# --------------------------------------------------------------------------- #
# Curves
# --------------------------------------------------------------------------- #
def scaled_curve(target_s, baseline_path=BASELINE_PATH):
    """The baseline lap scaled to `target_s`. Returns (speeds_ms, sections, info).

    Raises ValueError when the solver cannot land within TARGET_TOLERANCE_S or
    the baseline is not on the standard 10 m grid.

    Takes a profile CSV or, still, an .xlsx — the loader is chosen by extension
    so a baseline spreadsheet keeps working if anyone points this back at one.
    """
    gen = _generator()
    if baseline_path.lower().endswith((".xlsx", ".xls")):
        df = gen.load_baseline(baseline_path)
        dist = [float(x) for x in df[speed_profile.COL_DIST]]
        speed = [float(x) for x in df[speed_profile.COL_SPEED_MS]]
        section = [str(x) for x in df[speed_profile.COL_SECTION]]
    else:
        base = speed_profile.load_csv(baseline_path, lap_length_m=pb.LAP_M)
        dist, speed, section = (list(base.distances_m), list(base.speeds_ms),
                                list(base.sections))
    if dist != pb.GRID_M.tolist():
        raise ValueError(f"{os.path.basename(baseline_path)} is not on the "
                         f"standard 10 m grid ({len(dist)} points)")

    corners = gen.find_corners(dist, speed, section)
    accel, brake = gen.peak_accel_decel(dist, speed)
    k, out, achieved = gen.solve_for_target(dist, speed, corners, float(target_s),
                                            accel, brake)
    if abs(achieved - float(target_s)) > TARGET_TOLERANCE_S:
        raise ValueError(f"cannot reach {float(target_s):.1f} s from the baseline "
                         f"(closest was {achieved:.1f} s)")
    return out, section, {"k": k, "lap_s": achieved,
                          "max_kmh": max(out) * 3.6,
                          "avg_kmh": pb.LAP_M / achieved * 3.6}


def _backup(key, profile_dir, backup_dir, tag=""):
    """Copy profiles/<key>.csv aside. Returns the backup path, or None."""
    src = os.path.join(profile_dir, f"{key}.csv")
    if not os.path.exists(src):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(backup_dir, f"{key}.{stamp}{tag}.csv.bak")
    shutil.copy2(src, dst)
    return dst


def write_scaled(key, target_s, profile_dir=PROFILE_DIR, backup_dir=BACKUP_DIR,
                 baseline_path=BASELINE_PATH):
    """Generate and install profiles/<key>.csv at `target_s`.

    Staged and read back through the car's own loader before it replaces
    anything, like the builder's write. Returns info incl. `backup` (the old
    file's copy, or None for a new key).
    """
    speeds, sections, info = scaled_curve(target_s, baseline_path)
    os.makedirs(profile_dir, exist_ok=True)
    final = os.path.join(profile_dir, f"{key}.csv")
    staged = final + ".staged"
    pb.write_rows(staged, pb.GRID_M.tolist(), speeds, sections)
    try:
        p = speed_profile.load_csv(staged, name=key, lap_length_m=pb.LAP_M)
        if len(p) != len(pb.GRID_M):
            raise ValueError(f"wrote {len(p)} points, expected {len(pb.GRID_M)}")
        if abs(p.lap_time_s() - info["lap_s"]) > 0.05:
            raise ValueError("the written file reads back at a different lap time")
    except Exception:
        os.remove(staged)
        raise
    info["backup"] = _backup(key, profile_dir, backup_dir)
    os.replace(staged, final)
    return info


def remove_profile(key, profile_dir=PROFILE_DIR, backup_dir=BACKUP_DIR):
    """Back up and delete profiles/<key>.csv. Returns the backup path or None."""
    if key in PROTECTED_KEYS:
        raise ValueError(f"`{key}` is the base profile and cannot be removed")
    backup = _backup(key, profile_dir, backup_dir, tag=".removed")
    path = os.path.join(profile_dir, f"{key}.csv")
    if os.path.exists(path):
        os.remove(path)
    return backup


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #
def _self_check():
    import tempfile

    ok = True

    def want(cond, msg):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        ok &= bool(cond)

    print("keys")
    want(suggest_key("Eco Push", 205.4) == "eco_push_205s", "suggest_key slug")
    want(suggest_key("2 fast", 190) == "p_2_fast_190s", "suggest_key leading digit")
    want(key_problem("eco_290s", ["dor_280s"]) is None, "valid key accepted")
    want(key_problem("Dor_280s", ["dor_280s"]) is not None, "uppercase refused")
    want(key_problem("dor_280s", ["dor_280s"]) is not None, "duplicate refused")
    want(key_problem("a/b", []) is not None, "path characters refused")

    print("targets")
    want(target_problem(290.0, {"dor_280s": 280.0}) is None, "290 s accepted")
    want(target_problem(279.5, {"dor_280s": 280.0}) is not None, "too close refused")
    want(target_problem(100.0, {}) is not None, "out of range refused")

    print("constants.py matrix")
    with open(CONSTANTS_PATH, encoding="utf-8", newline="") as fh:
        original = fh.read()
    saved = read_saved_matrix()
    want(len(saved) >= 1, f"reads {len(saved)} profile(s) from constants.py")
    nl = "\r\n" if "\r\n" in original else "\n"
    want(render_matrix(saved, nl) == _split_constants(original)[1],
         "saving an unchanged matrix rewrites the block byte-for-byte")
    want(matrix_diff(saved, saved) == [], "no diff against itself")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "constants.py")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(original)
        draft = {k: dict(v) for k, v in saved.items()}
        draft["eco_205s"] = {"label": 'Driver\'s "eco"', "energy_wh": 77.25,
                             "target_s": 205.0}
        first = next(iter(draft))
        draft[first]["energy_wh"] = 91.0
        diff = matrix_diff(saved, draft)
        want([d[0] for d in diff].count("added") == 1
             and [d[0] for d in diff].count("changed") == 1, f"diff: {diff}")
        write_saved_matrix(draft, path, os.path.join(tmp, "backup"))
        want(read_saved_matrix(path) == {k: normalise_entry(v) for k, v in draft.items()},
             "written matrix reads back, quotes in the label included")
        with open(path, encoding="utf-8", newline="") as fh:
            written = fh.read()
        b0, _, a0 = _split_constants(original)
        b1, _, a1 = _split_constants(written)
        want(b0 == b1 and a0 == a1, "nothing outside the markers changed")
        want(("\r\n" in original) == ("\r\n" in written), "line endings kept")
        spec = importlib.util.spec_from_file_location("constants_written", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        want(mod.PROFILE_MATRIX["eco_205s"]["energy_wh"] == 77.2
             or mod.PROFILE_MATRIX["eco_205s"]["energy_wh"] == 77.3,
             "the rewritten constants.py imports and holds the new row")

    print("files")
    with tempfile.TemporaryDirectory() as tmp:
        prof, bak = os.path.join(tmp, "profiles"), os.path.join(tmp, "backup")
        info = write_scaled("eco_290s", 290.0, prof, bak)
        p = speed_profile.load_csv(os.path.join(prof, "eco_290s.csv"),
                                   lap_length_m=pb.LAP_M)
        want(len(p) == len(pb.GRID_M), f"new profile has {len(p)} points")
        want(abs(p.lap_time_s() - 290.0) <= TARGET_TOLERANCE_S,
             f"new profile laps in {p.lap_time_s():.2f} s")
        want(info["backup"] is None, "a new key makes no backup")

        # Scaling the base to its OWN lap time must give the base back: the
        # solver's k lands on 1.0 and nothing moves. That is the round trip
        # which proves BASELINE_PATH and the grid still agree — and it is worth
        # more than the old version of this check, which regenerated base_210s
        # from 210s.xlsx and could only ever confirm the model matched itself.
        committed = speed_profile.load_csv(BASELINE_PATH, lap_length_m=pb.LAP_M)
        base_s = committed.lap_time_s()
        write_scaled(DEFAULT_STRATEGY_KEY, base_s, prof, bak)
        regen = speed_profile.load_csv(
            os.path.join(prof, f"{DEFAULT_STRATEGY_KEY}.csv"),
            lap_length_m=pb.LAP_M)
        diff = max(abs(a - b) for a, b in zip(committed.speeds_ms, regen.speeds_ms))
        want(diff < 0.05, f"rescaling {DEFAULT_STRATEGY_KEY} to its own "
                          f"{base_s:.1f} s returns it unchanged "
                          f"(max {diff:.2e} m/s)")

        info = write_scaled("eco_290s", 275.0, prof, bak)
        want(info["backup"] is not None and os.path.exists(info["backup"]),
             "changing the target backs up the old curve")
        p = speed_profile.load_csv(os.path.join(prof, "eco_290s.csv"),
                                   lap_length_m=pb.LAP_M)
        want(abs(p.lap_time_s() - 275.0) <= TARGET_TOLERANCE_S,
             f"retargeted profile laps in {p.lap_time_s():.2f} s")

        backup = remove_profile("eco_290s", prof, bak)
        want(not os.path.exists(os.path.join(prof, "eco_290s.csv")), "removed")
        want(backup is not None and os.path.exists(backup), "removal backed up")
        try:
            remove_profile(DEFAULT_STRATEGY_KEY, prof, bak)
            want(False, "base profile removal refused")
        except ValueError:
            want(True, "base profile removal refused")
        want(not any(f.endswith(".staged") for f in os.listdir(prof)),
             "no staged files left behind")

    print("\nall checks passed" if ok else "\n*** CHECKS FAILED ***")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_self_check())
