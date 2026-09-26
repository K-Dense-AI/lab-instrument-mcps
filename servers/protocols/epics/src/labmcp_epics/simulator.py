"""In-process EPICS IOC for ``--simulate`` and tests (caproto asyncio server in a thread).

It is a real Channel Access server, so the whole client path (UDP search, TCP circuit, DBR_TIME /
DBR_CTRL reads, access rights, subscriptions, put-callback) is exercised. To stay off the network it
binds only to 127.0.0.1 on free, randomly chosen ports and points the client at exactly that address
(EPICS_CA_AUTO_ADDR_LIST=NO), so it never broadcasts to or answers searches from other hosts.

PVs (default prefix ``SIM:``):

==================  ======================================================================
TEMP                sample temperature, °C, read-only, alarms LOLO 0 / LOW 5 / HIGH 60 / HIHI 80,
                    follows TEMP:SP with a first-order lag (tau 8 s) while HEATER is On
TEMP:SP             temperature setpoint, °C, control limits (DRVL/DRVH) 0 .. 100
HEATER              enum Off / On
MTR                 motor position setpoint, mm, control limits -10 .. 10; a put-callback completes
                    only when the move is done (velocity MTR:VELO)
MTR:RBV / MTR:DMOV  readback (mm) and done-moving flag, read-only
MTR:STOP            write 1 to stop the motor (auto-resets to 0)
MTR:VELO            velocity, mm/s, control limits 0.1 .. 5
DET:FRAMES          detector frames per acquisition, LONG, control limits 1 .. 1000
SAMPLE              sample name, string (39 characters max)
SPECTRUM            512-point waveform (Gaussian peak on a Poisson background), updates at 2 Hz
BEAM:CURRENT        storage-ring current, mA, read-only, slow decay
STATUS              "Idle" / "Moving", read-only string
==================  ======================================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import threading
import time
from typing import Any

import numpy as np


def _free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _free_port_pair() -> int:
    """A port number free for both UDP (search) and TCP (circuit) on localhost."""
    # Pick the TCP port first: Windows reserves whole TCP port ranges (Hyper-V/WinNAT)
    # that the UDP allocator doesn't avoid, so UDP-first could fail every attempt.
    for _ in range(200):
        port = _free_port(socket.SOCK_STREAM)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("Could not find a free local port for the simulated IOC")


def build_group(prefix: str) -> Any:
    """Create the simulated PVGroup (caproto imported lazily)."""
    from caproto import ChannelType
    from caproto.server import PVGroup, pvproperty

    rng = np.random.default_rng(0)

    class SimulatedBeamline(PVGroup):
        temperature = pvproperty(
            name="TEMP", value=22.0, units="degC", precision=2, read_only=True,
            lower_alarm_limit=0.0, lower_warning_limit=5.0, upper_warning_limit=60.0, upper_alarm_limit=80.0,
            lower_disp_limit=-20.0, upper_disp_limit=120.0, doc="Sample temperature",
        )
        setpoint = pvproperty(
            name="TEMP:SP", value=22.0, units="degC", precision=1,
            lower_ctrl_limit=0.0, upper_ctrl_limit=100.0, lower_disp_limit=0.0, upper_disp_limit=100.0,
        )
        heater = pvproperty(name="HEATER", value="Off", enum_strings=["Off", "On"], dtype=ChannelType.ENUM)
        motor = pvproperty(
            name="MTR", value=0.0, units="mm", precision=3, lower_ctrl_limit=-10.0, upper_ctrl_limit=10.0,
            lower_disp_limit=-10.0, upper_disp_limit=10.0,
        )
        motor_rbv = pvproperty(name="MTR:RBV", value=0.0, units="mm", precision=3, read_only=True)
        motor_dmov = pvproperty(name="MTR:DMOV", value=1, read_only=True)
        motor_stop = pvproperty(name="MTR:STOP", value=0)
        motor_velo = pvproperty(
            name="MTR:VELO", value=2.0, units="mm/s", precision=2, lower_ctrl_limit=0.1, upper_ctrl_limit=5.0
        )
        frames = pvproperty(name="DET:FRAMES", value=10, lower_ctrl_limit=1, upper_ctrl_limit=1000)
        sample = pvproperty(name="SAMPLE", value="empty", dtype=ChannelType.STRING)
        spectrum = pvproperty(name="SPECTRUM", value=[0.0] * 512, max_length=512, units="counts", read_only=True)
        beam = pvproperty(name="BEAM:CURRENT", value=350.0, units="mA", precision=2, read_only=True)
        status = pvproperty(name="STATUS", value="Idle", dtype=ChannelType.STRING, read_only=True)

        _stop_requested = False

        @temperature.scan(period=0.2)
        async def temperature(self, instance: Any, async_lib: Any) -> None:
            target = self.setpoint.value if self.heater.value == "On" else 22.0
            value = instance.value + (target - instance.value) * 0.2 / 8.0 + rng.normal(0, 0.01)
            await instance.write(value)

        @motor.putter
        async def motor(self, instance: Any, value: float) -> float:
            self._stop_requested = False
            await self.motor_dmov.write(0)
            await self.status.write("Moving")
            pos = float(self.motor_rbv.value)
            dt = 0.02
            while abs(value - pos) > 1e-9:
                if self._stop_requested:
                    value = pos
                    break
                step = min(float(self.motor_velo.value) * dt, abs(value - pos))
                pos += step if value > pos else -step
                await self.motor_rbv.write(pos)
                await asyncio.sleep(dt)
            await self.motor_dmov.write(1)
            await self.status.write("Idle")
            return value  # the put-callback completes here, when the move is done

        @motor_stop.putter
        async def motor_stop(self, instance: Any, value: int) -> int:
            if value:
                self._stop_requested = True
            return 0

        @spectrum.scan(period=0.5)
        async def spectrum(self, instance: Any, async_lib: Any) -> None:
            x = np.arange(512)
            peak = 800.0 * np.exp(-0.5 * ((x - 256 - 20 * np.sin(time.time() / 10)) / 12.0) ** 2)
            await instance.write((rng.poisson(50, 512) + peak).tolist())

        @beam.scan(period=1.0)
        async def beam(self, instance: Any, async_lib: Any) -> None:
            await instance.write(max(0.0, instance.value * 0.99999 + rng.normal(0, 0.005)))

    return SimulatedBeamline(prefix=prefix)


class SimulatedIOC:
    """Run the simulated PVGroup on 127.0.0.1 in a background thread."""

    def __init__(self, prefix: str = "SIM:") -> None:
        self.prefix = prefix
        self.port = _free_port_pair()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        # UDP sinks for the IOC's beacons and the client's repeater registration: nothing is sent
        # to a closed port (an ICMP "port unreachable" can make the next recvfrom() on the client's
        # search socket fail on some platforms, delaying PV searches).
        self._sinks = []
        for _ in range(2):
            sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sink.bind(("127.0.0.1", 0))
            self._sinks.append(sink)
        self.beacon_port = int(self._sinks[0].getsockname()[1])
        self.repeater_port = int(self._sinks[1].getsockname()[1])

    @property
    def client_env(self) -> dict[str, str]:
        return {
            "EPICS_CA_ADDR_LIST": f"127.0.0.1:{self.port}",
            "EPICS_CA_AUTO_ADDR_LIST": "NO",
            "EPICS_CA_SERVER_PORT": str(self.port),
            "EPICS_CA_REPEATER_PORT": str(self.repeater_port),
        }

    @property
    def server_env(self) -> dict[str, str]:
        return {
            "EPICS_CA_SERVER_PORT": str(self.port),
            "EPICS_CAS_INTF_ADDR_LIST": "127.0.0.1",
            "EPICS_CAS_BEACON_ADDR_LIST": "127.0.0.1",
            "EPICS_CAS_AUTO_BEACON_ADDR_LIST": "NO",
            "EPICS_CAS_BEACON_PORT": str(self.beacon_port),
        }

    def start(self, timeout: float = 10.0) -> SimulatedIOC:
        import caproto.asyncio.server as ca_server

        for name in ("caproto", "caproto.ctx", "caproto.circ", "caproto.bcast"):
            logging.getLogger(name).setLevel(logging.WARNING)
        group = build_group(self.prefix)
        saved = {k: os.environ.get(k) for k in self.server_env}

        async def serve() -> None:
            ctx = ca_server.Context(group.pvdb, interfaces=["127.0.0.1"])  # reads EPICS_CAS_* now

            async def ready(_async_lib: Any) -> None:
                self._ready.set()

            self._task = asyncio.current_task()
            await ctx.run(startup_hook=ready)

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(serve())
            except asyncio.CancelledError:
                pass
            except BaseException as exc:  # pragma: no cover - surfaced by start()
                self._error = exc
                self._ready.set()
            finally:
                with contextlib.suppress(Exception):
                    self._loop.close()

        os.environ.update(self.server_env)
        try:
            self._thread = threading.Thread(target=run, name="labmcp-sim-ioc", daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout):
                raise RuntimeError("Simulated IOC did not start")
        finally:
            for key, old in saved.items():  # the server has read its configuration by now
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
        if self._error is not None:
            raise RuntimeError(f"Simulated IOC failed: {self._error}")
        return self

    def stop(self) -> None:
        if self._loop is not None and self._task is not None and not self._loop.is_closed():
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread is not None:
            self._thread.join(timeout=5)
        for sink in self._sinks:
            with contextlib.suppress(OSError):
                sink.close()
