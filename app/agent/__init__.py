"""The autonomous half of the app.

`settings` holds the policy, `runner` executes one pass of the loop, and
`scheduler` decides when a pass happens. Import order matters only in that
`runner` reads `settings` at the start of every run — so changing a limit takes
effect on the next run, never mid-run.
"""

from __future__ import annotations

from . import runner, scheduler, settings

__all__ = ["runner", "scheduler", "settings"]
