# LabMCP: Let AI Assistants Run Your Lab Instruments

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/Built_with-FastMCP_4-6E56CF.svg)](https://gofastmcp.com)
[![MCP](https://img.shields.io/badge/Protocol-Model_Context_Protocol-000000.svg)](https://modelcontextprotocol.io)
<!-- COUNTS:START -->
[![Servers](https://img.shields.io/badge/Servers-28-brightgreen.svg)](#-supported-instruments) [![Tools](https://img.shields.io/badge/Tools-354-blue.svg)](#-supported-instruments)
<!-- COUNTS:END -->
[![CI](https://github.com/K-Dense-AI/lab-instrument-mcps/actions/workflows/ci.yml/badge.svg)](https://github.com/K-Dense-AI/lab-instrument-mcps/actions/workflows/ci.yml)
[![Works with](https://img.shields.io/badge/Works_with-Claude_|_Cursor_|_VS_Code_|_Codex-blue.svg)](docs/clients.md)

**LabMCP lets an AI assistant such as Claude operate your laboratory instruments.** You describe what you want in plain language, and the assistant weighs, heats, stirs, pumps, measures and logs for you. It stays inside safety limits you set, keeps a record of every command, and has a practice mode that needs no hardware.

It supports balances, hotplate stirrers, syringe pumps, spectrometers, potentiostats, lock-in amplifiers, temperature controllers, vacuum gauges, flow controllers, biosensors, clinical analyzers and more. **You don't need to know how to program.** Setup means installing one free helper program and pasting a few lines into your AI app's settings.

Free and open source, built by [K-Dense](https://www.k-dense.ai).

```text
You:        Tare the balance, then heat the stirrer to 60 °C at 400 rpm and log the pH every
            30 s until it's stable. Tell me when to add the reagent.

Assistant:  [tares the balance → sets stirring to 400 rpm → sets heating to 60 °C → logs pH …]
            "pH has been 7.41 ± 0.01 for 3 minutes. Ready for the reagent."
```

> ⭐ **Help other scientists find this.** If LabMCP is useful to you, please star the repo.

---

## 📋 Contents

- [How it works](#-how-it-works)
- [Try it in 10 minutes (no instrument needed)](#-try-it-in-10-minutes-no-instrument-needed)
- [Supported instruments](#-supported-instruments)
- [Connect your real instrument](#-connect-your-real-instrument)
- [Safety](#%EF%B8%8F-safety)
- [What every connector includes](#-what-every-connector-includes)
- [Example requests](#-example-requests)
- [FAQ](#-faq)
- [Contributing](#-contributing)
- [Roadmap](#-roadmap)

---

## 💡 How it works

```text
  You  ──type──▶  AI app  ──▶  LabMCP connector  ──USB / serial / network cable──▶  Instrument
                 (Claude,       (runs on the computer
                  Cursor, …)     attached to the instrument)
```

- **The AI app** is where you type requests, e.g. Claude Desktop.
- **A LabMCP connector** is a small program that knows one family of instruments and its command language, so the AI never has to guess it. You add one connector per type of instrument. (Technically these are *MCP servers*, which is the term you'll see in the tables and docs.)
- **MCP** ([Model Context Protocol](https://modelcontextprotocol.io)) is the open standard that lets AI apps plug into tools like these. Think of it as USB for AI.
- The AI can only use the **actions** a connector offers, like "tare", "set temperature" or "read pH". In the docs these actions are called *tools*. The connector checks every request against your limits and refuses anything unsafe before it reaches the instrument.

---

## 🚀 Try it in 10 minutes (no instrument needed)

Each connector has a **practice mode** (a *simulator*) that behaves like the real instrument and gives realistic replies. Nothing is connected, so nothing can go wrong.

**You need:** a Mac, Windows or Linux computer, and an AI app that supports MCP. [Claude Desktop](https://claude.ai/download) is the easiest place to start.

### Step 1: Install `uv` (once)

`uv` is a free tool that downloads and runs LabMCP for you. Open a terminal:

- **Mac:** press ⌘ Space, type *Terminal*, press Enter.
- **Windows:** open the Start menu, type *PowerShell*, press Enter.

Paste the line for your system and press Enter:

```bash
# Mac or Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close the terminal and open it again so it finds the new program.

### Step 2: Add a practice balance to your AI app

In **Claude Desktop**, go to **Settings → Developer → Edit Config**. This opens a file called `claude_desktop_config.json`. Paste the following into it and save. If the file already has an `"mcpServers"` section, add only the `"balance"` line inside it.

```json
{
  "mcpServers": {
    "balance": { "command": "uvx", "args": ["labmcp-mettler-toledo", "--simulate"] }
  }
}
```

Quit Claude Desktop completely, then reopen it.

> **Mac tip:** if Claude reports that it can't find `uvx`, run `which uvx` in Terminal and replace `"uvx"` in the file with the full path it prints (usually `/Users/<your-name>/.local/bin/uvx`).

**Using a different app?** For Claude Code, run `claude mcp add balance -- uvx labmcp-mettler-toledo --simulate` in a terminal. For Cursor, VS Code, Codex and Windsurf, see [docs/clients.md](docs/clients.md).

### Step 3: Ask

> *"What instrument is connected? Tare it, then log the weight every second for 10 seconds."*

The first request can take a minute while the connector downloads. Your AI app will usually ask your permission before it takes an action. Say yes.

**Want a whole lab?** [`examples/`](examples/) sets up six practice instruments at once (balance, stirrer, pH probe, syringe pump, potentiostat and spectrometer).

---

## 📦 Supported instruments

Find your instrument below and click its name. Each page covers the settings to change on the instrument itself, the safety limits you can set, and everything the AI can do with it.

**Reading the tables:**

- **Interface** is the cable or connection the instrument uses.
- **Tools** is how many different actions the AI can take.
- **Install** is the name to put in your AI app's settings (the part after `uvx`).
- **Status:** 🧪 *simulated* means built from the manufacturer's published manual and tested thoroughly in practice mode, but not yet confirmed on a real instrument by a user. ✅ *hardware-verified* means someone has confirmed it on the real instrument. **If you own one of these instruments, you can [help verify it](CONTRIBUTING.md#1-test-a-server-on-real-hardware--most-needed). No coding needed.**

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

The **Universal Protocols** connectors work with many instruments from many brands, as long as the instrument speaks that standard (its manual will say, e.g., "SCPI commands" or "Modbus").

**Don't see your instrument?** [Request it](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=instrument-request.yml). Some manufacturers (for example Keysight and Rohde & Schwarz) publish their own AI connectors. We [list those](docs/official-servers.md) rather than duplicate them.

---

## 🔌 Connect your real instrument

Once practice mode works, switching to the real instrument is one change in the same settings file.

1. **Plug the instrument into the computer** that runs your AI app (by USB, serial cable or network) and switch it on. Click your instrument's name in the [table above](#-supported-instruments) to check whether anything needs changing on the instrument, such as enabling remote control or setting its communication speed (*baud rate*).

2. **Find its address**, meaning where the computer sees it. In a terminal, run:

   ```bash
   uvx labmcp ports
   ```

   It lists what's connected. **Tip:** run it once with the instrument unplugged and once plugged in. The new entry is your instrument.

   | If the instrument connects by… | Its address looks like |
   |---|---|
   | USB or serial cable | `COM3` (Windows), `/dev/tty.usbserial-XXXX` (Mac), `/dev/ttyUSB0` (Linux) |
   | Network (Ethernet or Wi-Fi) | `tcp://192.168.1.50:5025`: the instrument's IP address and port, found in its network settings menu |
   | Serial, with non-standard settings | `serial:///dev/ttyUSB0?baudrate=19200&parity=E`, needed only if the instrument's communication settings were changed from the factory defaults |
   | GPIB, USBTMC, LXI (via VISA) | `GPIB0::22::INSTR` or `visa://TCPIP0::192.168.1.50::INSTR` |

3. **Test the connection** (replace `COM3` with your address):

   ```bash
   uvx labmcp-mettler-toledo --address COM3 --check
   ```

   A working connection reports the instrument's model. If it doesn't, see [troubleshooting](docs/clients.md#troubleshooting).

4. **Update your settings file:** replace `"--simulate"` with `"--address", "COM3"`, save, and restart your AI app.

   ```json
   "balance": { "command": "uvx", "args": ["labmcp-mettler-toledo", "--address", "COM3"] }
   ```

   Or let LabMCP write the entry for you: `uvx labmcp config mettler-toledo --address COM3`.

**Several instruments?** Add one entry per instrument, each with its own name (`"balance"`, `"stirrer"`, `"pump"`, …). The assistant can then use them together.

---

## 🛡️ Safety

AI assistants operating physical equipment need guardrails. LabMCP has several layers of them, and **you stay in charge**:

1. **Practise first** with `--simulate`.
2. **Start in read-only mode** with `--read-only`. The AI can read measurements but can't change anything. Stop and switch-off actions still work.
3. **Set limits for your experiment**, e.g. `--limit max_temperature_c=60` to stay below your solvent's boiling point. Requests above a limit are refused before anything is sent to the instrument. Each instrument's page lists its limits and their defaults.
4. **Approve risky actions yourself.** Actions that heat, move, dispense or switch on power are marked as hazardous (⚠️), so your AI app asks before running them. Don't turn on "always allow" for these.
5. **Keep the instrument's own safety features on** (interlocks, over-temperature cut-offs). LabMCP's limits are an extra layer, not a replacement.

Options go in the same list as the address in your settings file, for example:

```json
"stirrer": { "command": "uvx", "args": ["labmcp-ika", "--address", "COM4", "--read-only", "--limit", "max_temperature_c=60"] }
```

Read **[SAFETY.md](SAFETY.md)** before connecting anything hazardous. LabMCP is a research tool, not a certified safety system or medical device.

---

## 🧰 What every connector includes

| Feature | What it means for you |
|---|---|
| **Practice mode** | `--simulate` answers exactly like the real instrument, so you can rehearse an experiment before running it. |
| **Read-only mode** | `--read-only` lets the AI observe but never change settings. |
| **Safety limits** | `--limit …` refuses dangerous setpoints before they reach the instrument. |
| **Asks before risky actions** | Heating, moving, dispensing and powering actions are flagged, so your AI app asks first. |
| **Record of every command** | Every command and reply is logged, like a lab notebook. Ask *"show me the command log"*, or save it to a file with `--audit-log run.jsonl`. |
| **Knows what it's connected to** | Ask *"what instrument is connected?"* to get the model, whether it's in practice mode, and the active limits. |
| **Clear error messages** | "Overload: too much weight on the pan", not a cryptic code. |
| **Connection check** | `--check` tests the connection from a terminal before you start. |
| **Works with any cable** | USB, serial (RS-232), network, GPIB and more. Just change `--address`. |
| **Works with many AI apps** | Claude Desktop, Claude Code, Cursor, VS Code, Codex, Windsurf and other MCP apps. |

---

## 🧪 Example requests

Add several instruments and the assistant can coordinate them:

- **Chemistry:** *"Weigh 250 mg ± 2 mg of salt into the vial: tell me how much to add, reading the balance every few seconds. Then set the stirrer to 500 rpm and 40 °C, and tell me when it's dissolved and at temperature."*
- **Electrochemistry:** *"Run cyclic voltammograms from −0.2 to 0.6 V at 25, 50, 100 and 200 mV/s, extract the anodic peak currents, and check whether they scale with the square root of scan rate."*
- **Physics:** *"Sweep the lock-in reference from 1 to 100 kHz in 40 log-spaced steps and fit the resonance. Then ramp the cryostat to 10 K at 2 K/min and repeat at 50 K and 100 K."*
- **Biology:** *"Infuse 2 mL of media at 0.5 mL/min, then log the bioreactor pH and dissolved oxygen every minute for an hour and flag any drift."*
- **Engineering:** *"Step the flow controller through 10, 20 and 50 sccm of N₂ and log the upstream pressure at each setpoint. Stop and close the valve if pressure goes above 30 psia."*
- **Clinical lab operations:** *"Summarise today's CBC results received from the analyzer and list any samples with flagged results."*

---

## ❓ FAQ

**Do I need to know how to program?** No. You install `uv` once and paste a few lines into your AI app's settings. LabMCP downloads and runs everything else.

**Which AI app should I use?** Any app that supports MCP. Claude Desktop is the simplest to set up. Claude Code, Cursor, VS Code, Codex and Windsurf also work ([setup for each](docs/clients.md)).

**Does it cost anything?** LabMCP is free and open source. Your AI app may need its own subscription.

**Which computer does it run on?** The one physically connected to the instrument. To control an instrument from another computer, see [remote instruments](docs/clients.md#remote--shared-instruments-http).

**Does my data leave the lab?** LabMCP runs on your computer and talks to the instruments directly. Your AI app sends the results to its AI provider, just like anything else you type into it, so follow your institution's data policies.

**Why not just ask the AI to write its own code for the instrument?** It can try, but it would start from scratch every time, with no safety limits, no command record, no practice mode, and no protection against a mistyped command. LabMCP encodes each instrument's manual once, carefully, for everyone.

**Something isn't working.** See the [troubleshooting table](docs/clients.md#troubleshooting). If that doesn't help, [open an issue](https://github.com/K-Dense-AI/lab-instrument-mcps/issues).

**My instrument's manufacturer has its own AI connector.** Great, use it. We only build connectors for instruments without one, and [list the official ones](docs/official-servers.md).

**Is this validated for GxP or clinical use?** No. See [SAFETY.md](SAFETY.md).

---

## 🤝 Contributing

**No coding needed for the most valuable contribution: testing a connector on your real instrument.** Most connectors are 🧪 simulated until someone who owns the instrument confirms they work. Run the connection check, try a few requests, and [file a short report](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml). You can also [request an instrument](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=instrument-request.yml), or add 👍 to existing requests.

### For developers

Each connector is a separate Python package (`labmcp-<name>`), so people install only what they need. All of them share the `labmcp` core, so they behave the same way: same options, same safety model, same built-in tools. See [docs/architecture.md](docs/architecture.md).

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

To add an instrument:

```bash
git clone https://github.com/K-Dense-AI/lab-instrument-mcps && cd lab-instrument-mcps
uv sync --all-packages && uv run pytest
uv run python scripts/new_server.py --domain chemistry --slug my-instrument \
    --package labmcp-my-instrument --name "My Instrument" --vendor "Vendor"
```

Then follow **[docs/writing-a-server.md](docs/writing-a-server.md)** and [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 🗺 Roadmap

Wanted next. Each needs a published protocol or SDK from the manufacturer, and someone to test it on real hardware:

- **Liquid handling & lab robotics:** [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) backends (Hamilton STAR, Tecan EVO, plate readers, centrifuges), Hamilton Microlab 600 pumps
- **Spectroscopy & analytics:** Zurich Instruments lock-ins, Avantes spectrometers, Knauer HPLC pumps, Anton Paar densitometers
- **Motion & optics:** Thorlabs Kinesis / APT motion controllers, Newport ESP/SMC controllers, Zaber stages
- **Environment & process:** Watlow/Eurotherm controllers (via the Modbus connector), Vaisala sensors, Binder/Memmert chambers, OPC UA LADS devices
- **Health:** Polar H10 raw ECG, BITalino / PLUX biosignals, Nonin pulse oximeters
- **Protocol features:** ASTM order download, EPICS PV Access, exposing LabMCP instruments *as* SiLA 2 servers

[Request an instrument](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=instrument-request.yml) or vote with 👍 on existing requests.

---

## 📄 License & citation

[Apache-2.0](LICENSE). If LabMCP helps your research, please cite it ([CITATION.cff](CITATION.cff)).

Made by [K-Dense](https://www.k-dense.ai), with contributions from scientists and engineers everywhere.
