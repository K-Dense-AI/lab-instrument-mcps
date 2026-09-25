"""Simulated Micro-Manager core: a fluorescence microscope with a synthetic cell sample.

:class:`FakeMicroscopeCore` implements the subset of ``pymmcore_plus.CMMCorePlus``
methods the driver uses, with the same names, overloads and error types
(``RuntimeError`` for device errors, ``ValueError`` for unknown presets), so
``--simulate`` exercises the real driver code without Micro-Manager's native
device adapters.

The simulated hardware is a 512 x 512, 12-bit camera, a motorised XY stage
(110 x 75 mm travel), a focus drive, a shutter, a filter wheel with DAPI / FITC /
TRITC / Brightfield presets, a 3-position objective turret (10x / 20x / 40x) and a
hardware autofocus. The sample is a field of cell nuclei on a slightly tilted
coverslip whose best focus is near Z = 1500 um. Images are physically plausible:
defocus blur grows with |Z - focus| and NA, intensity scales with exposure time,
fluorophores bleach with light dose, pixels saturate at 4095, and noise is
shot noise plus read noise on a camera offset.
"""

from __future__ import annotations

import enum
from typing import Any

import numpy as np

OFFSET = 100.0  # camera offset (counts)
READ_NOISE = 2.5  # counts rms
FULL_WELL = 4095  # 12-bit


class DeviceType(enum.IntEnum):
    """Same member names as ``pymmcore_plus.DeviceType``."""

    UnknownType = 0
    CameraDevice = 2
    ShutterDevice = 3
    StateDevice = 4
    StageDevice = 5
    XYStageDevice = 6
    AutoFocusDevice = 10
    CoreDevice = 11


_DEVICES: dict[str, tuple[DeviceType, str, str, str]] = {
    "Camera": (DeviceType.CameraDevice, "SimMicroscope", "SimCamera", "Simulated 12-bit sCMOS camera"),
    "XY": (DeviceType.XYStageDevice, "SimMicroscope", "SimXYStage", "Simulated motorised XY stage"),
    "Z": (DeviceType.StageDevice, "SimMicroscope", "SimFocus", "Simulated focus drive"),
    "Shutter": (DeviceType.ShutterDevice, "SimMicroscope", "SimShutter", "Simulated LED shutter"),
    "Filter Wheel": (DeviceType.StateDevice, "SimMicroscope", "SimFilterWheel", "Simulated filter wheel"),
    "Objective": (DeviceType.StateDevice, "SimMicroscope", "SimTurret", "Simulated objective turret"),
    "Autofocus": (DeviceType.AutoFocusDevice, "SimMicroscope", "SimAutofocus", "Simulated hardware autofocus"),
}

#: name -> (pixel size um, NA, relative brightness)
OBJECTIVES = {"10x": (0.65, 0.30, 1.0), "20x": (0.325, 0.75, 2.0), "40x": (0.1625, 0.95, 3.0)}
#: channel -> index into per-cell brightness (None = transmitted light)
CHANNELS: dict[str, int | None] = {"DAPI": 0, "FITC": 1, "TRITC": 2, "Brightfield": None}
PEAK_COUNTS_PER_MS = (55.0, 35.0, 25.0)  # brightest nucleus, per channel, 10x objective
BRIGHTFIELD_COUNTS_PER_MS = 90.0
BLEACH_TAU_MS = 60_000.0  # exposure time that bleaches a fluorophore to 1/e
X_RANGE_UM, Y_RANGE_UM, Z_RANGE_UM = (-55_000.0, 55_000.0), (-37_500.0, 37_500.0), (0.0, 10_000.0)


class FakeMicroscopeCore:
    def __init__(self, seed: int | None = 0, width: int = 512, height: int = 512) -> None:
        self.rng = np.random.default_rng(seed)
        self.width, self.height = width, height
        self.loaded = False
        self.config_file: str | None = None
        self.xy = [0.0, 0.0]
        self.z = 1488.0
        self.exposure_ms = 20.0
        self.shutter_open = False
        self.auto_shutter = True
        self.config = {"Channel": "DAPI", "Objective": "10x", "System": "Startup"}
        self.last_image: np.ndarray | None = None
        self.snaps = 0
        self.stops: list[str] = []
        # Sample: nuclei scattered over a 3 x 3 mm area around the origin.
        n = 3600
        self.cx = self.rng.uniform(-1500, 1500, n)
        self.cy = self.rng.uniform(-1500, 1500, n)
        self.cr = self.rng.uniform(3.0, 6.0, n)  # nucleus radius, um
        self.cdz = self.rng.normal(0, 1.2, n)  # height above the coverslip, um
        self.bright = np.stack(
            [
                self.rng.uniform(0.5, 1.0, n),  # DAPI: every nucleus
                self.rng.uniform(0.2, 1.0, n) * (self.rng.random(n) < 0.6),  # FITC: 60% of cells
                self.rng.uniform(0.2, 1.0, n) * (self.rng.random(n) < 0.3),  # TRITC: 30% of cells
            ]
        )

    # ------------------------------------------------------------ helpers

    def _loaded(self) -> None:
        if not self.loaded:
            raise RuntimeError("Camera not loaded or initialized.")

    def focus_z(self, x: float, y: float) -> float:
        """Best-focus Z (um) of the coverslip under stage position (x, y): tilted by 2 um/mm."""
        return 1500.0 + 0.002 * x - 0.001 * y

    # ------------------------------------------------------------ system

    def getVersionInfo(self) -> str:
        return "MMCore version 11.1.1 (simulated)"

    def getAPIVersionInfo(self) -> str:
        return "Device API version 73, Module API version 10 (simulated)"

    def loadSystemConfiguration(self, fileName: str = "sim") -> None:
        self.loaded = True
        self.config_file = str(fileName)

    def systemConfigurationFile(self) -> str | None:
        return self.config_file

    def unloadAllDevices(self) -> None:
        self.loaded = False

    def getLoadedDevices(self) -> tuple[str, ...]:
        return ("Core", *_DEVICES) if self.loaded else ("Core",)

    def _dev(self, label: str) -> tuple[DeviceType, str, str, str]:
        if label == "Core":
            return (DeviceType.CoreDevice, "", "Core", "Core device")
        if not self.loaded or label not in _DEVICES:
            raise RuntimeError(f'No device with label "{label}"')
        return _DEVICES[label]

    def getDeviceType(self, label: str) -> DeviceType:
        return self._dev(label)[0]

    def getDeviceLibrary(self, label: str) -> str:
        return self._dev(label)[1]

    def getDeviceName(self, label: str) -> str:
        return self._dev(label)[2]

    def getDeviceDescription(self, label: str) -> str:
        return self._dev(label)[3]

    def getCameraDevice(self) -> str:
        return "Camera" if self.loaded else ""

    def getXYStageDevice(self) -> str:
        return "XY" if self.loaded else ""

    def getFocusDevice(self) -> str:
        return "Z" if self.loaded else ""

    def getShutterDevice(self) -> str:
        return "Shutter" if self.loaded else ""

    def getAutoFocusDevice(self) -> str:
        return "Autofocus" if self.loaded else ""

    def getChannelGroup(self) -> str:
        return "Channel" if self.loaded else ""

    # ------------------------------------------------------------ config groups

    def _groups(self) -> dict[str, list[str]]:
        if not self.loaded:
            return {}
        return {"Channel": list(CHANNELS), "Objective": list(OBJECTIVES), "System": ["Startup"]}

    def getAvailableConfigGroups(self) -> tuple[str, ...]:
        return tuple(self._groups())

    def getAvailableConfigs(self, configGroup: str) -> tuple[str, ...]:
        return tuple(self._groups().get(configGroup, []))

    def getCurrentConfig(self, groupName: str) -> str:
        return self.config.get(groupName, "") if groupName in self._groups() else ""

    def setConfig(self, groupName: str, configName: str) -> None:
        if configName not in self._groups().get(groupName, []):
            raise ValueError(f'Preset "{configName}" of configuration group "{groupName}" does not exist')
        self.config[groupName] = configName

    def waitForConfig(self, group: str, configName: str) -> None:
        return None

    # ------------------------------------------------------------ camera

    def getExposure(self) -> float:
        return self.exposure_ms

    def setExposure(self, exp: float) -> None:
        self._loaded()
        if not 0.01 <= exp <= 30_000:
            raise RuntimeError(f"Camera: exposure {exp} ms is outside the allowed range 0.01-30000 ms")
        self.exposure_ms = float(exp)

    def getImageWidth(self) -> int:
        return self.width

    def getImageHeight(self) -> int:
        return self.height

    def getImageBitDepth(self) -> int:
        return 12

    def getBytesPerPixel(self) -> int:
        return 2

    def getPixelSizeUm(self) -> float:
        return OBJECTIVES[self.config["Objective"]][0] if self.loaded else 0.0

    def snapImage(self) -> None:
        self._loaded()
        self.last_image = self._render()
        self.snaps += 1

    def getImage(self) -> np.ndarray:
        if self.last_image is None:
            raise RuntimeError("Camera image buffer read failed: no image has been snapped")
        return self.last_image.copy()

    def _render(self) -> np.ndarray:
        px, na, gain = OBJECTIVES[self.config["Objective"]]
        channel = CHANNELS[self.config["Channel"]]
        lit = self.shutter_open or self.auto_shutter
        h, w = self.height, self.width
        x0, y0 = self.xy
        signal = np.zeros((h, w))
        if lit:
            ys = y0 + (np.arange(h) - h / 2) * px
            xs = x0 + (np.arange(w) - w / 2) * px
            dz = self.z - (self.focus_z(x0, y0) + self.cdz)
            sigma = np.sqrt((self.cr / 2) ** 2 + (0.5 * na * dz) ** 2)  # um, blur grows with defocus
            margin = 4 * sigma
            inside = (
                (self.cx > xs[0] - margin) & (self.cx < xs[-1] + margin)
                & (self.cy > ys[0] - margin) & (self.cy < ys[-1] + margin)
            )
            for i in np.flatnonzero(inside):
                if channel is None:  # brightfield: nuclei absorb ~25% of the light
                    amp = -0.25
                else:
                    amp = PEAK_COUNTS_PER_MS[channel] * gain * self.bright[channel, i] * self.exposure_ms
                amp *= (self.cr[i] / 2) ** 2 / sigma[i] ** 2  # conserve integrated intensity
                j0, j1 = np.searchsorted(xs, [self.cx[i] - margin[i], self.cx[i] + margin[i]])
                k0, k1 = np.searchsorted(ys, [self.cy[i] - margin[i], self.cy[i] + margin[i]])
                gx = np.exp(-((xs[j0:j1] - self.cx[i]) ** 2) / (2 * sigma[i] ** 2))
                gy = np.exp(-((ys[k0:k1] - self.cy[i]) ** 2) / (2 * sigma[i] ** 2))
                signal[k0:k1, j0:j1] += amp * np.outer(gy, gx)
            if channel is None:
                signal = BRIGHTFIELD_COUNTS_PER_MS * self.exposure_ms * (1 + signal)
            else:
                signal += 0.4 * self.exposure_ms  # autofluorescence background
                self.bright[channel, inside] *= np.exp(-self.exposure_ms / BLEACH_TAU_MS)
        electrons = self.rng.poisson(np.clip(signal, 0, None) / 0.5) * 0.5  # 0.5 counts per e-
        img = OFFSET + electrons + self.rng.normal(0, READ_NOISE, (h, w))
        return np.clip(np.round(img), 0, FULL_WELL).astype(np.uint16)

    # ------------------------------------------------------------ stages

    def getXYPosition(self, *label: str) -> list[float]:
        self._loaded()
        return list(self.xy)

    def setXYPosition(self, *args: Any) -> None:
        self._loaded()
        x, y = (float(a) for a in args[-2:])
        if not (X_RANGE_UM[0] <= x <= X_RANGE_UM[1] and Y_RANGE_UM[0] <= y <= Y_RANGE_UM[1]):
            raise RuntimeError(f"XY: position ({x:.1f}, {y:.1f}) um is outside the stage travel range")
        self.xy = [x, y]

    def getPosition(self, *label: str) -> float:
        self._loaded()
        return self.z

    def setPosition(self, *args: Any) -> None:
        self._loaded()
        z = float(args[-1])
        if not Z_RANGE_UM[0] <= z <= Z_RANGE_UM[1]:
            raise RuntimeError(f"Z: position {z:.2f} um is outside the focus drive range 0-10000 um")
        self.z = z

    def waitForDevice(self, label: str) -> None:
        self._dev(label)

    def stop(self, xyOrZStageLabel: str) -> None:
        self._dev(xyOrZStageLabel)
        self.stops.append(xyOrZStageLabel)

    def getFocusDirection(self, stageLabel: str) -> int:
        self._dev(stageLabel)
        return 1  # increasing Z moves the objective toward the sample

    def fullFocus(self) -> None:
        self._loaded()
        target = self.focus_z(*self.xy)
        if abs(self.z - target) > 300:
            raise RuntimeError("Autofocus: no focus found within the +/-300 um search range")
        self.z = target + float(self.rng.normal(0, 0.1))

    # ------------------------------------------------------------ shutter

    def getShutterOpen(self, *label: str) -> bool:
        self._loaded()
        return self.shutter_open

    def setShutterOpen(self, *args: Any) -> None:
        self._loaded()
        self.shutter_open = bool(args[-1])

    def getAutoShutter(self) -> bool:
        return self.auto_shutter

    def setAutoShutter(self, state: bool) -> None:
        self.auto_shutter = bool(state)
