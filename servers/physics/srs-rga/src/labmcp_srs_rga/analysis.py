"""Peak finding and a simple residual-gas lookup (no instrument I/O).

Fragment ratios are typical 70 eV electron-impact values (NIST / RGA literature); relative
sensitivities are typical ion-gauge values relative to N2. Both vary between instruments, so the
results are a screening aid only.
"""

from __future__ import annotations

from typing import Any

#: m/z -> the usual assignment in vacuum systems.
MZ_LABELS: dict[int, str] = {
    1: "H+ (H2, H2O fragment)",
    2: "H2",
    4: "He",
    12: "C+ (CO, CO2, CH4 fragment)",
    13: "CH+ (CH4 fragment)",
    14: "N+ (N2 fragment) / CH2+",
    15: "CH3+ (CH4, hydrocarbons)",
    16: "O+ / CH4",
    17: "OH+ (H2O fragment) / NH3",
    18: "H2O",
    19: "F+ / H3O+",
    20: "Ar++ / Ne / HF",
    22: "CO2++",
    28: "N2 / CO",
    29: "15N14N / 13CO / C2H5+",
    32: "O2",
    34: "18O16O / H2S",
    36: "36Ar / HCl",
    40: "Ar",
    41: "C3H5+ (hydrocarbons)",
    43: "C3H7+ (hydrocarbons)",
    44: "CO2",
    45: "13CO2 / C2H5O+",
    55: "C4H7+ (hydrocarbons)",
    57: "C4H9+ (hydrocarbons)",
    69: "CF3+ (fluorocarbon, e.g. PFPE oil)",
    78: "C6H6 (benzene)",
}

REL_SENS = {
    "H2": 0.44,
    "He": 0.14,
    "CH4": 1.6,
    "H2O": 1.0,
    "N2": 1.0,
    "CO": 1.05,
    "O2": 0.86,
    "Ar": 1.2,
    "CO2": 1.4,
}
NAMES = {
    "H2": "hydrogen",
    "He": "helium",
    "CH4": "methane",
    "H2O": "water vapour",
    "N2": "nitrogen",
    "CO": "carbon monoxide",
    "O2": "oxygen",
    "Ar": "argon",
    "CO2": "carbon dioxide",
}

CAVEATS = [
    "Pressures are estimates: the head stores a single N2 sensitivity factor; they were corrected with "
    "typical relative sensitivities, which vary with the ionizer settings and the age of the probe.",
    "N2 and CO both appear at m/z 28; they are separated only through the small 14 (N+) and 12 (C+) "
    "fragments, which is unreliable when those peaks are near the noise.",
    "Fragment patterns depend on the instrument; peaks at 16, 12 and 14 have several contributors.",
    "Only common vacuum gases are considered; solvents and other organics need a library search.",
]


def label(mz: float) -> str | None:
    return MZ_LABELS.get(round(mz))


def find_peaks(
    masses: list[float], torr: list[float], noise_torr: float, histogram: bool, max_peaks: int = 30
) -> list[tuple[float, float, str | None]]:
    """Return (m/z, partial pressure, likely species) for peaks above 5x noise, strongest first."""
    if not masses:
        return []
    top = max(torr)
    thr = max(5 * noise_torr, top * 1e-4)
    found: list[tuple[float, float, str | None]] = []
    if histogram:
        for m, p in zip(masses, torr, strict=True):
            if p > thr:
                found.append((m, p, label(m)))
    else:
        windows: dict[int, tuple[float, float]] = {}
        for m, p in zip(masses, torr, strict=True):
            k = round(m)
            if k not in windows or p > windows[k][1]:
                windows[k] = (m, p)
        for k, (m, p) in windows.items():
            if p > thr and abs(m - k) <= 0.45:
                found.append((round(m, 2), p, label(k)))
    found.sort(key=lambda x: -x[1])
    return found[:max_peaks]


def _conf(signal: float, thr: float, ratio_ok: bool = True) -> str:
    if signal > 20 * thr and ratio_ok:
        return "high"
    if signal > 2 * thr:
        return "medium"
    return "low"


def identify_gases(
    spectrum: dict[int, float], noise_torr: float
) -> tuple[list[dict[str, Any]], list[str], list[int], list[str]]:
    """Assign peaks to gases by successive subtraction of fragment patterns.

    Returns (assignments, diagnosis, unassigned m/z, caveats).
    """
    h = {m: max(p, 0.0) for m, p in spectrum.items()}
    top = max(h.values(), default=0.0)
    thr = max(5 * noise_torr, top * 1e-4)
    raw: dict[
        str, tuple[float, int, str, str]
    ] = {}  # gas -> (N2-eq current at main peak, mz, evidence, conf)

    def g(m: int) -> float:
        return h.get(m, 0.0)

    def sub(m: int, amount: float) -> None:
        if m in h:
            h[m] = max(0.0, h[m] - amount)

    if g(40) > thr:
        i = g(40)
        ok = g(20) >= 0.05 * i or i < 20 * thr
        raw["Ar"] = (
            i,
            40,
            f"m/z 40, with Ar++ at 20 ({g(20) / i:.2f} of 40; expected ~0.15)",
            _conf(i, thr, ok),
        )
        sub(40, i)
        sub(20, 0.15 * i)
        sub(36, 0.003 * i)
    if g(44) > thr:
        i = g(44)
        raw["CO2"] = (i, 44, "m/z 44 (fragments at 28, 16, 12 subtracted)", _conf(i, thr))
        for m, f in ((44, 1.0), (28, 0.11), (16, 0.09), (12, 0.087), (22, 0.019), (45, 0.012)):
            sub(m, f * i)
    if g(32) > thr:
        i = g(32)
        raw["O2"] = (i, 32, "m/z 32 (O+ fragment at 16 subtracted)", _conf(i, thr))
        for m, f in ((32, 1.0), (16, 0.11), (34, 0.004)):
            sub(m, f * i)
    if g(18) > thr:
        i = g(18)
        r = g(17) / i
        ok = 0.12 <= r <= 0.4
        raw["H2O"] = (i, 18, f"m/z 18 with OH+ at 17 ({r:.2f} of 18; expected ~0.23)", _conf(i, thr, ok))
        for m, f in ((18, 1.0), (17, 0.23), (16, 0.011), (20, 0.002)):
            sub(m, f * i)
    if g(15) > thr:
        i15 = g(15)
        ch4 = min(i15 / 0.86, max(g(16), i15))
        ok = g(16) >= 0.8 * ch4
        raw["CH4"] = (
            ch4,
            16,
            f"m/z 15 (CH3+) and 16 ({g(15) / max(g(16), 1e-30):.2f} ratio; expected ~0.86)",
            _conf(ch4, thr, ok),
        )
        for m, f in ((16, 1.0), (15, 0.86), (14, 0.16), (13, 0.08), (12, 0.03)):
            sub(m, f * ch4)
    if g(28) > thr:
        i = g(28)
        n2_est = min(g(14) / 0.07, i) if g(14) > thr else 0.0
        co_est = min(g(12) / 0.047, i) if g(12) > thr else 0.0
        if n2_est + co_est > 0:
            n2 = i * n2_est / (n2_est + co_est)
            co = i - n2
            if n2 > thr:
                raw["N2"] = (
                    n2,
                    28,
                    f"share of m/z 28 from the N+ fragment at 14 ({g(14):.1e})",
                    _conf(n2, thr, False),
                )
            if co > thr:
                raw["CO"] = (
                    co,
                    28,
                    f"share of m/z 28 from the C+ fragment at 12 ({g(12):.1e})",
                    _conf(co, thr, False),
                )
        else:
            n2, co = i, 0.0
            raw["N2"] = (i, 28, "m/z 28 (N2 or CO: no 14/12 fragments above noise to separate them)", "low")
        sub(28, i)
        sub(14, 0.07 * n2)
        sub(12, 0.047 * co)
        sub(29, 0.007 * n2 + 0.012 * co)
        sub(16, 0.017 * co)
    if g(2) > thr:
        i = g(2)
        raw["H2"] = (i, 2, "m/z 2", _conf(i, thr))
        sub(2, i)
        sub(1, 0.05 * i)
    if g(4) > thr:
        i = g(4)
        raw["He"] = (i, 4, "m/z 4", _conf(i, thr))
        sub(4, i)

    corrected = {gas: v[0] / REL_SENS[gas] for gas, v in raw.items()}
    total = sum(corrected.values()) or 1.0
    assignments = [
        {
            "species": NAMES[gas],
            "formula": gas,
            "main_mz": raw[gas][1],
            "partial_pressure_torr": corrected[gas],
            "fraction": corrected[gas] / total,
            "evidence": raw[gas][2],
            "confidence": raw[gas][3],
        }
        for gas in sorted(corrected, key=lambda k: -corrected[k])
    ]

    diagnosis: list[str] = []
    n28 = spectrum.get(28, 0.0)
    n32 = spectrum.get(32, 0.0)
    n40 = spectrum.get(40, 0.0)
    if n32 > thr and n28 > thr:
        ratio = n28 / n32
        if 2.5 <= ratio <= 6.0:
            msg = f"Air leak signature: m/z 28/32 = {ratio:.1f} (air ~3.7-4.3 after sensitivity)"
            if n40 > thr:
                msg += f", Ar present (40/28 = {n40 / n28:.3f}, air ~0.012)"
            diagnosis.append(msg + ". Leak-check with helium (`leak_check`).")
    elif n28 > thr and n32 <= thr:
        diagnosis.append("m/z 28 without O2 at 32: typical of CO/N2 outgassing rather than an air leak.")
    if assignments:
        main = assignments[0]["formula"]
        if main == "H2O":
            diagnosis.append("Water vapour dominates: typical of an unbaked system; a bakeout will lower it.")
        elif main == "H2":
            diagnosis.append("Hydrogen dominates: typical of a clean, baked UHV system.")
    if "He" in raw:
        diagnosis.append(
            "Helium detected: a leak-test tracer, a helium leak, or permeation through elastomers."
        )
    hydro = [m for m in (41, 43, 55, 57) if spectrum.get(m, 0.0) > thr]
    if len(hydro) >= 2:
        diagnosis.append(f"Hydrocarbon peaks at {hydro}: contamination, e.g. pump oil or fingerprints.")
    if spectrum.get(69, 0.0) > thr:
        diagnosis.append("m/z 69 (CF3+): fluorocarbon contamination, e.g. PFPE (Fomblin) pump oil.")

    residual_thr = max(thr, 0.02 * top)
    unassigned = sorted(m for m, p in h.items() if p > residual_thr and m not in (1,))
    return assignments, diagnosis, unassigned, list(CAVEATS)
