# ui.py
# UI rendering helpers for the Afeka Pit Wall dashboard.

from constants import (
    SECTION_NAMES,
    SECTION_TURN_LABELS,
    SECTION_RISK,
    SECTION_COLORS,
)

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

def render_metric(col, title, val, unit, condition="normal"):
    color_class = ""
    if condition == "warning": color_class = "warning"
    if condition == "critical": color_class = "critical"
    col.markdown(f"""
    <div class="metric-container">
        <div class="metric-title">{title}</div>
        <div class="metric-value {color_class}">{val} <span style="font-size:16px;">{unit}</span></div>
    </div>
    """, unsafe_allow_html=True)


def render_sector_display(track_status, current_dist_m, sector_id):
    """Sector card with the velocity-profile TARGET SPEED for this point."""
    risk = SECTION_RISK.get(sector_id, "normal")
    color = SECTION_COLORS[risk]
    current_class = "current" if risk == "normal" else f"current-{risk}"
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
