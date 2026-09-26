r"""Checks applied to raw SCPI text *before* anything is sent to the instrument.

A generic SCPI server lets the model compose arbitrary commands, so the server
itself cannot know which ones are dangerous. This module enforces what it can:

* ``scpi_query`` (a READ tool, available in ``--read-only`` mode) only accepts a
  *single query*: one program message unit whose header ends in ``?``, with no
  ``;`` and no control characters. Queries known to have side effects
  (``*TST?``, ``*CAL?``, ``CALibration...?``, ``DIAGnostic...?``) and anything
  matching ``--option query_denylist`` are refused and must go through
  ``scpi_write``, which the MCP client asks the user to confirm.
* ``--option write_denylist=REGEX`` is never sent by any tool.
* ``--option write_allowlist=REGEX``, when set, is the only thing ``scpi_write``,
  ``scpi_batch`` and ``reset_instrument`` may send (apart from queries that
  ``scpi_query`` would accept).

Patterns are Python regular expressions, matched case-insensitively with
``re.search`` against every message unit in two forms: the text as sent
(upper-cased, whitespace collapsed, leading colon removed) and a normalised
form in which every header mnemonic is reduced to its SCPI short form, e.g.
``:OUTPut1:STATe ON`` becomes ``OUTP1:STAT ON`` (SCPI-99 Vol. 1, 6.2.1
"Mnemonic Generation Rules"). Write patterns against the short form and they
also catch the long form.

SCPI booleans accept any number (SCPI-99 Vol. 1 ch. 7, Boolean: rounded, non-zero is ON),
so deny "everything but OFF" rather than listing the ON spellings:
``^OUTP\d*(:STAT)? (?!OFF\b)`` also refuses ``OUTP 2``, ``OUTP 1.0`` and ``OUTP #H1``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from labmcp import SafetyLimitError

MAX_COMMAND_CHARS = 2000

#: Queries whose *execution* has side effects on (almost) every instrument.
#: Matched against the normalised short form.
BUILTIN_QUERY_DENYLIST: dict[str, str] = {
    r"^\*TST\?": "*TST? runs the instrument's self-test, which can switch relays and outputs "
    "(IEEE 488.2 common command)",
    r"^\*CAL\?": "*CAL? runs an internal self-calibration (IEEE 488.2 common command)",
    r"^CAL\d*[:?]": "CALibration queries such as CAL:ALL? perform a calibration (SCPI-99 Vol. 2, ch. 5)",
    r"^DIAG\d*[:?]": "DIAGnostic commands are instrument-specific service routines (SCPI-99 Vol. 2, ch. 7)",
}

#: Extra queries refused in ``--read-only`` mode: they trigger a new measurement, and on
#: SMUs (e.g. Keithley 2400 with auto output-off disabled) that can energise the output.
READ_ONLY_QUERY_DENYLIST: dict[str, str] = {
    r"^MEAS\d*[:?]": "in --read-only mode MEASure? is refused because on source-measure units it can "
    "switch the output on (use FETCh? for the last reading, or set --option allow_measure_in_read_only=true)",
    r"^READ\d*[:?]": "in --read-only mode READ? is refused because on source-measure units it can switch "
    "the output on (use FETCh? for the last reading, or set --option allow_measure_in_read_only=true)",
}

_VOWELS = set("AEIOU")
_MNEMONIC = re.compile(r"^([A-Z_][A-Z_]*?)(\d*)$")


class CommandRefused(SafetyLimitError):
    """A raw SCPI command was refused by the server's policy. Nothing was sent."""


def short_form(mnemonic: str) -> str:
    """SCPI short form of one header mnemonic (``VOLTage`` -> ``VOLT``, ``LEVel`` -> ``LEV``).

    SCPI-99 Vol. 1, 6.2.1: the short form is the first four characters, or the
    first three when the long form is longer than four characters and the fourth
    is a vowel. A trailing numeric suffix (``OUTPut2``) is kept.
    """
    m = _MNEMONIC.match(mnemonic.upper())
    if not m:
        return mnemonic.upper()
    name, suffix = m.groups()
    if len(name) > 4:
        name = name[:3] if name[3] in _VOWELS else name[:4]
    return name + suffix


def split_units(message: str) -> list[str]:
    """Split a program message into its message units at ``;`` outside quoted strings."""
    units: list[str] = []
    quote: str | None = None
    current = ""
    for ch in message:
        if quote:
            current += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ";":
            units.append(current.strip())
            current = ""
        else:
            current += ch
    if quote:
        raise CommandRefused(
            f"Refused: {message!r} has an unterminated {quote} string. Nothing was sent to the instrument."
        )
    units.append(current.strip())
    return units


def _header_and_params(unit: str) -> tuple[str, str]:
    parts = unit.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _clean_params(params: str) -> str:
    params = re.sub(r"\s+", " ", params.strip().upper())
    return re.sub(r"\s*,\s*", ",", params)


def raw_form(unit: str) -> str:
    """Upper-cased unit with collapsed whitespace and no leading colon."""
    header, params = _header_and_params(unit)
    header = header.upper().lstrip(":")
    return f"{header} {_clean_params(params)}".strip()


def normalized_form(unit: str) -> str:
    """Unit with every header mnemonic reduced to its SCPI short form."""
    header, params = _header_and_params(unit)
    header = header.upper().lstrip(":")
    if header.startswith("*"):
        norm = header
    else:
        query = header.endswith("?")
        nodes = header.rstrip("?").split(":")
        norm = ":".join(short_form(n) for n in nodes) + ("?" if query else "")
    return f"{norm} {_clean_params(params)}".strip()


def expand_headers(units: list[str]) -> list[str]:
    """Message units with the header path the instrument will actually apply.

    SCPI-99 Vol. 1, 6.2.4 (and IEEE 488.2, A.1.1): after a ``;``, a header without a
    leading colon is resolved relative to the path of the previous command, so in
    ``OUTP:POL NORM;STAT ON`` the second unit means ``OUTP:STAT ON``. A leading
    colon returns to the root; common commands (``*...``) leave the path unchanged.
    """
    path: list[str] = []
    out: list[str] = []
    for unit in units:
        header, params = _header_and_params(unit)
        if not header or header.startswith("*"):
            out.append(unit)
            continue
        full = header.lstrip(":") if header.startswith(":") else ":".join([*path, header])
        path = full.split(":")[:-1]
        out.append(f"{full} {params}".strip())
    return out


def is_query_unit(unit: str) -> bool:
    header, _ = _header_and_params(unit)
    return header.endswith("?")


def _check_characters(text: str) -> None:
    if not text.strip():
        raise CommandRefused("Refused: empty command. Nothing was sent to the instrument.")
    if len(text) > MAX_COMMAND_CHARS:
        raise CommandRefused(
            f"Refused: command is {len(text)} characters long (maximum {MAX_COMMAND_CHARS}). "
            "Nothing was sent to the instrument."
        )
    bad = [ch for ch in text if not (32 <= ord(ch) < 127)]
    if bad:
        raise CommandRefused(
            f"Refused: {text!r} contains a newline, control or non-ASCII character ({bad[0]!r}). "
            "Send one program message per call; the server adds the line terminator. "
            "Nothing was sent to the instrument."
        )


def _compile(option: str, pattern: str | None) -> re.Pattern[str] | None:
    if pattern is None or not pattern.strip():
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"--option {option}={pattern!r} is not a valid regular expression: {exc}") from exc


@dataclass
class CommandPolicy:
    """What the raw SCPI tools may send. Build it with :meth:`from_options`."""

    write_denylist: re.Pattern[str] | None = None
    write_allowlist: re.Pattern[str] | None = None
    query_denylist: re.Pattern[str] | None = None
    builtin_query_denylist: dict[str, str] = field(default_factory=lambda: dict(BUILTIN_QUERY_DENYLIST))

    @classmethod
    def from_options(cls, options: dict[str, str], read_only: bool = False) -> CommandPolicy:
        """Read ``write_denylist`` (alias ``denylist``), ``write_allowlist`` and ``query_denylist``.

        In ``--read-only`` mode ``MEASure?`` and ``READ?`` are also refused, because on
        source-measure units they can switch the output on. ``FETCh?`` still works.
        ``--option allow_measure_in_read_only=true`` lifts this for DMM-only setups.
        """
        primary, alias = options.get("write_denylist"), options.get("denylist")
        if primary and alias and primary.strip() != alias.strip():
            # Using one and silently ignoring the other would drop part of the lab's denylist.
            raise ValueError(
                "--option write_denylist and --option denylist (its alias) are both set to different "
                "patterns. Set only one, e.g. write_denylist='(?:A)|(?:B)'"
            )
        deny = primary or alias
        builtin = dict(BUILTIN_QUERY_DENYLIST)
        allow_measure = str(options.get("allow_measure_in_read_only", "")).lower() in {"1", "true", "yes", "on"}
        if read_only and not allow_measure:
            builtin.update(READ_ONLY_QUERY_DENYLIST)
        return cls(
            write_denylist=_compile("write_denylist", deny),
            write_allowlist=_compile("write_allowlist", options.get("write_allowlist")),
            query_denylist=_compile("query_denylist", options.get("query_denylist")),
            builtin_query_denylist=builtin,
        )

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _matches(rx: re.Pattern[str], unit: str) -> bool:
        return bool(rx.search(raw_form(unit)) or rx.search(normalized_form(unit)))

    @staticmethod
    def _fullmatches(rx: re.Pattern[str], unit: str) -> bool:
        return bool(rx.fullmatch(raw_form(unit)) or rx.fullmatch(normalized_form(unit)))

    def _check_denylist(self, text: str, units: list[str]) -> None:
        if self.write_denylist is None:
            return
        # Also check each unit under its implied header path (see expand_headers), or
        # a denylisted command could be reached as the relative tail of a compound message.
        # And split at EVERY ';' too: the instrument's parser may not see the quotes the way
        # split_units does (a '"' inside #<n><len> block data, or a parser without '...'
        # strings), and a unit it runs must never hide inside what we took for a string.
        naive = [u.strip() for u in text.split(";") if u.strip()]
        for unit in [*units, *expand_headers(units), *naive, *expand_headers(naive), text]:
            if self._matches(self.write_denylist, unit):
                raise CommandRefused(
                    f"Refused: {unit!r} matches the command denylist "
                    f"`{self.write_denylist.pattern}` (--option write_denylist). "
                    "Nothing was sent to the instrument. Tell the user; do not try to work around it."
                )

    # ------------------------------------------------------------ public checks

    def check_query(self, text: str) -> str:
        """Validate a read-only query for ``scpi_query``; return it stripped."""
        _check_characters(text)
        text = text.strip()
        hint = (
            " scpi_query only sends a single read-only query. Use `scpi_write` instead "
            "(the user will be asked to confirm). Nothing was sent to the instrument."
        )
        if ";" in text:
            raise CommandRefused(f"Refused: {text!r} contains ';' (more than one message unit).{hint}")
        if not is_query_unit(text):
            raise CommandRefused(f"Refused: {text!r} is not a query (its header does not end with '?').{hint}")
        norm = normalized_form(text)
        for pattern, why in self.builtin_query_denylist.items():
            if re.search(pattern, norm, re.IGNORECASE):
                raise CommandRefused(f"Refused: {why}, so it is not treated as read-only.{hint}")
        if self.query_denylist is not None and self._matches(self.query_denylist, text):
            raise CommandRefused(
                f"Refused: {text!r} matches the query denylist `{self.query_denylist.pattern}` "
                f"(--option query_denylist), i.e. this lab treats it as having side effects.{hint}"
            )
        self._check_denylist(text, [text])
        return text

    def is_safe_query(self, text: str) -> bool:
        try:
            self.check_query(text)
        except CommandRefused:
            return False
        return True

    def check_command(self, text: str) -> list[str]:
        """Validate a command for ``scpi_write`` / ``scpi_batch``; return its message units."""
        _check_characters(text)
        text = text.strip()
        units = [u for u in split_units(text) if u]
        if not units:
            raise CommandRefused("Refused: empty command. Nothing was sent to the instrument.")
        self._check_denylist(text, units)
        if self.write_allowlist is not None:
            # Any ';' at all, even one that looks quoted: a '"' inside block data can make
            # split_units see one unit where the instrument executes several.
            if len(units) > 1 or ";" in text:
                raise CommandRefused(
                    f"Refused: {text!r} is a compound command (';'). A write allowlist is configured, "
                    "so send one command per call (or one per scpi_batch step), without ';'. "
                    "Nothing was sent to the instrument."
                )
            unit = units[0]
            if not self.is_safe_query(unit) and not self._fullmatches(self.write_allowlist, unit):
                raise CommandRefused(
                    f"Refused: {unit!r} (normalised {normalized_form(unit)!r}) is not in the command "
                    f"allowlist `{self.write_allowlist.pattern}` (--option write_allowlist). "
                    "Nothing was sent to the instrument. Tell the user; do not try to work around it."
                )
        return units

    def describe(self) -> dict[str, object]:
        return {
            "write_denylist": self.write_denylist.pattern if self.write_denylist else None,
            "write_allowlist": self.write_allowlist.pattern if self.write_allowlist else None,
            "query_denylist": self.query_denylist.pattern if self.query_denylist else None,
            "builtin_query_denylist": list(self.builtin_query_denylist.values()),
            "matching": "case-insensitive re.search on each message unit, as sent and in SCPI short form "
            "(e.g. ':OUTPut:STATe ON' is also checked as 'OUTP:STAT ON'); denylist checks also resolve "
            "relative headers in compound messages ('OUTP:POL NORM;STAT ON' -> 'OUTP:STAT ON') and also "
            "split at every ';', even inside quotes; the allowlist must match a whole unit and refuses any ';'",
        }
