"""Pure capture-program logic for reFrame.

A capture program decides which frames one shutter press produces, what each
frame overrides, and which one reaches the e-paper panel. Executing the plan is
reframe.py's job; deciding it is this module's.

The split exists so this logic is testable: picamera2 cannot be installed on a
development machine, so anything inside reframe.py can only be verified by
reading it.

No picamera2, no FastAPI, no I/O.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class PlannedFrame:
    """One frame to capture, as control overrides on top of the base controls."""
    controls: dict = field(default_factory=dict)
    wait_before: float = 0.0
    label: str = "0EV"


def _format_offset(offset):
    """Render an EV offset as a label: 0EV, -1EV, +0.5EV."""
    if offset == 0:
        return "0EV"
    text = f"{abs(offset):g}"
    return f"{'+' if offset > 0 else '-'}{text}EV"


class SingleShot:
    """One frame, exactly as the recipe asks. Today's behaviour."""

    id = "single"

    def plan(self, base_controls):
        return [PlannedFrame(controls={}, wait_before=0.0, label="0EV")]

    def select_for_display(self, frames):
        return 0

    def is_continuous(self):
        return False


class Bracket:
    """N frames stepped around the recipe's exposure."""

    id = "bracket"

    def __init__(self, frames=3, step_ev=1.0):
        self.frames = frames
        self.step_ev = step_ev

    def _offsets(self):
        half = self.frames // 2
        return [(index - half) * self.step_ev for index in range(self.frames)]

    def _overrides(self, base_controls, offset):
        if offset == 0:
            return {}

        if base_controls.get("AeEnable"):
            # Auto exposure: a stop is a change to the AE compensation control,
            # relative to whatever the recipe already asked for.
            return {"ExposureValue": base_controls.get("ExposureValue", 0) + offset}

        exposure = base_controls.get("ExposureTime")
        if exposure is None:
            # Manual mode with no exposure time is a misconfiguration handled
            # upstream; bracketing it would only multiply the confusion.
            return {}

        # Manual exposure: a stop is a doubling of exposure time.
        scaled = int(round(exposure * (2 ** offset)))

        # libcamera caps exposure at the frame duration, so a longer frame has
        # to carry a wider duration with it or it silently comes back the same
        # length as the base frame.
        base_low = base_controls.get("FrameDurationLimits", (scaled, scaled))[0]
        frame_us = max(scaled, base_low)
        return {"ExposureTime": scaled, "FrameDurationLimits": (frame_us, frame_us)}

    def plan(self, base_controls):
        base_controls = base_controls or {}
        return [
            PlannedFrame(controls=self._overrides(base_controls, offset),
                         wait_before=0.0,
                         label=_format_offset(offset))
            for offset in self._offsets()
        ]

    def select_for_display(self, frames):
        """Show the frame the recipe actually asked for.

        Auto-picking a "best" exposure by histogram would be a guess dressed as
        a decision; the offset frames wait in the gallery where there is a
        screen to decide on.
        """
        for index, frame in enumerate(frames):
            if frame.label == "0EV":
                return index
        return 0

    def is_continuous(self):
        return False


def program_for(trigger):
    """Pick the program named by the trigger settings."""
    trigger = trigger or {}
    if trigger.get("program") == "bracket":
        bracket = trigger.get("bracket") or {}
        return Bracket(frames=bracket.get("frames", 3),
                       step_ev=bracket.get("step_ev", 1.0))
    return SingleShot()


def make_group_id(when=None, token=None):
    """Identify the frames produced by one shutter press.

    Bracket siblings share this so the gallery can collapse them into one
    entry and delete them as a unit.
    """
    when = when or datetime.now(timezone.utc)
    token = token or uuid.uuid4().hex[:4]
    return f"{when:%Y%m%d-%H%M%S}-{token}"


def build_sidecar(recipe_id, recipe_name, program, group_id, frame_label,
                  resolved_controls, captured_at=None):
    """The provenance record written beside each photo."""
    captured_at = captured_at or datetime.now(timezone.utc)
    return {
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "program": program,
        "group_id": group_id,
        "frame_label": frame_label,
        "resolved_controls": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in (resolved_controls or {}).items()
        },
        "captured_at": captured_at.isoformat(),
    }
