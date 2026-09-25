"""Concise SCPI primer returned by the ``scpi_primer`` tool (SCPI-99 Vol. 1 and 2)."""

PRIMER = """\
SCPI QUICK PRIMER (SCPI-99, IEEE 488.2)

1. Headers are trees of mnemonics separated by ':'.  Each mnemonic has a long form and a short
   form: the documented spelling shows the short form in UPPER case, e.g. SOURce:VOLTage:LEVel
   -> SOUR:VOLT:LEV.  Only the exact short or exact long form is accepted; case does not matter.
   Short form rule: first 4 letters, or first 3 if the long form is longer than 4 letters and
   the 4th is a vowel (LEVel -> LEV, ERRor -> ERR).
2. [Bracketed] nodes are optional defaults: [SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude] 5
   can be sent as VOLT 5.  Numeric suffixes select a channel/port: OUTPut2, MEASure:VOLTage? (@101).
3. A query is a header ending in '?', optionally followed by parameters:
   *IDN?   MEAS:VOLT:DC?   MEAS:VOLT:DC? 10,0.001   VOLT? MAX.
   Commands set things and return nothing: VOLT 5.0, OUTP ON, CONF:VOLT:DC 10.
4. Parameters: numbers (5, 5.0, 5E-3, often with units: 5 V, 10 MHZ), MIN/MAX/DEF, booleans
   ON|OFF|1|0 (queries answer 1/0), quoted strings "text", enumerations (NORMal, SWAPped).
5. ';' joins message units in one message: VOLT 5;CURR 0.1.  A unit after ';' is relative to
   the previous header's node; start it with ':' to return to the root (VOLT 5;:OUTP ON).
   IEEE 488.2 common commands (*CLS, *RST ...) can appear anywhere.
6. IEEE 488.2 common commands:  *IDN? identity   *RST reset to defaults   *CLS clear status &
   error queue   *OPC? returns 1 when pending operations finish   *WAI wait   *ESR? event
   status register (bit 5 command error, 4 execution error, 3 device error, 2 query error,
   0 operation complete)   *STB? status byte (bit 2: error queue not empty)   *TST? self-test.
7. Errors are queued, not returned: after commands, read SYSTem:ERRor? until it answers
   0,"No error".  -1xx command/syntax errors (-113 Undefined header = wrong mnemonic),
   -2xx execution errors (-222 Data out of range, -221 Settings conflict, -224 Illegal
   parameter value), -3xx device errors (-350 Queue overflow), -4xx query errors
   (-410 Query INTERRUPTED, -420 Query UNTERMINATED).  Positive codes are vendor-specific.
   A query the instrument does not understand produces NO reply (the read times out) plus a
   -113 in the queue.
8. Measurement pattern (SCPI-99 Vol. 2, ch. 3): MEASure:<function>? (= CONFigure + READ?) for a
   quick reading; CONFigure:<function> ... then READ? (= INITiate + FETCh?) when
   you need to change settings in between; FETCh? returns the last readings without triggering.
9. Binary data: FORMat[:DATA] REAL,32|REAL,64|INTeger,16 selects IEEE 754 / integer blocks sent
   as #<n><length><bytes>; FORMat:BORDer NORMal|SWAPped sets byte order.  Read those replies with
   query_binary_block, not scpi_query.  FORMat[:DATA] ASCii returns comma-separated numbers.
10. Command sets differ between vendors and models (and older instruments may not be SCPI at
   all).  Always check the instrument's programming manual; read settings back with queries
   and check the error queue after every change.

SAFETY
- *RST returns every setting to its default. SCPI requires OUTPut:STATe OFF at *RST, but it also
  changes ranges, levels, trigger and source settings, and not every instrument complies.
- On source-measure units (e.g. Keithley 2400), MEASure?, READ?, INITiate and CONFigure can switch
  the source output ON (Keithley 2400 Series User's Manual 2400S-900-01, sections 11 and 17).
  Treat them as hazardous on SMUs: send them with scpi_write so the user confirms.
- A generic SCPI server cannot know how to make an arbitrary instrument safe. device_clear only
  recovers communication (and sends ABORt); apply_safe_state exists only if the user configured
  their instrument's safe-state commands.
"""
