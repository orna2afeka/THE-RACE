"""Telemetry & Edge-Sync edge SDK."""

from .client import (Client, RefetchBatch, RejectedError, auto_init, force_flush, init, track,
                     track_many, track_snapshot)

__version__ = "0.2.0"

__all__ = ["Client", "RefetchBatch", "RejectedError", "init", "auto_init", "track", "track_many",
           "track_snapshot", "force_flush"]
