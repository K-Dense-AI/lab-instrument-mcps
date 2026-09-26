"""Output paths for data files that tools write (CSV, TIFF, NPY...).

Paths come from the model, so they are checked before anything is written: the
file must have an expected extension (so a typo can't clobber ``~/.bashrc``
with CSV), and an existing file is never replaced unless the tool explicitly
offers ``overwrite``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from labmcp.errors import InstrumentError

# Windows device names: "COM3.csv" or "nul.csv" opens the device (on Windows 10, whatever the
# extension), so CSV data could be written to an instrument's serial port.
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in "0123456789¹²³"}
)


def prepare_save_path(
    save_path: str | os.PathLike[str],
    *,
    suffixes: Iterable[str] = (".csv",),
    overwrite: bool = False,
) -> Path:
    """Validate ``save_path`` and return it as an absolute path, ready to write.

    Refuses a path whose extension isn't one of ``suffixes`` (compound ones such as
    ``".csv.gz"`` work), a folder or other non-regular file, a symlink whose target
    doesn't have an allowed extension, Windows device names (``COM3.csv``), a folder
    the server can't write to, and an existing file (unless ``overwrite``). Creates
    missing parent folders. Relative paths are relative to the server's working
    directory, which for a server started by an MCP client is often ``/`` or the
    client's install folder, so errors say so.

    The file isn't created here (a failed acquisition would leave an empty file and
    block the retry). To close the small window between this check and the write,
    open the file with mode ``"x"`` when ``overwrite`` is false.
    """
    allowed = _normalise_suffixes(suffixes)
    raw = os.fspath(save_path)
    hint = ""
    if not str(raw).strip():
        raise InstrumentError("save_path is empty; give a file name. Nothing was written.")
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            hint = (
                f" (a relative save_path is relative to the server's working directory, {Path.cwd()}; "
                "give an absolute path)"
            )
        _check_name(path.name, allowed)
        resolved = path.resolve()  # follows symlinks, so the checks below apply to the real target
    except InstrumentError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:  # bad characters, unknown ~user, ...
        raise InstrumentError(f"save_path {raw!r} is not a usable path: {exc}. Nothing was written.") from exc
    if resolved.name != path.name:  # e.g. data.csv -> ~/.bashrc
        _check_name(resolved.name, allowed, f" ({path.name!r} is a link to {str(resolved)!r})")
    if os.name == "nt":
        _check_windows_name(resolved.name)
    if resolved.is_dir():
        raise InstrumentError(f"save_path {resolved} is a folder; give a file name. Nothing was written.")
    if resolved.exists():
        if not resolved.is_file():
            raise InstrumentError(f"save_path {resolved} is not a regular file. Nothing was written.")
        if not overwrite:
            raise InstrumentError(
                f"{resolved} already exists and was not overwritten. Choose a new save_path. Nothing was written."
            )
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstrumentError(
            f"Cannot create the folder {resolved.parent}: {exc}{hint}. Nothing was written."
        ) from exc
    if not os.access(resolved.parent, os.W_OK):
        raise InstrumentError(f"Cannot write to the folder {resolved.parent}{hint}. Nothing was written.")
    return resolved


def _normalise_suffixes(suffixes: Iterable[str]) -> tuple[str, ...]:
    if isinstance(suffixes, str):  # suffixes=".csv" would otherwise allow ".", "c", "s", "v"
        suffixes = (suffixes,)
    return tuple(s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes if s)


def _check_name(name: str, allowed: tuple[str, ...], why: str = "") -> None:
    low = name.lower()
    # Require a stem, so ".csv" (or ".bashrc") alone isn't accepted as a file name.
    if allowed and not any(low.endswith(s) and len(low) > len(s) for s in allowed):
        raise InstrumentError(
            f"save_path must end in {' or '.join(allowed)} (got {name!r}){why}. Nothing was written."
        )


def _check_windows_name(name: str) -> None:
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if stem in _WINDOWS_DEVICES or ":" in name:
        raise InstrumentError(
            f"save_path {name!r} is a reserved Windows device name or stream; choose another file name. "
            "Nothing was written."
        )
