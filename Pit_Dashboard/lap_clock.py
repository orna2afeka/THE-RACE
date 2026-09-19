# Pit_Dashboard/lap_clock.py — the lap clock, worked out in ONE place.
#
# WHY THIS IS NOT IN api.py, WHERE IT WAS WRITTEN. Two screens show the lap the
# car is driving: the dashboard header (Pit_Web/api.py -> App.tsx) and the
# garage TV (tools/pit_wall.py -> Pit_Dashboard/wall.html). They worked it out
# separately -- the header by the rule below, the TV by subtracting two of the
# car's own wall-clock stamps in JavaScript -- so the TV never saw a Stop lap
# clock, spent the four to five seconds of a Cut lap still counting the lap
# that had just been ended, and was free to disagree with the header for any
# other reason nobody had thought of yet.
#
# That is two clocks agreeing by coincidence, which is exactly what the car and
# the pit stopped doing, and it costs the same thing when it goes wrong: an
# afternoon was spent on a GPS that turned out to be fine, chased because two
# instruments disagreed. So the rule lives here and both screens read its
# answer. Same reason drivetrain.py owns the speed formula.
#
# NOTHING HERE TOUCHES THE DATABASE. The caller reads what it holds, on its own
# connection, and hands it in. That is what lets the pit wall go on being the
# independent process its own header argues for: it shares the dashboard's
# arithmetic without having to wait on, or even be started with, the dashboard.

# --------------------------------------------------------------------------- #
# The two app_state rows the pit writes about this clock. Pit_Web owns the
# writes (api.py); the pit wall only ever reads them. The names live here
# because both readers need them and neither may guess.
# --------------------------------------------------------------------------- #
LAP_HOLD_KEY = "lap_clock_hold"
# The instant the PIT last re-datumed the lap clock (Cut lap / Restart lap).
# The car answers a press in its own time -- the command has to reach it, be
# applied, and come back inside a telemetry sample -- and until it does, the
# screens would go on counting the lap the engineer has just ended. This is
# what they count from in the meantime. See lap_clock().
LAP_DATUM_KEY = "lap_clock_datum"


def lap_clock(state, sample_at, hold_at=None, datum_at=None, estimate=None):
    """{startedAt, atSampleS, source, heldAt} for the clock on both screens.

    THE PIT'S OWN PRESS COMES FIRST, and only while the car has not answered it
    yet -- see LAP_DATUM_KEY above. Everything after this paragraph is what the
    clock reads the rest of the time, which is almost all of it.

    THE SHARED STOPWATCH COMES NEXT. The car publishes the elapsed time its
    own HUD is showing and whether that is still moving
    (main._publish_stopwatch), and that is the one clock: the driver, the pit
    and the public page all read the same number, and either end can move it.
    Whichever button was pressed last is the one that stands, because the car
    applies them in the order they arrive.

    It is rebuilt against `sample_at` -- the instant of the newest sample minus
    the elapsed the car measured -- rather than taken as a timestamp. The car
    measures with time.monotonic(), which nothing can step; its wall clock has
    no RTC behind it and NTP shifts it minutes at a time after boot. NOTE that
    both callers pass the newest sample's own device_ts, which IS the car's
    clock: the elapsed is the car's contribution either way, and it is the
    datum that is only ever as good as the stamp it is hung on.

    THAT BRANCH IS NOT REACHED TODAY, and is here ready rather than removed:
    neither caller carries stopwatch_s in `state` (api._STATE_COLUMNS does not
    list it, tools/pit_wall.FIELDS does not either), and the car half of the
    shared stopwatch is still on branch pi-shared-stopwatch. Wiring it is one
    name in each of those two lists -- BOTH of them, or the screens part
    company again, which is the whole reason this file exists.

    Falls back to the lap datum, and then to the store's estimate, for a car on
    code older than the shared stopwatch. Never invents a datum: with none of
    the three, the screen shows a dash rather than counting from the start of
    the session.

    `atSampleS` is the figure at the NEWEST SAMPLE: the last elapsed the CAR
    confirmed, as opposed to the one a screen is counting on its own.

    SCREENS NO LONGER FREEZE ON IT. They used to, on the argument that a clock
    counting through a dead link reports a long lap rather than a dead link.
    That holds for a measurement and not for a stopwatch -- the lap does not
    pause because the telemetry did -- and on this car the link drops often
    enough that a clock stopping with it is a clock nobody can use. The pit
    asked for one that always runs.

    What the freeze protected is kept, in the LABEL rather than in the number:
    a screen counting past the newest sample says so, and for how long, so the
    figure is never read as one the car has confirmed. `atSampleS` is what it
    shows beside that when it wants to name the confirmed figure.

    Only `heldAt` stops a clock now, and that is a deliberate press at one end
    or the other -- not a missing link.

    Arguments, all read by the caller so this stays free of SQL:
      state      the newest reading: stopwatch_s, stopwatch_stopped,
                 lap_started_ts. A missing key is an absent reading, not zero.
      sample_at  when that reading was taken, or None if there is no sample.
      hold_at    LAP_HOLD_KEY's "heldAt" -- the pit's Stop lap clock.
      datum_at   LAP_DATUM_KEY's "atS" -- the pit's Cut lap / Restart lap.
      estimate   called with no arguments for the store's fallback datum
                 (db.lap_started_estimate), and only if it is needed.
    """
    # The pit's own press, shown before the car has had time to answer. Once a
    # sample TAKEN AFTER the press arrives, the car's own flag governs and this
    # is ignored -- so the screens feel instant without ever disagreeing with
    # the car for longer than one sample.
    pressed_at = float(hold_at) if hold_at else None
    unanswered = (pressed_at is not None and sample_at is not None
                  and pressed_at > sample_at)

    # THE PIT'S OWN RE-DATUM, ahead of everything the car has said. Cut lap and
    # Restart lap both start the lap again, and the round trip that proves it --
    # up to Firebase, down to the car over LTE, applied, and back inside the
    # next telemetry sample -- measured four to five seconds on a bad link. The
    # screens spent those seconds still counting the lap that had just been
    # ended, which is the one number an engineer presses that button to see
    # change.
    #
    # Same handover rule as the hold above, and the same reason to trust it: the
    # moment a sample TAKEN AFTER the press arrives, the car's own datum governs
    # and this is ignored, so a screen can never disagree with the car for
    # longer than one sample. It is a head start, not a second opinion.
    # A STORE WITH NO SAMPLES AT ALL IS NOT "the car has not answered yet", it
    # is a pit that has never heard from the car -- a fresh database, or the
    # morning before anything is switched on. The press stays in app_state
    # across a restart, and without this it would be the newest thing the pit
    # knew about for as long as that lasted. The screens say nothing instead,
    # as they did before any of this.
    datum_at = float(datum_at) if datum_at else None
    if datum_at is not None and sample_at is not None and datum_at > sample_at:
        # atSampleS is 0.0, not the elapsed at the last sample: that sample is
        # OLDER than the press, so its figure belongs to the lap just ended.
        # The car confirmed nothing since the press, and 0.0 says that without
        # offering the time the pit was trying to clear as if it still stood.
        return {"startedAt": datum_at, "source": "pit", "atSampleS": 0.0,
                "heldAt": pressed_at if (pressed_at is not None
                                         and pressed_at >= datum_at) else None}

    stopwatch = state.get("stopwatch_s")
    if stopwatch is not None and sample_at is not None:
        at_sample = max(0.0, float(stopwatch))
        started = sample_at - at_sample
        stopped = bool(state.get("stopwatch_stopped")) or unanswered
        return {"startedAt": started, "source": "car", "atSampleS": at_sample,
                "heldAt": (started + at_sample) if stopped else None}

    started = state.get("lap_started_ts")
    source = "car"
    if not started:
        started = estimate() if estimate is not None else None
        source = "store" if started else None
    if not started:
        return {"startedAt": None, "atSampleS": None, "source": None,
                "heldAt": None}
    started = float(started)
    # The newest sample's own clock, so this stays in step with the age every
    # tile is labelled with.
    at_sample = (sample_at - started) if sample_at is not None else None
    # A press stamped BEFORE this lap's datum belongs to a previous lap, so the
    # next crossing of the line releases the clock on its own.
    held = pressed_at if pressed_at and pressed_at >= started else None
    return {"startedAt": started, "source": source, "heldAt": held,
            "atSampleS": None if at_sample is None else max(0.0, at_sample)}
