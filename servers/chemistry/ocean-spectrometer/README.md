# Ocean Insight Spectrometer — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-ocean-spectrometer -->

Let an AI agent acquire spectra, set or auto-tune the integration time, store dark and reference spectra, and measure absorbance and transmittance with peak positions and FWHM on **Ocean Insight (Ocean Optics) spectrometers**. It works through the open-source **python-seabreeze** library.

| | |
|---|---|
| **Package** | `labmcp-ocean-spectrometer` |
| **Instruments** | Every model python-seabreeze supports: USB2000+, USB2000, USB4000, USB650, Flame-S, Flame-NIR/X, HR2000/HR2000+, HR4000, Maya2000/Pro/LSL, QE65000, QE Pro, STS, NIRQuest256/512, Jaz, Torus, Spark, Apex, Ventana, HDX, ADC1000-USB, HR2/4/6, SR2/4/6, ST-VIS ([support table](https://github.com/ap--/python-seabreeze#supported-devices); some models need a specific backend) |
| **Interfaces** | USB |
| **Protocol** | python-seabreeze 2.x ([docs](https://python-seabreeze.readthedocs.io/en/latest/), [source](https://github.com/ap--/python-seabreeze)), which implements Ocean's SeaBreeze (OOI / OBP USB protocols). TEC details: [Ocean QE Pro Installation and Operation Manual, MNL-0000](https://www.oceanoptics.com/wp-content/uploads/2025/04/OceanQEPro_Manual_June2025.pdf) |
| **Status** | 🧪 **simulated**: tested against a physically modelled fake spectrometer with the seabreeze `Spectrometer` interface, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-ocean-spectrometer --simulate --check
uvx labmcp-ocean-spectrometer --simulate --option sim_source=hg-ar --check      # Hg-Ar calibration lamp
uvx labmcp-ocean-spectrometer --simulate --option sim_model=QE-PRO --check      # TE-cooled, 1044 pixels
```

The simulator models a USB2000+ (or QE Pro) looking at a tungsten-halogen lamp, a white LED or a Hg-Ar lamp. It includes shot and read noise, dark offset and dark current, electric-dark pixels, detector nonlinearity, saturation, and the stale first spectrum after an integration-time change. In simulation a "virtual user" blocks the beam before `store_dark_reference`, inserts a blank before `store_reference`, and inserts a dye sample (absorbance band at 520 nm, 0.8 AU) before `measure_absorbance`/`measure_transmittance`. With real hardware your scientist does these steps.

## Connect your spectrometer

1. **Drivers / permissions** (once per computer), as documented by python-seabreeze:
   ```bash
   uvx --from seabreeze seabreeze_os_setup      # or, inside the environment: seabreeze_os_setup
   ```
   - **Linux:** this installs the udev rules (`/etc/udev/rules.d/10-oceanoptics.rules`, needs sudo). Re-plug the spectrometer afterwards.
   - **Windows:** this installs the USB driver files. The `pyseabreeze` backend also needs `libusb-1.0.dll` (see the seabreeze README).
   - **macOS:** works out of the box.
2. **Close OceanView** (or any other program using the spectrometer). Only one program can open it.
3. **Test the connection:**
   ```bash
   uvx labmcp-ocean-spectrometer --check                        # the only connected spectrometer
   uvx labmcp-ocean-spectrometer --address USB2+H01234 --check  # choose by serial number
   uvx labmcp-ocean-spectrometer --option backend=pyseabreeze --check
   ```
   `--address` is the spectrometer's **serial number** and is only needed when several are connected (`list_spectrometers` shows them). The default backend is `cseabreeze` (compiled libseabreeze); if it can't load, seabreeze falls back to the pure-Python `pyseabreeze` (pyusb).

On connect the server sets the integration time to 10 ms (`--option integration_ms=…`), clamped to the device range. It has to set it, because seabreeze cannot read the integration time back.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add spectrometer -- uvx labmcp-ocean-spectrometer
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "spectrometer": {
      "command": "uvx",
      "args": ["labmcp-ocean-spectrometer", "--address", "USB2+H01234"]
    }
  }
}
```

Add `--read-only` to allow acquisitions and absorbance measurements with the present settings while blocking integration-time changes, reference storage and TEC control. For other clients, generate the snippet with `uvx labmcp config ocean-spectrometer --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `acquire_spectrum` | 👁 read | Acquire an intensity spectrum (raw counts) with optional averaging, boxcar smoothing and corrections. Returns downsampled (wavelength, counts), summary statistics, the most prominent peaks with FWHM, and a saturation check; `save_path` writes the full spectrum. |
| `auto_integration_time` | 🎛 control | Adjust the integration time until the brightest raw pixel (optionally within a wavelength window) is within the target band of saturation (default 70-85 %). Stays within the device limits and the `max_integration_time_ms` safety limit. Re-take dark/reference afterwards. |
| `detector_cooling_off` | 🛑 safety | Switch the detector thermo-electric cooler off (the detector warms to ambient). Does nothing on spectrometers without a TEC. |
| `find_peaks` | 👁 read | Find peaks (or dips) with position, height, prominence and FWHM in the most recent spectrum (intensity, absorbance or transmittance). Acquires a fresh intensity spectrum if none has been taken yet. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Model, serial, pixel count, wavelength range, integration-time limits, saturation level, supported corrections, TEC presence, and which dark/reference spectra are stored. |
| `list_spectrometers` | 👁 read | List the Ocean spectrometers seabreeze can see (model, serial number, whether open) and which one this server is connected to. Use a serial number as `--address` to pick one. |
| `measure_absorbance` | 👁 read | Measure the absorbance spectrum A = -log10((S - D) / (R - D)) of the sample now in the beam, using the stored dark D and reference R (same settings). Ask the user to insert the sample first. Also returns values at `wavelengths_nm` and the absorbance maxima. |
| `measure_transmittance` | 👁 read | Measure the transmittance spectrum %T = 100 (S - D) / (R - D) of the sample now in the beam, using the stored dark and reference. Ask the user to insert the sample first. Also returns values at `wavelengths_nm` and the deepest transmission dips. |
| `read_detector_temperature` | 👁 read | Read the detector temperature from the thermo-electric cooler (TE-cooled models such as the QE Pro, when seabreeze exposes the thermo_electric feature for them). |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_detector_cooling` | ⚠️ hazard | Enable the detector thermo-electric cooler at `setpoint_c` (TE-cooled models only). The detector takes minutes to settle; dark current (and noise) drops as it cools. The TEC can only cool to roughly 15-40 °C below ambient (QE Pro manual). Checked against `min_tec_setpoint_c`. |
| `set_integration_time` | 🎛 control | Set the detector integration time (ms), within the device limits from `get_device_info`. Stored dark/reference spectra become unusable until re-taken at the new time. |
| `store_dark_reference` | 🎛 control | Record and store a DARK spectrum (kept in memory) for absorbance/transmittance. BEFORE calling, ask the user to BLOCK THE LIGHT PATH (close the shutter, switch the lamp off or cap the fiber) and wait for confirmation. Uses the present integration time. |
| `store_reference` | 🎛 control | Record and store the REFERENCE (100 % transmission) spectrum in memory. BEFORE calling, ask the user to put the BLANK (solvent-filled or empty cuvette) in the beam with the light on, and wait for confirmation. Use the same settings as the dark. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_integration_time_ms` | 10000 ms | Longest integration time an agent may set (also caps `auto_integration_time`) |
| `max_acquisition_duration_s` | 300 s | Longest single acquisition (scans × integration time) |
| `min_tec_setpoint_c` | −20 °C | Coldest TEC setpoint (TE-cooled models). The QE Pro's temperature readback is linearised for −20…40 °C |

Override at launch: `--limit max_integration_time_ms=60000`.

## Example prompts

- "Auto-adjust the integration time on the lamp, then take a 20-scan averaged spectrum and tell me where the peak is and how wide it is."
- "I'm going to measure a Beer–Lambert series. Walk me through dark, blank and each of my five standards, and report the absorbance at 520 nm for each."
- "Here's the Hg-Ar lamp. Find the lines between 400 and 600 nm and tell me how far they are from 404.66, 435.83 and 546.07 nm."
- "Measure the transmittance of this filter from 400 to 700 nm and save it to ~/data/filter_T.csv."
- "Cool the QE Pro to −10 °C and tell me when the detector temperature has settled."

## Notes

- **Raw counts.** Intensities are detector counts, not irradiance. Radiometric calibration is out of scope.
- **Saturation** is judged on the raw ADC counts of every averaged scan: a pixel is saturated at ≥ 98 % of `max_intensity`. Saturated pixels are flagged, and they're excluded from absorbance/transmittance. seabreeze notes that some detectors saturate below `max_intensity`, so keep peaks at 70–85 %.
- **Corrections.** `correct_dark_counts` subtracts the mean of the optically masked electric-dark pixels; `correct_nonlinearity` applies the EEPROM polynomial. The driver reads raw spectra and applies both corrections with the same formula as `seabreeze.spectrometers.Spectrometer.intensities`, so saturation can still be detected on raw data. A stored dark (`store_dark_reference`) is still required for absorbance, because electric-dark correction doesn't remove fixed-pattern dark signal.
- **Absorbance** is A = −log10((S − D)/(R − D)). Pixels where the reference has less than 0.5 % of its peak signal above dark, or where the reference or sample is saturated, are returned as `null`. The dark, reference and sample must share integration time, boxcar and corrections; the server enforces this and asks you to re-take references when they differ.
- **First spectrum after an integration-time change is discarded.** It may have been integrated partly with the old setting; the QE Pro manual notes that it returns the most recently *completed* spectrum.
- **Boxcar** follows OceanView's convention: `boxcar_half_width` = pixels averaged on each side. Peaks are found on the full-resolution spectrum. The returned curve is bin-averaged down to `max_points`.
- **TEC** tools use seabreeze's `thermo_electric` feature (`enable_tec`, `set_temperature_setpoint_degrees_celsius`, `read_temperature_degrees_celsius`) and only work when the backend exposes it for your model. In pyseabreeze 2.11 that is only the NIRQuest512. For the QE Pro it depends on libseabreeze (cseabreeze); this is **unverified on hardware**. The QE Pro manual says the TEC cools only ~15–40 °C below ambient.
- **Not exposed:** shutter and lamp (`light_source`/`strobe_lamp`) control, trigger modes, and irradiance calibration. seabreeze documents these features, but device support isn't specified and several aren't implemented in pyseabreeze, so they could not be verified.
- Dark and reference spectra live in the server's memory and are lost on `reconnect` or restart. Save data with `save_path`.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
