# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""punctim sim -- a stdlib-only system simulator: given a system description (nodes, media,
traffic per adapter, MAC, power) it prints which medium satisfies it, the exact airtime /
duty-cycle / queue / MAC / Pipe plan computed from the certified codecs, the modelled link
budget and energy, and a hardware-class recommendation.

    python3 python/punctim.py sim --spec tests/sim_example.json [--json]

  media   -- per-frame cost of each medium (mediumlab_core) + the lua M.media link budgets
  traffic -- adapter fragmentation via the certified packetizers + the DCF-Pipe plan
  plan    -- evaluation, recommendation, text/JSON rendering, the CLI (`main`)
"""
from .plan import main, build_plan, normalize, to_json, render_text  # noqa: F401

__all__ = ["main", "build_plan", "normalize", "to_json", "render_text"]
