"""Driver for Stanford Research Systems RGA100 / RGA200 / RGA300 residual gas analyzers.

Protocol reference: "Operating Manual and Programming Reference, Models RGA100, RGA200 and
RGA300 Residual Gas Analyzer", Stanford Research Systems, Revision 1.9 (2026),
https://www.thinksrs.com/downloads/pdfs/manuals/RGAm.pdf (chapter 6 "Programming the RGA Head":
"RS232 Interface" p. 6-6, "Command Syntax" p. 6-7, "Communication Errors" p. 6-9, "RGA Command
Set" p. 6-29 to 6-68, "Error Byte Definitions" p. 6-69). Cross-checked against SRS's own Python
driver ``srsinst.rga`` 0.3.9 (MIT licence), which is *not* a dependency.

Wire format (manual p. 6-7/6-8):

* Commands are a two-letter name plus a parameter (number, ``*`` = default, ``?`` = query, or
  nothing), terminated by CR. No space between name and parameter.
* ASCII replies (queries and the STATUS byte) end with LF CR: ``string<LF><CR>``.
* Ion currents (MR, TP?, and every point of SC/HS scans) are sent as **binary** 4-byte two's
  complement little-endian integers in units of 1e-16 A, with no terminator.
* Commands that act on hardware (IN, DG, EE, FL, IE, VF, CA, CL, HV) answer with the STATUS byte
  (ASCII decimal) when they finish. Parameter-setting commands NF, MI, MF, SA, SP, ST, MG, MV,
  ML, TP0/TP1, MR0, DG0 answer *nothing*.
* A bad command or parameter is **not answered at all**: the RGA flashes its error LED and sets
  bit 0 of STATUS plus a bit in RS232_ERR (read and cleared with ``EC?``). This driver therefore
  validates parameters before sending, reads back every silent setting, and turns a missing
  reply into a diagnosis via ``EC?``.
"""

from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass, field

from labmcp import InstrumentProtocolError, InstrumentTimeout, Transport

#: Scan rate (ms/amu) and single-mass measurement time (ms) per noise-floor setting NF0..NF7.
#: Manual, "RGA Electronics Control Unit" chapter, "Electrometer" section, p. 4-9.
NF_SCAN_MS_PER_AMU = (2000, 1000, 400, 200, 126, 45, 30, 15)
NF_SINGLE_MASS_MS = (2200, 1100, 440, 220, 139, 50, 33, 16.5)
NF_BASELINE_NOISE_A = (7e-15, 1e-14, 1.5e-14, 2e-14, 4e-14, 1.2e-13, 2.5e-13, 5e-13)

CURRENT_UNIT_A = 1e-16  # ion currents are integers in units of 1e-16 A (0.1 fA)

STATUS_BITS = {
    0: ("RS232_ERR", "EC?", "communication error"),
    1: ("FIL_ERR", "EF?", "filament error"),
    3: ("CEM_ERR", "EM?", "electron multiplier (CDEM) error"),
    4: ("QMF_ERR", "EQ?", "quadrupole mass filter RF power supply error"),
    5: ("DET_ERR", "ED?", "electrometer error"),
    6: ("PS_ERR", "EP?", "24 V external power supply error"),
}

#: Error byte bit meanings, manual "Error Byte Definitions" p. 6-69 to 6-72.
ERROR_BITS: dict[str, dict[int, str]] = {
    "RS232_ERR": {
        0: "CM0: bad command received",
        1: "CM1: bad parameter received",
        2: "CM2: command too long",
        3: "CM3: overwrite in receiving",
        4: "CM4: transmit buffer overwrite",
        5: "CM5: jumper protection violation",
        6: "CM6: parameter conflict",
    },
    "FIL_ERR": {
        0: "FL0: single filament operation",
        5: "FL5: vacuum chamber pressure too high",
        6: "FL6: unable to set the requested emission current (most often an overpressure / leak)",
        7: "FL7: no filament detected (burnt out or not connected)",
    },
    "CEM_ERR": {7: "EM7: no electron multiplier option installed"},
    "QMF_ERR": {
        4: "RF4: RF power supply in current-limited mode",
        6: "RF6: primary current exceeds 2.0 A",
        7: "RF7: RF_CT exceeds (V_EXT - 2 V) at M_MAX (probe not fully seated?)",
    },
    "DET_ERR": {
        1: "DET1: op-amp input offset voltage out of range",
        3: "DET3: COMPENSATE fails to read -5 nA input current",
        4: "DET4: COMPENSATE fails to read +5 nA input current",
        5: "DET5: DETECT fails to read -5 nA input current",
        6: "DET6: DETECT fails to read +5 nA input current",
        7: "DET7: ADC16 test failure",
    },
    "PS_ERR": {6: "PS6: external 24 V supply below 22 V", 7: "PS7: external 24 V supply above 26 V"},
}

_ID_RE = re.compile(r"SRSRGA(?P<mmax>\d{3}|\?{3})VER(?P<fw>[\d.]+)SN(?P<sn>\w+)")

DEFAULTS = {"EE": 70, "IE": 1, "VF": 90, "NF": 4, "SA": 10, "MI": 1}  # IN1 defaults, p. 6-31


def decode_bits(byte_name: str, value: int) -> list[str]:
    table = ERROR_BITS.get(byte_name, {})
    return [table.get(bit, f"{byte_name} bit {bit}") for bit in range(8) if value & (1 << bit)]


def decode_current(data: bytes) -> int:
    """Decode one binary ion current: 4-byte two's complement, little endian, 1e-16 A units."""
    if len(data) != 4:
        raise InstrumentProtocolError(f"Expected a 4-byte ion current, got {len(data)} bytes: {data!r}")
    return struct.unpack("<i", data)[0]


@dataclass
class ScanResult:
    currents_raw: list[int]
    total_raw: int
    start_mass: int
    stop_mass: int
    points_per_amu: int
    noise_floor: int

    @property
    def masses(self) -> list[float]:
        step = 1.0 / self.points_per_amu
        return [round(self.start_mass + i * step, 4) for i in range(len(self.currents_raw))]


@dataclass
class DegasState:
    started: float
    minutes: int
    until: float
    last_status: int | None = None


@dataclass
class PressureEvidence:
    torr: float
    source: str
    monotonic: float = field(default_factory=time.monotonic)


class RGAError(InstrumentProtocolError):
    """The RGA reported a non-zero STATUS byte after a command."""

    def __init__(self, message: str, status: int, details: dict[str, list[str]]) -> None:
        super().__init__(message)
        self.status = status
        self.details = details


class SrsRga:
    """RGA command set on top of a labmcp transport (RS-232 28,800 8N1 RTS/CTS, or TCP)."""

    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self.m_max = 100
        self.id_string = ""
        self.degas: DegasState | None = None
        self.finished_degas: DegasState | None = None
        self._has_cdem: bool | None = None
        self.last_total_pressure: PressureEvidence | None = None

    # ------------------------------------------------------------ low level

    @staticmethod
    def _clean(text: str) -> str:
        return text.strip("\r\n\t ")

    def _guard(self) -> None:
        """Handle a degas cycle that is running (commands would abort it) or has finished."""
        if self.degas is None:
            return
        remaining = self.degas.until - time.monotonic()
        if remaining > 0:
            raise InstrumentProtocolError(
                f"Ionizer degas in progress ({remaining:.0f} s remaining). Any command sent to the RGA "
                "would abort it, so nothing was sent. Wait, or call `filament_off` / `all_off` to stop it."
            )
        # The RGA sends the STATUS byte when the degas is over (manual DG, step 12). The filament
        # emission may need a few more seconds to be re-established.
        with self.t.lock:
            try:
                status = int(self._clean(self.t.read(timeout=30.0)))
            except (InstrumentTimeout, ValueError):
                status = None
        self.degas.last_status = status
        self.finished_degas = self.degas
        self.degas = None

    def write(self, cmd: str) -> None:
        """Send a command that produces no reply."""
        self._guard()
        self.t.write(cmd)

    def query(self, cmd: str, timeout: float | None = None) -> str:
        """Send a query and return its ASCII reply (LF CR stripped)."""
        self._guard()
        with self.t.lock:
            self.t.write(cmd)
            try:
                return self._clean(self.t.read(timeout))
            except InstrumentTimeout as exc:
                raise self._no_reply(cmd, exc) from exc

    def query_int(self, cmd: str, timeout: float | None = None) -> int:
        reply = self.query(cmd, timeout)
        try:
            return int(float(reply))
        except ValueError as exc:
            raise InstrumentProtocolError(f"RGA replied {reply!r} to {cmd!r}; expected an integer.") from exc

    def query_float(self, cmd: str, timeout: float | None = None) -> float:
        reply = self.query(cmd, timeout)
        try:
            return float(reply)
        except ValueError as exc:
            raise InstrumentProtocolError(f"RGA replied {reply!r} to {cmd!r}; expected a number.") from exc

    def status_command(self, cmd: str, timeout: float = 30.0, check: bool = True) -> int:
        """Send a command that answers with the STATUS byte; raise if any error bit is set."""
        status = self.query_int(cmd, timeout)
        if check and status:
            raise self._status_error(cmd, status)
        return status

    def read_current(self, cmd: str, timeout: float = 10.0) -> int:
        """Send ``MRn`` / ``TP?`` and return the binary ion current in 1e-16 A units."""
        self._guard()
        with self.t.lock:
            self.t.write(cmd)
            try:
                return decode_current(self.t.read_bytes(4, timeout))
            except InstrumentTimeout as exc:
                raise self._no_reply(cmd, exc) from exc

    def _no_reply(self, cmd: str, exc: InstrumentTimeout) -> InstrumentProtocolError | InstrumentTimeout:
        """The RGA ignores bad commands; ask RS232_ERR why (``EC?`` reads and clears it)."""
        try:
            self.t.flush_input()
            self.t.write("EC?")
            rs232 = int(float(self._clean(self.t.read(2.0))))
        except (InstrumentTimeout, ValueError):
            return exc
        if not rs232:
            return exc
        reasons = "; ".join(decode_bits("RS232_ERR", rs232))
        return InstrumentProtocolError(
            f"The RGA did not execute {cmd!r} (RS232_ERR={rs232}: {reasons}). The RGA ignores commands "
            "it rejects; the error byte has now been cleared."
        )

    def _status_error(self, cmd: str, status: int) -> RGAError:
        details = self.error_details(status)
        parts = [f"{name}: {', '.join(msgs) or 'no specific bit set'}" for name, msgs in details.items()]
        hint = ""
        if "FIL_ERR" in details:
            self.last_total_pressure = None
            hint = (
                " The filament protection shut the emission down: check the vacuum (overpressure/leak) "
                "and the filament. FIL_ERR clears only after the filament is switched on successfully."
            )
        return RGAError(
            f"RGA reported STATUS={status} after {cmd!r}: " + "; ".join(parts) + "." + hint, status, details
        )

    # ------------------------------------------------------------ connection

    def login(self, user: str, password: str) -> None:
        """Log in to an SRS RGA Ethernet adapter (REA, TCP port 818), as srsinst.rga does."""
        with self.t.lock:
            for _ in range(3):
                self.t.write_bytes(b" \r")
                try:
                    self.t.read_until(b"Name:", timeout=3.0)
                    break
                except InstrumentTimeout:
                    continue
            else:
                raise InstrumentProtocolError("No 'Name:' login prompt from the RGA Ethernet adapter.")
            self.t.write_bytes(user.encode() + b"\r")
            time.sleep(0.5)
            self.t.flush_input()
            self.t.write_bytes(password.encode() + b"\r")
            try:
                self.t.read_until(b"Welcome", timeout=5.0)
            except InstrumentTimeout as exc:
                raise InstrumentProtocolError(
                    "RGA Ethernet adapter login failed: check the user name and password."
                ) from exc
            time.sleep(0.2)
            self.t.flush_input()

    def identify(self) -> dict[str, object]:
        """``ID?`` -> ``SRSRGA###VER#.##SN#####`` (p. 6-30)."""
        reply = self.query("ID?")
        m = _ID_RE.search(reply)
        if not m:
            raise InstrumentProtocolError(
                f"Unexpected reply to 'ID?': {reply!r} (expected SRSRGA...VER...SN...)"
            )
        self.id_string = reply
        mmax = m["mmax"]
        self.m_max = int(mmax) if mmax.isdigit() else 100
        return {
            "manufacturer": "Stanford Research Systems",
            "model": f"RGA{mmax}",
            "max_mass_amu": self.m_max,
            "firmware": m["fw"],
            "serial": m["sn"],
            "id_string": reply,
        }

    def initialize(self, level: int = 0) -> int:
        """``IN0`` clear buffers + hardware check, ``IN1`` factory defaults, ``IN2`` standby."""
        if level not in (0, 1, 2):
            raise ValueError("IN level must be 0, 1 or 2")
        return self.status_command(f"IN{level}", timeout=60.0, check=False)

    def close(self) -> None:
        self.t.close()

    # ------------------------------------------------------------ errors

    def status_byte(self) -> int:
        return self.query_int("ER?")

    def error_details(self, status: int | None = None) -> dict[str, list[str]]:
        """Read the error bytes flagged in STATUS. Note EC? and EM? clear their bytes."""
        if status is None:
            status = self.status_byte()
        out: dict[str, list[str]] = {}
        for bit, (name, query, _desc) in STATUS_BITS.items():
            if status & (1 << bit):
                try:
                    value = self.query_int(query, timeout=10.0)
                except InstrumentProtocolError:
                    out[name] = ["could not be read"]
                    continue
                out[name] = decode_bits(name, value)
        return out

    def filament_error_byte(self) -> int:
        return self.query_int("EF?")

    # ------------------------------------------------------------ ionizer

    def emission_ma(self) -> float:
        return self.query_float("FL?")

    def set_emission(self, ma: float) -> int:
        """``FLx``: 0 = filament off, 0.02-3.50 mA = on (p. 6-34). Returns STATUS (raises on error)."""
        if not (ma == 0 or 0.02 <= ma <= 3.5):
            raise ValueError("Emission current must be 0 (off) or 0.02-3.50 mA")
        if ma == 0:
            return self.status_command("FL0", timeout=30.0)
        self.last_total_pressure = None  # a reading taken at another emission current is stale
        return self.status_command(f"FL{ma:.2f}", timeout=60.0)

    def electron_energy_ev(self) -> int:
        return self.query_int("EE?")

    def set_electron_energy(self, ev: int) -> int:
        if not 25 <= ev <= 105:
            raise ValueError("Electron energy must be 25-105 eV")
        return self.status_command(f"EE{int(ev)}", timeout=30.0)

    def ion_energy_high(self) -> bool:
        return self.query_int("IE?") == 1

    def set_ion_energy(self, high: bool) -> int:
        return self.status_command(f"IE{1 if high else 0}", timeout=30.0)

    def focus_voltage_v(self) -> int:
        return self.query_int("VF?")

    def set_focus_voltage(self, volts: int) -> int:
        if not 0 <= volts <= 150:
            raise ValueError("Focus plate voltage must be 0-150 V")
        return self.status_command(f"VF{int(volts)}", timeout=30.0)

    def start_degas(self, minutes: int) -> None:
        """``DGn``: degas for n minutes (1 min ramp to 20 mA at 400 eV). STATUS comes at the end."""
        if not 1 <= minutes <= 20:
            raise ValueError("Degas time must be 1-20 minutes")
        self._guard()
        self.t.write(f"DG{int(minutes)}")
        now = time.monotonic()
        self.last_total_pressure = None
        self.degas = DegasState(started=now, minutes=minutes, until=now + minutes * 60.0 + 2.0)

    def stop_degas(self) -> bool:
        """Abort a running degas with ``DG0`` (no echo). Returns True if one was running."""
        if self.degas is None:
            return False
        running = time.monotonic() < self.degas.until
        self.degas = None
        with self.t.lock:
            self.t.write("DG0")
            # DG polls for commands once a second, then restores the pre-degas emission.
            time.sleep(0.05 if self._simulated else 3.0)
            self.t.flush_input()
        return running

    @property
    def _simulated(self) -> bool:
        return self.t.description.startswith("sim://")

    # ------------------------------------------------------------ detector

    def has_cdem(self) -> bool:
        if self._has_cdem is None:
            self._has_cdem = self.query_int("MO?") == 1
        return self._has_cdem

    def cdem_voltage_v(self) -> float:
        return self.query_float("HV?")

    def set_cdem_voltage(self, volts: int) -> int:
        """``HVx``: 0 = CDEM off / Faraday cup, 10-2490 V = CDEM on (p. 6-40)."""
        if not (volts == 0 or 10 <= volts <= 2490):
            raise ValueError("CDEM voltage must be 0 (off) or 10-2490 V")
        if not self.has_cdem():
            # The RGA would silently reject HV (bad command); say why instead of timing out.
            raise InstrumentProtocolError(
                "This RGA has no electron multiplier (CDEM option 01 not installed)."
            )
        return self.status_command(f"HV{int(volts)}", timeout=60.0)

    def noise_floor(self) -> int:
        return self.query_int("NF?")

    def set_noise_floor(self, nf: int) -> None:
        if not 0 <= nf <= 7:
            raise ValueError("Noise floor must be 0-7")
        self._set_verified(f"NF{int(nf)}", "NF?", nf)

    def calibrate_all(self) -> int:
        """``CA``: re-zero the detector at the present settings and correct the RF scan table."""
        return self.status_command("CA", timeout=120.0)

    def calibrate_electrometer(self) -> int:
        """``CL``: full electrometer I-V calibration (clears all CA offset factors)."""
        return self.status_command("CL", timeout=180.0)

    # ------------------------------------------------------------ parameter storage

    def partial_sensitivity_ma_per_torr(self) -> float:
        return self.query_float("SP?")

    def total_sensitivity_ma_per_torr(self) -> float:
        return self.query_float("ST?")

    def cdem_stored_gain(self) -> float:
        """``MG?``: stored CDEM gain (the RGA stores it in thousands; returned here as a plain gain)."""
        return self.query_float("MG?") * 1000.0

    # ------------------------------------------------------------ scans

    def _set_verified(self, cmd: str, query: str, expected: float) -> None:
        with self.t.lock:
            self.write(cmd)
            got = self.query_float(query)
        if abs(got - expected) > 1e-6:
            rs232 = self.query_int("EC?")
            reasons = "; ".join(decode_bits("RS232_ERR", rs232)) or "no communication error recorded"
            raise InstrumentProtocolError(
                f"RGA did not accept {cmd!r}: {query} reads back {got:g} ({reasons})."
            )

    def scan_range(self) -> tuple[int, int]:
        return self.query_int("MI?"), self.query_int("MF?")

    def set_scan_range(self, start: int, stop: int) -> None:
        """Set MI/MF in an order that never makes MI > MF (a parameter-conflict error)."""
        if not 1 <= start <= stop <= self.m_max:
            raise ValueError(f"Mass range must satisfy 1 <= start <= stop <= {self.m_max} amu")
        with self.t.lock:
            cur_mi, cur_mf = self.scan_range()
            if (start, stop) == (cur_mi, cur_mf):
                return
            if stop >= cur_mi:
                self._set_verified(f"MF{stop}", "MF?", stop)
                self._set_verified(f"MI{start}", "MI?", start)
            else:
                self._set_verified(f"MI{start}", "MI?", start)
                self._set_verified(f"MF{stop}", "MF?", stop)

    def steps_per_amu(self) -> int:
        return self.query_int("SA?")

    def set_steps_per_amu(self, sa: int) -> None:
        if not 10 <= sa <= 25:
            raise ValueError("Steps per amu must be 10-25")
        self._set_verified(f"SA{int(sa)}", "SA?", sa)

    def estimate_scan_s(self, start: int, stop: int, nf: int, histogram: bool) -> float:
        if histogram:
            return (stop - start + 1) * NF_SINGLE_MASS_MS[nf] / 1000.0 + 2.0
        return max(stop - start, 1) * NF_SCAN_MS_PER_AMU[nf] / 1000.0 + 2.0

    def _run_scan(self, trigger: str, points_query: str, start: int, stop: int, sa: int) -> ScanResult:
        with self.t.lock:
            nf = self.noise_floor()
            n = self.query_int(points_query)
            histogram = trigger.startswith("HS")
            timeout = self.estimate_scan_s(start, stop, nf, histogram) * 2.0 + 15.0
            self._guard()
            self.t.write(trigger)
            try:
                data = self.t.read_bytes(4 * (n + 1), timeout)
            except InstrumentTimeout as exc:
                self.t.write("IN0")  # halt the scan and clear both buffers (manual p. 6-12)
                try:
                    self.t.read(10.0)
                except InstrumentTimeout:
                    pass
                self.t.flush_input()
                raise InstrumentTimeout(f"Scan data incomplete ({exc}); the RGA was reset with IN0.") from exc
        values = list(struct.unpack(f"<{n + 1}i", data))
        return ScanResult(values[:n], values[n], start, stop, sa if not histogram else 1, nf)

    def analog_scan(self, start: int, stop: int, steps_per_amu: int) -> ScanResult:
        """``SC1``: (MF-MI)*SA+1 currents plus one total-pressure current (p. 6-51)."""
        with self.t.lock:
            self.set_scan_range(start, stop)
            if self.steps_per_amu() != steps_per_amu:
                self.set_steps_per_amu(steps_per_amu)
            return self._run_scan("SC1", "AP?", start, stop, steps_per_amu)

    def histogram_scan(self, start: int, stop: int) -> ScanResult:
        """``HS1``: MF-MI+1 peak currents plus one total-pressure current (p. 6-45)."""
        with self.t.lock:
            self.set_scan_range(start, stop)
            return self._run_scan("HS1", "HP?", start, stop, 1)

    def single_mass(self, mass: int) -> int:
        """``MRn``: peak-locked single mass measurement (max of 7 points over +/-0.3 amu)."""
        if not 1 <= mass <= self.m_max:
            raise ValueError(f"Mass must be 1-{self.m_max} amu")
        nf = self.noise_floor()
        return self.read_current(f"MR{int(mass)}", timeout=NF_SINGLE_MASS_MS[nf] / 1000.0 * 3 + 5.0)

    def rf_off(self) -> None:
        """``MR0``: switch the quadrupole RF/DC off (no reply)."""
        self.write("MR0")

    def total_pressure_raw(self) -> int:
        """``TP?``: total ion current (binary). Returns 0 if TP_Flag is cleared (e.g. CDEM on)."""
        return self.read_current("TP?", timeout=10.0)

    def enable_total_pressure(self, on: bool = True) -> None:
        self.write(f"TP{1 if on else 0}")

    # ------------------------------------------------------------ conversions

    @staticmethod
    def current_a(raw: int) -> float:
        return raw * CURRENT_UNIT_A

    @staticmethod
    def to_torr(raw: int, sensitivity_ma_per_torr: float, cdem_gain: float | None = None) -> float:
        """Ion current -> pressure: P = I / S (S in A/Torr), divided by the CDEM gain if on."""
        s = max(sensitivity_ma_per_torr, 1e-4) * 1e-3
        p = raw * CURRENT_UNIT_A / s
        if cdem_gain:
            p /= max(cdem_gain, 1.0)
        return p
