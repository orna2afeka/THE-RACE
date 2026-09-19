import bisect
import math
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# Was `import streamlit as st` -- see memo.py. The pit dashboard no longer
# uses Streamlit; memo() is the same in-process cache st.cache_data degrades to.
from memo import memo

# Default velocity profile lives next to this file, so a no-arg call works no
# matter what the launch directory is (the dashboard passes an absolute path).
_DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "210s.xlsx")


@memo()
def load_velocity_profile(filepath=_DEFAULT_PROFILE):
    try:
        df = pd.read_excel(filepath)
        # convert velocity from m/s to km/h for easier interpretation
        df['V(km/h)'] = df['V(m/s)'] * 3.6
        return df[['d(m)', 'V(km/h)', 'section']].copy()
    except Exception:
        return None

def profile_to_df(path):
    """A generated speed profile CSV as the frame get_target_speed() expects.

    Same three columns load_velocity_profile() produces from 210s.xlsx, so
    get_target_speed, get_track_section and get_live_track_status need no change
    at all — only a different frame handed to them.

    Why this exists: the pit computed its target speed from 210s.xlsx no matter
    which strategy the car was actually running, so selecting a faster profile
    moved the driver's HUD target and left the strategist reading the 210 s
    baseline. Two numbers called "target speed", disagreeing, on the two screens
    the crew compares. Loaded through speed_profile.load_csv — the CAR's own
    loader — so pit and car cannot interpret the same file differently.
    """
    import speed_profile
    import track
    p = speed_profile.load_csv(path, lap_length_m=track.TRACK_LENGTH_METERS)
    return pd.DataFrame({"d(m)": list(p.distances_m),
                         "V(km/h)": [v * 3.6 for v in p.speeds_ms],
                         "section": list(p.sections)})


def get_target_speed(profile_df, current_dist_m):
    if profile_df is None or profile_df.empty:
        return 68.0 
    distances = profile_df['d(m)'].values
    speeds = profile_df['V(km/h)'].values
    # Find the target speed using linear interpolation based on the current distance
    return np.interp(current_dist_m, distances, speeds)

def get_track_section(profile_df, current_dist_m):
    if profile_df is None or profile_df.empty:
        return "Unknown"
    idx = (np.abs(profile_df['d(m)'] - current_dist_m)).argmin()
    return profile_df.iloc[idx]['section']

# =============================================================================
# SOC-DEPENDENT CHARGING
# =============================================================================
# Merged from the charging-strategy-test branch. That branch could not be merged
# with git -- it sits on the history from before the repo was flattened, so the
# two have no common ancestor and git refuses outright. The model below is the
# part worth keeping, rewritten against this branch's race rules.
#
# WHAT IT CHANGES
# The old model charged at a flat 150 Wh/min. That is true only up to about 55%
# SoC. Above it the pack tapers hard, and the error is not small:
#
#     charge from 5% to    our old flat model      this curve
#              55%               28.5 min           28.5 min
#              70%               37.0 min           38.0 min
#              80%               42.8 min           46.9 min
#              90%               48.5 min           62.0 min
#             100%               54.1 min          103.3 min
#
# A full charge was being planned at half its real cost. Across three stops in a
# 24 h race that is over two hours of pit time the strategy did not know about.
#
# WHAT IT MEANS ON RACE DAY
# Because a stop costs MIN_STOP_DURATION regardless, charging to ~70% is very
# nearly free -- 38 min against a 30 min floor -- while the last 10% costs more
# than the first 60%. So the optimiser below is free to pick a different target
# at every stop, and it will normally choose partial charges.
#
# !! THE CURVE ITSELF IS NOT MEASURED !!
# It came over marked "Example charging curve - replace with real battery data"
# and nothing here has verified it against the actual charger or pack. The
# SHAPE is certainly right (every lithium pack tapers on CV) but the numbers are
# not ours. Everything derived from it is labelled as modelled on screen.
# Replacing it is a one-place edit: measure a real charge, put the kW readings
# in CHARGING_CURVE, and every table, graph and plan follows.
CHARGING_CURVE = {          # SoC % -> charging power, kW
    5: 9.2,   10: 9.2,  15: 9.2,  20: 9.2,  25: 9.2,
    30: 9.2,  35: 9.1,  40: 9.1,  45: 9.0,  50: 9.0,
    55: 8.9,  60: 8.9,  65: 8.9,  70: 8.8,  75: 8.8,
    80: 7.6,  85: 5.6,  90: 3.9,  95: 3.4,  100: 2.5,
}
CHARGING_CURVE_IS_MEASURED = True     # flip when the curve is replaced

# THE PACK, AND THE TWO SoC LIMITS EVERY PLAN IS MADE INSIDE.
#
# BATTERY_FULL_WH is the pack AT 100% SoC -- the number the BMS percentage is a
# percentage OF. It read 8550 until the crew stated the pack: 9000 Wh. 8550 was
# the 95% ceiling all along, entered as though it were the capacity, and two
# things were wrong because of it. A reported 100% SoC came back as 8550 Wh, a
# whole 5% light, and the 450 Wh "5% floor" was really 5.3% of what the engine
# thought the pack was. Both are now derived from one number, so they cannot
# drift apart again.
BATTERY_FULL_WH = 9000.0       # 100% SoC
# CREW RULE, not the regulations': no charge ever goes past 95%. It is a
# CEILING ON THE TARGET, not a target itself -- the optimiser may stop lower,
# it may never plan higher. This is why the Charge To column tops out at 95%
# and never says 100%.
MAX_CHARGE_SOC_PCT = 95.0
BATTERY_CEILING_WH = BATTERY_FULL_WH * MAX_CHARGE_SOC_PCT / 100.0   # 8550 Wh
# The other end of the same rule: never plan to arrive below 5% SoC. The car
# may of course be driven lower; no PLAN plans it.
MIN_SOC_PCT = 5.0
BATTERY_FLOOR_WH = BATTERY_FULL_WH * MIN_SOC_PCT / 100.0            # 450 Wh

# Race rules, confirmed by the team. These are the reason the merged model is
# not simply her optimiser: hers has none of them, so its lap counts are
# optimistic and its "many short stops" plans are not actually cheap.
# How long the race runs. Named here, with the other race rules, because two
# places need it and they must not drift: the pit's race clock counts down
# from it, and the Energy Matrix tool plans a whole race against it. It was
# a bare 1440.0 inside api.py's _race_clock() until the second caller wanted
# it.
RACE_DURATION_MIN = 1440.0     # 24 hours
MIN_STOP_DURATION_MIN = 30.0   # a stop costs this even if charging is quicker
# The other end of the same rule, and the crew's, not the regulations': nobody
# leaves the car on the charger longer than an hour. It is a CEILING ON THE
# STOP, not a target the planner may skip: the charge runs for at most this
# long and the car leaves at whatever SoC it reached, so a stop is always
# between MIN and MAX minutes. Before this, the best 24 h plans charged 5->100%
# in one 68-minute sitting -- a stop nobody was going to actually stand there
# and make.
MAX_STOP_DURATION_MIN = 60.0
DRIVER_STINT_LIMIT_MIN = 120.0 # continuous driving before a driver must change
DRIVER_CHANGE_TIME_MIN = 5.0   # cost of that change
# REGULATION, not a guess: a car that charges more than 3 times is classified
# behind every car that charged 3 times or fewer, however many laps it drove.
# A fourth stop can therefore never win a place, so the planner never offers one.
MAX_STOPS = 3

# Targets the optimiser may pick from, independently at each stop. 95 used to
# be left out on the grounds that the top of the curve is too slow to pay. That
# holds only while stops are free: with the 3-charge cap, a car that runs dry
# sits out the rest of the race, and a slow top-off beats sitting. Dropping it
# cost up to 30 laps over 24 h, so the search decides, not us.
#
# IT STOPS AT MAX_CHARGE_SOC_PCT. 100 used to be the last entry; the crew's
# ceiling took it off the menu, and nothing here may put it back -- a plan the
# crew will not carry out is not a plan.
#
# A target above what MAX_STOP_DURATION_MIN buys is not dropped, it is CUT
# DOWN to where the hour ends (see the search). So the top of this tuple still
# earns its place: from a low SoC an hour reaches past 90%, and from a high one
# every entry above the cut collapses into the same "charge for the full hour"
# option.
CHARGE_TARGETS_PCT = (55, 60, 65, 70, 75, 80, 85, 90, 95)


def get_charging_power(soc_pct):
    """Charging power in kW at this state of charge, linear between points."""
    if soc_pct <= 5:
        return CHARGING_CURVE[5]
    if soc_pct >= 100:
        return CHARGING_CURVE[100]
    lower = int(soc_pct // 5) * 5
    upper = lower + 5
    lo, hi = CHARGING_CURVE[lower], CHARGING_CURVE[upper]
    return lo + ((soc_pct - lower) / (upper - lower)) * (hi - lo)


def _build_charge_clock(capacity_wh, step=0.1):
    """Cumulative minutes to charge from 5% up to each point on a 0.1% grid.

    Charging time is an integral over SoC, so it is path independent: the time
    from a to b is just clock(b) - clock(a). Building the clock once turns every
    later query into two lookups.

    This is not a micro-optimisation. Integrating the curve on every call made
    the strategy table take 9.6 SECONDS, inside a fragment that reran every 10
    on the same thread as the rest of the earlier Streamlit dashboard -- the
    exact failure that once made the whole pit refresh at 7-10 s instead of 1.
    The web backend polls it every 10 s too, so the cost still matters.
    """
    n = int(round(100.0 / step)) + 1
    clock = [0.0] * n
    total = 0.0
    for i in range(1, n):
        lo = i * step
        hi = lo + step
        kw = get_charging_power(lo + step / 2.0)
        total += ((capacity_wh * step / 100.0) / (kw * 1000.0)) * 60.0
        clock[i] = total
    return clock


_CHARGE_CLOCK_CACHE = {}


def _charge_clock(capacity_wh):
    key = round(float(capacity_wh), 3)
    clock = _CHARGE_CLOCK_CACHE.get(key)
    if clock is None:
        clock = _build_charge_clock(key)
        _CHARGE_CLOCK_CACHE[key] = clock
    return clock


def charging_time_min(start_soc, target_soc, capacity_wh=BATTERY_FULL_WH):
    """Minutes to charge between two SoC percentages, following the curve."""
    if target_soc <= start_soc:
        return 0.0
    clock = _charge_clock(capacity_wh)
    last = len(clock) - 1

    def at(soc):
        x = min(max(float(soc), 0.0), 100.0) * 10.0     # 0.1% grid
        i = int(x)
        if i >= last:
            return clock[last]
        return clock[i] + (x - i) * (clock[i + 1] - clock[i])

    return max(0.0, at(target_soc) - at(max(start_soc, MIN_SOC_PCT)))


def charge_soc_after_min(start_soc, minutes, capacity_wh=BATTERY_FULL_WH):
    """Where a charge of this many minutes gets to -- charging_time_min inverted.

    Reads the same clock, with the same interpolation, so the round trip holds:
    charging_time_min(a, charge_soc_after_min(a, m)) == m to the grid. That is
    what lets the search offer "charge for the full hour" as a target like any
    other, instead of dropping the targets an hour cannot reach.
    Stops at MAX_CHARGE_SOC_PCT, never 100%. The search uses this as the top
    of its target list ("charge for the full hour"), so an uncapped answer here
    would smuggle a 100% charge back into plans the crew has ruled out. A pack
    already above the ceiling is left where it is rather than dragged down.
    """
    start = min(max(float(start_soc), 0.0), 100.0)
    if minutes <= 0:
        return start
    ceiling = max(start, MAX_CHARGE_SOC_PCT)
    clock = _charge_clock(capacity_wh)
    last = len(clock) - 1
    x0 = max(start, MIN_SOC_PCT) * 10.0              # the 0.1% grid, as at() reads it
    i0 = min(int(x0), last)
    base = clock[i0] + (x0 - i0) * (clock[min(i0 + 1, last)] - clock[i0])
    goal = base + float(minutes)
    if goal >= clock[last]:
        return ceiling
    i = bisect.bisect_right(clock, goal) - 1
    if i >= last:
        return ceiling
    span = clock[i + 1] - clock[i]
    x = i + ((goal - clock[i]) / span if span > 0 else 0.0)
    return min(ceiling, max(start, x / 10.0))        # 0.1% grid -> percent


def stop_duration_min(start_soc, target_soc, capacity_wh=BATTERY_FULL_WH):
    """What the stop actually costs: charging, but inside the floor and ceiling."""
    return min(MAX_STOP_DURATION_MIN,
               max(MIN_STOP_DURATION_MIN,
                   charging_time_min(start_soc, target_soc, capacity_wh)))


def _plan_one_strategy(label, lap_time_min, energy_per_lap_wh, speed_kmh,
                       time_left_min, start_wh, current_lap,
                       capacity_wh=BATTERY_FULL_WH):
    """Best race plan for ONE driving strategy: laps, stops, and a full trace.

    Depth-first over "drive until you must stop, then pick a charge target",
    trying every target at every stop. Ranked on laps first, then total pit
    time, then number of stops -- laps win the race, and between two plans that
    finish on the same lap, the one that spends less time stationary is the one
    with room for a mistake.

    THE SEARCH CARRIES NO TRACE. It decides a stop plan and nothing else; the
    ~400-point curve the graph needs is rebuilt once, afterwards, by _replay().
    Threading the trace through the recursion meant copying a 400-element list
    at every node of a few-thousand-node search, and that was most of the cost.

    Returns ONE simulation. The table and the graph both render this same
    result and cannot disagree, which is exactly what went wrong before: the
    graph kept its own copy of the constants and re-derived the plan at a flat
    charge rate, so it drew a straight charging line under a table computed
    from the curve.
    """
    if lap_time_min <= 0 or energy_per_lap_wh <= 0:
        return None

    start_wh = min(float(start_wh), capacity_wh)
    if start_wh <= 0:
        start_wh = capacity_wh

    floor = BATTERY_FLOOR_WH
    stint_limit = DRIVER_STINT_LIMIT_MIN
    swap_min = DRIVER_CHANGE_TIME_MIN

    def drive(t, e, n, d, swaps):
        """Run laps until time, energy or the driver clock stops us.

        Advances a BLOCK of laps at a time -- as many as fit before the next
        driver change -- rather than one lap per iteration. Same answer, about
        twelve iterations instead of four hundred, and this is the hot loop of
        the whole search: it runs at every node.
        """
        while True:
            # Laps that fit in this driver's remaining stint. Zero means the
            # next lap would cross the limit, so a change comes first.
            in_stint = int((stint_limit - d) / lap_time_min)
            if in_stint <= 0:
                if t + swap_min + lap_time_min > time_left_min:
                    break
                if e - energy_per_lap_wh < floor:
                    break
                t += swap_min
                d = 0.0
                swaps += 1
                continue

            by_time = int((time_left_min - t) / lap_time_min)
            by_energy = int((e - floor) / energy_per_lap_wh)
            k = min(in_stint, by_time, by_energy)
            if k <= 0:
                break

            t += k * lap_time_min
            e -= k * energy_per_lap_wh
            n += k
            d += k * lap_time_min
            if k < in_stint:
                break          # stopped by time or energy, not by the stint
        return t, e, n, d, swaps

    best = {}
    memo = {}

    # `charge_min` here is the CHARGING stops only, which is what the search
    # itself decides. What the plan reports as pit time is that plus the
    # driver changes -- see the key below.
    def search(t0, e0, n0, d0, stops, charge_stop_min, swaps0):
        # Admissible bound: even driving flat out with free energy and no
        # further stops, the laps still available are (time left) / lap time.
        # If that cannot beat the best plan found so far, nothing below this
        # node can either. Pure pruning -- it changes the search cost, never
        # the answer, which the self-check's lap counts verify.
        if best:
            ceiling = n0 + int((time_left_min - t0) // lap_time_min)
            if ceiling < best["laps"]:
                return

        t, e, n, d, swaps = drive(t0, e0, n0, d0, swaps0)

        # PIT TIME IS EVERY MINUTE THE CAR IS NOT MOVING: the charging stops
        # plus the driver changes. A change made at a charging stop is already
        # in the stop (the car is stationary anyway, and `swaps` does not count
        # it); a change mid-stint costs DRIVER_CHANGE_TIME_MIN of its own, and
        # those minutes are just as gone as the ones spent on the charger.
        # Ranking on the total, not on charging alone, is the same rule it
        # always was -- between two plans that finish on the same lap, take the
        # one that stood still less.
        swap_total = swaps * swap_min
        pit_min = charge_stop_min + swap_total
        key = (n, -pit_min, -len(stops))
        if not best or key > best["_key"]:
            best.update({"_key": key, "laps": n, "stops": list(stops),
                         "pit_min": pit_min, "charge_stop_min": charge_stop_min,
                         "swap_min": swap_total, "swaps": swaps,
                         "final_wh": e, "time_used": t})

        if t >= time_left_min or len(stops) >= MAX_STOPS:
            return

        soc_now = (e / capacity_wh) * 100.0
        # Everything above where an hour on the charger ends is the SAME stop:
        # plug in, wait MAX_STOP_DURATION_MIN, leave at whatever SoC that is.
        # Cutting the targets down to it (rather than throwing them away) keeps
        # that stop on the menu, and collapses the ones above into one branch.
        soc_cap = charge_soc_after_min(soc_now, MAX_STOP_DURATION_MIN, capacity_wh)
        tried = []
        for want in CHARGE_TARGETS_PCT:
            target = min(float(want), soc_cap)
            if target <= soc_now + 1.0:
                continue
            if any(abs(target - x) < 1e-9 for x in tried):
                continue
            tried.append(target)
            target_wh = capacity_wh * target / 100.0
            if target_wh - energy_per_lap_wh < floor:
                continue
            charge = min(charging_time_min(soc_now, target, capacity_wh),
                         MAX_STOP_DURATION_MIN)
            dur = max(MIN_STOP_DURATION_MIN, charge)
            # A stop that does not finish before the flag is pure loss.
            if t + dur >= time_left_min:
                continue

            # Memo on where the stop LEAVES us. Two routes to the same
            # (time, energy, stop count) cannot be told apart afterwards, so
            # only the one that arrived there with more laps is worth expanding.
            mkey = (round(t + dur, 1), round(target_wh, -1), len(stops) + 1)
            seen = memo.get(mkey)
            if seen is not None and seen >= n:
                continue
            memo[mkey] = n

            search(t + dur, target_wh, n, 0.0,
                   stops + [{"number": len(stops) + 1,
                             "after_lap": current_lap + n,
                             "at_min": t,
                             "soc_before": soc_now,
                             "soc_after": float(target),
                             "charge_min": charge,
                             "stop_min": dur}],
                   charge_stop_min + dur, swaps)

    search(0.0, start_wh, 0, 0.0, [], 0.0, 0)

    if not best:
        return None
    best.update({"label": label, "lap_time_min": lap_time_min,
                 "energy_per_lap_wh": energy_per_lap_wh, "speed_kmh": speed_kmh,
                 "total_time_min": time_left_min, "start_wh": start_wh,
                 "capacity_wh": capacity_wh})
    best["trace"] = _replay(best)
    return best


def _replay(plan):
    """Rebuild the (minute, Wh) curve for a finished plan, for the graph.

    Deterministic given the stop plan, so it reproduces the search exactly --
    and being separate means the search pays nothing for it.
    """
    lap_time = plan["lap_time_min"]
    per_lap = plan["energy_per_lap_wh"]
    cap = plan["capacity_wh"]
    limit = plan["total_time_min"]

    t, e, d = 0.0, plan["start_wh"], 0.0
    pts = [(0.0, e, "start")]
    stops = list(plan["stops"])

    while True:
        stop = stops.pop(0) if stops else None
        while True:
            if stop is not None and t >= stop["at_min"] - 1e-9:
                break
            swap = DRIVER_CHANGE_TIME_MIN if (
                d + lap_time > DRIVER_STINT_LIMIT_MIN) else 0.0
            if t + swap + lap_time > limit:
                break
            if e - per_lap < BATTERY_FLOOR_WH:
                break
            if swap:
                # Stationary for the change: time moves, energy does not, so a
                # swap reads on the graph as a flat step, not a kink.
                t += swap
                d = 0.0
                pts.append((t, e, "swap"))
            t += lap_time
            e -= per_lap
            d += lap_time
            pts.append((t, e, "lap"))

        if stop is None:
            break

        soc_before = (e / cap) * 100.0
        target = stop["soc_after"]
        pts.append((t, e, "stop"))
        # Sample the charge so the graph draws the real taper, not a straight
        # line between the endpoints.
        steps = 10
        for i in range(1, steps + 1):
            soc_i = soc_before + (target - soc_before) * i / steps
            pts.append((t + charging_time_min(soc_before, soc_i, cap),
                        cap * soc_i / 100.0, "charge"))
        t += stop["stop_min"]
        e = cap * target / 100.0
        d = 0.0
        # Waiting out the minimum-stop floor after charging has finished.
        if pts[-1][0] < t - 1e-9:
            pts.append((t, e, "hold"))

    return pts


def format_lap_time(minutes):
    """3.5 -> '3:30'. The pit reads lap times as a stopwatch, never 3.50 min."""
    total_s = int(round(float(minutes) * 60.0))
    return f"{total_s // 60}:{total_s % 60:02d}"


def calculate_all_strategies(time_left_min, current_available_wh, current_lap,
                             consumption_table, track_length_km=4.0):
    """One row per driving strategy, each carrying its own full simulation.

    Same signature and same column names as before, so the dashboard and the
    exports keep working; the Pit Strategy column now says what the plan
    actually charges to, and three columns are new.
    """
    rows = []
    for option in consumption_table:
        label = option['label']
        lap_time_min = float(option['lap_time_min'])
        energy_per_lap = float(option['energy_wh'])
        speed_kmh = track_length_km / (lap_time_min / 60.0) if lap_time_min else 0.0

        plan = _plan_one_strategy(label, lap_time_min, energy_per_lap, speed_kmh,
                                  time_left_min, current_available_wh,
                                  current_lap)

        if plan is None or plan["laps"] == 0:
            rows.append({
                'Label': label, 'Lap Time': format_lap_time(lap_time_min),
                'Speed (km/h)': "-", 'Total Laps': 0,
                'Energy/Lap (Wh)': round(energy_per_lap, 1),
                'Pit Strategy': "-", 'Charge To': "-", 'Charge Time': "-",
                'Pit Time': "-",
                'Driver Swaps': "-", 'Final SoC': "-", '_graph_data': None,
            })
            continue

        stops = plan["stops"]
        final_soc = (plan["final_wh"] / plan["capacity_wh"]) * 100.0
        # A plan that stops driving well before the flag has not run out of
        # time -- it has run out of ALLOWED STOPS, and is sitting in the pit
        # box for the rest of the race. MAX_STOPS is the regulation cap, so
        # this is not a knob to raise: it says the charges should have been
        # spent differently (fewer, fuller ones), or the pace should drop.
        idle_min = time_left_min - plan["time_used"]
        # Idle long enough that ANOTHER STOP would have fitted. Less than that
        # is just time left over at the flag, which is normal and not worth
        # putting on screen.
        stop_limited = (len(stops) >= MAX_STOPS
                        and idle_min > MIN_STOP_DURATION_MIN)
        pit_label = f"{len(stops)} Stops" if stops else "No Stops"
        if stop_limited:
            pit_label += f" (limit, {idle_min:.0f}m idle)"
        rows.append({
            'Label': label,
            'Lap Time': format_lap_time(lap_time_min),
            'Speed (km/h)': f"{speed_kmh:.1f}",
            'Total Laps': plan["laps"],
            'Energy/Lap (Wh)': round(energy_per_lap, 1),
            'Pit Strategy': pit_label,
            'Charge To': (" / ".join(f"{s['soc_after']:.0f}%" for s in stops)
                          if stops else "-"),
            # One entry per stop, in the same order as Charge To beside it, so
            # the crew reads "65% in 34 m" across the row. This is the CHARGE,
            # not the stop: a charge under the floor still costs a 30 min stop,
            # and Pit Time is where that shows up. The two columns differing is
            # the floor doing its job, not a mistake.
            'Charge Time': (" / ".join(f"{s['charge_min']:.0f} m" for s in stops)
                            if stops else "-"),
            # EVERY stationary minute: the charge stops and the driver changes
            # between them. It read charging-only until the crew pointed out
            # that a column called Pit Time, on a plan with eight driver
            # changes in it, was 40 minutes short of the time the car actually
            # spends standing still. Driver Swaps beside it carries its own
            # share, so the sum stays legible.
            'Pit Time': (f"{plan['pit_min']:.0f} m"
                         if (stops or plan["swaps"]) else "-"),
            'Driver Swaps': (f"{plan['swaps']} Swaps ({plan['swap_min']:.0f} m)"
                             if plan["swaps"] else "0 Swaps"),
            'Final SoC': f"{final_soc:.0f}%",
            '_graph_data': plan,
        })
    return rows


def create_combined_graph(graph_data_list):
    """Plot the simulations the table was built from. It does NOT re-derive.

    The previous version kept its own CHARGE_RATE, DRIVER_STINT and swap
    constants and replayed the plan itself, so the picture could drift from the
    numbers beside it. Now it only draws the trace it is handed, which is why
    the charging segments curve: that is the real taper, not a straight line.
    """
    fig, ax = plt.subplots(figsize=(10, 3.4))
    colors = ['#e74c3c', '#e67e22', '#f1c40f', '#3498db', '#9b59b6']
    fig.patch.set_facecolor('#0e1117')
    ax.set_facecolor('#0e1117')
    ax.tick_params(colors='white')
    ax.xaxis.label.set_color('white')
    ax.yaxis.label.set_color('white')
    ax.title.set_color('white')

    total_time = 0.0
    for plan in graph_data_list:
        if plan:
            total_time = max(total_time, plan.get("total_time_min", 0.0))

    for idx, plan in enumerate(graph_data_list):
        if not plan:
            continue
        color = colors[idx % len(colors)]
        trace = plan["trace"]
        xs = [max(0.0, plan["total_time_min"] - t) for t, _wh, _k in trace]
        ys = [wh for _t, wh, _k in trace]
        ax.plot(xs, ys, label=plan["label"], color=color, linewidth=2)

        # Mark where the car enters the pit lane. The charge that follows is
        # already visible as the curve; this says which lap it happens on.
        for stop in plan["stops"]:
            x = max(0.0, plan["total_time_min"] - stop["at_min"])
            y = plan["capacity_wh"] * stop["soc_before"] / 100.0
            ax.plot([x], [y], marker='v', markersize=6, color=color,
                    markeredgecolor='white', markeredgewidth=0.6, zorder=5)

    ax.axhline(y=BATTERY_FLOOR_WH, color='#e74c3c', linestyle='--',
               linewidth=1.5, label=f"{BATTERY_FLOOR_WH:.0f} Wh Floor")
    # The ceiling is drawn for the same reason as the floor: every charge in
    # the picture stops there, and without the line it reads as the plan being
    # timid rather than as the rule it is. The start of the race is the one
    # point allowed above it -- the car rolls out at 100%.
    ax.axhline(y=BATTERY_CEILING_WH, color='#2ecc71', linestyle=':',
               linewidth=1.2,
               label=f"{MAX_CHARGE_SOC_PCT:.0f}% charge ceiling "
                     f"({BATTERY_CEILING_WH:.0f} Wh)")
    ax.set_ylim(0, BATTERY_FULL_WH * 1.05)
    ax.invert_xaxis()
    # The taper is in the data but is not visible at this zoom -- a 46 minute
    # charge is 3% of a 24 hour axis -- so the title does not claim it. Where
    # the taper actually shows is the Pit Time column beside this.
    ax.set_title("Battery forecast - Wh remaining vs minutes left")
    ax.set_xlabel("Minutes remaining")
    ax.grid(True, linestyle='--', alpha=0.3, color='gray')
    ax.legend(loc='upper left', fontsize='small', facecolor='#1a1a1a',
              edgecolor='none', labelcolor='white', ncol=2)
    fig.tight_layout()
    return fig


    # ==============================================================================
# LIVE TRACK MAPPING (Based on the defined Track Segments)
# ==============================================================================
Sections = [
{"segment_id": 1, "start_m": 0, "end_m": 600, "turns": [1], "max_speed_kmh": 75},
{"segment_id": 2, "start_m": 600, "end_m": 1000, "turns": [2, 3], "max_speed_kmh": 80},
{"segment_id": 3, "start_m": 1000, "end_m": 1800, "turns": [4], "max_speed_kmh": 110},
{"segment_id": 4, "start_m": 1800, "end_m": 1910, "turns": [5, 6], "max_speed_kmh": 60},
{"segment_id": 5, "start_m": 1910, "end_m": 2400, "turns": [7], "max_speed_kmh": 78},
{"segment_id": 6, "start_m": 2400, "end_m": 2500, "turns": [8, 9], "max_speed_kmh": 45},
{"segment_id": 7, "start_m": 2500, "end_m": 3000, "turns": [10, 11], "max_speed_kmh": 78},
{"segment_id": 8, "start_m": 3000, "end_m": 3430, "turns": [12], "max_speed_kmh": 40},
{"segment_id": 9, "start_m": 3430, "end_m": 4000, "turns": [15, 16], "max_speed_kmh": 54}
]


# "turn" is the ETCR turn number the landmark is drawn at on the circuit map
# (see TURN_START_TRACK_M below). Landmarks without one are drawn at dist_m.
TRACK_LANDMARKS = [
    {"name": "Turn 1", "dist_m": 600, "turn": 1, "max_speed": 75, "desc": "Slow down to 75 km/h"},
    {"name": "Turn 2", "dist_m": 710, "turn": 2, "max_speed": 80, "desc": "Ends at 800 meters, accelerate slowly downhill"},
    {"name": "Start of Uphill", "dist_m": 1010, "max_speed": 110, "desc": "Uphill section, Turn 4 ahead"},
    {"name": "Chicane (Turns 5,6)", "dist_m": 1860, "turn": 5, "max_speed": 60, "desc": "Left turn followed by sharp right"},
    {"name": "Turn 7", "dist_m": 2400, "turn": 7, "max_speed": 78, "desc": "The turn feels almost straight"},
    {"name": "Turns 8,9", "dist_m": 2500, "turn": 8, "max_speed": 45, "desc": "Very slow turns"},
    {"name": "Turns 10,11", "dist_m": 3000, "turn": 10, "max_speed": 78, "desc": "Followed by a slow turn"},
    {"name": "Turn 12", "dist_m": 3430, "turn": 12, "max_speed": 40, "desc": "Slow turn, followed by uphill acceleration"},
    {"name": "Chicane 15,16", "dist_m": 3900, "turn": 15, "max_speed": 54, "desc": "Large chicane near the finish line"},
    {"name": "Finish Line", "dist_m": 4000, "max_speed": 100, "desc": "End of lap"}
]

# Where each ETCR-numbered turn BEGINS on the real circuit, in metres from the
# track.py finish line (the car's zero). Measured from the OSM centreline in
# zolder_centreline.py: the point where the heading starts to change. Paired
# turns (5,6 / 8,9 / 13,14 / 15,16) list where the second half changes
# direction. tools/check_track.py re-measures every one of these.
TURN_START_TRACK_M = {
    1: 280, 2: 580, 3: 765, 4: 1075, 5: 1745, 6: 1780, 7: 2060, 8: 2305,
    9: 2345, 10: 2390, 11: 2525, 12: 3015, 13: 3125, 14: 3185, 15: 3605, 16: 3655,
}

# The sector document (and 210s.xlsx) measures its distances from a zero that
# sits this far BEFORE the car's zero. Evidence: with no offset, turns fall
# outside the sectors the document puts them in (T5,6 before the 110 m chicane
# sector S4); every offset from 110 to 130 m puts all of them inside, and the
# 210s.xlsx slowest points trail the real corners by 65-85 m. 120 is the middle
# of that window. check_track.py prints the window again.
#
# DEMO PROFILE ONLY. The car's lap distance, the target-speed lookup, the pit's
# sector timing AND the map's sector drawing all count from the track.py line
# (the team wants S1 to start on the start/finish line); this only shifts where
# the presentation map reads the 210 s profile. Set it to 0 once profiles
# measured on the car replace 210s.xlsx.
DOC_TO_TRACK_OFFSET_M = 120.0

SECTIONS_INFO = {
    1: {"range": (0, 600), "name": "Section 1"},
    2: {"range": (600, 1000), "name": "Section 2"},
    3: {"range": (1000, 1800), "name": "Section 3"},
    4: {"range": (1800, 1910), "name": "Section 4"},
    5: {"range": (1910, 2400), "name": "Section 5"},
    6: {"range": (2400, 2500), "name": "Section 6"},
    7: {"range": (2500, 3000), "name": "Section 7"},
    8: {"range": (3000, 3430), "name": "Section 8"},
    9: {"range": (3430, 4000), "name": "Section 9"},
}

def get_live_track_status(current_dist_m, profile_df=None):
    """
    Returns the current section, target speed from the profile, and next track landmark.
    """
    current_dist_m = current_dist_m % 4000.0  # Ensure we stay within 0-4000m

    # 1. Identify Current Section
    current_sec_name = "לא ידוע"
    for sec_id, info in SECTIONS_INFO.items():
        if info["range"][0] <= current_dist_m < info["range"][1]:
            current_sec_name = info["name"]
            break

    # 2. Get target speed from Excel profile
    target_speed = 68.0 
    if profile_df is not None and not profile_df.empty:
        target_speed = get_target_speed(profile_df, current_dist_m)

    # 3. Find next landmark
    next_landmark = None
    dist_to_landmark = 0

    for landmark in TRACK_LANDMARKS:
        if landmark["dist_m"] > current_dist_m:
            next_landmark = landmark
            dist_to_landmark = landmark["dist_m"] - current_dist_m
            break

    if not next_landmark:  # Failsafe
        next_landmark = TRACK_LANDMARKS[0]
        dist_to_landmark = (4000 - current_dist_m) + TRACK_LANDMARKS[0]["dist_m"]

    return {
        "section": current_sec_name,
        "target_speed": target_speed,
        "next_feature": next_landmark["name"],
        "next_feature_desc": next_landmark["desc"],
        "next_feature_speed": next_landmark["max_speed"],
        "distance_to_next": dist_to_landmark
    }


# =============================================================================
# Self-check:  python Pit_Dashboard/strategy_engine.py
# =============================================================================
# The strategy table is how the pit decides when to stop and how long to charge,
# and a plan that quietly breaks a race rule looks exactly like one that does
# not. So every plan this produces is checked against the rules it claims to
# obey, and -- the part that actually went wrong before the merge -- the trace
# the GRAPH draws is checked against the numbers in the TABLE.
if __name__ == "__main__":
    # THE MATRIX THE PIT ACTUALLY PLANS ON, not a copy of it. Both numbers a
    # row needs come from constants.PROFILE_MATRIX -- the lap time and the
    # Wh/lap -- and that same list is what /api/strategy builds its table from.
    # No speed curve is read here or there.
    #
    # It is a copy that made this check worth little: the literal that used to
    # sit here was a 210 s base at 80 Wh, from the generated profiles, while
    # the car laps near 285 s and pays about 126 Wh for it. Every plan verified
    # here was a race nobody was going to drive.
    #
    # A profile with no stated Wh/lap is skipped, the same rule api.py applies:
    # an unknown consumption is not a zero one. If nothing is left (or
    # constants cannot be read at all) the matrix below stands in -- keep it in
    # step with constants.PROFILE_MATRIX if that is re-measured.
    _FALLBACK_TABLE = [
        {'label': 'Fast (-10%)', 'lap_time_min': 256.5 / 60.0, 'energy_wh': 159.5},
        {'label': 'Med-Fast (-5%)', 'lap_time_min': 270.75 / 60.0, 'energy_wh': 152.25},
        {'label': 'Base (285s)', 'lap_time_min': 285.0 / 60.0, 'energy_wh': 145.0},
        {'label': 'Med-Slow (+5%)', 'lap_time_min': 299.25 / 60.0, 'energy_wh': 137.75},
        {'label': 'Slow (+10%)', 'lap_time_min': 313.5 / 60.0, 'energy_wh': 130.5},
    ]
    try:
        import constants as _C
        _TABLE = [{'label': _s['label'], 'lap_time_min': float(_s['lap_time_min']),
                   'energy_wh': float(_s['energy_wh'])}
                  for _s in _C.STRATEGIES if _s.get('energy_wh') is not None]
        _SOURCE = "constants.STRATEGIES"
    except Exception as _exc:          # noqa: BLE001 - the check must still run
        print("constants unreadable (%s)" % _exc)
        _TABLE, _SOURCE = [], "built-in five"
    if not _TABLE:
        _TABLE, _SOURCE = list(_FALLBACK_TABLE), "built-in five"

    print("consumption matrix (%s):" % _SOURCE)
    for _r in _TABLE:
        print("    %-18s %6.2f min/lap   %5.1f Wh/lap"
              % (_r['label'], _r['lap_time_min'], _r['energy_wh']))
    _ok = True

    def _fail(msg):
        global _ok
        _ok = False
        print("    ** FAIL ** " + msg)

    def _want(cond, msg):
        if not cond:
            _fail(msg)

    print("charging curve (NOT measured -- see CHARGING_CURVE):")
    for _a, _b in ((5, 55), (5, 70), (5, 80), (5, 90), (5, 100)):
        _t = charging_time_min(_a, _b)
        _wh = BATTERY_FULL_WH * (_b - _a) / 100.0
        print("    %2d%% -> %3d%%   %6.1f min   %5.0f Wh   %5.1f Wh/min"
              % (_a, _b, _t, _wh, _wh / _t))
    _want(charging_time_min(5, 55) < charging_time_min(5, 70)
          < charging_time_min(5, 90) < charging_time_min(5, 100),
          "charge time is not monotonic in target SoC")
    _want(charging_time_min(70, 70) == 0.0, "charging to where we already are costs time")
    _want(charging_time_min(90, 70) == 0.0, "charging DOWN returns a time")
    # THE TAPER, MEASURED PER WATT-HOUR. It used to compare wall-clock minutes:
    # "90-100% must take longer than 5-55%". That held for the example curve the
    # charging branch shipped, and broke the moment a real pack was measured --
    # not because the taper went away, but because 10% of a pack is a tenth of
    # the energy of 50% of it, so the top slice can taper hard and still finish
    # sooner. What a taper actually claims is that the last watt-hours come in
    # SLOWER, and that is what this asks.
    def _wh_per_min(a, b):
        t = charging_time_min(a, b)
        return (BATTERY_FULL_WH * (b - a) / 100.0) / t if t else float("inf")

    _want(_wh_per_min(90, 100) < _wh_per_min(5, 55),
          "the curve does not taper -- the last 10%% goes in at %.0f Wh/min "
          "against %.0f Wh/min for the first 50%%"
          % (_wh_per_min(90, 100), _wh_per_min(5, 55)))

    # The hour cap, and the inverse the search plans it with. If the round trip
    # drifts, the planner books a stop of one length and the trace draws
    # another -- the table/graph split this file exists to prevent.
    print("the %.0f min ceiling, from each starting SoC:" % MAX_STOP_DURATION_MIN)
    for _s0 in (5, 20, 40, 60, 80, 95):
        _to = charge_soc_after_min(_s0, MAX_STOP_DURATION_MIN)
        _back = charging_time_min(_s0, _to)
        print("    %2d%% -> %5.1f%% in %.1f min" % (_s0, _to, _back))
        _want(_back <= MAX_STOP_DURATION_MIN + 1e-6,
              "an hour from %d%% reaches %.1f%%, which takes %.2f min"
              % (_s0, _to, _back))
        _want(_to >= MAX_CHARGE_SOC_PCT - 1e-3
              or abs(_back - MAX_STOP_DURATION_MIN) < 1e-3,
              "the charge clock does not invert at %d%%: %.1f%% costs %.3f min"
              % (_s0, _to, _back))
    _want(charge_soc_after_min(70, 0) == 70.0, "a zero-minute charge moves the SoC")
    _want(charge_soc_after_min(100, MAX_STOP_DURATION_MIN) == 100.0,
          "charging a full pack goes over 100%")
    # THE CREW'S CEILING, checked where it is easiest to lose: the hour cap is
    # the one path that picks a target the CHARGE_TARGETS_PCT tuple never
    # names.
    _want(charge_soc_after_min(5, 24 * 60.0) == MAX_CHARGE_SOC_PCT,
          "a whole day on the charger goes past %.0f%%" % MAX_CHARGE_SOC_PCT)
    _want(max(CHARGE_TARGETS_PCT) == MAX_CHARGE_SOC_PCT,
          "the target list tops out at %d%%, not the %.0f%% ceiling"
          % (max(CHARGE_TARGETS_PCT), MAX_CHARGE_SOC_PCT))
    _want(abs(BATTERY_FLOOR_WH - BATTERY_FULL_WH * 0.05) < 1e-9,
          "the floor is %.1f%% of the pack, not 5%%"
          % (100.0 * BATTERY_FLOOR_WH / BATTERY_FULL_WH))

    for _label, _left, _start in (("full 24 h, full pack", 24 * 60.0, BATTERY_FULL_WH),
                                  ("6 h left, part charged", 360.0, 3000.0),
                                  ("45 min, no stop fits", 45.0, BATTERY_FULL_WH),
                                  ("20 min, nearly empty", 20.0, 600.0),
                                  ("24 h from the floor", 24 * 60.0, 500.0)):
        print("\n%s:" % _label)
        for _row in calculate_all_strategies(_left, _start, 0, _TABLE):
            _p = _row["_graph_data"]
            if _p is None:
                _want(_row["Total Laps"] == 0,
                      "%s: no plan but laps > 0" % _row["Label"])
                continue
            _tr = _p["trace"]
            _laps = sum(1 for _, _, k in _tr if k == "lap")
            _swaps = sum(1 for _, _, k in _tr if k == "swap")
            _stops = sum(1 for _, _, k in _tr if k == "stop")
            _pit = sum(x["stop_min"] for x in _p["stops"])
            _nm = _row["Label"]

            # graph and table must describe the same race
            _want(_laps == _row["Total Laps"],
                  "%s: trace %d laps, table %d" % (_nm, _laps, _row["Total Laps"]))
            _want(_stops == len(_p["stops"]), "%s: stop count disagrees" % _nm)
            _want(_swaps == _p["swaps"], "%s: swap count disagrees" % _nm)

            # the race must be legal
            _want(_tr[-1][0] <= _left + 1e-6, "%s: runs past the flag" % _nm)
            _want(min(w for _, w, _ in _tr) >= BATTERY_FLOOR_WH - 1e-6,
                  "%s: goes below the %.0f Wh floor" % (_nm, BATTERY_FLOOR_WH))
            _want(max(w for _, w, _ in _tr) <= _p["capacity_wh"] + 1e-6,
                  "%s: goes above capacity" % _nm)
            # Above the ceiling only where the race started there. Everything
            # after the first stop is a charge, and no charge may reach it.
            _want(all(x["soc_after"] <= MAX_CHARGE_SOC_PCT + 1e-9
                      for x in _p["stops"]),
                  "%s: a stop charges past %.0f%%" % (_nm, MAX_CHARGE_SOC_PCT))
            _want(all(w <= BATTERY_CEILING_WH + 1e-6
                      for t, w, k in _tr if t > 0.0 and k in ("charge", "hold")),
                  "%s: a charge goes above the %.0f Wh ceiling"
                  % (_nm, BATTERY_CEILING_WH))
            _want(all(_tr[i][0] >= _tr[i - 1][0] - 1e-9 for i in range(1, len(_tr))),
                  "%s: time runs backwards" % _nm)
            for _st in _p["stops"]:
                _want(_st["stop_min"] >= MIN_STOP_DURATION_MIN - 1e-6,
                      "%s: a stop is under the %.0f min minimum"
                      % (_nm, MIN_STOP_DURATION_MIN))
                _want(_st["stop_min"] <= MAX_STOP_DURATION_MIN + 1e-6,
                      "%s: a %.1f min stop is over the %.0f min maximum"
                      % (_nm, _st["stop_min"], MAX_STOP_DURATION_MIN))
                _want(_st["stop_min"] >= _st["charge_min"] - 1e-6,
                      "%s: a stop is shorter than its own charge" % _nm)
            # The Charge Time column is one entry per stop, aligned with
            # Charge To. A row where they cannot be read across is worse than
            # no column at all.
            _want(_row["Charge Time"] ==
                  (" / ".join("%.0f m" % _st["charge_min"] for _st in _p["stops"])
                   if _p["stops"] else "-"),
                  "%s: Charge Time column disagrees with the plan" % _nm)
            # Pit time is the charge stops AND the driver changes, so the
            # race splits in two: driving, and standing still.
            _want(abs(_pit - _p["charge_stop_min"]) < 1e-6,
                  "%s: the stops do not add up to the charging time" % _nm)
            _want(abs(_p["swap_min"] - _p["swaps"] * DRIVER_CHANGE_TIME_MIN) < 1e-6,
                  "%s: swap time is not %d changes at %.0f min"
                  % (_nm, _p["swaps"], DRIVER_CHANGE_TIME_MIN))
            _want(abs(_p["pit_min"] - (_pit + _p["swap_min"])) < 1e-6,
                  "%s: pit time is not charging plus driver changes" % _nm)
            _want(abs((_row["Total Laps"] * _p["lap_time_min"] + _p["pit_min"])
                      - _p["time_used"]) < 1e-6,
                  "%s: driving + pit != time used" % _nm)
            # no driver may exceed the stint limit
            _run = _worst = 0.0
            for _, _, _k in _tr:
                if _k == "lap":
                    _run += _p["lap_time_min"]
                    _worst = max(_worst, _run)
                elif _k in ("swap", "stop"):
                    _run = 0.0
            _want(_worst <= DRIVER_STINT_LIMIT_MIN + 1e-6,
                  "%s: a driver runs %.1f min, over the %.0f limit"
                  % (_nm, _worst, DRIVER_STINT_LIMIT_MIN))

            print("    %-16s %3d laps | %-26s | pit %5.1f (charge %5.1f + %d swaps)"
                  % (_nm, _row["Total Laps"], _row["Pit Strategy"],
                     _p["pit_min"], _pit, _p["swaps"]))

    import time as _time
    # BEST OF THREE, not one cold run. On this laptop the same unchanged code
    # has measured 178, 291, 333, 458, 598 and 893 ms depending on what else is
    # running -- and a check that fails for reasons the code cannot control is
    # one people learn to ignore, which is worse than not having it. The
    # minimum is the honest answer to "what does this cost when it gets to
    # run", and a real regression still moves it.
    _runs = []
    for _ in range(3):
        _t0 = _time.perf_counter()
        calculate_all_strategies(24 * 60.0, BATTERY_FULL_WH, 0, _TABLE)
        _runs.append((_time.perf_counter() - _t0) * 1000)
    _ms = min(_runs)
    print("\nfull 24 h table: %.0f ms (best of 3: %s)"
          % (_ms, ", ".join("%.0f" % r for r in _runs)))
    # The pit backend recomputes this table on its strategy poll, in a worker
    # thread shared with the other heavy endpoints. Half a second here is half
    # a second those wait.
    _want(_ms < 500, "too slow for the strategy poll: %.0f ms" % _ms)

    print("\nSELF-CHECK", "PASSED" if _ok else "FAILED")
    raise SystemExit(0 if _ok else 1)
