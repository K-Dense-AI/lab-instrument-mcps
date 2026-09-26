"""Configurable safety limits.

Each server declares the physical quantities an agent could push too far (a
hotplate setpoint, a power-supply voltage, a pump flow rate). Limits are
checked *before* a command is sent to the instrument, and the scientist can
tighten or relax them at launch with ``--limit name=value`` or the
``LABMCP_LIMITS`` environment variable, e.g.::

    labmcp-ika-stirrer --limit max_temperature_c=80 --limit max_speed_rpm=600
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from labmcp.errors import SafetyLimitError


@dataclass(frozen=True)
class Limit:
    """A named, upper (or lower) bound on a physical quantity.

    Args:
        name: Identifier used on the command line, e.g. ``max_temperature_c``.
        default: Value used when the scientist does not override it.
        unit: Unit shown in messages, e.g. ``"°C"``.
        description: What the limit protects.
        kind: ``"max"`` (value must be <= limit) or ``"min"`` (value must be >= limit).
    """

    name: str
    default: float
    unit: str = ""
    description: str = ""
    kind: str = "max"

    def __post_init__(self) -> None:
        # Anything but "max" used to be treated as "min", so a typo ("Max", "maximum") silently
        # inverted the limit: it refused safe values and let dangerous ones through.
        if self.kind not in ("max", "min"):
            raise ValueError(f"Limit {self.name!r}: kind must be 'max' or 'min', got {self.kind!r}")
        if not math.isfinite(float(self.default)):
            raise ValueError(f"Limit {self.name!r}: default must be a finite number, got {self.default!r}")


class SafetyLimits:
    def __init__(self, limits: list[Limit] | tuple[Limit, ...] = ()) -> None:
        self._defs = {lim.name: lim for lim in limits}
        self._values = {lim.name: float(lim.default) for lim in limits}

    def override(self, overrides: dict[str, float]) -> None:
        for name, value in overrides.items():
            if name not in self._defs:
                known = ", ".join(sorted(self._defs)) or "(none)"
                raise ValueError(f"Unknown safety limit {name!r}. Known limits: {known}")
            value = float(value)
            if math.isnan(value):  # NaN compares False with everything and would disable the limit
                raise ValueError(f"Safety limit {name!r} must be a number, got NaN")
            if math.isinf(value):  # also not representable in JSON (get_connection_info would show null)
                raise ValueError(
                    f"Safety limit {name!r} must be a finite number, got {value}. "
                    "To relax it, give the largest value you are prepared to allow."
                )
            self._values[name] = value

    def __getitem__(self, name: str) -> float:
        return self._values[name]

    def check(self, name: str, value: float, what: str | None = None) -> float:
        """Raise :class:`SafetyLimitError` if ``value`` violates limit ``name``; else return it."""
        lim = self._defs[name]
        bound = self._values[name]
        if math.isnan(value):  # NaN > bound and NaN < bound are both False: never let it through
            raise SafetyLimitError(
                f"Refused: {what or lim.description or name} is not a number (NaN) (safety limit `{name}`). "
                "Nothing was sent to the instrument."
            )
        if math.isinf(value):  # -inf passes a max check (and +inf a min check), but is never a real setpoint
            raise SafetyLimitError(
                f"Refused: {what or lim.description or name} of {value} is not a finite number "
                f"(safety limit `{name}`). Nothing was sent to the instrument."
            )
        bad = value > bound if lim.kind == "max" else value < bound
        if bad:
            rel = "exceeds the maximum" if lim.kind == "max" else "is below the minimum"
            label = what or lim.description or name
            raise SafetyLimitError(
                f"Refused: {label} of {value:g} {lim.unit} {rel} allowed value of {bound:g} "
                f"{lim.unit} (safety limit `{name}`). Nothing was sent to the instrument. "
                f"If this is intentional, restart the server with `--limit {name}=<value>`."
            )
        return value

    def as_dict(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                "value": self._values[name],
                "unit": lim.unit,
                "kind": lim.kind,
                "description": lim.description,
                "default": lim.default,
            }
            for name, lim in self._defs.items()
        }


def parse_limit_args(items: list[str] | None) -> dict[str, float]:
    """Parse ``["max_temperature_c=80", "max_speed_rpm=600"]`` (or a comma-joined string)."""
    out: dict[str, float] = {}
    for item in items or []:
        for part in item.split(","):
            part = part.strip()
            if not part:
                continue
            name, sep, value = part.partition("=")
            if not sep:
                raise ValueError(f"Limit must look like name=value, got {part!r}")
            try:
                out[name.strip()] = float(value)
            except ValueError:
                raise ValueError(
                    f"Limit {name.strip()!r} must be a plain number in the limit's unit (e.g. 80, not 80C), "
                    f"got {value.strip()!r}"
                ) from None
    return out
