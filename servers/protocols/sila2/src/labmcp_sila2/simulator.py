"""A real, in-process SiLA 2 server used by ``--simulate`` and the tests.

It is built with the ``sila2`` library's server classes directly (no code generator): a hand-written
Feature Definition (FDL, validated by sila2 against the official FeatureDefinition.xsd) plus a
``FeatureImplementationBase`` subclass whose method names follow sila2's servicer conventions
(``get_<Property>``, ``<Property>_on_subscription``, ``<Command>(..., metadata, [instance])``).
The server listens on 127.0.0.1 only, on a free port, unencrypted, with SiLA Server Discovery
(mDNS) disabled, so it never touches the network.

Features:

* ``org.labmcp/simulation/TemperatureController/v1``: a simulated Peltier block, -20..120 °C.
  - ``CurrentTemperature`` (observable property, °C), ``TargetTemperature``, ``RampRate`` (K/s),
    ``DeviceState`` (Set: Idle / Controlling / RunningProgram / Off).
  - ``ControlTemperature(TargetTemperature)``: observable; ramps at RampRate, intermediate response
    CurrentTemperature, response FinalTemperature. Defined error ``DeviceBusy``.
  - ``RunProgram(Steps: List[ProgramStep{TargetTemperature, HoldTime}])``: observable, 1-10 steps.
  - ``SetRampRate(RampRate)``: unobservable, 0 < rate <= 10 K/s.
  - ``SwitchOff()``: unobservable, stops control and holds nothing.
* ``org.silastandard/core.commands/CancelController/v1``: the official SiLA feature (FDL from
  gitlab.com/SiLA2/sila_base, MIT licence), cancelling running observable commands.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import timedelta
from queue import Queue
from typing import Any
from uuid import UUID

TEMPERATURE_CONTROLLER_FDL = """<?xml version="1.0" encoding="utf-8" ?>
<Feature SiLA2Version="1.0" FeatureVersion="1.0" MaturityLevel="Draft" Originator="org.labmcp"
         Category="simulation" xmlns="http://www.sila-standard.org"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://www.sila-standard.org https://gitlab.com/SiLA2/sila_base/raw/master/schema/FeatureDefinition.xsd">
  <Identifier>TemperatureController</Identifier>
  <DisplayName>Temperature Controller</DisplayName>
  <Description>Simulated Peltier temperature block (LabMCP). Controls a sample block between -20 and 120 degrees Celsius.</Description>
  <Command>
    <Identifier>ControlTemperature</Identifier>
    <DisplayName>Control Temperature</DisplayName>
    <Description>Ramp the block to the target temperature at the configured ramp rate and finish when it is within 0.1 degC.</Description>
    <Observable>Yes</Observable>
    <Parameter>
      <Identifier>TargetTemperature</Identifier>
      <DisplayName>Target Temperature</DisplayName>
      <Description>The target temperature.</Description>
      <DataType><DataTypeIdentifier>Celsius</DataTypeIdentifier></DataType>
    </Parameter>
    <Response>
      <Identifier>FinalTemperature</Identifier>
      <DisplayName>Final Temperature</DisplayName>
      <Description>Block temperature when the command finished.</Description>
      <DataType><Basic>Real</Basic></DataType>
    </Response>
    <IntermediateResponse>
      <Identifier>CurrentTemperature</Identifier>
      <DisplayName>Current Temperature</DisplayName>
      <Description>Block temperature during the ramp.</Description>
      <DataType><Basic>Real</Basic></DataType>
    </IntermediateResponse>
    <DefinedExecutionErrors><Identifier>DeviceBusy</Identifier></DefinedExecutionErrors>
  </Command>
  <Command>
    <Identifier>RunProgram</Identifier>
    <DisplayName>Run Program</DisplayName>
    <Description>Run a temperature program: go to each step's temperature and hold it for the step's hold time.</Description>
    <Observable>Yes</Observable>
    <Parameter>
      <Identifier>Steps</Identifier>
      <DisplayName>Steps</DisplayName>
      <Description>Program steps, executed in order (1-10 steps).</Description>
      <DataType>
        <List>
          <DataType><DataTypeIdentifier>ProgramStep</DataTypeIdentifier></DataType>
        </List>
      </DataType>
    </Parameter>
    <Response>
      <Identifier>StepsCompleted</Identifier>
      <DisplayName>Steps Completed</DisplayName>
      <Description>Number of steps completed.</Description>
      <DataType><Basic>Integer</Basic></DataType>
    </Response>
    <DefinedExecutionErrors><Identifier>DeviceBusy</Identifier></DefinedExecutionErrors>
  </Command>
  <Command>
    <Identifier>SetRampRate</Identifier>
    <DisplayName>Set Ramp Rate</DisplayName>
    <Description>Set the heating/cooling ramp rate.</Description>
    <Observable>No</Observable>
    <Parameter>
      <Identifier>RampRate</Identifier>
      <DisplayName>Ramp Rate</DisplayName>
      <Description>Ramp rate in kelvin per second.</Description>
      <DataType>
        <Constrained>
          <DataType><Basic>Real</Basic></DataType>
          <Constraints>
            <MinimalExclusive>0</MinimalExclusive>
            <MaximalInclusive>10</MaximalInclusive>
            <Unit>
              <Label>K/s</Label><Factor>1</Factor><Offset>0</Offset>
              <UnitComponent><SIUnit>Kelvin</SIUnit><Exponent>1</Exponent></UnitComponent>
              <UnitComponent><SIUnit>Second</SIUnit><Exponent>-1</Exponent></UnitComponent>
            </Unit>
          </Constraints>
        </Constrained>
      </DataType>
    </Parameter>
    <Response>
      <Identifier>PreviousRampRate</Identifier>
      <DisplayName>Previous Ramp Rate</DisplayName>
      <Description>The ramp rate before this call, K/s.</Description>
      <DataType><Basic>Real</Basic></DataType>
    </Response>
  </Command>
  <Command>
    <Identifier>SwitchOff</Identifier>
    <DisplayName>Switch Off</DisplayName>
    <Description>Stop temperature control; the block drifts back to ambient (22 degC).</Description>
    <Observable>No</Observable>
  </Command>
  <Property>
    <Identifier>CurrentTemperature</Identifier>
    <DisplayName>Current Temperature</DisplayName>
    <Description>Measured block temperature.</Description>
    <Observable>Yes</Observable>
    <DataType><DataTypeIdentifier>Celsius</DataTypeIdentifier></DataType>
  </Property>
  <Property>
    <Identifier>TargetTemperature</Identifier>
    <DisplayName>Target Temperature</DisplayName>
    <Description>Current control target (ambient when switched off).</Description>
    <Observable>No</Observable>
    <DataType><DataTypeIdentifier>Celsius</DataTypeIdentifier></DataType>
  </Property>
  <Property>
    <Identifier>RampRate</Identifier>
    <DisplayName>Ramp Rate</DisplayName>
    <Description>Heating/cooling ramp rate in K/s.</Description>
    <Observable>No</Observable>
    <DataType><Basic>Real</Basic></DataType>
  </Property>
  <Property>
    <Identifier>DeviceState</Identifier>
    <DisplayName>Device State</DisplayName>
    <Description>What the controller is doing.</Description>
    <Observable>No</Observable>
    <DataType>
      <Constrained>
        <DataType><Basic>String</Basic></DataType>
        <Constraints>
          <Set><Value>Idle</Value><Value>Controlling</Value><Value>RunningProgram</Value><Value>Off</Value></Set>
        </Constraints>
      </Constrained>
    </DataType>
  </Property>
  <DefinedExecutionError>
    <Identifier>DeviceBusy</Identifier>
    <DisplayName>Device Busy</DisplayName>
    <Description>Another temperature command is already running. Cancel it first.</Description>
  </DefinedExecutionError>
  <DataTypeDefinition>
    <Identifier>ProgramStep</Identifier>
    <DisplayName>Program Step</DisplayName>
    <Description>One step of a temperature program.</Description>
    <DataType>
      <Structure>
        <Element>
          <Identifier>TargetTemperature</Identifier>
          <DisplayName>Target Temperature</DisplayName>
          <Description>Step temperature.</Description>
          <DataType><DataTypeIdentifier>Celsius</DataTypeIdentifier></DataType>
        </Element>
        <Element>
          <Identifier>HoldTime</Identifier>
          <DisplayName>Hold Time</DisplayName>
          <Description>Seconds to hold the step temperature.</Description>
          <DataType>
            <Constrained>
              <DataType><Basic>Integer</Basic></DataType>
              <Constraints>
                <MinimalInclusive>0</MinimalInclusive>
                <MaximalInclusive>3600</MaximalInclusive>
                <Unit>
                  <Label>s</Label><Factor>1</Factor><Offset>0</Offset>
                  <UnitComponent><SIUnit>Second</SIUnit><Exponent>1</Exponent></UnitComponent>
                </Unit>
              </Constraints>
            </Constrained>
          </DataType>
        </Element>
      </Structure>
    </DataType>
  </DataTypeDefinition>
  <DataTypeDefinition>
    <Identifier>Celsius</Identifier>
    <DisplayName>Celsius</DisplayName>
    <Description>A temperature in degrees Celsius within the block's range.</Description>
    <DataType>
      <Constrained>
        <DataType><Basic>Real</Basic></DataType>
        <Constraints>
          <MaximalInclusive>120</MaximalInclusive>
          <MinimalInclusive>-20</MinimalInclusive>
          <Unit>
            <Label>degC</Label><Factor>1</Factor><Offset>273.15</Offset>
            <UnitComponent><SIUnit>Kelvin</SIUnit><Exponent>1</Exponent></UnitComponent>
          </Unit>
        </Constraints>
      </Constrained>
    </DataType>
  </DataTypeDefinition>
</Feature>
"""

# Official SiLA 2 feature, verbatim from sila_base (feature_definitions/org/silastandard/core/commands/
# CancelController-v1_0.sila.xml), whitespace condensed.
CANCEL_CONTROLLER_FDL = """<?xml version="1.0" encoding="utf-8" ?>
<Feature SiLA2Version="1.0" FeatureVersion="1.0" MaturityLevel="Verified" Originator="org.silastandard"
         Category="core.commands" xmlns="http://www.sila-standard.org"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://www.sila-standard.org https://gitlab.com/SiLA2/sila_base/raw/master/schema/FeatureDefinition.xsd">
  <Identifier>CancelController</Identifier>
  <DisplayName>Cancel Controller</DisplayName>
  <Description>This feature offers commands to cancel/terminate Commands. Cancellation is the act of stopping the running Command execution(s), irrevocably. The SiLA Server SHOULD be able to be in a state where any further commands can be issued after a cancellation.</Description>
  <Command>
    <Identifier>CancelCommand</Identifier>
    <DisplayName>Cancel Command</DisplayName>
    <Description>Cancel a specified currently running Observable Command or cancel all currently running Observable Commands . For any canceled Observable Command the SiLA Server MUST update the Command Execution Status to "Command Finished with Error". The SiLA Server MUST throw a descriptive error message indicating cancellation as the reason for the Command execution not being able to finish successfully for any canceled Command.</Description>
    <Observable>No</Observable>
    <Parameter>
      <Identifier>CommandExecutionUUID</Identifier>
      <DisplayName>Command Execution UUID</DisplayName>
      <Description>The Command Execution UUID according to the SiLA Standard.</Description>
      <DataType><DataTypeIdentifier>UUID</DataTypeIdentifier></DataType>
    </Parameter>
    <DefinedExecutionErrors>
      <Identifier>InvalidCommandExecutionUUID</Identifier>
      <Identifier>OperationNotSupported</Identifier>
    </DefinedExecutionErrors>
  </Command>
  <Command>
    <Identifier>CancelAll</Identifier>
    <DisplayName>Cancel All</DisplayName>
    <Description>Cancels all currently running Observable and Unobservable Commands running on this SiLA Server. The SiLA Server MUST throw an Execution Error indicating 'cancellation' as the reason for the Command not being able to finish successfully.</Description>
    <Observable>No</Observable>
  </Command>
  <DataTypeDefinition>
    <Identifier>UUID</Identifier>
    <DisplayName>UUID</DisplayName>
    <Description>A Universally Unique Identifier (UUID) referring to observable command executions.</Description>
    <DataType>
      <Constrained>
        <DataType><Basic>String</Basic></DataType>
        <Constraints>
          <Length>36</Length>
          <Pattern>[0-9a-f]{8}\\-[0-9a-f]{4}\\-[0-9a-f]{4}\\-[0-9a-f]{4}\\-[0-9a-f]{12}</Pattern>
        </Constraints>
      </Constrained>
    </DataType>
  </DataTypeDefinition>
  <DefinedExecutionError>
    <Identifier>InvalidCommandExecutionUUID</Identifier>
    <DisplayName>Invalid Command Execution UUID</DisplayName>
    <Description>The given Command Execution UUID does not specify a command that is currently being executed.</Description>
  </DefinedExecutionError>
  <DefinedExecutionError>
    <Identifier>OperationNotSupported</Identifier>
    <DisplayName>Operation Not Supported</DisplayName>
    <Description>Canceling is not supported for the SiLA 2 Command with the specified CommandExecutionUUID.</Description>
  </DefinedExecutionError>
</Feature>
"""

AMBIENT_C = 22.0
SIM_SERVER_UUID = "5d6b0d1e-6f3c-4c8a-9a51-1a2b3c4d5e6f"


class CancelledByClient(Exception):
    """Raised inside a running command when a client cancels it (reported as an execution error)."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Block:
    """Thermal model shared by the feature implementations: first-order approach to the setpoint,
    limited to the ramp rate, plus a little sensor noise."""

    def __init__(self) -> None:
        import random

        self.rng = random.Random(0)
        self.temperature = AMBIENT_C
        self.target = AMBIENT_C
        self.ramp_rate = 2.0
        self.controlling = False
        self.state = "Idle"
        self.lock = threading.RLock()
        self.running: dict[UUID, threading.Event] = {}  # execution uuid -> cancel flag
        self._last = time.monotonic()

    def step(self) -> float:
        with self.lock:
            now = time.monotonic()
            dt, self._last = now - self._last, now
            goal = self.target if self.controlling else AMBIENT_C
            rate = self.ramp_rate if self.controlling else 0.2
            delta = goal - self.temperature
            self.temperature += max(-rate * dt, min(rate * dt, delta))
            return self.temperature + self.rng.gauss(0, 0.01)


def build_server(port: int | None = None) -> tuple[Any, int]:
    """Create (but do not start) the simulated SiLA server. Returns (server, port)."""
    from sila2.framework import DefinedExecutionError, Feature
    from sila2.server import FeatureImplementationBase, SilaServer

    temp_feature = Feature(TEMPERATURE_CONTROLLER_FDL)
    cancel_feature = Feature(CANCEL_CONTROLLER_FDL)
    block = _Block()

    def busy_error() -> Exception:
        return DefinedExecutionError(
            temp_feature.defined_execution_errors["DeviceBusy"],
            "Another temperature command is already running; cancel it first.",
        )

    class TemperatureControllerImpl(FeatureImplementationBase):
        def __init__(self, parent_server: Any) -> None:
            super().__init__(parent_server=parent_server)
            self._CurrentTemperature_producer_queue: Queue[Any] = Queue()
            self.ControlTemperature_default_lifetime_of_execution = timedelta(minutes=30)
            self.RunProgram_default_lifetime_of_execution = timedelta(minutes=30)
            self.run_periodically(self._publish, delay_seconds=0.2)

        def _publish(self) -> None:
            self._CurrentTemperature_producer_queue.put(round(block.step(), 3))

        # properties
        def CurrentTemperature_on_subscription(self, *, metadata: Any) -> None:
            return None  # use the default queue

        def get_TargetTemperature(self, *, metadata: Any) -> float:
            return block.target if block.controlling else AMBIENT_C

        def get_RampRate(self, *, metadata: Any) -> float:
            return block.ramp_rate

        def get_DeviceState(self, *, metadata: Any) -> str:
            return block.state

        # unobservable commands
        # (sila2 convention: a single response is returned as the bare value, none as None)
        def SetRampRate(self, RampRate: float, *, metadata: Any) -> float:
            with block.lock:
                previous, block.ramp_rate = block.ramp_rate, float(RampRate)
            return previous

        def SwitchOff(self, *, metadata: Any) -> None:
            with block.lock:
                for flag in block.running.values():
                    flag.set()
                block.controlling = False
                block.state = "Off"

        # observable commands
        def _start(self, instance: Any, state: str) -> threading.Event:
            with block.lock:
                if block.running:
                    raise busy_error()
                flag = threading.Event()
                block.running[instance.execution_uuid] = flag
                block.state = state
                block.controlling = True
            instance.begin_execution()
            return flag

        def _finish(self, instance: Any) -> None:
            with block.lock:
                block.running.pop(instance.execution_uuid, None)
                if block.state != "Off":
                    block.state = "Idle"

        def _ramp_to(self, target: float, instance: Any, flag: threading.Event, send: bool) -> float:
            start = block.step()
            block.target = float(target)
            while True:
                if flag.is_set():
                    raise CancelledByClient("Command was cancelled by a SiLA client (CancelController).")
                t = block.step()
                span = abs(target - start) or 1.0
                instance.progress = max(0.0, min(0.99, 1 - abs(target - t) / span))
                instance.estimated_remaining_time = timedelta(seconds=abs(target - t) / block.ramp_rate)
                if send:
                    instance.send_intermediate_response(round(t, 3))
                if abs(target - t) <= 0.1:
                    return t
                time.sleep(0.05)

        def ControlTemperature(self, TargetTemperature: float, *, metadata: Any, instance: Any) -> float:
            flag = self._start(instance, "Controlling")
            try:
                final = self._ramp_to(TargetTemperature, instance, flag, send=True)
            finally:
                self._finish(instance)
            return round(final, 3)

        def RunProgram(self, Steps: list[Any], *, metadata: Any, instance: Any) -> int:
            if not 1 <= len(Steps) <= 10:
                # A Constrained List parameter would express this in the FDL, but sila2 0.14 cannot
                # unpack top-level constrained lists on the server side, so it is checked here.
                raise ValueError(f"A program needs 1-10 steps, got {len(Steps)}.")
            flag = self._start(instance, "RunningProgram")
            done = 0
            try:
                for step in Steps:
                    self._ramp_to(step.TargetTemperature, instance, flag, send=False)
                    end = time.monotonic() + step.HoldTime
                    while time.monotonic() < end:
                        if flag.is_set():
                            raise CancelledByClient("Program was cancelled by a SiLA client (CancelController).")
                        time.sleep(0.05)
                    done += 1
                    instance.progress = min(0.99, done / len(Steps))
            finally:
                self._finish(instance)
            return done

    class CancelControllerImpl(FeatureImplementationBase):
        def CancelCommand(self, CommandExecutionUUID: str, *, metadata: Any) -> None:
            with block.lock:
                flag = block.running.get(UUID(CommandExecutionUUID))
            if flag is None:
                raise DefinedExecutionError(
                    cancel_feature.defined_execution_errors["InvalidCommandExecutionUUID"],
                    f"No running command with execution UUID {CommandExecutionUUID}.",
                )
            flag.set()

        def CancelAll(self, *, metadata: Any) -> None:
            with block.lock:
                for flag in block.running.values():
                    flag.set()

    server = SilaServer(
        server_name="LabMCP Simulated Thermoblock",
        server_type="TemperatureController",
        server_description="Simulated SiLA 2 temperature controller for LabMCP (no hardware).",
        server_version="1.0.0",
        server_vendor_url="https://github.com/K-Dense-AI/lab-instrument-mcps",
        server_uuid=SIM_SERVER_UUID,
    )
    server.set_feature_implementation(temp_feature, TemperatureControllerImpl(server))
    server.set_feature_implementation(cancel_feature, CancelControllerImpl(server))
    return server, port or _free_port()


class SimulatedSilaServer:
    """Start/stop the simulated server on 127.0.0.1 (insecure, discovery off)."""

    def __init__(self) -> None:
        # sila2 logs every property update at INFO and every (intentional) command error with a
        # traceback; keep the simulator quiet. Dotted logger names make these hierarchy parents.
        for name in ("sila2.server", "TemperatureController", "CancelController", "SiLAService",
                     "SubscriptionManagerThread[TemperatureController", "SubscriptionManagerThread[CancelController",
                     "observable-command-manager-org"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self.server, self.port = build_server()
        self.host = "127.0.0.1"
        self._running = False

    def start(self) -> SimulatedSilaServer:
        self.server.start_insecure(self.host, self.port, enable_discovery=False)
        self._running = True
        return self

    def stop(self) -> None:
        if self._running:
            self._running = False
            self.server.stop(grace_period=0.5)
