"""MCP server for Bluetooth LE health sensors (standard Bluetooth SIG GATT profiles).

Research / lab-operations use only. Not a medical device; not for diagnosis or clinical
decision-making.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Annotated, Any, Literal

from labmcp import READ, ConnectContext, InstrumentProtocolError, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_ble_health.driver import (
    CHR_PLX_CONTINUOUS,
    KG_PER_LB,
    M_PER_INCH,
    SERVICE_BY_NAME,
    SERVICE_NAMES,
    SPECIAL_MEANING,
    BleakBackend,
    BLEHealthSensor,
    MedFloat,
    ScanResult,
    hrv_statistics,
    normalize_address,
    to_celsius,
    to_mmhg,
    utc_now,
    uuid16,
)
from labmcp_ble_health.simulator import SIM_DEFAULT_ADDRESS, SimulatedBLEBackend

_TRUE = {"1", "true", "yes", "on"}


def _make_backend(address: str | None, simulate: bool, options: dict[str, str]) -> Any:
    if simulate:
        return SimulatedBLEBackend(address)
    return BleakBackend(
        address,
        adapter=options.get("adapter") or None,
        pair=str(options.get("pair", "false")).lower() in _TRUE,
    )


def connect(ctx: ConnectContext) -> BLEHealthSensor:
    # The address is a BLE MAC (Linux/Windows) or a CoreBluetooth UUID (macOS), not a port,
    # so no labmcp transport is opened: the GATT link is owned by the backend.
    if ctx.simulate:
        address = normalize_address(ctx.address) if ctx.address else SIM_DEFAULT_ADDRESS
    else:
        address = normalize_address(ctx.require_address())
    backend = _make_backend(address, ctx.simulate, ctx.settings.options)
    timeout = float(ctx.option("connect_timeout_s") or ctx.settings.timeout or 20.0)
    return BLEHealthSensor(backend, address, audit=ctx.audit, connect_timeout_s=timeout)


server = InstrumentServer(
    "Bluetooth LE Health Sensors (GATT)",
    connect=connect,
    package="labmcp-ble-health",
    instructions="""
Reads Bluetooth LE sensors that implement the standard Bluetooth SIG health profiles (Heart
Rate, Pulse Oximeter, Blood Pressure, Health Thermometer, Weight Scale, Battery, Device
Information). RESEARCH / LAB-OPERATIONS USE ONLY: this is not a medical device. Never offer a
diagnosis, triage or treatment advice from these readings; refer health concerns to a clinician.
- If no device address is configured, use `scan_devices` (optionally filtered by service) and
  ask the user to restart the server with `--address <address>`.
- Heart-rate straps stream continuously: `record_heart_rate` records for N seconds and returns
  HRV statistics (5 minutes is the conventional short-term HRV window). HRV needs a sensor that
  sends RR intervals; most wrist optical sensors do not.
- Blood pressure monitors, thermometers, scales and spot-check oximeters only transmit when a
  measurement finishes: call the wait/read tool first, then ask the participant to take the
  measurement. The tool keeps (re)connecting until its timeout.
- Monitors may first send older stored measurements. Tools return the newest as the main
  result and list the others; check `device_timestamp` before using a value.
- Readings are personal data: do not attach participant names to them in your replies.
""",
    limits=[
        Limit("max_record_duration_s", 600, "s", "Longest heart-rate / oximetry recording"),
        Limit("max_wait_s", 300, "s", "Longest wait for a measurement (BP, temperature, weight, spot-check)"),
    ],
    address_help="""\
  AA:BB:CC:DD:EE:FF                            BLE MAC address (Linux, Windows)
  1A2B3C4D-1111-2222-3333-444455556666         CoreBluetooth peripheral UUID (macOS)
  (run the `scan_devices` tool, or start without --address, to discover devices)""",
    option_help={
        "pair": "true to pair/bond before connecting (Linux/Windows; macOS pairs on demand)",
        "adapter": "Bluetooth adapter to use on Linux, e.g. hci1 (default: system default)",
        "connect_timeout_s": "seconds to look for and connect to the device (default 20)",
    },
)
mcp = server.mcp

# ----------------------------------------------------------------------------- models


class FoundDevice(BaseModel):
    address: str = Field(description="Pass this to --address to use the device")
    name: str | None
    rssi_dbm: int | None = Field(description="Signal strength; closer to 0 is stronger")
    advertised_services: list[str] = Field(description="Standard health services listed in its advertisement")


class ScanReport(BaseModel):
    devices: list[FoundDevice]
    service_filter: str
    scanned_s: float
    timestamp: str
    note: str


class DeviceInfo(BaseModel):
    address: str
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    hardware_revision: str | None = None
    firmware_revision: str | None = None
    software_revision: str | None = None
    system_id: str | None = None
    services: list[str] = Field(description="Standard Bluetooth SIG services found on the device")
    features: dict[str, Any] = Field(description="Decoded feature characteristics (sensor location, supported flags, resolutions)")
    timestamp: str


class BatteryReading(BaseModel):
    battery_pct: int = Field(description="Battery Level characteristic (0-100 %)")
    address: str
    timestamp: str


class HeartRatePoint(BaseModel):
    t_s: float = Field(description="Seconds since the start of the recording")
    bpm: int
    sensor_contact: bool | None


class HRVStats(BaseModel):
    rr_count: int
    rr_excluded: int = Field(description="RR intervals outside 0.3-2.0 s treated as artefacts")
    mean_rr_ms: float | None
    mean_hr_bpm: float | None = Field(description="Mean heart rate computed from the RR intervals")
    sdnn_ms: float | None = Field(description="Standard deviation of RR intervals")
    rmssd_ms: float | None = Field(description="Root mean square of successive RR differences")
    pnn50_pct: float | None = Field(description="Percentage of successive RR differences > 50 ms")


class HeartRateRecording(BaseModel):
    address: str
    started_at: str
    duration_s: float
    notifications: int
    bpm_series: list[HeartRatePoint] = Field(description="Reported heart rate (downsampled to max_points)")
    bpm_mean: float | None
    bpm_min: int | None
    bpm_max: int | None
    rr_intervals_ms: list[float] = Field(description="RR intervals in ms (downsampled to max_points if longer)")
    rr_intervals_downsampled: bool
    hrv: HRVStats
    sensor_contact_pct: float | None = Field(description="Share of packets with skin contact (None if unsupported)")
    energy_expended_kj: int | None = Field(description="Last cumulative energy expended reported by the sensor")
    malformed_packets: int
    disconnected_early: bool
    saved_to: str | None
    notes: list[str]


class PulseOximetryReading(BaseModel):
    address: str
    mode: str = Field(description="continuous (PLX Continuous Measurement) or spot_check")
    spo2_pct: float | None = Field(description="Latest valid SpO2")
    pulse_rate_bpm: float | None = Field(description="Latest valid pulse rate")
    spo2_mean_pct: float | None
    spo2_min_pct: float | None
    pulse_rate_mean_bpm: float | None
    samples: int
    unavailable_samples: int = Field(description="Packets whose SpO2 or PR was a special value (NaN, NRes, ...)")
    pulse_amplitude_index_pct: float | None
    measurement_status: list[str] = Field(description="Measurement Status flags of the latest packet")
    device_sensor_status: list[str] = Field(description="Device and Sensor Status flags of the latest packet")
    device_timestamp: str | None = Field(description="Device clock time of a spot-check measurement")
    timestamp: str


class BloodPressureSummary(BaseModel):
    systolic_mmhg: float | None
    diastolic_mmhg: float | None
    mean_arterial_mmhg: float | None
    pulse_rate_bpm: float | None
    device_timestamp: str | None


class BloodPressureReading(BaseModel):
    address: str
    systolic_mmhg: float | None
    diastolic_mmhg: float | None
    mean_arterial_mmhg: float | None
    pulse_rate_bpm: float | None
    unit_reported: str = Field(description="Unit the device used (kPa values are converted to mmHg)")
    device_timestamp: str | None = Field(description="Measurement time from the device clock (device local time)")
    user_id: int | None
    measurement_status: list[str]
    special_values: list[str] = Field(description="Fields the device reported as NaN / NRes / INF")
    cuff_pressure_updates: int
    max_cuff_pressure_mmhg: float | None
    other_measurements: list[BloodPressureSummary] = Field(description="Older stored measurements received in the same session (oldest first)")
    received_at: str


class TemperatureReading(BaseModel):
    address: str
    temperature_c: float | None
    value_reported: float | None
    unit_reported: str = Field(description="'C' or 'F' as sent by the device")
    final: bool = Field(description="False if this is an Intermediate Temperature (probe still settling)")
    measurement_site: str | None
    device_timestamp: str | None
    special_value: str | None
    other_measurements_c: list[float | None]
    received_at: str


class WeightReading(BaseModel):
    address: str
    weight_kg: float | None = Field(description="None if the scale reported 'measurement unsuccessful'")
    value_reported: float | None
    unit_reported: str = Field(description="'kg' or 'lb' as sent by the scale")
    bmi_kg_m2: float | None
    height_m: float | None
    user_id: int | None
    device_timestamp: str | None
    scale_resolution_kg: float | None
    other_measurements_kg: list[float | None]
    received_at: str
    notes: list[str]


# ----------------------------------------------------------------------------- helpers


def _downsample(items: list[Any], max_points: int) -> list[Any]:
    if len(items) <= max_points:
        return list(items)
    step = (len(items) - 1) / (max_points - 1)
    return [items[round(i * step)] for i in range(max_points)]


def _special(label: str, value: MedFloat | None) -> list[str]:
    if value is not None and value.special:
        return [f"{label}: {value.special} ({SPECIAL_MEANING[value.special]})"]
    return []


def _scan_backend_results(timeout_s: float) -> list[ScanResult]:
    s = server.settings
    if s.simulate or s.address:
        return server.driver.scan(timeout_s)
    # No device configured yet: scan with a temporary backend so users can find the address.
    backend = _make_backend(None, False, s.options)
    try:
        server.audit.event(f"scan {timeout_s:g} s", "ble")
        return backend.scan(timeout_s)
    finally:
        backend.close()


# ----------------------------------------------------------------------------- tools

ServiceFilter = Literal["any", "heart_rate", "pulse_oximeter", "blood_pressure", "health_thermometer", "weight_scale"]


@mcp.tool(**READ, timeout=60)
def scan_devices(
    timeout_s: Annotated[float, Field(ge=1, le=30, description="How long to listen for advertisements")] = 8.0,
    service: Annotated[ServiceFilter, Field(description="Only list devices advertising this service")] = "any",
    name_contains: Annotated[str | None, Field(max_length=40, description="Case-insensitive name filter")] = None,
) -> ScanReport:
    """Scan for nearby Bluetooth LE devices: address, name, signal strength and advertised health
    services. Works without a configured --address.

    Devices only show up while advertising (monitors often advertise only right after a
    measurement), and some do not list every service in their advertisement, so `service`
    filtering can miss them."""
    found = _scan_backend_results(timeout_s)
    wanted = uuid16(SERVICE_BY_NAME[service]) if service != "any" else None
    devices = []
    for r in sorted(found, key=lambda r: -(r.rssi_dbm if r.rssi_dbm is not None else -999)):
        if wanted and wanted not in r.service_uuids:
            continue
        if name_contains and name_contains.lower() not in (r.name or "").lower():
            continue
        names = [n for code, n in SERVICE_NAMES.items() if uuid16(code) in r.service_uuids]
        devices.append(FoundDevice(address=r.address, name=r.name, rssi_dbm=r.rssi_dbm, advertised_services=names))
    return ScanReport(
        devices=devices,
        service_filter=service,
        scanned_s=timeout_s,
        timestamp=utc_now(),
        note="On macOS addresses are CoreBluetooth UUIDs that are specific to this computer.",
    )


@mcp.tool(**READ, timeout=90)
def get_device_info() -> DeviceInfo:
    """Read the device's identity (manufacturer, model, serial, firmware), the standard health
    services it exposes and its features.

    Features include the body sensor location, supported oximeter / blood-pressure status flags,
    scale resolution and thermometer site."""
    info = server.driver.device_info()
    return DeviceInfo(**info, timestamp=utc_now())


@mcp.tool(**READ, timeout=90)
def read_battery() -> BatteryReading:
    """Read the device's battery level (Battery Service, 0-100 %)."""
    level = server.driver.battery_level()
    return BatteryReading(battery_pct=level, address=server.driver.address, timestamp=utc_now())


@mcp.tool(**READ, timeout=3720)
def record_heart_rate(
    duration_s: Annotated[float, Field(ge=5, le=3600, description="Recording length in seconds")] = 60.0,
    max_points: Annotated[int, Field(ge=10, le=5000, description="Max points returned per series")] = 300,
    save_path: Annotated[str | None, Field(description="Optional CSV path for the full recording")] = None,
) -> HeartRateRecording:
    """Record heart rate for `duration_s`: bpm series, RR intervals and time-domain HRV (mean HR,
    SDNN, RMSSD, pNN50).

    The sensor must be worn with good skin contact. HRV statistics are for research, not clinical
    ECG analysis. Use `save_path` to keep every packet as CSV."""
    server.check("max_record_duration_s", duration_s, "recording duration")
    driver = server.driver
    started = utc_now()
    samples, malformed, disconnected = driver.record_heart_rate(duration_s)
    notes: list[str] = []
    if not samples:
        raise InstrumentProtocolError(
            f"No heart-rate notifications received in {duration_s:g} s. Is the strap worn (wet the "
            "electrodes) and not connected to another app?"
        )
    rr_s = [rr for _, m in samples for rr in m.rr_intervals_s]
    if not rr_s:
        notes.append("The sensor sent no RR intervals, so HRV statistics are unavailable.")
    contacts = [m.sensor_contact for _, m in samples if m.sensor_contact is not None]
    energies = [m.energy_expended_kj for _, m in samples if m.energy_expended_kj is not None]
    # bpm statistics ignore packets without skin contact or with a 0 bpm placeholder.
    bpms = [m.bpm for _, m in samples if m.bpm > 0 and m.sensor_contact is not False]
    if len(bpms) < len(samples):
        notes.append(f"{len(samples) - len(bpms)} packet(s) without skin contact / 0 bpm excluded from bpm statistics.")
    if disconnected:
        notes.append("The sensor disconnected before the recording finished; data is partial.")
    if malformed:
        notes.append(f"{malformed} malformed packet(s) were skipped.")
    saved_to = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["t_s", "bpm", "sensor_contact", "energy_expended_kj", "rr_intervals_ms"])
            for t, m in samples:
                w.writerow([f"{t:.3f}", m.bpm, m.sensor_contact, m.energy_expended_kj,
                            ";".join(f"{rr * 1000:.1f}" for rr in m.rr_intervals_s)])
        saved_to = str(path)
    rr_ms = [round(rr * 1000.0, 1) for rr in rr_s]
    return HeartRateRecording(
        address=driver.address,
        started_at=started,
        duration_s=round(samples[-1][0], 2) if disconnected else duration_s,
        notifications=len(samples),
        bpm_series=[
            HeartRatePoint(t_s=round(t, 2), bpm=m.bpm, sensor_contact=m.sensor_contact)
            for t, m in _downsample(samples, max_points)
        ],
        bpm_mean=round(statistics.fmean(bpms), 1) if bpms else None,
        bpm_min=min(bpms) if bpms else None,
        bpm_max=max(bpms) if bpms else None,
        rr_intervals_ms=_downsample(rr_ms, max_points),
        rr_intervals_downsampled=len(rr_ms) > max_points,
        hrv=HRVStats(**hrv_statistics(rr_s)),
        sensor_contact_pct=round(100.0 * sum(contacts) / len(contacts), 1) if contacts else None,
        energy_expended_kj=energies[-1] if energies else None,
        malformed_packets=malformed,
        disconnected_early=disconnected,
        saved_to=saved_to,
        notes=notes,
    )


@mcp.tool(**READ, timeout=1860)
def read_pulse_oximetry(
    mode: Annotated[
        Literal["auto", "continuous", "spot_check"],
        Field(description="auto: continuous if the oximeter supports it, else wait for a spot-check"),
    ] = "auto",
    duration_s: Annotated[float, Field(ge=1, le=600, description="Continuous mode: seconds to average over")] = 10.0,
    timeout_s: Annotated[float, Field(ge=5, le=1800, description="Spot-check mode: seconds to wait")] = 60.0,
) -> PulseOximetryReading:
    """Read SpO2 (%) and pulse rate from a pulse oximeter, averaged over a few seconds or as a
    single spot-check reading.

    Continuous mode listens for `duration_s` and returns the latest valid values plus mean/min;
    spot-check mode waits for the oximeter's single stable reading. Early packets are often NaN
    while the sensor acquires; these are counted in `unavailable_samples`, never reported as values."""
    if mode in ("auto", "spot_check"):
        server.check("max_wait_s", timeout_s, "spot-check wait time")
    if mode in ("auto", "continuous"):
        server.check("max_record_duration_s", duration_s, "continuous measurement duration")
    driver = server.driver
    driver.ensure_connected()
    use = mode if mode != "auto" else ("continuous" if driver.has(CHR_PLX_CONTINUOUS) else "spot_check")
    ms = driver.pulse_oximetry(use, duration_s, timeout_s)
    valid = [m for m in ms if m.spo2_pct.value is not None and m.pulse_rate_bpm.value is not None]
    latest = valid[-1] if valid else ms[-1]
    spo2 = [m.spo2_pct.value for m in valid if m.spo2_pct.value is not None]
    prs = [m.pulse_rate_bpm.value for m in valid if m.pulse_rate_bpm.value is not None]
    pai = latest.pulse_amplitude_index_pct
    return PulseOximetryReading(
        address=driver.address,
        mode=use,
        spo2_pct=latest.spo2_pct.value,
        pulse_rate_bpm=latest.pulse_rate_bpm.value,
        spo2_mean_pct=round(statistics.fmean(spo2), 1) if spo2 else None,
        spo2_min_pct=min(spo2) if spo2 else None,
        pulse_rate_mean_bpm=round(statistics.fmean(prs), 1) if prs else None,
        samples=len(ms),
        unavailable_samples=len(ms) - len(valid),
        pulse_amplitude_index_pct=pai.value if pai else None,
        measurement_status=latest.measurement_status,
        device_sensor_status=latest.device_sensor_status,
        device_timestamp=latest.timestamp,
        timestamp=utc_now(),
    )


@mcp.tool(**READ, timeout=1860)
def wait_for_blood_pressure(
    timeout_s: Annotated[float, Field(ge=5, le=1800, description="Seconds to wait for a measurement")] = 120.0,
) -> BloodPressureReading:
    """Wait for a blood pressure monitor to send a measurement: systolic, diastolic and mean
    arterial pressure (mmHg) and pulse rate.

    Call this, then ask the participant to start a measurement on the monitor. kPa readings are
    converted to mmHg; the unit sent is reported. Older stored readings are listed separately."""
    server.check("max_wait_s", timeout_s, "wait time")
    driver = server.driver
    measurements, cuff = driver.blood_pressure(timeout_s)
    m = measurements[-1]
    cuff_values = [to_mmhg(c.systolic, c.unit) for c in cuff]
    cuff_values = [v for v in cuff_values if v is not None]
    special = (
        _special("systolic", m.systolic)
        + _special("diastolic", m.diastolic)
        + _special("mean_arterial", m.mean_arterial)
        + _special("pulse_rate", m.pulse_rate_bpm)
    )
    return BloodPressureReading(
        address=driver.address,
        systolic_mmhg=to_mmhg(m.systolic, m.unit),
        diastolic_mmhg=to_mmhg(m.diastolic, m.unit),
        mean_arterial_mmhg=to_mmhg(m.mean_arterial, m.unit),
        pulse_rate_bpm=m.pulse_rate_bpm.value if m.pulse_rate_bpm else None,
        unit_reported=m.unit,
        device_timestamp=m.timestamp,
        user_id=m.user_id,
        measurement_status=m.status,
        special_values=special,
        cuff_pressure_updates=len(cuff),
        max_cuff_pressure_mmhg=max(cuff_values) if cuff_values else None,
        other_measurements=[
            BloodPressureSummary(
                systolic_mmhg=to_mmhg(o.systolic, o.unit),
                diastolic_mmhg=to_mmhg(o.diastolic, o.unit),
                mean_arterial_mmhg=to_mmhg(o.mean_arterial, o.unit),
                pulse_rate_bpm=o.pulse_rate_bpm.value if o.pulse_rate_bpm else None,
                device_timestamp=o.timestamp,
            )
            for o in measurements[:-1]
        ],
        received_at=utc_now(),
    )


@mcp.tool(**READ, timeout=1860)
def read_temperature(
    timeout_s: Annotated[float, Field(ge=5, le=1800, description="Seconds to wait for a measurement")] = 60.0,
    accept_intermediate: Annotated[
        bool, Field(description="Also accept Intermediate Temperature values (probe still settling)")
    ] = False,
) -> TemperatureReading:
    """Wait for a thermometer to send a temperature measurement and return it in °C.

    °F readings are converted; the measurement site is included if the device reports one."""
    server.check("max_wait_s", timeout_s, "wait time")
    driver = server.driver
    readings, sensor_type = driver.temperature(timeout_s, accept_intermediate)
    finals = [m for is_final, m in readings if is_final]
    is_final = bool(finals)
    m = finals[-1] if finals else readings[-1][1]
    others = [to_celsius(o.value, o.unit) for o in (finals[:-1] if finals else [])]
    return TemperatureReading(
        address=driver.address,
        temperature_c=to_celsius(m.value, m.unit),
        value_reported=m.value.value,
        unit_reported=m.unit,
        final=is_final,
        measurement_site=m.temperature_type or sensor_type,
        device_timestamp=m.timestamp,
        special_value=m.value.special,
        other_measurements_c=others,
        received_at=utc_now(),
    )


@mcp.tool(**READ, timeout=1860)
def read_weight(
    timeout_s: Annotated[float, Field(ge=5, le=1800, description="Seconds to wait for a measurement")] = 60.0,
) -> WeightReading:
    """Wait for a scale to send a weight measurement (kg), with BMI and height if the scale sends
    them.

    Ask the person to step on the scale after calling this. lb readings are converted to kg."""
    server.check("max_wait_s", timeout_s, "wait time")
    driver = server.driver
    measurements, features = driver.weight(timeout_s)
    m = measurements[-1]
    notes: list[str] = []

    def kg(w: float | None, unit: str) -> float | None:
        if w is None:
            return None
        return round(w * KG_PER_LB, 3) if unit == "lb" else w

    if m.weight is None:
        notes.append("The scale reported 'measurement unsuccessful' (0xFFFF).")
    height_m = None
    if m.height is not None:
        height_m = round(m.height * M_PER_INCH, 3) if m.height_unit == "in" else m.height
    return WeightReading(
        address=driver.address,
        weight_kg=kg(m.weight, m.unit),
        value_reported=m.weight,
        unit_reported=m.unit,
        bmi_kg_m2=m.bmi_kg_m2,
        height_m=height_m,
        user_id=m.user_id,
        device_timestamp=m.timestamp,
        scale_resolution_kg=(features or {}).get("weight_resolution_kg"),
        other_measurements_kg=[kg(o.weight, o.unit) for o in measurements[:-1]],
        received_at=utc_now(),
        notes=notes,
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
