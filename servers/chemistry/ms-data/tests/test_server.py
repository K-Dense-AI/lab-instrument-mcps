import base64
import csv
import gzip
import os
import shutil
import sqlite3
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_ms_data import analysis, converter
from labmcp_ms_data.driver import BrukerTdfBackend, MsDataDriver, MzMLBackend
from labmcp_ms_data.files import DataRoot, detect_format
from labmcp_ms_data.server import server
from labmcp_ms_data.simulator import COMPOUNDS, DROPOUT_RT_MIN, SyntheticRun, write_mzml

DATA = Path(__file__).parent / "data"
CAFFEINE = COMPOUNDS[0]
VERAPAMIL = COMPOUNDS[4]


@pytest.fixture(scope="module")
def sim_run() -> SyntheticRun:
    return SyntheticRun()


@pytest.fixture(scope="module")
def sim_mzml(tmp_path_factory, sim_run) -> Path:
    folder = tmp_path_factory.mktemp("mzml")
    return write_mzml(sim_run, folder / "synthetic.mzML")


# ---------------------------------------------------------------- files & sandbox


def test_detect_formats(tmp_path):
    (tmp_path / "a.mzML").write_text("<mzML/>")
    (tmp_path / "b.mzML.gz").write_bytes(b"")
    (tmp_path / "c.raw").write_bytes(b"")
    (tmp_path / "d.wiff").write_bytes(b"")
    (tmp_path / "e.lcd").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "tims.d").mkdir()
    (tmp_path / "tims.d" / "analysis.tdf").write_bytes(b"")
    (tmp_path / "baf.d").mkdir()
    (tmp_path / "baf.d" / "analysis.baf").write_bytes(b"")
    (tmp_path / "agilent.d" / "AcqData").mkdir(parents=True)
    (tmp_path / "waters.raw").mkdir()
    (tmp_path / "waters.raw" / "_FUNC001.DAT").write_bytes(b"")
    got = {p.name: detect_format(p) for p in tmp_path.iterdir()}
    assert got == {
        "a.mzML": "mzml",
        "b.mzML.gz": "mzml",
        "c.raw": "thermo_raw",
        "d.wiff": "sciex_wiff",
        "e.lcd": "shimadzu_lcd",
        "notes.txt": None,
        "tims.d": "bruker_tdf",
        "baf.d": "bruker_baf",
        "agilent.d": "agilent_d",
        "waters.raw": "waters_raw",
    }
    runs = DataRoot(tmp_path).find_runs()
    assert len(runs) == 9  # vendor folders are not descended into
    assert not any("AcqData" in str(r.path) or "_FUNC" in str(r.path) for r in runs)


def test_sandbox_refuses_escapes(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "x.mzML").write_text("<mzML/>")
    outside = tmp_path / "secret.mzML"
    outside.write_text("<mzML/>")
    dr = DataRoot(root)
    assert dr.resolve("sub/x.mzML") == (root / "sub" / "x.mzML").resolve()
    assert dr.resolve(str(root / "sub" / "x.mzML")).name == "x.mzML"  # absolute inside is fine
    for bad in ("../secret.mzML", "sub/../../secret.mzML", str(outside)):
        with pytest.raises(InstrumentProtocolError, match="outside the data folder"):
            dr.resolve(bad)
    with pytest.raises(InstrumentProtocolError, match="outside the data folder"):
        dr.output_path("../out.csv")
    try:
        (root / "link.mzML").symlink_to(outside)
        (root / "linkdir").symlink_to(tmp_path)
    except OSError:
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(InstrumentProtocolError, match="outside the data folder"):
        dr.resolve("link.mzML")
    with pytest.raises(InstrumentProtocolError, match="outside the data folder"):
        dr.resolve("linkdir/secret.mzML")
    with pytest.raises(InstrumentProtocolError, match="outside the data folder"):
        dr.output_path("linkdir/out.csv")
    assert [r.path.name for r in dr.find_runs()] == ["x.mzML"]  # the outside link is not listed


def test_missing_data_folder(tmp_path):
    with pytest.raises(InstrumentProtocolError, match="does not exist"):
        DataRoot(tmp_path / "nope")


# ---------------------------------------------------------------- analysis


def test_integrate_gaussian_peak():
    rt = np.linspace(0, 2, 2001)
    sigma_min = 0.05
    y = 1e6 * np.exp(-0.5 * ((rt - 1.0) / sigma_min) ** 2)
    pk = analysis.integrate_apex_peak(rt, y)
    assert pk.apex_rt_min == pytest.approx(1.0)
    assert pk.apex_intensity == pytest.approx(1e6)
    assert pk.area == pytest.approx(1e6 * sigma_min * np.sqrt(2 * np.pi), rel=0.01)
    assert pk.fwhm_s == pytest.approx(2.3548 * sigma_min * 60, rel=0.01)
    assert analysis.integrate_apex_peak(rt, np.zeros_like(rt)) is None


def test_downsample_keeps_maximum():
    x = np.arange(1000.0)
    y = np.zeros(1000)
    y[537] = 5.0
    xs, ys = analysis.downsample_max(x, y, 10)
    assert len(xs) == 10 and max(ys) == 5.0 and 537.0 in xs


# ---------------------------------------------------------------- simulator


def test_synthetic_run_is_plausible(sim_run):
    t = sim_run.table
    assert (t.ms_level == 1).sum() > 1000 and (t.ms_level == 2).sum() > 1000
    assert np.all(np.diff(t.rt_min) > 0)
    assert t.rt_min[-1] == pytest.approx(12.0, abs=0.01)
    ms2 = t.ms_level == 2
    assert np.all(np.isfinite(t.precursor_mz[ms2])) and np.all(t.precursor_charge[ms2] >= 1)
    # MS1 of caffeine at its apex shows the M and M+1 isotopes at the right spacing
    i = int(np.argmin(np.abs(np.where(t.ms_level == 1, t.rt_min, 1e9) - CAFFEINE.rt_min)))
    mz, inten = sim_run.read_peaks(i)
    m0 = np.argmin(np.abs(mz - CAFFEINE.mz))
    m1 = np.argmin(np.abs(mz - (CAFFEINE.mz + 1.003355)))
    assert abs(mz[m0] - CAFFEINE.mz) / CAFFEINE.mz * 1e6 < 5
    assert 0.05 < inten[m1] / inten[m0] < 0.15  # ~8.9 % for C8
    with pytest.raises(InstrumentProtocolError, match="out of range"):
        sim_run.read_peaks(len(t))


# ---------------------------------------------------------------- real mzML through pyteomics


def test_mzml_roundtrip_matches_simulator(sim_run, sim_mzml):
    b = MzMLBackend(sim_mzml)
    try:
        t, s = b.table, sim_run.table
        assert len(t) == len(s)
        assert t.native_id == s.native_id
        np.testing.assert_array_equal(t.ms_level, s.ms_level)
        np.testing.assert_array_equal(t.scan, s.scan)
        np.testing.assert_allclose(t.rt_min, s.rt_min, atol=1e-6)
        np.testing.assert_allclose(t.tic, s.tic, rtol=1e-5)
        np.testing.assert_allclose(t.precursor_mz, s.precursor_mz, rtol=1e-8, equal_nan=True)
        np.testing.assert_array_equal(t.precursor_charge, s.precursor_charge)
        np.testing.assert_array_equal(t.precursor_scan, s.precursor_scan)
        np.testing.assert_allclose(t.injection_time_ms, s.injection_time_ms, rtol=1e-3)
        assert set(t.centroided) == {1} and set(t.polarity) == {1}
        for i in (0, 1, 777, len(t) - 1):
            mz_a, int_a = b.read_peaks(i)
            mz_b, int_b = sim_run.read_peaks(i)
            np.testing.assert_allclose(mz_a, mz_b)
            np.testing.assert_allclose(int_a, int_b)
        md = b.metadata()
        assert md.instrument_model == "Q Exactive"
        assert md.instrument_serial == "SIM-0001"
        assert md.acquisition_date == "2026-01-15T09:30:00Z"
        assert md.ion_mobility is False
        ms1 = np.flatnonzero(t.ms_level == 1)
        assert sum(1 for _ in b.iter_peaks(ms1)) == ms1.size  # sequential path
        assert [i for i, _, _ in b.iter_peaks([5, 3])] == [3, 5]  # random-access path
    finally:
        b.close()


def test_gzipped_mzml(tmp_path, sim_mzml):
    gz = tmp_path / "run.mzML.gz"
    with open(sim_mzml, "rb") as src, gzip.open(gz, "wb", compresslevel=1) as dst:
        shutil.copyfileobj(src, dst)
    b = MzMLBackend(gz)
    try:
        assert len(b.table) > 3000
        assert b.read_peaks(10)[0].size > 0
    finally:
        b.close()


def _b64(values, dtype="<f4", compress=False):
    raw = np.asarray(values, dtype=dtype).tobytes()
    return base64.b64encode(zlib.compress(raw) if compress else raw).decode()


MINIMAL_MZML = """<?xml version="1.0" encoding="utf-8"?>
<indexedmzML xmlns="http://psi.hupo.org/ms/mzml">
<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">
 <cvList count="2"><cv id="MS" fullName="PSI-MS" URI="x"/><cv id="UO" fullName="Unit" URI="y"/></cvList>
 <fileDescription><fileContent><cvParam cvRef="MS" accession="MS:1000579" name="MS1 spectrum" value=""/></fileContent></fileDescription>
 <referenceableParamGroupList count="1"><referenceableParamGroup id="CommonInstrumentParams">
   <cvParam cvRef="MS" accession="MS:1002634" name="Q Exactive Plus" value=""/>
   <cvParam cvRef="MS" accession="MS:1000529" name="instrument serial number" value="Exactive Series slot #1234"/>
 </referenceableParamGroup></referenceableParamGroupList>
 <softwareList count="1"><software id="pwiz" version="3.0"><cvParam cvRef="MS" accession="MS:1000615" name="ProteoWizard software" value=""/></software></softwareList>
 <instrumentConfigurationList count="1"><instrumentConfiguration id="IC1">
   <referenceableParamGroupRef ref="CommonInstrumentParams"/>
   <componentList count="1"><source order="1"><cvParam cvRef="MS" accession="MS:1000073" name="electrospray ionization" value=""/></source></componentList>
 </instrumentConfiguration></instrumentConfigurationList>
 <dataProcessingList count="1"><dataProcessing id="dp"><processingMethod order="0" softwareRef="pwiz"><cvParam cvRef="MS" accession="MS:1000544" name="Conversion to mzML" value=""/></processingMethod></dataProcessing></dataProcessingList>
 <run id="r" defaultInstrumentConfigurationRef="IC1" startTimeStamp="2024-03-01T10:00:00Z">
  <spectrumList count="3" defaultDataProcessingRef="dp">
   {spectra}
  </spectrumList>
 </run>
</mzML>
</indexedmzML>
"""

SPECTRUM = """<spectrum index="{i}" id="scan={scan}" defaultArrayLength="{n}">
 <cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="{level}"/>
 <cvParam cvRef="MS" accession="MS:1000128" name="profile spectrum" value=""/>
 <cvParam cvRef="MS" accession="MS:1000129" name="negative scan" value=""/>
 <scanList count="1"><scan>
  <cvParam cvRef="MS" accession="MS:1000016" name="scan start time" value="{rt_s}" unitCvRef="UO" unitAccession="UO:0000010" unitName="second"/>
 </scan></scanList>
 {precursor}
 <binaryDataArrayList count="2">
  <binaryDataArray encodedLength="0">
   <cvParam cvRef="MS" accession="MS:1000523" name="64-bit float" value=""/>
   <cvParam cvRef="MS" accession="MS:1000576" name="no compression" value=""/>
   <cvParam cvRef="MS" accession="MS:1000514" name="m/z array" value="" unitCvRef="MS" unitAccession="MS:1000040" unitName="m/z"/>
   <binary>{mz}</binary></binaryDataArray>
  <binaryDataArray encodedLength="0">
   <cvParam cvRef="MS" accession="MS:1000521" name="32-bit float" value=""/>
   <cvParam cvRef="MS" accession="MS:1000574" name="zlib compression" value=""/>
   <cvParam cvRef="MS" accession="MS:1000515" name="intensity array" value="" unitCvRef="MS" unitAccession="MS:1000131" unitName="number of detector counts"/>
   <binary>{inten}</binary></binaryDataArray>
 </binaryDataArrayList>
</spectrum>"""

PRECURSOR = """<precursorList count="1"><precursor spectrumRef="scan=1"><selectedIonList count="1"><selectedIon>
 <cvParam cvRef="MS" accession="MS:1000744" name="selected ion m/z" value="301.1" unitCvRef="MS" unitAccession="MS:1000040" unitName="m/z"/>
 </selectedIon></selectedIonList><activation><cvParam cvRef="MS" accession="MS:1000133" name="collision-induced dissociation" value=""/></activation></precursor></precursorList>"""


def write_minimal_mzml(path: Path) -> Path:
    """Hand-written vendor-style mzML: profile, negative mode, RT in seconds, no TIC/base-peak cvParams,
    mixed 64/32-bit and compressed/uncompressed arrays, instrument model via a referenceable group."""
    specs = []
    for i, (scan, level, rt_s, mz, inten) in enumerate(
        [
            (1, 1, 30.0, [300.0, 300.5, 301.1, 301.2, 301.3], [10, 50, 200, 900, 150]),
            (2, 2, 31.5, [100.0, 150.0, 283.0], [5, 40, 20]),
            (3, 1, 33.0, [300.0, 301.2, 400.0], [20, 450, 30]),
        ]
    ):
        specs.append(
            SPECTRUM.format(
                i=i,
                scan=scan,
                n=len(mz),
                level=level,
                rt_s=rt_s,
                precursor=PRECURSOR if level == 2 else "",
                mz=_b64(mz, "<f8"),
                inten=_b64(inten, "<f4", compress=True),
            )  # fmt: skip
        )
    path.write_text(MINIMAL_MZML.replace("{spectra}", "\n".join(specs)))
    return path


def test_minimal_hand_written_mzml(tmp_path):
    b = MzMLBackend(write_minimal_mzml(tmp_path / "vendorish.mzML"))
    t = b.table
    np.testing.assert_allclose(t.rt_min, [0.5, 0.525, 0.55])  # seconds converted to minutes
    np.testing.assert_array_equal(t.scan, [1, 2, 3])
    np.testing.assert_array_equal(t.ms_level, [1, 2, 1])
    np.testing.assert_allclose(t.tic, [1310, 65, 500])  # computed from the arrays
    assert t.base_peak_mz[0] == pytest.approx(301.2)
    assert t.precursor_mz[1] == pytest.approx(301.1) and t.precursor_charge[1] == 0
    assert t.precursor_scan[1] == 1
    assert set(t.centroided) == {0} and set(t.polarity) == {-1}
    md = b.metadata()
    assert md.instrument_model == "Q Exactive Plus"
    assert md.instrument_serial == "Exactive Series slot #1234"
    assert md.acquisition_date == "2024-03-01T10:00:00Z"
    mz, inten = b.read_peaks(2)
    np.testing.assert_allclose(mz, [300.0, 301.2, 400.0])
    np.testing.assert_allclose(inten, [20, 450, 30])
    b.close()


def test_corrupt_mzml_reports_clearly(tmp_path):
    p = tmp_path / "broken.mzML"
    p.write_text("<mzML><run><spectrumList><spectrum index='0'")
    with pytest.raises(InstrumentProtocolError):
        MzMLBackend(p)


# ---------------------------------------------------------------- Bruker TDF


def make_tdf(folder: Path) -> Path:
    d = folder / "sample.d"
    d.mkdir()
    con = sqlite3.connect(d / "analysis.tdf")
    con.executescript(
        """
        CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT);
        INSERT INTO GlobalMetadata VALUES ('InstrumentName','timsTOF Pro 2'), ('InstrumentVendor','Bruker'),
          ('InstrumentSerialNumber','1234567.10'), ('AcquisitionDateTime','2025-05-05T12:00:00.000+02:00'),
          ('AcquisitionSoftware','timsTOF'), ('AcquisitionSoftwareVersion','4.1'),
          ('MzAcqRangeLower','100.0'), ('MzAcqRangeUpper','1700.0'),
          ('OneOverK0AcqRangeLower','0.6'), ('OneOverK0AcqRangeUpper','1.6'), ('SampleName','HeLa 200 ng');
        CREATE TABLE Frames (Id INTEGER PRIMARY KEY, Time REAL, Polarity CHAR(1), ScanMode INTEGER,
          MsMsType INTEGER, TimsId INTEGER, MaxIntensity INTEGER, SummedIntensities INTEGER,
          NumScans INTEGER, NumPeaks INTEGER, AccumulationTime REAL, RampTime REAL);
        CREATE TABLE Precursors (Id INTEGER PRIMARY KEY, LargestPeakMz REAL, AverageMz REAL,
          MonoisotopicMz REAL, Charge INTEGER, ScanNumber REAL, Intensity REAL, Parent INTEGER);
        CREATE TABLE PasefFrameMsMsInfo (Frame INTEGER, ScanNumBegin INTEGER, ScanNumEnd INTEGER,
          IsolationMz REAL, IsolationWidth REAL, CollisionEnergy REAL, Precursor INTEGER);
        """
    )
    frames = []
    for k in range(20):
        t = 60.0 + k * 0.5
        msms = 0 if k % 4 == 0 else 8
        summed = 1e7 if msms == 0 else 1e5
        frames.append((k + 1, t, "+", 8, msms, 0, int(summed / 50), int(summed), 900, 1000, 100.0, 100.0))
    con.executemany("INSERT INTO Frames VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", frames)
    con.executemany(
        "INSERT INTO Precursors VALUES (?,?,?,?,?,?,?,?)",
        [(1, 650.33, 650.8, 650.3301, 2, 400, 5e4, 1), (2, 800.4, 800.9, None, 3, 300, 2e4, 5)],
    )
    con.executemany(
        "INSERT INTO PasefFrameMsMsInfo VALUES (?,?,?,?,?,?,?)",
        [
            (2, 390, 410, 650.8, 2.0, 35.0, 1),
            (3, 390, 410, 650.8, 2.0, 35.0, 1),
            (6, 290, 310, 800.9, 3.0, 40.0, 2),
        ],
    )
    con.commit()
    con.close()
    return d


def test_bruker_tdf_metadata_without_timsrust(tmp_path):
    d = make_tdf(tmp_path)
    assert detect_format(d) == "bruker_tdf"
    b = BrukerTdfBackend(d)
    b.tr = None  # emulate an installation without the [bruker] extra
    md = b.metadata()
    assert md.instrument_model == "timsTOF Pro 2" and md.instrument_serial == "1234567.10"
    assert md.ion_mobility and md.mz_acquisition_range == (100.0, 1700.0)
    assert md.sample_name == "HeLa 200 ng"
    t = b.table
    assert (t.ms_level == 1).sum() == 5 and (t.ms_level == 2).sum() == 2  # ddaPASEF frames -> precursors
    assert np.all(np.diff(t.rt_min) >= 0)
    p1 = t.native_id.index("precursor=1")
    assert t.precursor_mz[p1] == pytest.approx(650.3301) and t.precursor_charge[p1] == 2
    assert t.rt_min[p1] == pytest.approx(60.5 / 60)  # first PASEF frame of the precursor
    p2 = t.native_id.index("precursor=2")
    assert t.precursor_mz[p2] == pytest.approx(800.4)  # no monoisotopic m/z: largest peak
    assert t.tic[t.native_id.index("frame=1")] == 1e7
    with pytest.raises(InstrumentProtocolError, match=r"\[bruker\]"):
        b.read_peaks(0)


def test_bruker_tdf_peaks_with_timsrust(tmp_path):
    pytest.importorskip("timsrust_pyo3")
    d = tmp_path / "dda_test.d"
    shutil.copytree(DATA / "dda_test.d", d)
    b = BrukerTdfBackend(d)
    t = b.table
    assert t.native_id == ["frame=1", "precursor=1", "precursor=2", "frame=3", "precursor=3"]
    mz, inten = b.read_peaks(t.native_id.index("precursor=1"))
    np.testing.assert_allclose(mz, [199.7633445943076])
    np.testing.assert_allclose(inten, [162.0])
    mz, inten = b.read_peaks(0)  # MS1 frame 1, summed over mobility
    assert inten.sum() == pytest.approx(110)  # Frames.SummedIntensities
    assert mz[0] == pytest.approx(100.0) and np.all(np.diff(mz) > 0)


# ---------------------------------------------------------------- converter


def test_conversion_command_lines(tmp_path):
    raw = tmp_path / "sample.raw"
    raw.write_bytes(b"")
    out = tmp_path / "out"
    p = converter.plan_conversion(
        raw,
        "thermo_raw",
        out,
        converter="thermorawfileparser",
        converter_path="/opt/trfp/ThermoRawFileParser.sh",
    )
    assert p.argv == ["/opt/trfp/ThermoRawFileParser.sh", f"-i={raw}", f"-o={out}", "-f=2"]
    assert p.output == out / "sample.mzML"
    p = converter.plan_conversion(
        raw,
        "thermo_raw",
        out,
        converter="thermorawfileparser",
        converter_path="/opt/trfp/ThermoRawFileParser.dll",
        peak_picking=False,
        gzip=True,
    )
    assert p.argv[:2] == ["dotnet", "/opt/trfp/ThermoRawFileParser.dll"] and p.argv[-2:] == ["-p", "-g"]
    assert p.output.name == "sample.mzML.gz"
    p = converter.plan_conversion(
        raw, "thermo_raw", out, converter="msconvert", converter_path="C:/pwiz/msconvert.exe"
    )
    assert p.argv == [
        "C:/pwiz/msconvert.exe",
        str(raw),
        "-o",
        str(out),
        "--mzML",
        "--zlib",
        "--filter",
        "peakPicking vendor msLevel=1-",
    ]
    wiff = tmp_path / "run1.wiff"
    wiff.write_bytes(b"")
    p = converter.plan_conversion(wiff, "sciex_wiff", tmp_path, converter="docker", converter_path="docker")
    assert p.argv[:3] == ["docker", "run", "--rm"]
    assert "-v" in p.argv and f"{tmp_path}:/data" in p.argv
    i = p.argv.index(converter.DOCKER_IMAGE)
    assert p.argv[i + 1 : i + 6] == ["wine", "msconvert", "/data/run1.wiff", "-o", "/data"]
    assert p.output == tmp_path / "run1.mzML"
    with pytest.raises(InstrumentProtocolError, match="only reads Thermo"):
        converter.plan_conversion(wiff, "sciex_wiff", tmp_path, converter="thermorawfileparser")
    with pytest.raises(InstrumentProtocolError, match="no conversion needed"):
        converter.plan_conversion(tmp_path / "x.mzML", "mzml", tmp_path)


def _fake_converter(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_msconvert"
    script.write_text(f"#!{sys.executable}\nimport sys, pathlib, time\n{body}\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.skipif(os.name == "nt", reason="uses a shebang script as the fake converter")
async def test_convert_tool_runs_user_converter(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "QC_01.raw").write_bytes(b"RAW")
    fake = _fake_converter(
        tmp_path,
        "args = sys.argv[1:]\nsrc = pathlib.Path(args[0]); out = pathlib.Path(args[args.index('-o') + 1])\n"
        "(out / (src.stem + '.mzML')).write_text('<mzML/>')\nprint('converted', src.name)",
    )
    opts = {"converter": "msconvert", "converter_path": fake}
    async with simulated_client(server, simulate=False, address=str(data), options=opts) as client:
        dry = (
            await client.call_tool("convert_to_mzml", {"path": "QC_01.raw", "dry_run": True})
        ).structured_content
        assert dry["dry_run"] and dry["command"][0] == fake and not (data / "QC_01.mzML").exists()
        res = (await client.call_tool("convert_to_mzml", {"path": "QC_01.raw"})).structured_content
        assert res["success"] and res["output"] == "QC_01.mzML" and (data / "QC_01.mzML").exists()
        again = await client.call_tool("convert_to_mzml", {"path": "QC_01.raw"}, raise_on_error=False)
        assert again.is_error and "already exists" in again.content[0].text
        bad = await client.call_tool(
            "convert_to_mzml", {"path": "QC_01.raw", "output_folder": "../elsewhere"}, raise_on_error=False
        )
        assert bad.is_error and "outside the data folder" in bad.content[0].text
        refused = await client.call_tool(
            "convert_to_mzml", {"path": "QC_01.raw", "timeout_s": 7200}, raise_on_error=False
        )
        assert refused.is_error and "max_conversion_time_s" in refused.content[0].text


@pytest.mark.skipif(os.name == "nt", reason="uses a shebang script as the fake converter")
def test_conversion_timeout(tmp_path):
    fake = _fake_converter(tmp_path, "time.sleep(30)")
    plan = converter.ConversionPlan("msconvert", [fake], tmp_path / "x.mzML")
    res = converter.run_conversion(plan, timeout_s=0.5)
    assert res.timed_out and res.returncode is None and res.duration_s < 10


def test_no_converter_gives_install_help(tmp_path, monkeypatch):
    monkeypatch.setattr(converter.shutil, "which", lambda name: None)
    raw = tmp_path / "a.raw"
    raw.write_bytes(b"")
    with pytest.raises(InstrumentProtocolError, match="ThermoRawFileParser"):
        converter.plan_conversion(raw, "thermo_raw", tmp_path)


# ---------------------------------------------------------------- MCP round trips


async def test_simulated_tools_via_mcp(tmp_path):
    async with simulated_client(server, address=str(tmp_path)) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        runs = (await client.call_tool("list_runs", {})).structured_content
        assert runs["simulated"] and runs["runs"][0]["path"] == "simulated_lcms_run.mzML"

        ri = (await client.call_tool("get_run_info", {})).structured_content
        assert ri["instrument_model"].startswith("Q Exactive") and ri["polarity"] == "positive"
        assert ri["spectra_by_ms_level"]["1"] > 1000 and ri["rt_end_min"] == pytest.approx(12.0, abs=0.01)
        assert ri["spectrum_type"] == "centroid" and ri["ion_mobility"] is False

        tic = (await client.call_tool("get_tic", {"max_points": 200})).structured_content
        assert tic["points_returned"] == 200 and tic["spectra_used"] > 1000 and tic["kind"] == "TIC"
        bpc = (await client.call_tool("get_bpc", {"rt_start_min": 7.5, "rt_end_min": 8.1})).structured_content
        assert bpc["max_at_rt_min"] == pytest.approx(VERAPAMIL.rt_min, abs=0.05)
        assert bpc["base_peak_mz"][bpc["rt_min"].index(bpc["max_at_rt_min"])] == pytest.approx(
            VERAPAMIL.mz, abs=0.01
        )

        xic = (
            await client.call_tool(
                "extract_ion_chromatogram",
                {"mz": [CAFFEINE.mz, VERAPAMIL.mz, 1500.0], "tolerance": 10, "save_path": "xic.csv"},
            )
        ).structured_content
        caf, ver, none = xic["traces"]
        assert caf["apex_rt_min"] == pytest.approx(CAFFEINE.rt_min, abs=0.02)
        assert ver["apex_rt_min"] == pytest.approx(VERAPAMIL.rt_min, abs=0.02)
        assert caf["apex_intensity"] == pytest.approx(CAFFEINE.height, rel=0.2)
        assert 4 < caf["fwhm_s"] < 10 and caf["area"] > 0 and caf["points_across_peak"] >= 5
        assert none["apex_rt_min"] is None and any("No signal" in w for w in xic["warnings"])
        with open(tmp_path / "xic.csv") as fh:
            rows = list(csv.reader(fh))
        assert rows[0][0] == "rt_min" and len(rows) - 1 == xic["spectra_used"]

        sp = (
            await client.call_tool("get_spectrum", {"rt_min": CAFFEINE.rt_min, "top_n": 5})
        ).structured_content
        assert sp["ms_level"] == 1 and sp["top_peaks"][0]["relative_intensity_percent"] == 100.0
        assert any(abs(p["mz"] - CAFFEINE.mz) < 0.002 for p in sp["top_peaks"])

        hits = (
            await client.call_tool("find_ms2_scans", {"precursor_mz": VERAPAMIL.mz, "tolerance": 5})
        ).structured_content
        assert hits["total_matches"] >= 2
        first = hits["matches"][0]
        assert abs(first["rt_min"] - VERAPAMIL.rt_min) < 0.3 and abs(first["delta_ppm"]) < 5
        ms2 = (
            await client.call_tool("get_spectrum", {"scan_number": first["scan_number"], "save_path": "ms2"})
        ).structured_content
        assert ms2["ms_level"] == 2 and ms2["precursor"]["charge"] == 1
        assert ms2["precursor"]["mz"] == pytest.approx(VERAPAMIL.mz)
        assert any(abs(p["mz"] - 165.0910) < 0.01 for p in ms2["top_peaks"])
        assert (tmp_path / "ms2.csv").exists()

        qc = (await client.call_tool("summarise_run", {})).structured_content
        assert qc["ms1_spectra"] > 1000 and qc["ms2_spectra"] > 1000
        assert qc["tic_dropouts"] >= 1 and qc["tic_dropout_rts_min"][0] == pytest.approx(
            DROPOUT_RT_MIN, abs=0.01
        )
        assert 0.2 < qc["median_cycle_time_s"] < 1.5 and qc["ms2_per_cycle_max"] <= 5
        assert qc["median_ms2_injection_time_ms"] is not None and "1" in qc["ms2_precursor_charges"]
        assert "2" in qc["ms2_precursor_charges"]


async def test_tool_errors_are_clear(tmp_path):
    async with simulated_client(server, address=str(tmp_path)) as client:
        r = await client.call_tool("get_spectrum", {"index": 1, "scan_number": 2}, raise_on_error=False)
        assert r.is_error and "exactly one" in r.content[0].text
        r = await client.call_tool("get_spectrum", {"index": 10**7}, raise_on_error=False)
        assert r.is_error and "out of range" in r.content[0].text
        r = await client.call_tool("get_tic", {"save_path": "../escape.csv"}, raise_on_error=False)
        assert r.is_error and "outside the data folder" in r.content[0].text
        r = await client.call_tool("get_run_info", {"path": "other.mzML"}, raise_on_error=False)
        assert r.is_error and "one synthetic run" in r.content[0].text
        r = await client.call_tool("convert_to_mzml", {"path": "x.raw"}, raise_on_error=False)
        assert r.is_error and "Simulation mode" in r.content[0].text


async def test_xic_target_limit(tmp_path):
    async with simulated_client(server, address=str(tmp_path), limits={"max_xic_targets": 2}) as client:
        r = await client.call_tool(
            "extract_ion_chromatogram", {"mz": [100.0, 200.0, 300.0]}, raise_on_error=False
        )
        assert r.is_error and "max_xic_targets" in r.content[0].text
    with pytest.raises(SafetyLimitError):
        server.limits.check("max_xic_targets", 51)


async def test_read_only_hides_conversion(tmp_path):
    async with simulated_client(server, address=str(tmp_path), read_only=True) as client:
        names = await tool_names(client)
        assert "convert_to_mzml" not in names
        assert {"list_runs", "get_run_info", "get_tic", "get_bpc", "extract_ion_chromatogram", "get_spectrum",
                "find_ms2_scans", "summarise_run", "get_connection_info", "reconnect"} <= names  # fmt: skip


async def test_real_folder_via_mcp(tmp_path, sim_mzml):
    data = tmp_path / "data"
    (data / "batch1").mkdir(parents=True)
    shutil.copy(sim_mzml, data / "batch1" / "sample_A.mzML")
    write_minimal_mzml(data / "blank.mzML")
    (data / "QC.raw").write_bytes(b"")
    shutil.copytree(DATA / "dda_test.d", data / "tims_dda.d")
    async with simulated_client(server, simulate=False, address=str(data)) as client:
        runs = (await client.call_tool("list_runs", {})).structured_content
        by_path = {r["path"]: r for r in runs["runs"]}
        assert set(by_path) == {"batch1/sample_A.mzML", "blank.mzML", "QC.raw", "tims_dda.d"}
        assert by_path["QC.raw"]["readable_directly"] is False and by_path["QC.raw"]["vendor"].startswith(
            "Thermo"
        )
        assert by_path["tims_dda.d"]["format"] == "bruker_tdf"

        r = await client.call_tool("get_run_info", {}, raise_on_error=False)
        assert r.is_error and "Give the `path`" in r.content[0].text
        r = await client.call_tool("get_tic", {"path": "QC.raw"}, raise_on_error=False)
        assert r.is_error and "convert_to_mzml" in r.content[0].text

        ri = (await client.call_tool("get_run_info", {"path": "batch1/sample_A.mzML"})).structured_content
        assert ri["instrument_model"] == "Q Exactive" and not ri["simulated"]
        xic = (
            await client.call_tool(
                "extract_ion_chromatogram",
                {"path": "batch1/sample_A.mzML", "mz": [CAFFEINE.mz], "rt_start_min": 1.5, "rt_end_min": 3.0},
            )
        ).structured_content
        assert xic["traces"][0]["apex_rt_min"] == pytest.approx(CAFFEINE.rt_min, abs=0.02)

        blank = (
            await client.call_tool("get_spectrum", {"path": "blank.mzML", "index": 0, "top_n": 2})
        ).structured_content
        assert blank["spectrum_type"] == "profile" and blank["polarity"] == "negative"
        assert blank["top_peaks"][0]["mz"] == pytest.approx(301.2)

        tims = (await client.call_tool("get_run_info", {"path": "tims_dda.d"})).structured_content
        assert tims["format"] == "bruker_tdf" and tims["ion_mobility"] is True
        assert tims["spectra_by_ms_level"] == {"1": 2, "2": 3}
        ms2 = (
            await client.call_tool("find_ms2_scans", {"path": "tims_dda.d", "precursor_mz": 500.0})
        ).structured_content
        assert ms2["total_matches"] == 1 and ms2["matches"][0]["charge"] == 2


def test_driver_cache_reopens_changed_files(tmp_path, sim_mzml):
    shutil.copy(sim_mzml, tmp_path / "a.mzML")
    drv = MsDataDriver(DataRoot(tmp_path), cache_size=1)
    b1 = drv.open_run("a.mzML")
    assert drv.open_run("a.mzML") is b1
    write_minimal_mzml(tmp_path / "a.mzML")
    os.utime(tmp_path / "a.mzML", (1e9, 1e9))
    b2 = drv.open_run("a.mzML")
    assert b2 is not b1 and len(b2.table) == 3
    drv.close()


def test_mzmlb_via_psims_writer(tmp_path):
    pytest.importorskip("h5py")
    import warnings

    from labmcp_ms_data.driver import MzMLbBackend

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from psims.mzmlb.writer import MzMLbWriter

    path = tmp_path / "run.mzMLb"
    with MzMLbWriter(str(path), close=True) as out:
        out.controlled_vocabularies()
        out.file_description(["MS1 spectrum", "MSn spectrum"])
        out.software_list([{"id": "psims-writer", "version": "1.0", "params": ["python-psims"]}])
        out.instrument_configuration_list(
            [
                out.InstrumentConfiguration(
                    id="IC1", component_list=[], params=["Q Exactive", {"instrument serial number": "X1"}]
                )
            ]
        )
        out.data_processing_list(
            [
                out.DataProcessing(
                    [
                        out.ProcessingMethod(
                            order=1, software_reference="psims-writer", params=["Conversion to mzML"]
                        )
                    ],
                    id="DP1",
                )
            ]
        )
        with out.run(id="r1", instrument_configuration="IC1"), out.spectrum_list(count=2):
            out.write_spectrum(
                np.array([100.0, 200.0]), np.array([10.0, 20.0]), id="scan=1", centroided=True,
                scan_start_time=1.5, params=[{"ms level": 1}, {"total ion current": 30.0}],
            )  # fmt: skip
            out.write_spectrum(
                np.array([50.0, 60.0]), np.array([1.0, 2.0]), id="scan=2", centroided=True, scan_start_time=1.6,
                params=[{"ms level": 2}, {"total ion current": 3.0}],
                precursor_information={"mz": 150.0, "charge": 2, "intensity": 5.0, "scan_id": "scan=1", "activation": ["beam-type collision-induced dissociation", {"collision energy": 30}]},
            )  # fmt: skip
    assert detect_format(path) == "mzmlb"
    b = MzMLbBackend(path)
    t = b.table
    assert b.metadata().instrument_model == "Q Exactive" and b.metadata().instrument_serial == "X1"
    np.testing.assert_array_equal(t.ms_level, [1, 2])
    np.testing.assert_allclose(t.rt_min, [1.5, 1.6])
    assert t.precursor_mz[1] == 150.0 and t.precursor_charge[1] == 2 and t.precursor_scan[1] == 1
    np.testing.assert_allclose(b.read_peaks(1)[1], [1.0, 2.0])
    b.close()
