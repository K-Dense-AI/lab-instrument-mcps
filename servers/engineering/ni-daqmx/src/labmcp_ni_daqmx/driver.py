"""Driver for National Instruments DAQ devices through the official NI-DAQmx Python API.

Every operation is a short-lived DAQmx task (create channels, optionally configure the sample
clock, read or write, close), exactly as in NI's own examples, so no task is left reserving the
device between tool calls.

References:

* NI-DAQmx Python API (``nidaqmx``) documentation, https://nidaqmx-python.readthedocs.io/ and
  source https://github.com/ni/nidaqmx-python (v1.x): ``nidaqmx.Task``,
  ``task.ai_channels.add_ai_voltage_chan(terminal_config=, min_val=, max_val=)``,
  ``add_ai_thrmcpl_chan(thermocouple_type=, cjc_source=, cjc_val=)``,
  ``task.timing.cfg_samp_clk_timing(rate, sample_mode=, samps_per_chan=)``,
  ``task.read(number_of_samples_per_channel=, timeout=)``, ``ao_channels.add_ao_voltage_chan``,
  ``do_channels``/``di_channels.add_do_chan/add_di_chan(line_grouping=)``, ``task.write``,
  ``nidaqmx.system.System.local().devices`` and the ``Device`` properties.
* NI-DAQmx driver download (Windows/Linux), https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html
* USB-6008/6009 specifications (AO 0-5 V, software-timed), https://www.ni.com/docs/en-US/bundle/usb-6008-specs/page/specs.html
"""

from __future__ import annotations

import contextlib
import re
import threading
import warnings
from dataclasses import dataclass
from typing import Any, Literal

from labmcp import InstrumentConnectionError, InstrumentError, InstrumentProtocolError
from labmcp.audit import AuditLog

TerminalConfig = Literal["default", "rse", "nrse", "diff", "pseudo_diff"]
THERMOCOUPLE_TYPES = ("B", "E", "J", "K", "N", "R", "S", "T")

_CONNECTION_CODES = {-201003, -200220, -88705, -88709}
_HINTS = {
    -200170: "that physical channel does not exist on this device - call get_device_info for the channel list",
    -200077: "a requested value is not supported by this device",
    -200081: "the sample rate is too high for this number of channels",
    -200284: "the acquisition did not finish in time (timeout)",
    -200576: "this device has no built-in cold-junction sensor; use cjc_source='constant'",
    -201003: "the device cannot be accessed - is it plugged in and powered?",
    -200220: "the device name is not valid - check NI MAX",
    -50103: "the device is in use by another task or program (NI MAX test panel, LabVIEW, another script)",
}
_RANGE = re.compile(r"^(?P<prefix>.*?)(?P<start>\d+):(?P<stop>\d+)$")
_LOCAL_PREFIXES = ("ai", "ao", "port", "ctr", "pfi", "_")
#: Most channels one range such as ``ai0:31`` may expand to (a typo like ``ai0:1000000`` must not
#: build a million-entry list).
MAX_RANGE_CHANNELS = 1024


def load_nidaqmx() -> Any:
    """Import the official ``nidaqmx`` package (the NI-DAQmx C library is loaded on first use)."""
    try:
        import nidaqmx
        import nidaqmx.constants
        import nidaqmx.errors
        import nidaqmx.system
    except ImportError as exc:
        raise InstrumentConnectionError(
            "The `nidaqmx` Python package is not installed. Install it with `pip install nidaqmx`, "
            "or use --simulate to try the server without hardware."
        ) from exc
    return nidaqmx


def expand_channels(spec: str | list[str], device: str) -> list[str]:
    """Expand ``"ai0:3"``, ``"Dev1/ai0, Dev1/ai5"`` or ``["port0/line0:3"]`` into full physical
    channel names on ``device``. Refuses channels on other devices."""
    items = spec.split(",") if isinstance(spec, str) else [p for s in spec for p in s.split(",")]
    out: list[str] = []
    for raw in items:
        item = raw.strip()
        if not item:
            continue
        head, sep, rest = item.partition("/")
        if sep and head.lower() == device.lower():
            local = rest
        elif sep and not head.lower().startswith(_LOCAL_PREFIXES):
            raise InstrumentProtocolError(
                f"Channel {item!r} is on device {head!r}, but this server is connected to {device!r}."
            )
        else:
            local = item
        m = _RANGE.match(local)
        if m:
            start, stop = int(m["start"]), int(m["stop"])
            if abs(stop - start) >= MAX_RANGE_CHANNELS:
                raise InstrumentProtocolError(
                    f"Channel range {item!r} spans {abs(stop - start) + 1} channels; at most {MAX_RANGE_CHANNELS} are allowed."
                )
            step = 1 if stop >= start else -1
            out.extend(f"{device}/{m['prefix']}{i}" for i in range(start, stop + step, step))
        else:
            out.append(f"{device}/{local}")
    if not out:
        raise InstrumentProtocolError("No channels given.")
    if len({c.lower() for c in out}) != len(out):
        raise InstrumentProtocolError(f"Channel list contains duplicates: {out}")
    return out


def _pairs(flat: list[float] | None) -> list[tuple[float, float]]:
    flat = list(flat or [])
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


@dataclass
class Acquisition:
    channels: list[str]
    #: One list per channel (volts or °C).
    data: list[list[float]]
    #: Actual sample clock rate (None for a single on-demand sample).
    rate_hz: float | None
    warnings: list[str]


class NIDAQ:
    """One NI-DAQmx device. ``nidaqmx`` is the ``nidaqmx`` package (or the simulator)."""

    def __init__(
        self,
        nidaqmx: Any,
        device_name: str | None = None,
        audit: AuditLog | None = None,
        safe_do_lines: str = "",
    ) -> None:
        self.nidaqmx = nidaqmx
        self.audit = audit
        #: Serialises analog-input tasks (acquisitions can last minutes).
        self.lock = threading.RLock()
        #: Serialises AO / DO / DI tasks. Separate from ``lock`` so that `safe_state` never waits for a
        #: running acquisition: NI-DAQmx runs AI and AO/DIO tasks on the same device side by side.
        self.output_lock = threading.RLock()
        self.system = self._call("opening the NI-DAQmx system", nidaqmx.system.System.local)
        names = self.device_names()
        if not names:
            raise InstrumentConnectionError(
                "NI-DAQmx found no devices. Check the device appears in NI MAX (Windows) or "
                "`nilsdev` (Linux), or create a simulated device in NI MAX."
            )
        if device_name is None:
            if len(names) != 1:
                raise InstrumentConnectionError(
                    f"{len(names)} NI-DAQmx devices found ({', '.join(names)}); choose one with --address <name>."
                )
            device_name = names[0]
        match = [n for n in names if n.lower() == device_name.lower()]
        if not match:
            raise InstrumentConnectionError(
                f"NI-DAQmx device {device_name!r} not found. Available devices: {', '.join(names)}."
            )
        self.name = match[0]
        self.device = self._call("opening the device", lambda: self.system.devices[self.name])
        #: Last value this server wrote to each AO channel / DO line.
        self.ao_last: dict[str, float] = {}
        self.do_last: dict[str, bool] = {}
        self.safe_do_lines: list[str] = []
        if safe_do_lines.strip():
            try:  # validated now: a bad line would otherwise only fail inside safe_state
                self.safe_do_lines = self._expand(safe_do_lines, "do")
            except InstrumentProtocolError as exc:
                raise InstrumentConnectionError(f"--option safe_do_lines: {exc}") from exc

    # ------------------------------------------------------------ low level

    def _log(self, message: str) -> None:
        if self.audit is not None:
            self.audit.event(message, "nidaqmx")

    def _call(self, what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        errors = self.nidaqmx.errors
        try:
            return fn(*args, **kwargs)
        except errors.DaqNotFoundError as exc:
            raise InstrumentConnectionError(
                "The NI-DAQmx driver is not installed (the nidaqmx package could not find the NI-DAQmx "
                "library). Install NI-DAQmx from ni.com (Windows/Linux), or use --simulate."
            ) from exc
        except errors.DaqNotSupportedError as exc:
            raise InstrumentConnectionError(
                "NI-DAQmx is not supported on this operating system (Windows and Linux only)."
            ) from exc
        except errors.DaqError as exc:
            code = getattr(exc, "error_code", None)
            text = " ".join(str(exc).split())
            hint = _HINTS.get(code or 0)
            message = f"NI-DAQmx error {code} during {what}: {text}" + (f" ({hint})" if hint else "")
            self._log(f"error: {message}")
            if code in _CONNECTION_CODES:
                raise InstrumentConnectionError(message) from exc
            raise InstrumentProtocolError(message) from exc

    @contextlib.contextmanager
    def _task(self, what: str, lock: Any = None) -> Any:
        """A short-lived DAQmx task, closed even on errors; DAQmx warnings are collected."""
        with lock or self.lock, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            task = self._call("creating a task", self.nidaqmx.Task)
            try:
                yield task, caught
            finally:
                with contextlib.suppress(Exception):
                    task.close()

    @staticmethod
    def _warnings(caught: list[warnings.WarningMessage]) -> list[str]:
        return [" ".join(str(w.message).split()) for w in caught]

    def _get(self, obj: Any, attr: str, default: Any = None) -> Any:
        """Read a device property; some properties are unsupported on some devices."""
        try:
            return getattr(obj, attr)
        except (self.nidaqmx.errors.DaqError, AttributeError, NotImplementedError):
            return default

    def _names(self, collection_attr: str, device: Any | None = None) -> list[str]:
        collection = self._get(device or self.device, collection_attr)
        return list(self._get(collection, "channel_names", []) or []) if collection is not None else []

    # ------------------------------------------------------------ system / identity

    def device_names(self) -> list[str]:
        devices = self._call("listing devices", lambda: list(self.system.devices))
        return [d.name for d in devices]

    def driver_version(self) -> str:
        v = self._get(self.system, "driver_version")
        return f"{v.major_version}.{v.minor_version}.{v.update_version}" if v is not None else "unknown"

    def describe(self, device: Any | None = None, detail: bool = True) -> dict[str, Any]:
        d = device or self.device
        category = self._get(d, "product_category")
        serial = self._get(d, "serial_num")
        info: dict[str, Any] = {
            "name": d.name,
            "product_type": self._get(d, "product_type"),
            "product_category": getattr(category, "name", None),
            "serial_number": f"{serial:X}" if serial else None,
            "is_simulated": bool(self._get(d, "is_simulated", False)),
            "analog_inputs": self._names("ai_physical_chans", d),
            "analog_outputs": self._names("ao_physical_chans", d),
            "digital_lines": self._names("do_lines", d) or self._names("di_lines", d),
        }
        if detail:
            info.update(
                ai_voltage_ranges_v=[list(p) for p in _pairs(self._get(d, "ai_voltage_rngs"))],
                ao_voltage_ranges_v=[list(p) for p in _pairs(self._get(d, "ao_voltage_rngs"))],
                ai_max_single_channel_rate_hz=self._get(d, "ai_max_single_chan_rate"),
                ai_max_multi_channel_rate_hz=self._get(d, "ai_max_multi_chan_rate"),
                ai_simultaneous_sampling=self._get(d, "ai_simultaneous_sampling_supported"),
                di_lines=self._names("di_lines", d),
                do_lines=self._names("do_lines", d),
            )
        return info

    def list_devices(self) -> list[dict[str, Any]]:
        devices = self._call("listing devices", lambda: list(self.system.devices))
        return [self.describe(d, detail=False) for d in devices]

    def identify(self) -> dict[str, str]:
        info = self.describe(detail=False)
        return {
            "manufacturer": "National Instruments",
            "model": str(info["product_type"]),
            "serial": str(info["serial_number"]),
            "device": self.name,
            "nidaqmx_driver": self.driver_version(),
            "ni_max_simulated_device": str(info["is_simulated"]),
        }

    # ------------------------------------------------------------ validation

    def _expand(self, spec: str | list[str], kind: Literal["ai", "ao", "di", "do"]) -> list[str]:
        names = expand_channels(spec, self.name)
        attr = {"ai": "ai_physical_chans", "ao": "ao_physical_chans", "di": "di_lines", "do": "do_lines"}[kind]
        valid = {n.lower(): n for n in self._names(attr)}
        if valid:
            bad = [n for n in names if n.lower() not in valid]
            if bad:
                label = {"ai": "analog inputs", "ao": "analog outputs", "di": "digital input lines", "do": "digital output lines"}[kind]
                raise InstrumentProtocolError(
                    f"{bad[0]} is not one of the {label} of {self.name}. Call get_device_info for the channel list."
                )
            # The device's own spelling, so 'Port0/Line1' and 'port0/line1' are one key in do_last/ao_last.
            names = [valid[n.lower()] for n in names]
        return names

    @staticmethod
    def _per_channel(data: Any, n_channels: int, multi_sample: bool) -> list[list[float]]:
        """Normalise ``task.read()`` output (scalar / list / list of lists) to one list per channel."""
        if not multi_sample:
            values = data if isinstance(data, list) else [data]
            return [[float(v)] for v in values]
        if n_channels == 1:
            return [[float(v) for v in data]]
        return [[float(v) for v in row] for row in data]

    def _ao_range(self, voltage_v: float, channel: str) -> tuple[float, float]:
        ranges = _pairs(self._get(self.device, "ao_voltage_rngs"))
        for lo, hi in ranges:
            if lo <= voltage_v <= hi:
                return lo, hi
        if not ranges:
            return (-10.0, 10.0) if voltage_v < 0 else (0.0, 10.0)
        text = ", ".join(f"{lo:g} to {hi:g} V" for lo, hi in ranges)
        raise InstrumentProtocolError(
            f"{voltage_v:g} V is outside the analog output range of {channel} ({text}). Nothing was sent."
        )

    # ------------------------------------------------------------ analog input

    def read_voltage(
        self,
        channels: str | list[str],
        terminal_config: TerminalConfig = "default",
        min_v: float = -10.0,
        max_v: float = 10.0,
        samples: int = 1,
        rate_hz: float | None = None,
    ) -> Acquisition:
        """One on-demand sample (``samples=1``) or a finite, hardware-timed acquisition."""
        names = self._expand(channels, "ai")
        if min_v >= max_v:
            raise InstrumentProtocolError("min_v must be lower than max_v.")
        c = self.nidaqmx.constants
        term = getattr(c.TerminalConfiguration, terminal_config.upper())
        with self._task("analog input") as (task, caught):
            self._log(f"AI voltage {','.join(names)} {terminal_config} [{min_v:g}, {max_v:g}] V, {samples} samples @ {rate_hz}")
            self._call(
                "add_ai_voltage_chan",
                task.ai_channels.add_ai_voltage_chan,
                ",".join(names),
                terminal_config=term,
                min_val=min_v,
                max_val=max_v,
            )
            rate, data = self._acquire(task, samples, rate_hz)
            found = self._warnings(caught)
        return Acquisition(names, self._per_channel(data, len(names), samples > 1), rate, found)

    def read_thermocouple(
        self,
        channels: str | list[str],
        thermocouple_type: str = "K",
        cjc_source: Literal["built_in", "constant"] = "built_in",
        cjc_value_c: float = 25.0,
        min_c: float = 0.0,
        max_c: float = 100.0,
        samples: int = 1,
        rate_hz: float | None = None,
    ) -> Acquisition:
        """Thermocouple temperature in °C (devices/modules that support thermocouple channels)."""
        names = self._expand(channels, "ai")
        tc = thermocouple_type.upper()
        if tc not in THERMOCOUPLE_TYPES:
            raise InstrumentProtocolError(f"Thermocouple type must be one of {', '.join(THERMOCOUPLE_TYPES)}.")
        if min_c >= max_c:
            raise InstrumentProtocolError("min_c must be lower than max_c.")
        c = self.nidaqmx.constants
        source = c.CJCSource.BUILT_IN if cjc_source == "built_in" else c.CJCSource.CONSTANT_USER_VALUE
        with self._task("thermocouple") as (task, caught):
            self._log(f"AI thermocouple {','.join(names)} type {tc}, CJC {cjc_source}")
            self._call(
                "add_ai_thrmcpl_chan",
                task.ai_channels.add_ai_thrmcpl_chan,
                ",".join(names),
                min_val=min_c,
                max_val=max_c,
                units=c.TemperatureUnits.DEG_C,
                thermocouple_type=getattr(c.ThermocoupleType, tc),
                cjc_source=source,
                cjc_val=cjc_value_c,
            )
            rate, data = self._acquire(task, samples, rate_hz)
            found = self._warnings(caught)
        return Acquisition(names, self._per_channel(data, len(names), samples > 1), rate, found)

    def _acquire(self, task: Any, samples: int, rate_hz: float | None) -> tuple[float | None, Any]:
        if samples == 1:
            return None, self._call("read", task.read, timeout=10.0)
        if rate_hz is None:
            raise InstrumentProtocolError("A sample rate is needed for more than one sample.")
        c = self.nidaqmx.constants
        self._call(
            "cfg_samp_clk_timing",
            task.timing.cfg_samp_clk_timing,
            rate_hz,
            sample_mode=c.AcquisitionType.FINITE,
            samps_per_chan=samples,
        )
        actual = float(self._call("reading the sample clock rate", lambda: task.timing.samp_clk_rate))
        timeout = samples / actual + 10.0
        data = self._call("read", task.read, number_of_samples_per_channel=samples, timeout=timeout)
        return actual, data

    # ------------------------------------------------------------ digital I/O

    def read_lines(self, lines: str | list[str]) -> list[dict[str, Any]]:
        """Read digital lines. Lines this server is driving are reported from their last commanded
        value instead of being read, because a DI task would reconfigure them as inputs."""
        names = self._expand(lines, "di")
        driven = {n.lower(): n for n in self.do_last}
        to_read = [n for n in names if n.lower() not in driven]
        measured: dict[str, bool] = {}
        if to_read:
            c = self.nidaqmx.constants
            with self._task("digital input", self.output_lock) as (task, _caught):
                self._call(
                    "add_di_chan", task.di_channels.add_di_chan, ",".join(to_read), line_grouping=c.LineGrouping.CHAN_PER_LINE
                )
                data = self._call("read", task.read, timeout=10.0)
            values = data if isinstance(data, list) else [data]
            measured = {n: bool(v) for n, v in zip(to_read, values, strict=True)}
            self._log("DI " + ", ".join(f"{n}={int(v)}" for n, v in measured.items()))
        out = []
        for n in names:
            if n in measured:
                out.append({"line": n, "high": measured[n], "source": "measured"})
            else:
                out.append({"line": n, "high": self.do_last[driven[n.lower()]], "source": "last commanded (driven by this server)"})
        return out

    def write_lines(self, lines: str | list[str], levels: list[bool]) -> list[str]:
        names = self._expand(lines, "do")
        if len(levels) == 1:
            levels = levels * len(names)
        if len(levels) != len(names):
            raise InstrumentProtocolError(f"Got {len(levels)} levels for {len(names)} lines.")
        c = self.nidaqmx.constants
        self._log("DO " + ", ".join(f"{n}={int(v)}" for n, v in zip(names, levels, strict=True)))
        with self._task("digital output", self.output_lock) as (task, _caught):
            self._call("add_do_chan", task.do_channels.add_do_chan, ",".join(names), line_grouping=c.LineGrouping.CHAN_PER_LINE)
            self._call("write", task.write, levels[0] if len(names) == 1 else list(levels), auto_start=True, timeout=10.0)
        for n, v in zip(names, levels, strict=True):
            self.do_last[n] = bool(v)
        return names

    # ------------------------------------------------------------ analog output

    def write_voltage(self, channel: str, voltage_v: float) -> str:
        """Set one AO channel to a DC voltage (on-demand write). Most NI devices hold the value
        after the task ends."""
        names = self._expand(channel, "ao")
        if len(names) != 1:
            raise InstrumentProtocolError("Give exactly one analog output channel, e.g. 'ao0'.")
        name = names[0]
        lo, hi = self._ao_range(voltage_v, name)
        self._log(f"AO {name} = {voltage_v:g} V (range {lo:g}..{hi:g} V)")
        with self._task("analog output", self.output_lock) as (task, _caught):
            self._call("add_ao_voltage_chan", task.ao_channels.add_ao_voltage_chan, name, min_val=lo, max_val=hi)
            self._call("write", task.write, float(voltage_v), auto_start=True, timeout=10.0)
        self.ao_last[name] = float(voltage_v)
        return name

    # ------------------------------------------------------------ safety

    def safe_state(self, digital_low: bool = True) -> list[str]:
        """AO channels to 0 V (or the range minimum if 0 V is outside it) and, if ``digital_low``,
        every DO line this server drove plus the ``safe_do_lines`` option driven low."""
        done: list[str] = []
        errors: list[str] = []
        ranges = _pairs(self._get(self.device, "ao_voltage_rngs"))
        zero = 0.0
        if ranges and not any(lo <= 0.0 <= hi for lo, hi in ranges):
            zero = min(lo for lo, _ in ranges)
        aos = self._names("ao_physical_chans")
        aos += [n for n in self.ao_last if n.lower() not in {a.lower() for a in aos}]
        for ao in aos:
            try:
                self.write_voltage(ao, zero)
                done.append(f"{ao} set to {zero:g} V")
            except InstrumentError as exc:
                errors.append(str(exc))
        lines = sorted({n.lower(): n for n in [*self.do_last, *self.safe_do_lines]}.values())
        if digital_low and lines:
            try:
                self.write_lines(lines, [False])
                done.append("driven low: " + ", ".join(lines))
            except InstrumentError:
                # One bad line must not keep the others high: retry them one by one.
                for line in lines:
                    try:
                        self.write_lines([line], [False])
                        done.append(f"driven low: {line}")
                    except InstrumentError as exc:
                        errors.append(f"{line}: {exc}")
        if errors:
            raise InstrumentProtocolError(
                "Safe state only partly applied. Done: " + ("; ".join(done) or "nothing") + ". Failed: " + "; ".join(errors)
            )
        return done

    def close(self) -> None:
        """Nothing stays open between calls (every operation uses its own task)."""
