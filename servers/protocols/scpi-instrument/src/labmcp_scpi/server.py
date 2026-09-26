"""MCP server for any IEEE 488.2 / SCPI instrument."""

from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import math
import struct
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from labmcp import (
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentServer,
    Limit,
    prepare_save_path,
)
from pydantic import BaseModel, Field

from labmcp_scpi.driver import ExchangeResult, SCPIError, SCPIInstrument
from labmcp_scpi.policy import CommandPolicy
from labmcp_scpi.primer import PRIMER
from labmcp_scpi.simulator import DMMPowerSupplySimulator

#: Safe state used in --simulate mode when none is configured (the simulator is a PSU).
DEMO_SAFE_STATE = "OUTP OFF"


def _configured_safe_state(simulate: bool, options: dict[str, str]) -> str | None:
    value = (options.get("safe_state") or "").strip()
    if value:
        return value
    return DEMO_SAFE_STATE if simulate else None


def connect(ctx: ConnectContext) -> SCPIInstrument:
    try:
        policy = CommandPolicy.from_options(ctx.settings.options, read_only=ctx.settings.read_only)
    except ValueError as exc:
        raise InstrumentConnectionError(f"Invalid server configuration: {exc}") from exc
    # SCPI instruments terminate messages with LF (NL). latin-1 keeps any stray 8-bit byte intact.
    transport = ctx.open_transport(
        simulator=DMMPowerSupplySimulator,
        read_termination="\n",
        write_termination="\n",
        encoding="latin-1",
        timeout=5.0,
    )
    return SCPIInstrument(
        transport,
        policy,
        error_query=ctx.option("error_query", "SYST:ERR?") or "SYST:ERR?",
        safe_state=_configured_safe_state(ctx.simulate, ctx.settings.options),
    )


class SCPIServer(InstrumentServer[SCPIInstrument]):
    """Shows the `apply_safe_state` tool only when a safe state is configured."""

    def configure(self, **kwargs: Any) -> SCPIServer:
        super().configure(**kwargs)
        if _configured_safe_state(self.settings.simulate, self.settings.options):
            self.mcp.enable(names={"apply_safe_state"})
        else:
            self.mcp.disable(names={"apply_safe_state"})
        return self


server = SCPIServer(
    "Generic SCPI Instrument (IEEE 488.2)",
    connect=connect,
    package="labmcp-scpi",
    instructions="""
Generic server for ANY SCPI / IEEE 488.2 instrument (DMM, oscilloscope, SMU, power supply, load,
function generator, analyzer...). The server does not know what the instrument is or which
commands are dangerous, so be careful and explicit.
- Call `identify` first, then use that model's command set. Call `scpi_primer` if unsure of SCPI
  syntax. `-113 Undefined header` means the mnemonic is wrong for THIS model: stop guessing and ask
  the user for the programming manual.
- `scpi_query` sends one read-only query (header ending in '?'). Everything else - settings,
  compound messages, queries with side effects - goes through `scpi_write` / `scpi_batch`, which
  the user must approve. Before calling them, say exactly what will be sent and what it does.
- Never switch an output on or raise a source level/limit beyond what the user asked for. Set
  levels and compliance/current limits BEFORE enabling an output; read them back afterwards.
- On source-measure units, MEASure?, READ?, INITiate and CONFigure can switch the output on:
  send them with `scpi_write`, not `scpi_query`.
- Check `errors` in every write/batch result. Binary replies (#<n><len>...) must be read with
  `query_binary_block`.
- If something looks wrong: `apply_safe_state` (if listed) runs the lab's configured safe-state
  commands; `device_clear` only recovers a stuck interface. Only call `reset_instrument` (*RST)
  when the user asks for it.
- Command-policy refusals are deliberate lab settings: report them, never rephrase to evade them.
""",
    limits=[
        Limit("max_operation_wait_s", 300, "s", "Longest *OPC? wait an agent may request"),
        Limit("max_block_bytes", 16 * 1024 * 1024, "bytes", "Largest binary block the server will read"),
    ],
    address_help="""\
  visa://TCPIP0::192.168.1.50::inst0::INSTR     LAN, VXI-11 (pyvisa-py by default)
  visa://TCPIP0::192.168.1.50::hislip0::INSTR   LAN, HiSLIP
  visa://USB0::0x1AB1::0x0588::DS1ZA000000::INSTR  USBTMC
  visa://GPIB0::22::INSTR?backend=@ivi           GPIB via NI-VISA / vendor VISA
  tcp://192.168.1.50:5025                        raw SCPI socket (port varies: 5025, 5555, 4000...)
  serial:///dev/ttyUSB0?baudrate=9600            RS-232 (add read_termination=CRLF if needed)""",
    option_help={
        "write_denylist": "regex; matching commands are never sent by any tool (alias: denylist)",
        "write_allowlist": "regex; if set, scpi_write/scpi_batch/reset may only send matching commands",
        "query_denylist": "regex; queries scpi_query must refuse (e.g. '^(MEAS|READ)' on an SMU)",
        "allow_measure_in_read_only": "true to allow MEAS?/READ? in --read-only mode (meters that cannot source)",
        "safe_state": "program message sent by apply_safe_state, e.g. 'OUTP OFF' or 'OUTP1 OFF;:OUTP2 OFF'",
        "error_query": "error-queue query (default SYST:ERR?)",
    },
)
mcp = server.mcp


# ---------------------------------------------------------------- models


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class QueueEntry(BaseModel):
    code: int | None = Field(description="SCPI error number (negative: SCPI-defined, positive: vendor)")
    message: str
    raw: str = Field(description="Reply exactly as read from the error queue")


def _errs(errors: list[SCPIError]) -> list[QueueEntry]:
    return [QueueEntry(code=e.code, message=e.message, raw=e.raw) for e in errors]


class Identity(BaseModel):
    manufacturer: str
    model: str
    serial: str
    firmware: str
    raw: str = Field(description="Raw *IDN? reply")
    timestamp: str


class QueryResult(BaseModel):
    command: str
    response: str
    length: int = Field(description="Length of the full response in characters")
    truncated: bool = Field(description="True if the response was cut to max_chars")
    elapsed_ms: float
    timestamp: str


class WriteResult(BaseModel):
    command: str
    response: str | None = Field(description="Reply, if the message contained a query")
    errors: list[QueueEntry] = Field(description="Error-queue entries read after the command")
    error_check: str = Field(description="'ok', 'errors', or 'unavailable: ...' if the queue could not be read")
    ok: bool = Field(description="True if the error queue was read and empty")
    elapsed_ms: float
    timestamp: str


def _write_result(r: ExchangeResult) -> WriteResult:
    return WriteResult(
        command=r.command,
        response=r.response,
        errors=_errs(r.errors),
        error_check=r.error_check,
        ok=r.error_check == "ok",
        elapsed_ms=round(r.elapsed_s * 1000, 2),
        timestamp=_now(),
    )


class BatchStep(BaseModel):
    index: int
    command: str
    status: Literal["ok", "errors", "failed", "not_run", "unchecked"]
    response: str | None = None
    errors: list[QueueEntry] = Field(default_factory=list)
    detail: str | None = None
    elapsed_ms: float | None = None


class BatchResult(BaseModel):
    steps: list[BatchStep]
    completed: int = Field(description="Number of steps sent to the instrument")
    stopped_early: bool
    timestamp: str


class DecodedBlock(BaseModel):
    data_type: str
    byte_order: str
    count: int = Field(description="Number of values in the block")
    minimum: float | None
    maximum: float | None
    mean: float | None
    non_finite: int = Field(description="Number of NaN / infinite values (left out of the statistics)")
    stride: int = Field(description="Every stride-th value is returned in `values`")
    values: list[float | None] = Field(description="Downsampled values; NaN and infinities are null")


class BlockResult(BaseModel):
    command: str
    length_bytes: int
    sha256: str
    data_base64: str | None = Field(description="Whole payload, only if <= max_inline_bytes")
    preview_hex: str = Field(description="First 32 bytes as hex")
    saved_to: str | None
    decoded: DecodedBlock | None
    timestamp: str


class VisaResource(BaseModel):
    resource: str
    address: str = Field(description="Value to pass as --address")
    interface_type: str | None = None
    resource_class: str | None = None
    alias: str | None = None


class VisaResources(BaseModel):
    available: bool = Field(description="False if PyVISA or a VISA backend is not usable")
    simulated: bool
    backend: str
    resources: list[VisaResource]
    note: str


# ---------------------------------------------------------------- read tools


@mcp.tool(**READ)
def scpi_primer() -> dict[str, Any]:
    """Concise SCPI syntax guide (long/short forms, queries, compound commands, common
    commands, error queue, binary blocks) plus this server's active command policy. Works
    without a connection."""
    try:
        policy: dict[str, Any] = CommandPolicy.from_options(
            server.settings.options, read_only=server.settings.read_only
        ).describe()
    except ValueError as exc:
        policy = {"error": str(exc)}
    return {
        "primer": PRIMER,
        "command_policy": policy,
        "safe_state": _configured_safe_state(server.settings.simulate, server.settings.options),
        "error_query": server.settings.options.get("error_query", "SYST:ERR?"),
    }


@mcp.tool(**READ)
def identify() -> Identity:
    """Ask the instrument who it is (*IDN?): manufacturer, model, serial number, firmware."""
    info = server.driver.identify()
    return Identity(**info, timestamp=_now())


@mcp.tool(**READ, timeout=60)
def list_visa_resources(
    query: Annotated[str, Field(max_length=100, description="VISA resource filter")] = "?*::INSTR",
    backend: Annotated[
        Literal["@py", "@ivi"], Field(description="@py = pyvisa-py, @ivi = NI-VISA / Keysight / R&S VISA")
    ] = "@py",
) -> VisaResources:
    """List GPIB / USBTMC / LAN (VXI-11, HiSLIP) instruments visible to VISA on this computer.
    Works without --address. Raw-socket instruments (tcp://host:5025) are not discoverable."""
    if server.settings.simulate:
        return VisaResources(
            available=True,
            simulated=True,
            backend="sim",
            resources=[
                VisaResource(resource="SIM::DMM-PSU::INSTR", address="(use --simulate)", resource_class="INSTR")
            ],
            note="SIMULATION MODE: no VISA scan was performed; the only instrument is the simulator.",
        )
    try:
        import pyvisa
    except ImportError:
        return VisaResources(
            available=False,
            simulated=False,
            backend=backend,
            resources=[],
            note='PyVISA is not installed. Install it with `pip install "labmcp[visa]"`.',
        )
    rm = None
    try:
        rm = pyvisa.ResourceManager(backend)
        found = rm.list_resources_info(query)
    except Exception as exc:  # backend missing, no drivers, ... (pyvisa raises many types)
        return VisaResources(
            available=False,
            simulated=False,
            backend=backend,
            resources=[],
            note=f"VISA backend {backend} is not usable: {type(exc).__name__}: {exc}",
        )
    finally:
        if rm is not None:
            with contextlib.suppress(Exception):
                rm.close()
    resources = [
        VisaResource(
            resource=name,
            address=f"visa://{name}" + ("" if backend == "@py" else f"?backend={backend}"),
            interface_type=str(getattr(info, "interface_type", "")) or None,
            resource_class=getattr(info, "resource_class", None),
            alias=getattr(info, "alias", None),
        )
        for name, info in sorted(found.items())
    ]
    return VisaResources(
        available=True,
        simulated=False,
        backend=backend,
        resources=resources,
        note="pyvisa-py finds USBTMC devices only with pyusb/libusb installed and GPIB only with a "
        "GPIB driver; LAN discovery (VXI-11 broadcast, HiSLIP mDNS) can miss instruments on other "
        "subnets.",
    )


@mcp.tool(**READ)
def scpi_query(
    command: Annotated[
        str, Field(max_length=500, description="One SCPI query, e.g. '*IDN?', 'MEAS:VOLT:DC?', 'VOLT? MAX'")
    ],
    timeout_s: Annotated[float | None, Field(ge=0.1, le=300, description="Reply timeout (default: server's)")] = None,
    max_chars: Annotated[int, Field(ge=100, le=1_000_000, description="Truncate longer replies")] = 20_000,
) -> QueryResult:
    """Send ONE read-only SCPI query and return the instrument's text reply.

    Only single queries are accepted: the header must end with '?' (parameters such as
    'VOLT? MAX' are allowed), with no ';' and no line breaks. Queries with known side
    effects (*TST?, *CAL?, CALibration, DIAGnostic) and anything in the lab's query
    denylist are refused - use `scpi_write` for those. An unknown query gets no reply
    (timeout); the error queue is then reported."""
    r = server.driver.checked_query(command, timeout=timeout_s)
    text = r.response or ""
    return QueryResult(
        command=r.command,
        response=text[:max_chars],
        length=len(text),
        truncated=len(text) > max_chars,
        elapsed_ms=round(r.elapsed_s * 1000, 2),
        timestamp=_now(),
    )


_DTYPES = {
    "int8": "b",
    "uint8": "B",
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "float32": "f",
    "float64": "d",
}


def _decode(data: bytes, data_type: str, byte_order: str, max_points: int) -> tuple[DecodedBlock, list[float]]:
    code = _DTYPES[data_type]
    size = struct.calcsize(code)
    if len(data) % size:
        raise InstrumentError(
            f"The {len(data)}-byte block is not a whole number of {data_type} values ({size} bytes each). "
            "Check the instrument's data format (e.g. FORMat:DATA?) and pick the matching decode_as."
        )
    n = len(data) // size
    values = [float(v) for v in struct.unpack(f"{'<' if byte_order == 'little' else '>'}{n}{code}", data)]
    finite = [v for v in values if math.isfinite(v)]
    stride = max(1, math.ceil(n / max_points)) if n else 1
    decoded = DecodedBlock(
        data_type=data_type,
        byte_order=byte_order,
        count=n,
        minimum=min(finite) if finite else None,
        maximum=max(finite) if finite else None,
        mean=math.fsum(finite) / len(finite) if finite else None,
        non_finite=n - len(finite),
        stride=stride,
        # JSON has no NaN/Infinity: they would break the tool's structured output
        values=[v if math.isfinite(v) else None for v in values[::stride]],
    )
    return decoded, values


#: Extensions query_binary_block may write: .csv holds decoded values (or the raw bytes when
#: decode_as is "none", e.g. a CSV file read from the instrument), the rest hold the raw payload.
SAVE_SUFFIXES = (".csv", ".bin", ".dat", ".raw", ".txt", ".png", ".bmp", ".jpg", ".jpeg", ".gif", ".tif", ".tiff")


@mcp.tool(**READ, timeout=310)
def query_binary_block(
    command: Annotated[str, Field(max_length=500, description="One SCPI query answering with #<n><len><data>")],
    decode_as: Annotated[
        Literal["none", "int8", "uint8", "int16", "uint16", "int32", "uint32", "float32", "float64"],
        Field(description="Interpret the payload as an array of this type (see the instrument's FORMat)"),
    ] = "none",
    byte_order: Annotated[
        Literal["big", "little"], Field(description="big = SCPI FORMat:BORDer NORMal, little = SWAPped")
    ] = "big",
    max_points: Annotated[int, Field(ge=1, le=10_000, description="Max decoded values returned inline")] = 200,
    max_inline_bytes: Annotated[
        int, Field(ge=0, le=262_144, description="Return the raw payload as base64 only up to this size")
    ] = 4096,
    save_path: Annotated[
        str | None,
        Field(
            max_length=1000,
            description="Write the full data here: .csv with decode_as writes index,value rows; .bin/.dat/.raw/"
            ".txt/.png/.bmp/.jpg/.gif/.tif (or .csv with decode_as none) get the raw payload",
        ),
    ] = None,
    overwrite: Annotated[bool, Field(description="Allow replacing an existing save_path")] = False,
    timeout_s: Annotated[float, Field(ge=0.1, le=300, description="Timeout for the whole transfer")] = 10.0,
) -> BlockResult:
    """Read an IEEE 488.2 definite-length binary block (waveforms, trace data, screenshots,
    FORMat REAL/INTeger readings). The query must pass the same read-only checks as
    `scpi_query`. Returns length, SHA-256 and (optionally) decoded values with summary
    statistics, downsampled to max_points; use save_path for the full data. Blocks larger
    than the `max_block_bytes` limit are discarded."""
    path = prepare_save_path(save_path, suffixes=SAVE_SUFFIXES, overwrite=overwrite) if save_path else None
    max_bytes = int(server.limits["max_block_bytes"])
    data = server.driver.read_block(command, max_bytes=max_bytes, timeout=timeout_s)
    decoded: DecodedBlock | None = None
    values: list[float] = []
    if decode_as != "none":
        decoded, values = _decode(data, decode_as, byte_order, max_points)
    if path is not None:
        try:
            if path.suffix.lower() == ".csv" and decode_as != "none":
                with path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["index", "value"])
                    writer.writerows(enumerate(values))
            else:
                path.write_bytes(data)
        except OSError as exc:
            raise InstrumentError(f"The block was read but could not be saved to {path}: {exc}") from exc
    return BlockResult(
        command=command.strip(),
        length_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data_base64=base64.b64encode(data).decode("ascii") if len(data) <= max_inline_bytes else None,
        preview_hex=data[:32].hex(" "),
        saved_to=str(path) if path else None,
        decoded=decoded,
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_errors(
    max_errors: Annotated[int, Field(ge=1, le=100, description="Stop after this many entries")] = 20,
) -> list[QueueEntry]:
    """Read and clear the instrument's error/event queue (SYSTem:ERRor? until 0,"No error").
    Entries are returned oldest first; each can only be read once. An empty list means no
    errors."""
    return _errs(server.driver.read_errors(max_errors))


@mcp.tool(**READ, timeout=3700)
def wait_operation_complete(
    timeout_s: Annotated[float, Field(ge=0.1, le=3600, description="Longest time to wait")] = 30.0,
) -> dict[str, Any]:
    """Wait until the instrument has finished all pending operations (*OPC? returns 1),
    e.g. after starting a sweep, an acquisition or a settling source."""
    server.check("max_operation_wait_s", timeout_s, "*OPC? wait")
    waited = server.driver.wait_operation_complete(timeout_s)
    return {"complete": True, "waited_s": round(waited, 3), "timestamp": _now()}


# ---------------------------------------------------------------- hazard tools


@mcp.tool(**HAZARD)
def scpi_write(
    command: Annotated[
        str, Field(max_length=2000, description="SCPI program message, e.g. 'VOLT 5;CURR 0.1' or 'OUTP ON'")
    ],
    timeout_s: Annotated[float | None, Field(ge=0.1, le=300, description="Reply timeout if it contains a query")] = None,
) -> WriteResult:
    """Send any SCPI program message (settings, compound 'A;B' messages, queries with side
    effects) and then read the error queue. If the message contains a query, its reply is
    returned. This can change the instrument's state, including switching outputs on;
    say what the command does before calling it. Commands in the lab's denylist, or
    outside its allowlist, are refused before anything is sent."""
    return _write_result(server.driver.checked_write(command, timeout=timeout_s))


#: scpi_batch's tool timeout. FastMCP cannot stop a sync tool's thread when it times out, so
#: the batch itself stops starting new steps BATCH_MARGIN_S before it; otherwise steps would
#: keep reaching the instrument after the client was told the call failed.
BATCH_TIMEOUT_S = 900
BATCH_MARGIN_S = 30


@mcp.tool(**HAZARD, timeout=BATCH_TIMEOUT_S)
def scpi_batch(
    steps: Annotated[
        list[Annotated[str, Field(max_length=2000)]],
        Field(min_length=1, max_length=50, description="Commands/queries to send in order"),
    ],
    stop_on_error: Annotated[bool, Field(description="Stop at the first step that reports an error")] = True,
    delay_between_s: Annotated[float, Field(ge=0, le=10, description="Pause between steps")] = 0.0,
    timeout_s: Annotated[float | None, Field(ge=0.1, le=60, description="Reply timeout per query")] = None,
) -> BatchResult:
    """Run a short sequence of SCPI commands and queries in order, checking the error queue
    after every step. Every step is checked against the command policy BEFORE the first one
    is sent, so a refused step means nothing was sent. Returns per-step replies and errors.
    Steps not started within ~14 minutes are not sent (status not_run)."""
    for command in steps:  # validate everything first
        server.driver.policy.check_command(command)
    deadline = time.monotonic() + BATCH_TIMEOUT_S - BATCH_MARGIN_S
    results: list[BatchStep] = []
    stopped = False
    for i, command in enumerate(steps):
        if stopped:
            results.append(BatchStep(index=i, command=command, status="not_run"))
            continue
        if i and delay_between_s:
            time.sleep(delay_between_s)
        if time.monotonic() > deadline:
            results.append(
                BatchStep(
                    index=i,
                    command=command,
                    status="not_run",
                    detail=f"not sent: the batch used up its {BATCH_TIMEOUT_S - BATCH_MARGIN_S} s time budget",
                )
            )
            stopped = True
            continue
        try:
            r = server.driver.checked_write(command, timeout=timeout_s)
        except InstrumentError as exc:
            results.append(BatchStep(index=i, command=command, status="failed", detail=str(exc)))
            stopped = stop_on_error
            continue
        status: Literal["ok", "errors", "unchecked"] = (
            "ok" if r.error_check == "ok" else "errors" if r.errors else "unchecked"
        )
        results.append(
            BatchStep(
                index=i,
                command=r.command,
                status=status,
                response=r.response,
                errors=_errs(r.errors),
                detail=None if status == "ok" else r.error_check,
                elapsed_ms=round(r.elapsed_s * 1000, 2),
            )
        )
        if status == "errors" and stop_on_error:
            stopped = True
    return BatchResult(
        steps=results,
        completed=sum(1 for s in results if s.status != "not_run"),
        stopped_early=stopped and any(s.status == "not_run" for s in results),
        timestamp=_now(),
    )


@mcp.tool(**HAZARD, timeout=90)
def reset_instrument(
    clear_status: Annotated[bool, Field(description="Also send *CLS (clear status and error queue)")] = True,
) -> WriteResult:
    """Reset the instrument to its default settings (*RST, then *CLS), wait for completion and
    check errors. SCPI requires outputs OFF after *RST, but every other setting (levels,
    ranges, triggers, limits) returns to its default and some instruments differ: check the
    manual before resetting anything connected to a device under test."""
    return _write_result(server.driver.reset(clear_status=clear_status))


# ---------------------------------------------------------------- safety tools


@mcp.tool(**SAFETY)
def device_clear(
    send_abort: Annotated[bool, Field(description="Also send ABORt (stop sweeps / measurements)")] = True,
) -> dict[str, Any]:
    """Recover a stuck or confused instrument: VISA device clear (or discard unread input on
    socket/serial links), report the error queue, optionally ABORt, then *CLS. This does NOT
    make an arbitrary instrument safe (outputs stay as they are): use `apply_safe_state` if
    the lab configured one, or the instrument's own controls."""
    result = server.driver.device_clear(abort=send_abort)
    return {
        "steps": result["steps"],
        "errors_before_clear": _errs(result["errors_before_clear"]),  # type: ignore[arg-type]
        "error_check": result["error_check"],
        "timestamp": _now(),
    }


@mcp.tool(**SAFETY)
def apply_safe_state() -> WriteResult:
    """Put the instrument into the lab's configured safe state (the `--option safe_state`
    program message, e.g. 'OUTP OFF'), then read the error queue. Only listed when a safe
    state is configured. Call it immediately if anything looks wrong."""
    return _write_result(server.driver.apply_safe_state())


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
