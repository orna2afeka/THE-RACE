"""Network-aware sync policy.

A cellular-aware device shouldn't sync the same way on Wi-Fi as it does on a
weak Edge link with a near-dead battery. This module maps the current link
conditions (network type + battery level) to two knobs the batcher uses:

  * batch_size      -- how many points to group per request
  * flush_interval  -- how often to attempt a send

The intent: on a fast link, send small batches often (low latency); on a slow
or expensive link, send larger batches less often (fewer round-trips, less
radio wake-up). On low battery, back off further. When the device knows it's
offline, don't even try -- just keep buffering durably.

This lives outside the pipeline so the policy is swappable and testable on its
own; the SDK core stays generic.
"""

from __future__ import annotations

# Faster / cheaper links -> smaller batches, more often (low latency). Slower /
# costlier links -> bigger batches, less often, so each expensive round-trip and
# radio wake-up carries more samples.
NETWORK_PROFILES = {
    "wifi":    {"batch_size": 25,  "flush_interval": 1.0},
    "5g":      {"batch_size": 40,  "flush_interval": 1.5},
    "lte":     {"batch_size": 50,  "flush_interval": 2.0},
    "3g":      {"batch_size": 75,  "flush_interval": 4.0},
    "edge":    {"batch_size": 100, "flush_interval": 6.0},
    "offline": {"batch_size": 50,  "flush_interval": 2.0},  # buffer only
}

LOW_BATTERY_PCT = 20


def plan(network: str, battery: float | None = None) -> tuple[int, float, bool]:
    """Return (batch_size, flush_interval, allow_send) for the conditions."""
    profile = NETWORK_PROFILES.get(network, NETWORK_PROFILES["lte"])
    batch_size = profile["batch_size"]
    flush_interval = profile["flush_interval"]
    allow_send = network != "offline"

    # Low battery: sync less aggressively, in fewer/larger bursts, to cut the
    # number of energy-hungry radio wake-ups.
    if battery is not None and battery < LOW_BATTERY_PCT:
        flush_interval *= 2.0
        batch_size = min(batch_size * 2, 200)

    return batch_size, flush_interval, allow_send
