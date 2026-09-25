"""In-memory simulated Modbus device: the generic PID temperature controller
described by ``examples/generic_pid_controller.yaml``.

It implements the same :class:`~labmcp_modbus.driver.ModbusClient` interface as
the pymodbus wrapper, so ``--simulate`` exercises the real decoding, limit
checks and audit trail. Behaviour follows the Modbus rules a real device
applies: unimplemented addresses answer exception 02 (ILLEGAL DATA ADDRESS),
values outside the device's own range answer exception 03 (ILLEGAL DATA VALUE),
and only the configured unit id answers.

Physics: a heater (3 °C/s at 100 % output) on a thermal mass that loses heat to
a 22 °C room with a 90 s time constant, driven by a PID loop with a ramping
working setpoint. Readings carry ~0.03 °C of noise. Extra points from a custom
register map get plain zero-initialised storage so the map can be tried out.
"""

from __future__ import annotations

import random
import struct
import time

from labmcp_modbus.driver import ModbusExceptionResponse
from labmcp_modbus.registers import RegisterMap, bytes_to_registers, registers_to_bytes

AMBIENT_C = 22.0
HEATER_C_PER_S = 3.0  # heating rate at 100 % output, ignoring losses
LOSS_TAU_S = 90.0
STEP_S = 0.1
MAX_CATCH_UP_S = 3600.0

# Layout of the example map (0-based addresses)
HR_SETPOINT, HR_MODE, HR_MANUAL, HR_RAMP, HR_PB, HR_TI, HR_TD, HR_ALARM = 0, 1, 2, 3, 4, 6, 8, 10
IR_PV, IR_OUT, IR_PV_FLOAT, IR_UPTIME, IR_WSP = 0, 1, 2, 4, 6
CO_ENABLE = 0
DI_ALARM, DI_FAULT, DI_AT_SP = 0, 1, 2


def _f32(value: float) -> list[int]:
    return bytes_to_registers(struct.pack(">f", value), "big", "big")


def _from_f32(regs: list[int]) -> float:
    return struct.unpack(">f", registers_to_bytes(regs, "big", "big"))[0]


def _s16(value: int) -> int:
    return value & 0xFFFF


def _signed(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


class SimulatedPIDController:
    """Fake Modbus client for one simulated PID controller at ``unit_id``."""

    def __init__(self, unit_id: int = 1, extra_map: RegisterMap | None = None, seed: int | None = 0) -> None:
        self.description = "sim://SimulatedPIDController"
        self.unit_id = unit_id
        self.rng = random.Random(seed)
        self.sensor_fault = False
        self._offset_s = 0.0
        self._t0 = self._clock()
        self._t_last = self._t0
        self.temperature_c = AMBIENT_C
        self.output_pct = 0.0
        self.working_setpoint_c = AMBIENT_C
        self._integral = 0.0
        self.holding: dict[int, int] = {
            HR_SETPOINT: 250,  # 25.0 °C
            HR_MODE: 0,  # standby
            HR_MANUAL: 0,
            HR_RAMP: 0,
            HR_ALARM: 1600,  # 160.0 °C
        }
        for addr, value in ((HR_PB, 10.0), (HR_TI, 120.0), (HR_TD, 0.0)):
            self.holding[addr], self.holding[addr + 1] = _f32(value)
        self.coils: dict[int, bool] = {CO_ENABLE: False}
        self.input: dict[int, int] = {}
        self.discrete: dict[int, bool] = {}
        self._extra: dict[str, dict[int, int | bool]] = {"holding": {}, "input": {}, "coil": {}, "discrete": {}}
        self._builtin = {
            "holding": set(range(0, 11)),
            "input": set(range(0, 7)),
            "coil": {CO_ENABLE},
            "discrete": {DI_ALARM, DI_FAULT, DI_AT_SP},
        }
        if extra_map is not None:
            for p in extra_map.points.values():
                for a in range(p.address, p.address + p.count):
                    if a not in self._builtin[p.table]:
                        self._extra[p.table][a] = False if p.is_bit else 0
        self._refresh_inputs()

    # ------------------------------------------------------------- time

    def _clock(self) -> float:
        return time.monotonic() + self._offset_s

    def advance(self, seconds: float) -> None:
        """Fast-forward simulated time (tests, demos)."""
        self._offset_s += seconds
        self._update()

    # ------------------------------------------------------------- physics

    def _update(self) -> None:
        now = self._clock()
        elapsed = min(now - self._t_last, MAX_CATCH_UP_S)
        self._t_last = now
        while elapsed > 1e-9:
            dt = min(STEP_S, elapsed)
            elapsed -= dt
            self._step(dt)
        self._refresh_inputs()

    def _step(self, dt: float) -> None:
        sp = _signed(self.holding[HR_SETPOINT]) / 10.0
        ramp = self.holding[HR_RAMP] / 10.0 / 60.0  # °C/s
        if ramp <= 0:
            self.working_setpoint_c = sp
        else:
            delta = sp - self.working_setpoint_c
            self.working_setpoint_c += max(-ramp * dt, min(ramp * dt, delta))
        mode = self.holding[HR_MODE]
        if not self.coils[CO_ENABLE] or mode == 0:
            out = 0.0
            self._integral = 0.0
        elif mode == 2:
            out = self.holding[HR_MANUAL] / 10.0
        else:
            pb = max(_from_f32([self.holding[HR_PB], self.holding[HR_PB + 1]]), 0.1)
            ti = _from_f32([self.holding[HR_TI], self.holding[HR_TI + 1]])
            error = self.working_setpoint_c - self.temperature_c
            p_term = 100.0 / pb * error
            out = p_term + self._integral
            if ti > 0 and 0.0 < out < 100.0:  # conditional integration (anti-windup)
                self._integral += 100.0 / pb * error * dt / ti
            out = p_term + self._integral
        self.output_pct = max(0.0, min(100.0, out))
        heating = self.output_pct / 100.0 * HEATER_C_PER_S
        self.temperature_c += (heating - (self.temperature_c - AMBIENT_C) / LOSS_TAU_S) * dt

    def _refresh_inputs(self) -> None:
        pv = self.temperature_c + self.rng.gauss(0, 0.03)
        self.input[IR_PV] = _s16(round(pv * 10))
        self.input[IR_OUT] = round(self.output_pct * 10)
        self.input[IR_PV_FLOAT], self.input[IR_PV_FLOAT + 1] = _f32(pv)
        uptime = int(self._clock() - self._t0) & 0xFFFFFFFF
        self.input[IR_UPTIME], self.input[IR_UPTIME + 1] = uptime >> 16, uptime & 0xFFFF
        self.input[IR_WSP] = _s16(round(self.working_setpoint_c * 10))
        self.discrete[DI_ALARM] = pv > _signed(self.holding[HR_ALARM]) / 10.0
        self.discrete[DI_FAULT] = self.sensor_fault
        self.discrete[DI_AT_SP] = abs(pv - self.working_setpoint_c) < 0.5

    # ------------------------------------------------------------- protocol helpers

    def _check_unit(self, unit: int, fn: str, address: int) -> None:
        if unit != self.unit_id:
            # A gateway answers 0B; a device on a serial bus stays silent. Model the TCP gateway case.
            raise ModbusExceptionResponse(fn, address, 0x0B)

    def _read(self, table: str, store: dict, address: int, count: int, unit: int, fn: str) -> list:
        self._check_unit(unit, fn, address)
        self._update()
        extra = self._extra[table]
        out = []
        for a in range(address, address + count):
            if a in store:
                out.append(store[a])
            elif a in extra:
                out.append(extra[a])
            else:
                raise ModbusExceptionResponse(fn, address, 0x02)
        return out

    # ------------------------------------------------------------- ModbusClient interface

    def read_holding_registers(self, address: int, count: int, unit: int) -> list[int]:
        return [int(v) for v in self._read("holding", self.holding, address, count, unit, "read_holding_registers")]

    def read_input_registers(self, address: int, count: int, unit: int) -> list[int]:
        return [int(v) for v in self._read("input", self.input, address, count, unit, "read_input_registers")]

    def read_coils(self, address: int, count: int, unit: int) -> list[bool]:
        return [bool(v) for v in self._read("coil", self.coils, address, count, unit, "read_coils")]

    def read_discrete_inputs(self, address: int, count: int, unit: int) -> list[bool]:
        return [bool(v) for v in self._read("discrete", self.discrete, address, count, unit, "read_discrete_inputs")]

    def write_register(self, address: int, value: int, unit: int) -> None:
        self.write_registers(address, [value], unit, fn="write_register")

    def write_registers(self, address: int, values: list[int], unit: int, fn: str = "write_registers") -> None:
        self._check_unit(unit, fn, address)
        self._update()
        pending = dict(self.holding)
        for i, value in enumerate(values):
            a = address + i
            if a in self._extra["holding"]:
                self._extra["holding"][a] = value
                continue
            if a not in pending:
                raise ModbusExceptionResponse(fn, address, 0x02)
            pending[a] = value
        # the device validates the new values before accepting them
        valid = (
            -500 <= _signed(pending[HR_SETPOINT]) <= 4000
            and pending[HR_MODE] in (0, 1, 2)
            and 0 <= pending[HR_MANUAL] <= 1000
            and 0 <= pending[HR_RAMP] <= 9999
            and -500 <= _signed(pending[HR_ALARM]) <= 4000
            and _from_f32([pending[HR_PB], pending[HR_PB + 1]]) > 0
            and _from_f32([pending[HR_TI], pending[HR_TI + 1]]) >= 0
            and _from_f32([pending[HR_TD], pending[HR_TD + 1]]) >= 0
        )
        if not valid:
            raise ModbusExceptionResponse(fn, address, 0x03)
        self.holding = pending

    def write_coil(self, address: int, value: bool, unit: int) -> None:
        self._check_unit(unit, "write_coil", address)
        self._update()
        if address in self._extra["coil"]:
            self._extra["coil"][address] = bool(value)
        elif address in self.coils:
            self.coils[address] = bool(value)
        else:
            raise ModbusExceptionResponse("write_coil", address, 0x02)

    def close(self) -> None:
        pass
