"""Vendor-file conversion to mzML with a converter the USER has installed.

No vendor libraries are bundled or downloaded (their licences do not allow redistribution).
Supported converters (command lines checked against the official documentation):

* **ProteoWizard msconvert** (https://proteowizard.sourceforge.io/tools/msconvert.html), native
  on Windows with the vendor readers::

      msconvert <input> -o <outdir> --mzML --zlib [--gzip] [--filter "peakPicking vendor msLevel=1-"]

  (``peakPicking [<PickerType> [snr=] [peakSpace=] [msLevel=<ms_levels>]]``; it must be the first
  filter to use the vendor centroiding.)
* **msconvert in Docker** on Linux/macOS, using the image
  ``proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses`` (by pulling it the user accepts
  the vendor licences; https://hub.docker.com/r/proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses)::

      docker run --rm -e WINEDEBUG=-all -v <dir>:/data <image> wine msconvert /data/<file> -o /data ...

* **ThermoRawFileParser** (https://github.com/compomics/ThermoRawFileParser), cross-platform .NET,
  Thermo ``.raw`` only. Options only work in ``-option=value`` form::

      ThermoRawFileParser -i=<input.raw> -o=<outdir> -f=2 [-p] [-g]

  (``-f=2`` indexed mzML; ``-p`` disables the native Thermo peak picking; ``-g`` gzips the output.)

Argument lists are built as lists and run without a shell, with a timeout.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from labmcp import InstrumentProtocolError

from labmcp_ms_data.files import FORMATS, run_stem

DOCKER_IMAGE = "proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses"
CONVERTERS = ("auto", "msconvert", "docker", "thermorawfileparser")
CONVERTIBLE = {"thermo_raw", "waters_raw", "agilent_d", "bruker_baf", "bruker_tdf", "sciex_wiff",
               "shimadzu_lcd", "mzxml"}  # fmt: skip
_TRFP_NAMES = ("ThermoRawFileParser", "ThermoRawFileParser.sh", "ThermoRawFileParser.exe")
_MSCONVERT_NAMES = ("msconvert", "msconvert.exe")


@dataclass
class ConversionPlan:
    converter: str
    argv: list[str]
    output: Path
    notes: list[str] = field(default_factory=list)
    container_name: str | None = None


@dataclass
class ConversionResult:
    returncode: int | None
    duration_s: float
    stdout_tail: str
    stderr_tail: str
    timed_out: bool


def _which(names: tuple[str, ...]) -> str | None:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _launcher(path: str) -> list[str]:
    """How to start a .NET tool given as a .dll / .exe path (ThermoRawFileParser releases)."""
    low = path.lower()
    if low.endswith(".dll"):
        return ["dotnet", path]
    if low.endswith(".exe") and os.name != "nt":
        return ["mono", path]
    return [path]


def choose_converter(fmt: str, requested: str, converter_path: str | None) -> tuple[str, str | None]:
    """Return (converter, executable) for ``fmt``; raise with install hints if none is usable."""
    requested = (requested or "auto").lower()
    if requested not in CONVERTERS:
        raise InstrumentProtocolError(
            f"--option converter must be one of {', '.join(CONVERTERS)} (got {requested!r})."
        )
    if requested == "thermorawfileparser" and fmt != "thermo_raw":
        raise InstrumentProtocolError(
            "ThermoRawFileParser only reads Thermo .raw files. Use converter=msconvert or converter=docker."
        )
    if requested != "auto":
        exe = converter_path
        if exe is None:
            exe = {
                "msconvert": _which(_MSCONVERT_NAMES),
                "docker": _which(("docker",)),
                "thermorawfileparser": _which(_TRFP_NAMES),
            }[requested]
        return requested, exe
    order = ["thermorawfileparser", "msconvert", "docker"] if fmt == "thermo_raw" else ["msconvert", "docker"]
    for conv in order:
        exe = {
            "msconvert": _which(_MSCONVERT_NAMES),
            "docker": _which(("docker",)),
            "thermorawfileparser": _which(_TRFP_NAMES),
        }[conv]
        if exe:
            return conv, exe
    return "none", None


INSTALL_HELP = (
    "No converter found. Install one and restart the server with --option converter=...: "
    "ThermoRawFileParser for Thermo .raw (https://github.com/compomics/ThermoRawFileParser, any OS); "
    "ProteoWizard msconvert on Windows (https://proteowizard.sourceforge.io/download.html); or Docker with "
    f"`docker pull {DOCKER_IMAGE}` on Linux/macOS (pulling it means you accept the vendor licences). "
    "Use --option converter_path=<path> if the program is not on PATH."
)


def plan_conversion(
    input_path: Path,
    fmt: str,
    out_dir: Path,
    *,
    converter: str = "auto",
    converter_path: str | None = None,
    docker_image: str | None = None,
    peak_picking: bool = True,
    gzip: bool = False,
) -> ConversionPlan:
    if fmt not in CONVERTIBLE:
        raise InstrumentProtocolError(
            f"{input_path.name} is {FORMATS.get(fmt, ('', fmt, False))[1]}; it can be read directly, no conversion needed."
            if fmt in ("mzml", "mzmlb")
            else f"{input_path.name} is not a convertible vendor format."
        )
    conv, exe = choose_converter(fmt, converter, converter_path)
    if conv == "none" or not exe:
        raise InstrumentProtocolError(
            INSTALL_HELP if conv == "none" else f"{conv} was not found on PATH. {INSTALL_HELP}"
        )
    ext = ".mzML.gz" if gzip else ".mzML"
    output = out_dir / (run_stem(input_path) + ext)
    notes: list[str] = []
    if conv == "thermorawfileparser":
        argv = _launcher(exe) + [f"-i={input_path}", f"-o={out_dir}", "-f=2"]
        if not peak_picking:
            argv.append("-p")
        if gzip:
            argv.append("-g")
        return ConversionPlan(conv, argv, output, notes)
    flags = ["--mzML", "--zlib"]
    if gzip:
        flags.append("--gzip")
    if peak_picking:
        flags += ["--filter", "peakPicking vendor msLevel=1-"]
    if conv == "msconvert":
        if os.name != "nt":
            notes.append(
                "Native msconvert reads vendor formats only on Windows; on Linux/macOS use converter=docker."
            )
        return ConversionPlan(conv, [exe, str(input_path), "-o", str(out_dir), *flags], output, notes)
    # docker + wine
    image = docker_image or DOCKER_IMAGE
    name = f"labmcp-msconvert-{uuid.uuid4().hex[:12]}"
    in_dir = input_path.parent
    argv = [exe, "run", "--rm", "--name", name, "-e", "WINEDEBUG=-all"]
    if platform.machine().lower() in ("arm64", "aarch64"):
        argv += ["--platform", "linux/amd64"]  # the image is x86-64 only (runs under emulation)
        notes.append("Apple Silicon / ARM: the x86-64 image runs under emulation and is slow.")
    argv += ["-v", f"{in_dir}:/data"]
    out_mount = "/data"
    if out_dir != in_dir:
        argv += ["-v", f"{out_dir}:/out"]
        out_mount = "/out"
    argv += [image, "wine", "msconvert", f"/data/{input_path.name}", "-o", out_mount, *flags]
    if os.name == "posix" and platform.system() == "Linux":
        notes.append("On Linux the output file is owned by root (written inside the container).")
    return ConversionPlan(conv, argv, output, notes, container_name=name)


def _tail(text: str | bytes | None, n: int = 1500) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return text[-n:]


def run_conversion(plan: ConversionPlan, timeout_s: float) -> ConversionResult:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            plan.argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if plan.container_name:  # killing the docker client does not stop the container
            subprocess.run(
                [plan.argv[0], "kill", plan.container_name], capture_output=True, timeout=30, check=False
            )
        return ConversionResult(None, time.monotonic() - t0, _tail(exc.stdout), _tail(exc.stderr), True)
    except OSError as exc:
        raise InstrumentProtocolError(f"Could not start {plan.argv[0]!r}: {exc}. {INSTALL_HELP}") from exc
    return ConversionResult(
        proc.returncode, time.monotonic() - t0, _tail(proc.stdout), _tail(proc.stderr), False
    )
