"""A fake ``nidaqmx`` package: a simulated X Series multifunction DAQ ("Dev1", USB-6341-like).

It implements the subset of the ``nidaqmx`` API that the driver uses - ``nidaqmx.Task`` with
``ai_channels.add_ai_voltage_chan`` / ``add_ai_thrmcpl_chan``, ``ao_channels``, ``di_channels``,
``do_channels``, ``timing.cfg_samp_clk_timing`` / ``samp_clk_rate``, ``read`` and ``write``;
``nidaqmx.system.System.local().devices`` with the ``Device`` properties; the constants enums
(same member names and values as ``nidaqmx.constants``) and ``nidaqmx.errors.DaqError`` with real
NI-DAQmx error codes. ``read()`` returns the same shapes as ``nidaqmx`` (scalar / list / list of
lists).

For a zero-hardware test of the *real* NI-DAQmx path, create a simulated device in NI MAX instead
and run the server without --simulate.

Simulated wiring: ai0 = 1 V, 50 Hz sine; ai1 = 2.5 V DC sensor; ai2 = loopback from ao0;
ai3 = loopback from ao1; other inputs grounded (noise). Thermocouple channels: a ~37 °C probe on
ai4, other inputs shorted (read the cold-junction temperature). port1/line0 is held high externally
(e.g. an interlock switch); other input lines read low (pull-downs).
"""

from __future__ import annotations

import math
import random
import threading
import time
from enum import Enum
from types import SimpleNamespace
from typing import Any


class TerminalConfiguration(Enum):
    RSE = 10083
    NRSE = 10078
    DIFF = 10106
    PSEUDO_DIFF = 12529
    DEFAULT = -1


class ThermocoupleType(Enum):
    J = 10072
    K = 10073
    N = 10077
    R = 10082
    S = 10085
    T = 10086
    B = 10047
    E = 10055


class CJCSource(Enum):
    BUILT_IN = 10200
    CONSTANT_USER_VALUE = 10116
    SCANNABLE_CHANNEL = 10113


class TemperatureUnits(Enum):
    DEG_C = 10143
    DEG_F = 10144
    K = 10325
    DEG_R = 10145


class AcquisitionType(Enum):
    FINITE = 10178
    CONTINUOUS = 10123
    HW_TIMED_SINGLE_POINT = 12522


class LineGrouping(Enum):
    CHAN_PER_LINE = 0
    CHAN_FOR_ALL_LINES = 1


class ProductCategory(Enum):
    M_SERIES_DAQ = 14643
    X_SERIES_DAQ = 15858
    USBDAQ = 14646
    C_SERIES_MODULE = 14659


class SimDaqError(Exception):
    """Same interface as ``nidaqmx.errors.DaqError`` (``error_code``, message with status code)."""

    def __init__(self, message: str, error_code: int, task_name: str = "") -> None:
        text = f"{message}\n\nStatus Code: {error_code}"
        if task_name:
            text = f"{message}\n\nTask Name: {task_name}\n\nStatus Code: {error_code}"
        super().__init__(text)
        self.error_code = error_code


class SimDaqNotFoundError(Exception):
    pass


class SimDaqNotSupportedError(Exception):
    pass


_UNSET = object()


class _SimHardware:
    """State of one simulated device."""

    def __init__(self, name: str, serial: int, seed: int | None) -> None:
        self.name = name
        self.serial = serial
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.ai = [f"{name}/ai{i}" for i in range(16)]
        self.ao = [f"{name}/ao{i}" for i in range(2)]
        self.lines = [f"{name}/port{p}/line{i}" for p in range(3) for i in range(8)]
        self.ai_ranges = [-10.0, 10.0, -5.0, 5.0, -1.0, 1.0, -0.2, 0.2]
        self.ao_ranges = [-10.0, 10.0]
        self.max_rate = 500_000.0
        self.ao_value = {n.lower(): 0.0 for n in self.ao}
        self.line_output: dict[str, bool] = {}  # lines currently driven (lower-case name -> level)
        self.external = {f"{name}/port1/line0".lower(): True}
        self.reserved = False

    def now(self) -> float:
        return time.monotonic() - self.t0

    def ai_voltage(self, channel: str, t: float) -> float:
        idx = int(channel.rsplit("ai", 1)[1])
        if idx == 0:
            v = 1.0 * math.sin(2 * math.pi * 50.0 * t)
        elif idx == 1:
            v = 2.5
        elif idx in (2, 3):
            v = self.ao_value[f"{self.name}/ao{idx - 2}".lower()] * 0.9998
        else:
            v = 0.0
        return v + self.rng.gauss(0.0, 150e-6)

    def tc_temperature(self, channel: str, cjc_c: float, t: float) -> float:
        if channel.lower().endswith("/ai4"):
            return 37.0 + 0.15 * math.sin(2 * math.pi * t / 90.0) + self.rng.gauss(0.0, 0.03)
        return cjc_c + self.rng.gauss(0.0, 0.05)

    def line_level(self, line: str) -> bool:
        key = line.lower()
        if key in self.line_output:
            return self.line_output[key]
        return self.external.get(key, False)


class _Collection:
    def __init__(self, names: list[str]) -> None:
        self.channel_names = list(names)


class _SimDevice:
    def __init__(self, hw: _SimHardware) -> None:
        self._hw = hw
        self.name = hw.name
        self.product_type = "USB-6341"
        self.product_category = ProductCategory.X_SERIES_DAQ
        self.serial_num = hw.serial
        self.is_simulated = False
        self.ai_physical_chans = _Collection(hw.ai)
        self.ao_physical_chans = _Collection(hw.ao)
        self.di_lines = _Collection(hw.lines)
        self.do_lines = _Collection(hw.lines)
        self.ai_voltage_rngs = list(hw.ai_ranges)
        self.ao_voltage_rngs = list(hw.ao_ranges)
        self.ai_max_single_chan_rate = hw.max_rate
        self.ai_max_multi_chan_rate = hw.max_rate
        self.ai_simultaneous_sampling_supported = False


class _SimDeviceCollection:
    def __init__(self, devices: list[_SimDevice]) -> None:
        self._devices = devices

    @property
    def device_names(self) -> list[str]:
        return [d.name for d in self._devices]

    def __iter__(self) -> Any:
        return iter(self._devices)

    def __len__(self) -> int:
        return len(self._devices)

    def __getitem__(self, index: int | str) -> _SimDevice:
        if isinstance(index, int):
            return self._devices[index]
        for d in self._devices:
            if d.name.lower() == index.lower():
                return d
        raise SimDaqError(f"Device identifier is invalid.\nDevice Specified: {index}", -200220)


class _SimSystem:
    def __init__(self, devices: list[_SimDevice]) -> None:
        self._devices = devices

    @property
    def devices(self) -> _SimDeviceCollection:
        return _SimDeviceCollection(self._devices)

    @property
    def driver_version(self) -> SimpleNamespace:
        return SimpleNamespace(major_version=24, minor_version=8, update_version=0)


def _split(names: str) -> list[str]:
    return [n.strip() for n in names.split(",") if n.strip()]


class _ChannelGroup:
    def __init__(self, task: _SimTask, kind: str) -> None:
        self._task = task
        self._kind = kind

    def _physical(self, names: str, attr: str) -> list[str]:
        items = _split(names)
        if not items:
            raise SimDaqError("Physical channel name is empty.", -200170, self._task.name)
        hw = self._task.bind(items[0].split("/", 1)[0])
        lookup = {v.lower(): v for v in getattr(hw, attr)}
        out = []
        for n in items:
            if n.lower() not in lookup:
                raise SimDaqError(
                    f"Physical channel specified does not exist on this device.\nPhysical Channel Name: {n}",
                    -200170,
                    self._task.name,
                )
            out.append(lookup[n.lower()])
        return out

    # analog input -------------------------------------------------------

    def add_ai_voltage_chan(self, physical_channel: str, name_to_assign_to_channel: str = "",
                            terminal_config: TerminalConfiguration = TerminalConfiguration.DEFAULT,
                            min_val: float = -5.0, max_val: float = 5.0, **_: Any) -> None:
        chans = self._physical(physical_channel, "ai")
        if terminal_config is TerminalConfiguration.PSEUDO_DIFF:
            raise SimDaqError("Requested value is not a supported value for this property.\nProperty: AI.TermCfg", -200077, self._task.name)
        if terminal_config is TerminalConfiguration.DIFF and any(int(c.rsplit("ai", 1)[1]) >= 8 for c in chans):
            raise SimDaqError("Terminal configuration DIFF is only supported on ai0-ai7 (ai8-ai15 are their negative inputs).", -200077, self._task.name)
        if min_val >= max_val:
            raise SimDaqError("Minimum is not less than maximum.", -200082, self._task.name)
        if min_val < -10.0 or max_val > 10.0:
            raise SimDaqError("Requested value is not a supported value for this property.\nProperty: AI.Max / AI.Min", -200077, self._task.name)
        self._task.add("ai_v", chans, {"min": min_val, "max": max_val})

    def add_ai_thrmcpl_chan(self, physical_channel: str, name_to_assign_to_channel: str = "",
                            min_val: float = 0.0, max_val: float = 100.0, units: TemperatureUnits = TemperatureUnits.DEG_C,
                            thermocouple_type: ThermocoupleType = ThermocoupleType.J,
                            cjc_source: CJCSource = CJCSource.CONSTANT_USER_VALUE, cjc_val: float = 25.0,
                            cjc_channel: str = "") -> None:
        chans = self._physical(physical_channel, "ai")
        if cjc_source is CJCSource.BUILT_IN:
            raise SimDaqError("Built-in cold-junction compensation (CJC) is not supported by this device.", -200576, self._task.name)
        if cjc_source is CJCSource.SCANNABLE_CHANNEL and not cjc_channel:
            raise SimDaqError("CJC channel name must be set when CJC source is scannable channel.", -201085, self._task.name)
        if min_val >= max_val:
            raise SimDaqError("Minimum is not less than maximum.", -200082, self._task.name)
        self._task.add("ai_tc", chans, {"cjc": cjc_val, "type": thermocouple_type})

    # analog output ------------------------------------------------------

    def add_ao_voltage_chan(self, physical_channel: str, name_to_assign_to_channel: str = "",
                            min_val: float = -10.0, max_val: float = 10.0, **_: Any) -> None:
        chans = self._physical(physical_channel, "ao")
        if min_val < -10.0 or max_val > 10.0 or min_val >= max_val:
            raise SimDaqError("Requested value is not a supported value for this property.\nProperty: AO.Max / AO.Min", -200077, self._task.name)
        self._task.add("ao", chans, {"min": min_val, "max": max_val})

    # digital ------------------------------------------------------------

    def add_di_chan(self, lines: str, name_to_assign_to_lines: str = "",
                    line_grouping: LineGrouping = LineGrouping.CHAN_FOR_ALL_LINES) -> None:
        self._task.add("di", self._physical(lines, "lines"), {"grouping": line_grouping})

    def add_do_chan(self, lines: str, name_to_assign_to_lines: str = "",
                    line_grouping: LineGrouping = LineGrouping.CHAN_FOR_ALL_LINES) -> None:
        self._task.add("do", self._physical(lines, "lines"), {"grouping": line_grouping})


class _SimTiming:
    def __init__(self, task: _SimTask) -> None:
        self._task = task
        self.rate: float | None = None
        self.samples: int | None = None

    def cfg_samp_clk_timing(self, rate: float, source: str = "", active_edge: Any = None,
                            sample_mode: AcquisitionType = AcquisitionType.FINITE, samps_per_chan: int = 1000) -> None:
        n = max(1, len(self._task.channels))
        if rate <= 0:
            raise SimDaqError("Requested value is not a supported value for this property.\nProperty: SampClk.Rate", -200077, self._task.name)
        if rate * n > self._task.hw.max_rate:
            raise SimDaqError(
                "Sample rate exceeds the maximum sample rate for the number of channels specified.",
                -200081,
                self._task.name,
            )
        if sample_mode is AcquisitionType.FINITE and samps_per_chan < 2:
            raise SimDaqError("Requested value is not a supported value for this property.\nProperty: SampQuant.SampPerChan", -200077, self._task.name)
        self.rate = 100e6 / round(100e6 / rate)  # 100 MHz timebase divisor
        self.samples = samps_per_chan

    @property
    def samp_clk_rate(self) -> float:
        return float(self.rate or 0.0)


class _SimTask:
    def __init__(self, sim: SimulatedNIDAQmx, name: str = "") -> None:
        self.sim = sim
        self.hw: _SimHardware = None  # type: ignore[assignment] - bound by the first channel
        self.name = name or f"_unnamedTask<{id(self):x}>"
        self.channels: list[str] = []
        self.kind: str | None = None
        self.params: dict[str, Any] = {}
        self.timing = _SimTiming(self)
        self.ai_channels = _ChannelGroup(self, "ai")
        self.ao_channels = _ChannelGroup(self, "ao")
        self.di_channels = _ChannelGroup(self, "di")
        self.do_channels = _ChannelGroup(self, "do")
        self.closed = False

    def bind(self, device: str) -> _SimHardware:
        """Attach the task to a device (like DAQmx, one task uses one device here) and reserve it."""
        hw = next((h for n, h in self.sim.hardware.items() if n.lower() == device.lower()), None)
        if hw is None:
            raise SimDaqError(f"Device identifier is invalid.\nDevice Specified: {device}", -200220, self.name)
        if self.hw is None:
            with hw.lock:
                if hw.reserved:
                    raise SimDaqError(
                        "The specified resource is reserved. The operation could not be completed as specified.", -50103, self.name
                    )
                hw.reserved = True
            self.hw = hw
        elif hw is not self.hw:
            raise SimDaqError("This simulator does not combine channels from several devices in one task.", -200559, self.name)
        return hw

    def add(self, kind: str, chans: list[str], params: dict[str, Any]) -> None:
        if self.kind not in (None, kind):
            raise SimDaqError("Channels of different types cannot be combined in one task.", -200559, self.name)
        self.kind = kind
        self.channels.extend(chans)
        self.params = params

    def __enter__(self) -> _SimTask:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.hw is not None:
                with self.hw.lock:
                    self.hw.reserved = False

    def read(self, number_of_samples_per_channel: Any = _UNSET, timeout: float = 10.0) -> Any:
        hw = self.hw
        multi = number_of_samples_per_channel is not _UNSET
        if self.kind == "di":
            levels = []
            for line in self.channels:
                hw.line_output.pop(line.lower(), None)  # a DI task makes the line an input
                levels.append(hw.line_level(line))
            return levels[0] if len(levels) == 1 else levels
        if self.kind not in ("ai_v", "ai_tc"):
            raise SimDaqError("Read cannot be performed because this task has no input channels.", -200478, self.name)
        rate = self.timing.rate
        n = int(number_of_samples_per_channel) if multi else 1
        if multi and rate is None:
            rate = 1000.0  # on-demand reads of several samples
        if rate and multi:
            needed = n / rate
            if needed > timeout:
                time.sleep(timeout)
                raise SimDaqError("Some or all of the samples requested have not yet been acquired.", -200284, self.name)
            time.sleep(needed)  # the acquisition takes real time
        t0 = hw.now() - (n / rate if rate and multi else 0.0)
        rows = []
        for ch in self.channels:
            if self.kind == "ai_v":
                lo, hi = self.params["min"], self.params["max"]
                limit = max(abs(lo), abs(hi))
                rng = min((r for r in (10.0, 5.0, 1.0, 0.2) if r >= limit), default=10.0) * 1.05
                values = [min(max(hw.ai_voltage(ch, t0 + k / (rate or 1.0)), -rng), rng) for k in range(n)]
            else:
                values = [hw.tc_temperature(ch, self.params["cjc"], t0 + k / (rate or 1.0)) for k in range(n)]
            rows.append(values)
        if not multi:
            scalars = [r[0] for r in rows]
            return scalars[0] if len(scalars) == 1 else scalars
        return rows[0] if len(rows) == 1 else rows

    def write(self, data: Any, auto_start: Any = _UNSET, timeout: float = 10.0) -> int:
        hw = self.hw
        values = data if isinstance(data, list) else [data]
        if len(values) != len(self.channels):
            raise SimDaqError("Write data does not match the number of channels in the task.", -200524, self.name)
        if self.kind == "ao":
            for ch, v in zip(self.channels, values, strict=True):
                if not self.params["min"] <= float(v) <= self.params["max"]:
                    raise SimDaqError("Value passed to the Task/Channels In control is outside the output range.", -200561, self.name)
                hw.ao_value[ch.lower()] = float(v)
        elif self.kind == "do":
            for ch, v in zip(self.channels, values, strict=True):
                hw.line_output[ch.lower()] = bool(v)
        else:
            raise SimDaqError("Write cannot be performed because this task has no output channels.", -200477, self.name)
        return 1


class SimulatedNIDAQmx:
    """Stand-in for the ``nidaqmx`` package with simulated devices attached."""

    def __init__(self, devices: tuple[str, ...] = ("Dev1",), seed: int | None = 0) -> None:
        self.hardware = {name: _SimHardware(name, 0x1F2E3D4C + i, None if seed is None else seed + i) for i, name in enumerate(devices)}
        self._devices = [_SimDevice(hw) for hw in self.hardware.values()]
        self.constants = SimpleNamespace(
            TerminalConfiguration=TerminalConfiguration,
            ThermocoupleType=ThermocoupleType,
            CJCSource=CJCSource,
            TemperatureUnits=TemperatureUnits,
            AcquisitionType=AcquisitionType,
            LineGrouping=LineGrouping,
            ProductCategory=ProductCategory,
        )
        self.errors = SimpleNamespace(
            DaqError=SimDaqError, DaqNotFoundError=SimDaqNotFoundError, DaqNotSupportedError=SimDaqNotSupportedError
        )
        self.system = SimpleNamespace(System=SimpleNamespace(local=lambda: _SimSystem(self._devices)))

    def Task(self, new_task_name: str = "") -> _SimTask:  # noqa: N802 - mirrors nidaqmx.Task
        return _SimTask(self, new_task_name)
