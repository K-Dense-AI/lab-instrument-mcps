# LabMCP: MCP Servers for Lab Instruments

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/Built_with-FastMCP_4-6E56CF.svg)](https://gofastmcp.com)
[![MCP](https://img.shields.io/badge/Protocol-Model_Context_Protocol-000000.svg)](https://modelcontextprotocol.io)
<!-- COUNTS:START -->
[![Servers](https://img.shields.io/badge/Servers-28-brightgreen.svg)](#-server-catalog) [![Tools](https://img.shields.io/badge/Tools-354-blue.svg)](#-server-catalog)
<!-- COUNTS:END -->
[![CI](https://github.com/K-Dense-AI/lab-instrument-mcps/actions/workflows/ci.yml/badge.svg)](https://github.com/K-Dense-AI/lab-instrument-mcps/actions/workflows/ci.yml)
[![Works with](https://img.shields.io/badge/Works_with-Claude_|_Cursor_|_VS_Code_|_Codex-blue.svg)](docs/clients.md)

**Open-source [Model Context Protocol](https://modelcontextprotocol.io) servers that let AI agents operate real laboratory instruments** (balances, hotplate stirrers, syringe pumps, potentiostats, lock-in amplifiers, cryostat controllers, vacuum gauges, flow controllers, biosensors, clinical analyzers and more), with safety limits, read-only modes, audit trails, and a simulator for every instrument.

Built by [K-Dense](https://www.k-dense.ai) on [FastMCP](https://gofastmcp.com). Every server covers an instrument family that **has no official MCP server** from its vendor.

```text
You:     Tare the balance, then heat the stirrer to 60 °C at 400 rpm and log the pH every 30 s
         until it's stable. Tell me when to add the reagent.
Agent:   → balance.tare()  → stirrer.set_speed(400)  → stirrer.set_temperature(60)
         → ph.log_series(...)  …  "pH has been 7.41 ± 0.01 for 3 min. Ready for the reagent."
```

> ⭐ **Help make AI for the lab easier to find.** If LabMCP connects your instruments to your agent, please star the repo. It helps other scientists find it.

---

## 📋 Contents

- [Quick start (no hardware needed)](#-quick-start-no-hardware-needed)
- [Server catalog](#-server-catalog)
- [What every server gives you](#-what-every-server-gives-you)
- [Connect a real instrument](#-connect-a-real-instrument)
- [Safety](#%EF%B8%8F-safety)
- [Example workflows](#-example-workflows)
- [How it's organised](#-how-its-organised)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)
- [FAQ](#-faq)

---

## 🚀 Quick start (no hardware needed)

Every server ships with a **wire-level simulator**, so you can try it before plugging anything in.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (one line; it provides `uvx`).
2. Add a simulated instrument to your client:

   **Claude Code**
   ```bash
   claude mcp add balance -- uvx labmcp-mettler-toledo --simulate
   ```

   **Claude Desktop / Cursor / Windsurf** (`mcpServers` config)
   ```json
   {
     "mcpServers": {
       "balance": { "command": "uvx", "args": ["labmcp-mettler-toledo", "--simulate"] }
     }
   }
   ```
3. Ask: *"What instrument is connected? Tare it and log the weight every second for 10 seconds."*

Then swap `--simulate` for `--address /dev/ttyUSB0` (or `COM3`, or `tcp://192.168.1.50:5025`) to talk to the real instrument. See [docs/clients.md](docs/clients.md) for VS Code, Codex, and HTTP setups.

**Want a whole lab?** [`examples/`](examples/) sets up a simulated wet lab of six instruments (balance, stirrer, pH probe, syringe pump, potentiostat, spectrometer) in one step.

---

## 📦 Server catalog

Status: **🧪 simulated** = built from the vendor's published protocol and tested against a wire-level simulator, awaiting hardware reports · **✅ hardware-verified** = confirmed on real instruments by the community ([help verify one!](CONTRIBUTING.md#1-test-a-server-on-real-hardware--most-needed)).

<!-- CATALOG:START -->
### 🧬 Biology & Life Sciences

| Server | Instruments | Interface | Tools | Status | Install |
|---|---|---|---|---|---|
| [**Atlas Scientific EZO Sensors**](servers/biology/atlas-ezo-sensors)<br><sub>Read pH, ORP, dissolved oxygen, conductivity/TDS/salinity, temperature, humidity, CO2 or pressure; calibrate, set temperature compensation, log series.</sub> | Atlas Scientific: EZO-pH, EZO-ORP, EZO-DO, EZO-EC … | UART (USB-serial adapter), Ethernet (serial bridge) | 13 | 🧪 simulated | `uvx labmcp-atlas-ezo` |
| [**Micro-Manager Microscope**](servers/biology/micro-manager)<br><sub>Snap images (stats + preview + TIFF), z-stacks and time-lapses, move XY/Z within step and soft limits, channel/objective presets, exposure, shutter, autofocus.</sub> | Micro-Manager (open source; hundreds of camera, stage, filter and light-source vendors): Any microscope with a Micro-Manager hardware configuration (.cfg), Micro-Manager demo configuration | USB, Serial, Camera Link / CoaXPress / GigE (through Micro-Manager device adapters) | 19 | 🧪 simulated | `uvx labmcp-micro-manager` |
| [**New Era Syringe Pump**](servers/biology/new-era-syringe-pump)<br><sub>Set the syringe (presets or diameter), infuse or withdraw an exact volume at a set rate, track dispensed volume, stop.</sub> | New Era Pump Systems: NE-1000, NE-1002X, NE-1010, NE-4000 … | RS-232, USB (RS-232 adapter), Serial-to-Ethernet | 11 | 🧪 simulated | `uvx labmcp-new-era` |
| [**Opentrons OT-2 / Flex**](servers/biology/opentrons)<br><sub>Upload and analyze protocols, review deck layout, start/pause/stop/resume runs, track progress, home, lights, read pipettes and modules, switch modules off.</sub> | Opentrons: Flex, OT-2 | Ethernet, Wi-Fi, USB (network over USB) | 18 | 🧪 simulated | `uvx labmcp-opentrons` |
| [**Tecan Cavro Syringe Pump**](servers/biology/tecan-cavro-pump)<br><sub>Initialize, aspirate and dispense microlitre volumes at set flow rates, switch valve ports, read plunger/valve status, terminate moves.</sub> | Tecan: Cavro XLP 6000, Cavro XMP 6000, Cavro XCalibur | RS-232, RS-485 (via adapter), Serial-to-Ethernet | 9 | 🧪 simulated | `uvx labmcp-cavro` |

### ⚗️ Chemistry

| Server | Instruments | Interface | Tools | Status | Install |
|---|---|---|---|---|---|
| [**IKA Hotplate & Overhead Stirrer**](servers/chemistry/ika-stirrer)<br><sub>Read plate/probe temperature and speed, set and start heating and stirring, hardware watchdog, wait for temperature, stop all.</sub> | IKA: C-MAG HS 7 control, IKA Plate (RCT digital), EUROSTAR 60 control, EUROSTAR 100 control | RS-232, USB (virtual COM) | 14 | 🧪 simulated | `uvx labmcp-ika` |
| [**JULABO Circulator**](servers/chemistry/julabo-circulator)<br><sub>Read bath/external temperature and heating power, set setpoint, start/stop tempering, decode status and alarms, wait for temperature.</sub> | JULABO: CORIO CD, CORIO CP, MAGIO MS, DYNEO DD | USB (virtual COM), RS-232 | 10 | 🧪 simulated | `uvx labmcp-julabo` |
| [**Mettler Toledo Balance**](servers/chemistry/mettler-toledo-balance)<br><sub>Weigh (stable/immediate), tare, zero, log drift series, internal adjustment, draft shield, display messages.</sub> | Mettler Toledo: Excellence XPR/XSR, XP/XS, XA/XE, MS/ML … | RS-232, USB (virtual COM), Ethernet | 17 | 🧪 simulated | `uvx labmcp-mettler-toledo` |
| [**Ocean Insight Spectrometer**](servers/chemistry/ocean-spectrometer)<br><sub>Acquire averaged/smoothed spectra with saturation checks, auto integration time, dark and reference storage, absorbance and transmittance spectra, peak positions with FWHM, detector TEC (QE Pro).</sub> | Ocean Insight (Ocean Optics): USB2000+, USB2000, USB4000, USB650 … | USB | 16 | 🧪 simulated | `uvx labmcp-ocean-spectrometer` |
| [**PalmSens Potentiostat**](servers/chemistry/palmsens-potentiostat)<br><sub>Cyclic, linear-sweep and differential-pulse voltammetry, chronoamperometry and raw MethodSCRIPT, with peak summaries and CSV export.</sub> | PalmSens: EmStat Pico, EmStat4 LR/HR (incl. EmStat4S/4M/4X), Sensit Wearable / Sensit BT, Nexus | USB (virtual COM port), UART / RS-232 | 10 | 🧪 simulated | `uvx labmcp-palmsens` |
| [**Sartorius Balance**](servers/chemistry/sartorius-balance)<br><sub>Weigh (stable/immediate), tare, zero, log drift series, internal adjustment, ambient filter, keypad lock.</sub> | Sartorius: Cubis MSE, Cubis II MCA, Secura, Quintix … | RS-232, USB (virtual COM), Ethernet (Cubis II) | 10 | 🧪 simulated | `uvx labmcp-sartorius` |

### 🔭 Physics & Optics

| Server | Instruments | Interface | Tools | Status | Install |
|---|---|---|---|---|---|
| [**Keithley SourceMeter SMU**](servers/physics/keithley-smu)<br><sub>Configure a voltage or current source with compliance, switch the output on/off, measure V and I, run bounded linear/log/dual IV sweeps with resistance fit and CSV export, 2/4-wire sense.</sub> | Keithley (Tektronix): 2400, 2401, 2410, 2420 … | GPIB, USB (USBTMC), Ethernet (LXI / raw socket), RS-232 | 12 | 🧪 simulated | `uvx labmcp-keithley-smu` |
| [**Lake Shore Temperature Controller**](servers/physics/lakeshore-temperature)<br><sub>Read all sensor inputs with status, heater status, set setpoints/ramps/heater ranges/PID, wait for stable temperature, all heaters off.</sub> | Lake Shore Cryotronics: Model 335, Model 336, Model 350 | USB (virtual COM), Ethernet (TCP 7777, 336/350), IEEE-488 (GPIB) | 11 | 🧪 simulated | `uvx labmcp-lakeshore` |
| [**Pfeiffer Vacuum Gauge Controller**](servers/physics/pfeiffer-vacuum-gauge)<br><sub>Read and log pressures with status per channel, identify gauges, change units, switch ionisation gauges on/off with a pressure interlock, read errors.</sub> | Pfeiffer Vacuum: TPG 361, TPG 362, TPG 366 MaxiGauge, TPG 261 … | USB (virtual COM), Ethernet (TCP 8000), RS-232 (TPG 26x) | 11 | 🧪 simulated | `uvx labmcp-pfeiffer-tpg` |
| [**SRS Lock-in Amplifier**](servers/physics/srs-lockin)<br><sub>Read X/Y/R/θ snapshots, set reference frequency/harmonic/phase, sine amplitude, sensitivity, time constant, auto phase/gain/range, bounded frequency sweeps.</sub> | Stanford Research Systems: SR830, SR810, SR860, SR865A | GPIB, RS-232, USB (USBTMC), Ethernet (VXI-11) | 15 | 🧪 simulated | `uvx labmcp-srs-lockin` |
| [**Thorlabs Optical Power Meter**](servers/physics/thorlabs-power-meter)<br><sub>Read optical power (W and dBm), log power stability series, set wavelength correction, averaging and range, dark-zero the sensor, read sensor head temperature.</sub> | Thorlabs: PM100D, PM100A, PM100USB, PM400 … | USB (USBTMC / VISA) | 11 | 🧪 simulated | `uvx labmcp-thorlabs-pm` |

### 🩺 Health & Biosignals

| Server | Instruments | Interface | Tools | Status | Install |
|---|---|---|---|---|---|
| [**ASTM LIS Analyzer Receiver**](servers/health/astm-lis-analyzer)<br><sub>Receive-only LIS: ACK/NAK frames with checksum validation, reassemble messages, and query results by sample, patient, test or time. Patient name/DOB redacted by default. Research use only.</sub> | Any (ASTM E1381/E1394, CLSI LIS1-A/LIS2-A2): Hematology analyzers, Clinical chemistry analyzers, Immunoassay analyzers, Urinalysis and coagulation analyzers … | RS-232, TCP/IP (client), TCP/IP (listener) | 8 | 🧪 simulated | `uvx labmcp-astm-lis` |
| [**Bluetooth LE Health Sensors**](servers/health/ble-health-sensors)<br><sub>Scan, read device info/battery, record heart rate with RR intervals and HRV, read SpO2, blood pressure, temperature and weight. Research use only, not a medical device.</sub> | Any (Bluetooth SIG standard profiles): Heart-rate chest straps and armbands (Heart Rate Service), Pulse oximeters (Pulse Oximeter Service), Blood pressure monitors (Blood Pressure Service), Thermometers (Health Thermometer Service) … | Bluetooth Low Energy | 11 | 🧪 simulated | `uvx labmcp-ble-health` |
| [**BrainFlow Biosensing Boards**](servers/health/brainflow-biosensors)<br><sub>List boards, stream, record N seconds (stats + downsampled traces + CSV), EEG band powers, event markers, per-channel signal quality (flat, railed, mains noise). Research use only.</sub> | OpenBCI, Interaxon (Muse), Neurosity, g.tec, BrainBit, Mentalab, EmotiBit and others (via BrainFlow): OpenBCI Cyton / Cyton+Daisy / Ganglion / Galea (USB, BLE, WiFi Shield), Muse 2 / Muse S / Muse S Athena / Muse 2016, Neurosity Crown / Notion 1 / Notion 2, g.tec Unicorn Hybrid Black … | USB (serial dongle), Bluetooth LE, WiFi, Vendor SDK (via BrainFlow) | 12 | 🧪 simulated | `uvx labmcp-brainflow` |

### ⚙️ Engineering & Data Acquisition

| Server | Instruments | Interface | Tools | Status | Install |
|---|---|---|---|---|---|
| [**Alicat Mass Flow & Pressure Controller**](servers/engineering/alicat-flow-controller)<br><sub>Read mass/volumetric flow, pressure and temperature, set setpoints, select gases, tare, hold or close valves, log flow series.</sub> | Alicat Scientific: MC/MCR/MCS/MCE/MCV/MCW mass flow controllers, M/MS/MW mass flow meters, PC/PCD pressure controllers, P pressure gauges … | RS-232, RS-485, USB (virtual COM), Ethernet (serial bridge) | 14 | 🧪 simulated | `uvx labmcp-alicat` |
| [**Bench DC Power Supply**](servers/engineering/bench-power-supply)<br><sub>Read setpoints and measured V/I/CV-CC per channel, set voltage and current limits, switch outputs on/off, set OVP/OCP, read errors.</sub> | Rigol, Siglent, Aim-TTi: Rigol DP832/DP831/DP822/DP821/DP811/DP813, Rigol DP711/DP712, Rigol DP932A/U/E, Siglent SPD3303X/X-E … | USB (USB-TMC / virtual COM), LAN (VISA / raw socket), RS-232, GPIB (via VISA) | 12 | 🧪 simulated | `uvx labmcp-bench-psu` |
| [**LabJack T-series DAQ**](servers/engineering/labjack)<br><sub>Read analog inputs, stream waveforms, read thermocouples and the device temperature, read/set digital I/O and set DACs, with voltage and stream limits.</sub> | LabJack: T4, T7, T7-Pro, T8 | USB, Ethernet, WiFi (T7-Pro) | 12 | 🧪 simulated | `uvx labmcp-labjack` |
| [**NI-DAQmx DAQ Devices**](servers/engineering/ni-daqmx)<br><sub>List devices, acquire finite analog voltage and thermocouple data, read/write digital lines and set analog outputs, with voltage, rate and sample limits.</sub> | National Instruments (NI): USB-6001/6002/6003, USB-6008/6009, USB-621x, USB/PCIe-63xx (M/X series) … | USB, PCI/PCIe, PXI, Ethernet (cDAQ) | 11 | 🧪 simulated | `uvx labmcp-ni-daqmx` |
| [**Rigol Oscilloscope**](servers/engineering/rigol-oscilloscope)<br><sub>Read settings, autoscale, run/stop/single, set channels, timebase and edge trigger, take measurements, capture scaled waveforms (screen or deep memory) and screenshots.</sub> | Rigol: DS1000Z / MSO1000Z (DS1054Z, DS1104Z...), DS1000Z-E (DS1202Z-E), MSO5000, DHO800 / DHO900 … | USB (USB-TMC), LAN (VXI-11 / raw socket) | 16 | 🧪 simulated | `uvx labmcp-rigol-scope` |

### 🔌 Universal Protocols (many instruments)

| Server | Instruments | Interface | Tools | Status | Install |
|---|---|---|---|---|---|
| [**EPICS Channel Access**](servers/protocols/epics)<br><sub>Read PVs with units, precision, alarms, timestamps and limits; monitor PVs for a bounded time; guarded writes (allow-list regex, access rights, DRVH/DRVL control limits, put-callback completion); configurable safe-state action.</sub> | EPICS collaboration (any EPICS IOC): Any EPICS IOC serving Channel Access (EPICS Base 3.14-7.x, areaDetector, motor, asyn, PyDevice, caproto IOCs), CA gateways | Ethernet (CA over UDP/TCP 5064/5065) | 10 | 🧪 simulated | `uvx labmcp-epics` |
| [**Generic SCPI Instrument**](servers/protocols/scpi-instrument)<br><sub>Identify, query, write and batch raw SCPI with a query-only read path, configurable command allow/deny lists, error-queue checks, binary block transfer and a user-defined safe state.</sub> | Any (SCPI / IEEE 488.2): Digital multimeters, Oscilloscopes, Source-measure units, DC power supplies and electronic loads … | GPIB (VISA), USBTMC (VISA), LAN VXI-11 / HiSLIP (VISA), LAN raw socket (e.g. port 5025), RS-232 / USB-serial | 15 | 🧪 simulated | `uvx labmcp-scpi` |
| [**Modbus TCP/RTU Device**](servers/protocols/modbus)<br><sub>Read and write named, typed, scaled points from a YAML/JSON register map with per-point min/max, raw register/coil access, and a map-defined safe state.</sub> | Any (Modbus): PID temperature controllers (e.g. Watlow, Eurotherm, Omega, Autonics), Recirculating chillers and thermostats, PLCs and remote I/O, Process sensors and transmitters … | Ethernet (Modbus TCP), RS-485 / RS-232 (Modbus RTU, ASCII), Serial-to-Ethernet gateways | 13 | 🧪 simulated | `uvx labmcp-modbus` |
| [**SiLA 2 Bridge**](servers/protocols/sila2)<br><sub>Discover SiLA servers, read features/commands/properties with typed schemas from the FDL, read and subscribe to properties, run commands with client-side FDL validation and an allow-list, track observable commands, cancel via CancelController.</sub> | SiLA consortium standard (any SiLA 2 server): Any SiLA 2 server: devices and middleware with SiLA 2 interfaces (liquid handlers, incubators, readers, robots, schedulers), sila_python, sila_java, sila_csharp, sila_cpp and vendor SiLA 2 servers | Ethernet (gRPC over HTTP/2, TLS), mDNS discovery | 13 | 🧪 simulated | `uvx labmcp-sila2` |
<!-- CATALOG:END -->

Browse from the terminal: `uvx labmcp list`, `uvx labmcp info <server>`.

**Looking for something else?** [Request an instrument](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=instrument-request.yml), or check [instruments with official vendor MCP servers](docs/official-servers.md).

---

## 🧰 What every server gives you

| Feature | How |
|---|---|
| **Simulator** | `--simulate` runs the full server against a simulator that speaks the instrument's real wire protocol, for demos, CI, and rehearsing workflows. |
| **Read-only mode** | `--read-only` hides every tool that changes instrument state. Stop/off tools stay available. |
| **Safety limits** | Refuse dangerous setpoints *before* anything is sent: `--limit max_temperature_c=80`. |
| **Hazard annotations** | Tools that heat, move, dispense, or energise carry MCP `destructiveHint`, so clients ask you first. |
| **Audit trail** | Every command and reply is logged. `get_command_log` shows it, and `--audit-log run.jsonl` keeps it. |
| **Self-describing** | `get_connection_info` reports the instrument's identity, whether it's simulated, and the active limits. |
| **Helpful errors** | "Balance replied `S +`: overload, too much weight on the pan", not a stack trace. |
| **Pre-flight check** | `uvx labmcp-<server> --address … --check` tests the connection from a terminal. |
| **Any transport** | RS-232/USB serial, TCP/LAN, and VISA (GPIB, USBTMC, LXI, HiSLIP), all set with one `--address`. |
| **Any client** | Standard MCP over stdio (or streamable HTTP): Claude, Cursor, VS Code, Codex, Windsurf, and others. |

---

## 🔌 Connect a real instrument

```bash
uvx labmcp ports                                              # find serial ports / VISA resources
uvx labmcp-mettler-toledo --address /dev/ttyUSB0 --check      # test the connection
uvx labmcp config mettler-toledo --address /dev/ttyUSB0       # print a client config snippet
```

| Address | Meaning |
|---|---|
| `/dev/ttyUSB0`, `/dev/tty.usbserial-XXXX`, `COM3` | Serial port with the instrument's factory settings |
| `serial:///dev/ttyUSB0?baudrate=19200&parity=E` | Serial with overrides (`baudrate`, `parity`, `bytesize`, `stopbits`, `xonxoff`, `rtscts`, `timeout`) |
| `tcp://192.168.1.50:5025` | LAN instrument or serial-to-Ethernet adapter |
| `visa://TCPIP0::192.168.1.50::INSTR`, `GPIB0::22::INSTR`, `USB0::…::INSTR` | VISA resources (servers built on SCPI) |

Each server's README lists the instrument-side settings (interface mode, baud rate, and so on).

---

## 🛡️ Safety

AI agents operating physical equipment need guardrails. LabMCP has several layers of them, and **you stay in charge**:

1. **Rehearse in `--simulate`**, then **start `--read-only`**, and grant control only when needed.
2. **Set limits for your experiment**, e.g. `--limit max_temperature_c=60` below your solvent's boiling point.
3. **Keep a human in the loop** for ⚠️ hazard tools. Don't auto-approve them in your client.
4. Keep the instrument's **own** interlocks and cut-offs on. Software limits are extra protection. They don't replace hardware safety.

Read **[SAFETY.md](SAFETY.md)** before connecting anything hazardous. LabMCP is a research tool, not a certified safety system or medical device.

---

## 🧪 Example workflows

Run several servers side by side and the agent can coordinate them:

- **Chemistry:** *"Weigh 250 mg ± 2 mg of salt into the vial: tell me how much to add, reading the balance every few seconds. Then set the stirrer to 500 rpm and 40 °C, and tell me when it's dissolved and at temperature."*
- **Electrochemistry:** *"Run cyclic voltammograms from −0.2 to 0.6 V at 25, 50, 100 and 200 mV/s, extract the anodic peak currents, and check whether they scale with the square root of scan rate."*
- **Physics:** *"Sweep the lock-in reference from 1 to 100 kHz in 40 log-spaced steps and fit the resonance. Then ramp the cryostat to 10 K at 2 K/min and repeat at 50 K and 100 K."*
- **Biology:** *"Infuse 2 mL of media at 0.5 mL/min, then log the bioreactor pH and dissolved oxygen every minute for an hour and flag any drift."*
- **Engineering:** *"Step the flow controller through 10, 20 and 50 sccm of N₂ and log the upstream pressure at each setpoint. Stop and close the valve if pressure goes above 30 psia."*
- **Clinical lab operations:** *"Summarise today's CBC results received from the analyzer and list any samples with flagged results."*

---

## 🗂 How it's organised

```
lab-instrument-mcps/
├── packages/labmcp/            # shared core: transports, simulator base, safety, audit, CLI
├── servers/
│   ├── biology/                # one folder = one installable server (labmcp-<name>)
│   ├── chemistry/
│   ├── physics/
│   ├── health/
│   ├── engineering/
│   └── protocols/              # generic: SCPI, Modbus, … (many instruments each)
├── docs/                       # architecture, client setup, contributor guide, official servers
├── examples/                   # ready-made client configs, e.g. a simulated "virtual lab"
├── scripts/                    # new_server.py (scaffold), build_catalog.py (catalog & docs)
└── catalog.json                # machine-readable index of every server and tool (generated)
```

Each server is a separate PyPI package, so scientists install only what they need, and each server can be versioned and verified on its own. They all share the `labmcp` core, so they behave the same way: same flags, same safety model, same built-in tools. See [docs/architecture.md](docs/architecture.md).

---

## 🗺 Roadmap

Wanted next. Each needs an open protocol or SDK, and someone to verify it on real hardware:

- **Liquid handling & lab robotics:** [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) backends (Hamilton STAR, Tecan EVO, plate readers, centrifuges), Hamilton Microlab 600 pumps
- **Spectroscopy & analytics:** Zurich Instruments lock-ins (`zhinst-toolkit`), Avantes spectrometers, Knauer HPLC pumps, Anton Paar densitometers
- **Motion & optics:** Thorlabs Kinesis / APT motion controllers, Newport ESP/SMC controllers, Zaber stages
- **Environment & process:** Watlow/Eurotherm controllers via the Modbus server's register maps, Vaisala sensors, Binder/Memmert chambers, OPC UA LADS devices
- **Health:** Polar H10 raw ECG (PMD protocol), BITalino / PLUX biosignals, Nonin pulse oximeters
- **Protocol features:** ASTM order download, EPICS PV Access, SiLA 2 server-side (expose LabMCP instruments *as* SiLA servers)

[Request an instrument](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=instrument-request.yml) or vote with 👍 on existing requests.

---

## 🤝 Contributing

The most valuable contribution is **testing a server on real hardware**. Most servers are 🧪 simulated until someone with the instrument confirms they work. Run `--check`, try a few tools, and [file a verification report](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml).

To add an instrument:

```bash
git clone https://github.com/K-Dense-AI/lab-instrument-mcps && cd lab-instrument-mcps
uv sync --all-packages && uv run pytest
uv run python scripts/new_server.py --domain chemistry --slug my-instrument \
    --package labmcp-my-instrument --name "My Instrument" --vendor "Vendor"
```

…then follow **[docs/writing-a-server.md](docs/writing-a-server.md)** and [CONTRIBUTING.md](CONTRIBUTING.md).

---

## ❓ FAQ

**Do I need to know Python?** No. `uvx labmcp-<server>` installs and runs everything. You only edit your AI client's config.

**Does my data leave the lab?** The servers run locally and talk to instruments directly. Your AI client sends tool results to whichever model provider you use, so follow your institution's data policies.

**Why not just let the agent write pyserial code?** It can, but every experiment would start from scratch, with no limits, no audit trail, no simulator, and no protection against a mistyped command. These servers encode the manual once, carefully, for everyone.

**My instrument's vendor has an official MCP server.** Great, use it. We only build servers for instruments without one and [list the official ones](docs/official-servers.md). Keysight and Rohde & Schwarz, for example, ship their own.

**Is this validated for GxP / clinical use?** No. See [SAFETY.md](SAFETY.md).

---

## 📄 License & citation

[Apache-2.0](LICENSE). If LabMCP helps your research, please cite it ([CITATION.cff](CITATION.cff)).

Made by [K-Dense](https://www.k-dense.ai), with contributions from scientists and engineers everywhere.
