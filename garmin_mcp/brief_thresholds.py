"""Thresholds the daily rules judge by, in a module with no imports of its own.

brief.py imports notes.py (a note can lower the day's ceiling) and notes.py needs
brief.py's resting-heart-rate threshold (it asks about the same signal). Rather
than let one of them restate the number — which is how "elevated" comes to mean
two things in one system — both read it from here.
"""

from __future__ import annotations

# How far above his own fourteen-day mean a resting heart rate has to sit before
# it counts as elevated.
RHR_OVER_BASELINE = 3.0
