# ui.py
# UI rendering helpers for the Afeka Pit Wall dashboard.

from constants import (
    SECTION_NAMES,
    SECTION_TURN_LABELS,
    SECTION_RISK,
    SECTION_COLORS,
    NORMAL,
    WARNING,
    CRITICAL,
    DATA_STALE_AFTER_S,
)


def _age_text(seconds):
    """Data age in units a human reads at a glance.

    "Stale · 77585s ago" is a number you have to do arithmetic on before you
    know whether to worry; "21h 33m ago" is not.

    Lives here (not in pit_dashboard.py, where it originated) so render_metric
    can use it for a carried-forward field's age caption without an import
    cycle -- pit_dashboard.py imports FROM ui, not the other way round. Other
    call sites now import it from here instead of keeping a second copy."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"

# Sharp inline-SVG glyphs for the sector card (raw HTML can't use Streamlit's
# native :material/…: icons). `currentColor` makes each inherit its span's color,
# and being inline they need no network — fine on the offline pit LAN.
_SVG_CHEVRON = (
    '<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align:-1px;'
    'margin-right:5px" aria-hidden="true"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>'
)
_SVG_BOLT = (
    '<svg viewBox="0 0 24 24" width="11" height="11" style="vertical-align:-1px;'
    'margin-right:3px" aria-hidden="true"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>'
)

def render_metric(col, title, val, unit, condition=NORMAL, large=False,
                  note=None, stale_s=None):
    """One tile. `condition` is a tier from limits.classify().

    Until now this function existed twice, byte-identical, here and in
    pit_dashboard.py -- and the pit_dashboard copy shadowed the import, so this
    file was dead code. Editing it changed nothing, which is a trap worth more
    than the duplication saved.

    `large` grows the number for the Live Metrics tab, where the tiles are the
    whole point of the page rather than a strip above it.

    `note` is a small line under the value, for a reading that needs a caveat
    attached to it wherever it appears -- the BMS pack voltage is known to
    decode about 2.3x high, and a number like that must never be shown bare.

    `stale_s`, when given, is how old a CARRIED-FORWARD value is (seconds) --
    i.e. the newest sample was null for this field and it fell back to
    last_known. Only actually shown once it exceeds DATA_STALE_AFTER_S (a
    value that's merely a couple seconds old from normal carry-forward jitter
    isn't worth a caption). Deliberately does NOT touch `condition`/the tier
    colour: a genuinely critical carried-forward reading must stay exactly as
    alarming as a live one, never muted to grey, which would read as "this
    isn't urgent" -- precisely backwards for an old critical number. Staleness
    is presentation, added here as an age caption + a de-emphasized class on
    the container, not physics -- classify() itself never sees it.
    """
    # An unrecognised tier deliberately falls through to no class rather than
    # raising: a tile with the default colour is a far better failure than a
    # dashboard that will not render.
    color_class = condition if condition in (WARNING, CRITICAL) else ""
    size_class = " large" if large else ""
    unit_px = 22 if large else 16
    stale = stale_s is not None and stale_s > DATA_STALE_AFTER_S
    if stale:
        size_class += " stale-carried"
        age_caption = f"· {_age_text(stale_s)} ago"
        note = f"{note} {age_caption}" if note else age_caption
    note_html = (f'<div class="metric-note">{note}</div>' if note else "")
    col.markdown(f"""
    <div class="metric-container{size_class}">
        <div class="metric-title">{title}</div>
        <div class="metric-value{size_class} {color_class}">{val} <span style="font-size:{unit_px}px;">{unit}</span></div>
        {note_html}
    </div>
    """, unsafe_allow_html=True)


def render_sector_display(track_status, current_dist_m, sector_id):
    """Sector card with the velocity-profile TARGET SPEED for this point."""
    risk = SECTION_RISK.get(sector_id, NORMAL)
    color = SECTION_COLORS[risk]
    # Class names follow the tier names, so .current-warning / .current-critical
    # in the stylesheet. They were .current-warn / .current-crit when the risk
    # values used the short spelling.
    current_class = "current" if risk == NORMAL else f"current-{risk}"
    sector_name = SECTION_NAMES.get(sector_id, f"Section {sector_id}")
    target_speed = track_status.get("target_speed", 0)
    next_name = track_status.get("next_feature", "N/A")
    next_dist = track_status.get("distance_to_next", 0)
    next_desc = track_status.get("next_feature_desc", "")
    next_speed = track_status.get("next_feature_speed", 0)

    segs_html = ""
    for i in range(1, 10):
        if i < sector_id:
            cls = "past"
        elif i == sector_id:
            cls = current_class
        else:
            cls = "future"
        turn = SECTION_TURN_LABELS.get(i, "") if i == sector_id else ""
        segs_html += (
            f'<div class="sector-seg {cls}"><div>S{i}</div>'
            f'<div style="font-size:13px;margin-top:2px;opacity:0.9">{turn}</div></div>'
        )

    return f"""
    <div class="sector-card">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;">
            <div>
                <div class="sector-sub">CURRENT SECTOR</div>
                <div class="sector-headline" style="color:{color}">SECTOR {sector_id}</div>
                <div style="color:#8899aa;font-size:13px;margin-top:5px;">{sector_name}</div>
            </div>
            <div style="text-align:right;">
                <div class="sector-sub">TARGET SPEED</div>
                <div style="font-size:30px;font-weight:800;color:{color}">{target_speed:.0f}<span style="font-size:13px;color:#556;font-weight:400;"> km/h</span></div>
                <div style="color:#445566;font-size:11px;margin-top:3px;">{current_dist_m:.0f} m / 4000 m</div>
            </div>
        </div>
        <div class="sector-strip">{segs_html}</div>
        <div class="next-feature-panel">
            <div class="sector-sub">NEXT FEATURE</div>
            <div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:5px;">
                <span class="next-feature-name">{_SVG_CHEVRON}{next_name}</span>
                <span class="dist-badge">in&nbsp;<b>{next_dist:.0f}m</b></span>
                <span class="speed-badge">{_SVG_BOLT}{next_speed} km/h</span>
            </div>
            <div class="next-feature-desc">"{next_desc}"</div>
        </div>
    </div>
    """
