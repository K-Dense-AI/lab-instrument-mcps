# Micro-Manager Microscope — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-micro-manager -->

Let an AI agent snap images, acquire z-stacks and time-lapses, move the stage and focus within limits you set, switch channels and objectives, set exposure, operate shutters and run autofocus on **any microscope controlled by Micro-Manager**, through its open device layer (MMCore) and [pymmcore-plus](https://pymmcore-plus.github.io/pymmcore-plus/).

| | |
|---|---|
| **Package** | `labmcp-micro-manager` |
| **Instruments** | Any hardware with a Micro-Manager device adapter (hundreds of cameras, XY/Z stages, microscope stands, filter wheels, shutters and light sources from Hamamatsu, Photometrics, Andor, PCO, Basler, ASI, Prior, Ludl, Märzhäuser, Nikon, Zeiss, Olympus/Evident, Leica, Lumencor, CoolLED and others), described by a Micro-Manager `.cfg` hardware configuration |
| **Interfaces** | Whatever the device adapters use: USB, serial, Camera Link / CoaXPress / GigE frame grabbers |
| **Protocol** | Micro-Manager MMCore API via pymmcore-plus `CMMCorePlus` ([MMCore API reference](https://valelab4.ucsf.edu/~MM/doc/MMCore/html/class_c_m_m_core.html), [pymmcore-plus API](https://pymmcore-plus.github.io/pymmcore-plus/api/cmmcoreplus/), [configuration guide](https://micro-manager.org/Micro-Manager_Configuration_Guide)) |
| **Status** | 🧪 **simulated**: tested against a simulated microscope and against pymmcore-plus' real core with Python test devices, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-micro-manager --simulate --check
```

The simulator is a widefield fluorescence microscope with a 512 x 512, 12-bit camera, an XY stage, a focus drive, a shutter, a DAPI / FITC / TRITC / Brightfield filter wheel, a 10x / 20x / 40x turret and a hardware autofocus, looking at a field of cell nuclei. Its images defocus with Z, scale with exposure, bleach with light dose, saturate at 4095 and carry shot and read noise, so focusing, exposure and bleaching behave like on a real microscope.

## Connect your microscope

1. **Hardware configuration:** create and test a `.cfg` file with Micro-Manager's Hardware Configuration Wizard (in the Micro-Manager app), including config groups for channels (usually `Channel`) and objectives (`Objective`) and a pixel-size calibration. Close the Micro-Manager app afterwards: a device can only be opened by one program.
2. **Device adapters:** pymmcore-plus needs Micro-Manager's device adapter libraries in a version that matches its `pymmcore` build. Either download them with pymmcore-plus' own installer:
   ```bash
   uvx --from labmcp-micro-manager mmcore install     # into pymmcore-plus' data folder
   uvx --from labmcp-micro-manager mmcore list        # show what was found
   ```
   or point to an existing Micro-Manager installation with `--option mm_path=/path/to/Micro-Manager` (it must be a build whose device interface matches pymmcore; `mmcore info` shows the version). Vendor camera SDKs and drivers must be installed separately, as for Micro-Manager itself.
3. **Test the connection:**
   ```bash
   uvx labmcp-micro-manager --address ~/scopes/Ti2_Prime95B.cfg --check
   uvx labmcp-micro-manager --address MMConfig_demo.cfg --check      # Micro-Manager demo devices
   ```
   `--check` loads the configuration and prints the camera, stages and other device roles.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add microscope -- uvx labmcp-micro-manager --address /path/to/MyScope.cfg --option z_min_um=0 --option z_max_um=4500
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "microscope": {
      "command": "uvx",
      "args": ["labmcp-micro-manager", "--address", "/path/to/MyScope.cfg",
               "--option", "z_min_um=0", "--option", "z_max_um=4500",
               "--option", "data_dir=/data/microscope"]
    }
  }
}
```

Add `--read-only` to let the agent read positions and settings without imaging or moving anything (`stop_stage` and `close_shutter` stay available). For other clients, generate the snippet with `uvx labmcp config micro-manager --address /path/to/MyScope.cfg --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `acquire_time_lapse` | ⚠️ hazard | Acquire a time-lapse at the current position: images every `interval_s` (optionally several channels per timepoint), saves a multi-page TIFF and reports per-frame statistics and the intensity change (bleaching). Bounded by `max_frames` and `max_acquisition_duration_s`; `stop_stage` aborts it. |
| `acquire_z_stack` | ⚠️ hazard | Acquire a z-stack around the current focus: moves Z through the slices (optionally imaging several channels at each), saves a multi-page TIFF, reports per-slice statistics and the sharpest slice, and returns Z to where it started. Every slice must stay within `max_z_step_um` of the start and the Z soft limits. |
| `autofocus` | ⚠️ hazard | Run the configured autofocus device (e.g. a hardware focus lock) once; it moves the focus drive. If the result is outside the Z step or soft limits, the focus is moved back and an error is raised. |
| `close_shutter` | 🛑 safety | Close the shutter (stop illuminating the sample) and abort any running acquisition. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_exposure` | 👁 read | Return the camera exposure time in milliseconds. |
| `get_position` | 👁 read | Read the XY stage and focus (Z) positions in micrometres, and which way increasing Z moves the objective. |
| `get_system_info` | 👁 read | Describe the microscope: loaded devices (camera, stages, shutters, filter wheels...), which device has which role, camera size/bit depth/exposure, pixel size, config groups with their presets, the current position, and the soft limits. Call this first. |
| `list_config_groups` | 👁 read | List Micro-Manager config groups (e.g. Channel, Objective) with their presets and the preset currently active. |
| `move_stage_xy` | ⚠️ hazard | Move the XY stage. Moves longer than `max_xy_step_um` or outside the XY soft limits are refused before anything moves. Make sure the objective and condenser clear the sample holder. |
| `move_z` | ⚠️ hazard | Move the focus drive (Z). Moving the objective toward the sample can crash it into the slide: moves larger than `max_z_step_um` or outside the Z soft limits are refused before anything moves. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_config` | 🎛 control | Apply a config-group preset (e.g. switch the channel: filter cube, light source, emission filter). Objective/turret groups are refused here: use `set_objective`. |
| `set_exposure` | 🎛 control | Set the camera exposure time (ms). Longer exposures give brighter images but more photobleaching. Returns the exposure the camera accepted. |
| `set_objective` | ⚠️ hazard | Switch the objective (rotates the nosepiece/turret). A longer or immersion objective can hit the sample holder or need oil/water: confirm with the user first. Pixel size changes with the objective. |
| `set_shutter` | ⚠️ hazard | Open or close the current shutter, and optionally switch auto-shutter. An open shutter illuminates (and bleaches) the sample until it is closed again. |
| `snap_image` | 🎛 control | Acquire one image with the current channel, exposure and position. The sample is illuminated during the exposure. Saves the full image as TIFF and returns summary statistics (min/max/mean, saturation, sharpness) plus an optional contrast-stretched preview. |
| `stop_stage` | 🛑 safety | Immediately stop XY and Z stage motion and abort any z-stack or time-lapse in progress. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. `get_command_log` lists every state-changing MMCore call (`setPosition(1502.0)`, `setConfig('Channel', 'FITC')`, `snapImage()`, ...).

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_z_step_um` | 50 µm | Largest focus move from the current position (also the largest z-stack offset, and the largest autofocus correction accepted) |
| `max_xy_step_um` | 20000 µm | Largest XY stage move in one call (straight-line distance) |
| `max_exposure_ms` | 10000 ms | Longest camera exposure |
| `max_frames` | 500 | Most images in one z-stack or time-lapse (slices or timepoints x channels) |
| `max_acquisition_duration_s` | 3600 s | Longest time-lapse (one tool call can run at most 3600 s whatever this is set to) |

Override at launch, e.g. `--limit max_z_step_um=10 --limit max_frames=2000`.

**Soft limits (recommended):** absolute stage bounds in micrometres, set with driver options. Find them by moving to the safe extremes in Micro-Manager (e.g. Z with the objective just below the coverslip) and reading the positions:

| Option | Meaning |
|---|---|
| `z_min_um`, `z_max_um` | Allowed focus range. Set the side that moves the objective toward the sample tightly. |
| `x_min_um`, `x_max_um`, `y_min_um`, `y_max_um` | Allowed XY range (e.g. the area of your slide or plate holder) |

Every Z and XY move, every z-stack slice and every autofocus result is checked against the step limits and soft limits before anything moves (autofocus results are checked afterwards and undone if out of range).

## Example prompts

- "What's on this microscope? List the channels and objectives, and the current position."
- "Switch to FITC, find an exposure that uses about half the camera's range without saturating, then snap an image and show me."
- "Autofocus, then take a z-stack from −10 to +10 µm in 1 µm steps in DAPI and FITC and tell me which slice is sharpest."
- "Move 500 µm to the right and snap a DAPI image; repeat three more times to make a row of four fields."
- "Record a time-lapse in TRITC every 30 s for 20 minutes and tell me how much the signal bleached."
- "Something is wrong — stop the stage and close the shutter."

## Notes

- **Objective crashes.** Moving the focus drive toward the sample can drive the objective into the coverslip or dish. `get_position` / `get_system_info` report `focus_direction` (whether increasing Z moves the objective toward the sample, as declared by the device adapter; often `unknown`). Configure `z_min_um` / `z_max_um` for every microscope you connect.
- **Objective changes** (`set_objective`) rotate the nosepiece and are marked as hazardous; `set_config` refuses config groups whose names look like objective or turret groups (`Objective`, `Nosepiece`, `Turret`, `Magnification`, `Lens`). Other groups are applied as-is: if one of your presets moves hardware near the sample, name the group accordingly or leave it out of the configuration.
- **Light exposure.** With auto-shutter on (the default), the light path only opens while the camera exposes. `set_shutter(open=True)` leaves the sample lit until `close_shutter` is called.
- **Images** are saved as TIFF files (ImageJ hyperstacks with pixel size, Z spacing and frame interval) in `save_path`, or in `--option data_dir=...` (default: the system temp folder, `labmcp-micro-manager/`). `save_path` must end in `.tif`/`.tiff` and must not be a folder; it is checked before anything moves or is exposed. Existing files are never overwritten (a numeric suffix is added). Tools return statistics (min/max/mean/std, saturated fraction, a sharpness score) and a small contrast-stretched PNG preview, never the full image.
- **Acquisitions** (`acquire_z_stack`, `acquire_time_lapse`) snap image by image with the current camera settings; they do not use hardware-triggered sequencing, so they are slower than Micro-Manager's MDA engine but behave identically on every device. `stop_stage` or `close_shutter` aborts them between frames; partial data is saved (an incomplete slice or timepoint is dropped). A z-stack returns the focus to its starting position unless it was aborted. An acquisition must fit in one tool call: one whose exposure time alone exceeds 3600 s is refused, and one that runs long because of stage, filter or camera overheads stops between slices/timepoints and reports why in `note`. Channels cannot come from an objective/turret group.
- **Autofocus** uses the configured autofocus device (`fullFocus`). Software autofocus plugins from the Micro-Manager Java app are not available.
- pymmcore-plus is imported only when a real configuration is loaded, so `--simulate` works without Micro-Manager's native libraries.

## Hardware verification

| Microscope / camera / stage | Micro-Manager adapters | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
