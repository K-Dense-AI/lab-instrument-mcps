import os
from pathlib import Path

import pytest
from labmcp import SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_ms_worklist.driver import ControlPlan, Defaults, WorklistError, WorklistStore
from labmcp_ms_worklist.formats import (
    FORMATS,
    Sample,
    check_position,
    detect_format,
    encode,
    filename_problem,
    generate_positions,
    parse,
    render,
)
from labmcp_ms_worklist.server import server

GOLDEN = Path(__file__).parent / "golden"
REGEN = os.environ.get("REGEN_GOLDEN") == "1"


def golden_samples(fmt: str) -> list[Sample]:
    """Fixed samples using only the fields each format has a column for."""
    if fmt == "waters_masslynx":
        return [
            Sample(sample_name="Solvent blank", data_file="ASSAY001", sample_type="blank", position="1:A,1",
                   injection_volume_ul=5, ms_method="MRM_panel", lc_method="Gradient_10min", sample_id="B1"),
            Sample(sample_name="Cal 1 ng/mL", data_file="ASSAY002", sample_type="standard", position="1:A,2",
                   injection_volume_ul=5, ms_method="MRM_panel", lc_method="Gradient_10min", tune_file="ESIpos.ipr",
                   extra={"CONC_A": "1"}),
            Sample(sample_name="Plasma 01", data_file="ASSAY003", sample_type="sample", position="1:B,1",
                   injection_volume_ul=2.5, ms_method="MRM_panel", lc_method="Gradient_10min", sample_id="P-01"),
            Sample(sample_name="Pooled QC", data_file="ASSAY004", sample_type="qc", position="1:H,12",
                   injection_volume_ul=5, ms_method="MRM_panel", lc_method="Gradient_10min"),
        ]
    if fmt == "sciex_os":
        return [
            Sample(sample_name="Blank", data_file="Batch_2026-09-26", sample_type="blank", position="1",
                   injection_volume_ul=5, ms_method="MRM_panel.msm", lc_method="Gradient.lcm", sample_id="B1"),
            Sample(sample_name="Cal 1", data_file="Batch_2026-09-26", sample_type="standard", position="2",
                   injection_volume_ul=5, ms_method="MRM_panel.msm", lc_method="Gradient.lcm",
                   processing_method="Panel.qmethod", comment="1 ng/mL, in water"),
            Sample(sample_name="Plasma 01", data_file="Batch_2026-09-26", sample_type="sample", position="3",
                   injection_volume_ul=2.5, ms_method="MRM_panel.msm", lc_method="Gradient.lcm", sample_id="P-01"),
            Sample(sample_name="QC mid", data_file="Batch_2026-09-26", sample_type="qc", position="4",
                   ms_method="MRM_panel.msm", lc_method="Gradient.lcm"),
        ]
    if fmt == "agilent_masshunter":
        return [
            Sample(sample_name="Blank", data_file="Run001", sample_type="blank", position="P1-A1",
                   injection_volume_ul=5, ms_method="D:\\MassHunter\\methods\\Panel.m"),
            Sample(sample_name="Cal L1", data_file="Run002", sample_type="standard", position="P1-A2", level="L1",
                   injection_volume_ul=5, ms_method="D:\\MassHunter\\methods\\Panel.m", comment="1 ng/mL"),
            Sample(sample_name="Plasma 01", data_file="Run003", sample_type="sample", position="P1-B1",
                   injection_volume_ul=None, ms_method="D:\\MassHunter\\methods\\Panel.m"),
            Sample(sample_name="QC, mid", data_file="Run004", sample_type="qc", position="P2-A1",
                   injection_volume_ul=2.5, ms_method="D:\\MassHunter\\methods\\Panel.m",
                   extra={"Barcode": "QC123"}),
        ]
    if fmt == "thermo_xcalibur":
        return [
            Sample(sample_name="Blank", data_file="20260926_blank_01", sample_type="blank", position="R:A1",
                   injection_volume_ul=5, ms_method="C:\\Xcalibur\\methods\\DIA_60min",
                   data_path="D:\\Data\\2026", sample_id="B1"),
            Sample(sample_name="Cal 1", data_file="20260926_cal_01", sample_type="standard", position="R:A2",
                   injection_volume_ul=5, ms_method="C:\\Xcalibur\\methods\\DIA_60min", level="1",
                   processing_method="C:\\Xcalibur\\methods\\quan.pmd", data_path="D:\\Data\\2026"),
            Sample(sample_name="Plasma 01", data_file="20260926_s01", sample_type="sample", position="R:B1",
                   injection_volume_ul=2.5, ms_method="C:\\Xcalibur\\methods\\DIA_60min",
                   data_path="D:\\Data\\2026", comment='said "hi", twice'),
            Sample(sample_name="QC", data_file="20260926_qc_01", sample_type="qc", position="R:H12",
                   injection_volume_ul=5, ms_method="C:\\Xcalibur\\methods\\DIA_60min",
                   data_path="D:\\Data\\2026", extra={"Dil Factor": "2"}),
        ]
    raise AssertionError(fmt)


EXT = {"waters_masslynx": "csv", "sciex_os": "csv", "agilent_masshunter": "csv", "thermo_xcalibur": "csv"}


# ------------------------------------------------------------------ golden files


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_golden_exact_bytes(fmt):
    spec = FORMATS[fmt]
    data = encode(spec, render(spec, golden_samples(fmt)))
    path = GOLDEN / f"{fmt}.{EXT[fmt]}"
    if REGEN:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    assert data == path.read_bytes()


def test_golden_details():
    waters = (GOLDEN / "waters_masslynx.csv").read_bytes()
    assert waters.startswith(b"FILE_NAME,FILE_TEXT,MS_FILE,MS_TUNE_FILE,INLET_FILE,SAMPLE_LOCATION,TYPE,ID,INJ_VOL,CONC_A,Index\r\n")
    assert b'"1:A,1"' in waters  # the comma in a MassLynx plate position is quoted
    assert b"\n" not in waters.replace(b"\r\n", b"")

    agilent = (GOLDEN / "agilent_masshunter.csv").read_bytes()
    assert agilent.startswith("\ufeffSample Name,Barcode,Rack Code,Sample Position,Method,Data File,Sample Type,"
                              "Level Name,Inj Vol (µL),Comment\r\n".encode())
    assert b",Run003.d,Sample,,-1," in agilent  # "As method" is -1
    assert b'"QC, mid"' in agilent

    xcal = (GOLDEN / "thermo_xcalibur.csv").read_bytes()
    lines = xcal.split(b"\r\n")
    assert lines[0] == b"Bracket Type=4"
    assert lines[1].startswith(b"Sample Type,File Name,Sample ID,Path,Instrument Method,Process Method,")
    assert b'"said ""hi"", twice"' in xcal

    sciex = (GOLDEN / "sciex_os.csv").read_bytes()
    assert sciex.startswith(b"Sample Name,Sample ID,Sample Type,MS Method,LC Method,")
    assert b"QualityControl" in sciex


# ------------------------------------------------------------------ round trips


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_round_trip(fmt):
    spec = FORMATS[fmt]
    original = golden_samples(fmt)
    parsed = parse(encode(spec, render(spec, original)).decode("utf-8-sig"))
    assert parsed.format == fmt
    assert [s.content() for s in parsed.samples] == [s.content() for s in original]


def test_round_trip_semicolon_xcalibur():
    spec = FORMATS["thermo_xcalibur"]
    original = golden_samples("thermo_xcalibur")
    text = render(spec, original, delimiter=";", bracket_type=2)
    parsed = parse(text)
    assert parsed.delimiter == ";"
    assert parsed.bracket_type == 2
    assert [s.content() for s in parsed.samples] == [s.content() for s in original]


def test_parse_waters_vendor_example():
    # First lines of ANALYSIS_qmeth1.csv attached to Waters KB WKB63781.
    text = (
        "FILE_NAME,FILE_TEXT,MS_FILE,MS_TUNE_FILE,INLET_FILE,SAMPLE_LOCATION,SAMPLE_GROUP,TYPE,ID,CONC_A,CONC_B,"
        "CONC_C,CONC_D,CONC_E,CONC_F,INJ_VOL,QUAN_REF,METH_DB,Index\r\n"
        "ASSAY01,plasma blank,DEFAULT,,DEFAULT,1,,Blank,ID,0,0,0,0,0,0,10,,Qmeth1.mdb,1\r\n"
        "ASSAY03,0.5pg/ml std,DEFAULT,,DEFAULT,3,,Standard,ID3,0.5,0,0,0,0,0,10,X,Qmeth1.mdb,3\r\n"
        "ASSAY14,Rat sample 02,DEFAULT,,DEFAULT,14,,Analyte,ID14,0,0,0,0,0,0,10,,Qmeth1.mdb,14\r\n"
    )
    assert detect_format(text) == "waters_masslynx"
    p = parse(text)
    assert [s.sample_type for s in p.samples] == ["blank", "standard", "sample"]
    s = p.samples[1]
    assert (s.data_file, s.sample_name, s.position, s.injection_volume_ul) == ("ASSAY03", "0.5pg/ml std", "3", 10)
    assert s.extra["QUAN_REF"] == "X" and s.extra["CONC_A"] == "0.5"


def test_parse_agilent_vendor_example():
    text = (
        "Sample Name,Barcode,Rack Code,Sample Position,Method,Data File,Sample Type,Level Name,Inj Vol (µL),Comment\n"
        "Standard1,,,P1-A1,,,Calibration,L1,1,Comment1\n"
        "Blank0,,,,,,Sample,,1,Comment6\n"
    )
    p = parse(text)
    assert p.format == "agilent_masshunter"
    assert p.samples[0].sample_type == "standard" and p.samples[0].level == "L1"
    assert p.samples[0].position == "P1-A1"


def test_xcalibur_unknown_type_kept_verbatim():
    text = "Bracket Type=4\r\nSample Type,File Name,Position,Instrument Method\r\nStd Clear,f1,1,m\r\n"
    p = parse(text)
    assert p.samples[0].sample_type == "standard"
    assert p.samples[0].extra["Sample Type"] == "Std Clear"
    assert "Std Clear" in render(FORMATS["thermo_xcalibur"], p.samples)


def test_detect_format_rejects_unknown():
    with pytest.raises(ValueError, match="recognise"):
        detect_format("foo,bar\n1,2\n")


# ------------------------------------------------------------------ helpers


def test_positions():
    assert check_position("A1", "well", 96, 100) is None
    assert "outside" in check_position("I1", "well", 96, 100)
    assert check_position("P16-P24", "agilent_plate_well", 384, 0) is None
    assert "does not match" in check_position("A1", "vial_number", 96, 100)
    assert "outside" in check_position("150", "vial_number", 96, 100)
    assert generate_positions("well", 96, 0, "H11", 2) == ["H11", "H12"]
    assert generate_positions("agilent_plate_well", 96, 0, "P1-H12", 2) == ["P1-H12", "P2-A1"]
    assert generate_positions("waters_plate_well", 96, 0, "1:A,12", 2) == ["1:A,12", "1:B,1"]
    with pytest.raises(ValueError):
        generate_positions("well", 96, 0, "H12", 2)


def test_filename_rules():
    assert filename_problem("ok_name-1") is None
    assert "not allow" in filename_problem("a/b")
    assert "reserved" in filename_problem("CON")
    assert "space or a dot" in filename_problem("name.")


# ------------------------------------------------------------------ store / sandbox


def make_store(tmp_path) -> WorklistStore:
    return WorklistStore(tmp_path / "out")


def test_sandbox_refuses_escapes(tmp_path):
    store = make_store(tmp_path)
    for bad in ("../x.csv", "sub/../../x.csv", str(tmp_path / "elsewhere.csv"), "..\\x.csv"):
        with pytest.raises(WorklistError, match="Refused"):
            store.write_file(bad, b"x", overwrite=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorklistError, match="outside"):
        store.write_file("link/x.csv", b"x", overwrite=False)
    assert not (outside / "x.csv").exists()
    # absolute path inside the folder is fine
    store.write_file(str(store.root / "ok.csv"), b"x", overwrite=False)


def test_no_overwrite_without_flag(tmp_path):
    store = make_store(tmp_path)
    store.write_file("a.csv", b"one", overwrite=False)
    with pytest.raises(WorklistError, match="already exists"):
        store.write_file("a.csv", b"two", overwrite=False)
    store.write_file("a.csv", b"two", overwrite=True)
    assert (store.root / "a.csv").read_bytes() == b"two"


def test_qc_blank_insertion_and_seeded_randomisation(tmp_path):
    store = make_store(tmp_path)
    samples = [Sample(sample_name=f"S{i}") for i in range(1, 7)] + [Sample(sample_name="Cal", sample_type="standard")]
    d = Defaults(ms_method="m.meth", injection_volume_ul=2, position_pattern="well", first_position="A1")
    wl = store.create("WL1", "thermo_xcalibur", samples, d)
    plan = ControlPlan(blank_every_n=3, qc_at_start=1, qc_at_end=1, qc_position="H12", blank_position="H11",
                       randomize=True, seed=42)
    store.set_plan("WL1", plan)
    rows = store.samples(wl)
    types = [s.sample_type for s in rows]
    assert types[0] == "qc" and types[-1] == "qc"
    assert types.count("blank") == 2  # after samples 3 and 6 of 7 (none after the last)
    assert rows[-2].sample_name == "Cal"  # standards are not shuffled by default
    order = [s.sample_name for s in rows if s.sample_type == "sample"]
    assert sorted(order) == [f"S{i}" for i in range(1, 7)] and order != sorted(order)
    # same seed -> same order; calling again doesn't duplicate controls
    store.set_plan("WL1", plan.model_copy())
    assert [s.sample_name for s in store.samples(wl)] == [s.sample_name for s in rows]
    # data files follow run order and are unique
    files = [s.data_file for s in rows]
    assert files[0] == "WL1_001_QC" and len(set(files)) == len(files)
    # positions were auto-assigned to samples, controls use their own vial
    assert rows[1].position in {f"A{i}" for i in range(1, 8)}
    assert wl.history[-1]["seed"] == 42


def test_seed_recorded_when_omitted(tmp_path):
    store = make_store(tmp_path)
    store.create("WL", "sciex_os", [Sample(sample_name="a"), Sample(sample_name="b")], Defaults())
    wl = store.set_plan("WL", ControlPlan(randomize=True))
    assert isinstance(wl.plan.seed, int)
    assert wl.history[-1]["seed"] == wl.plan.seed


def test_validation_catches_problems(tmp_path):
    store = make_store(tmp_path)
    samples = [
        Sample(sample_name="a", data_file="dup", position="A1", injection_volume_ul=5),
        Sample(sample_name="b", data_file="DUP", position="Z9", injection_volume_ul=500),
        Sample(sample_name="c", data_file="bad:name", position="A2", injection_volume_ul=5, ms_method="x.dam"),
        Sample(sample_name="d", data_file="ok", position="A3"),
    ]
    store.create("V", "agilent_masshunter", samples, Defaults(ms_method="Panel.m", position_pattern="well"))
    report = store.validate("V", max_injection_volume_ul=100)
    msgs = " | ".join(f"{i.row}:{i.field}:{i.message}" for i in report.errors)
    assert not report.valid
    assert "2:data_file:duplicate data file" in msgs
    assert "2:position" in msgs
    assert "2:injection_volume_ul" in msgs and "max_injection_volume_ul" in msgs
    assert "3:data_file" in msgs and "not allow" in msgs
    assert "3:ms_method" in msgs  # .dam is not a MassHunter .m method
    with pytest.raises(WorklistError, match="Not exported"):
        store.export("V", max_injection_volume_ul=100)
    assert store.list_files() == []


def test_waters_requires_its_columns(tmp_path):
    store = make_store(tmp_path)
    store.create("W", "waters_masslynx", [Sample(sample_name="x")], Defaults())
    fields = {i.field for i in store.validate("W", max_injection_volume_ul=100).errors}
    assert {"ms_method", "lc_method", "position", "injection_volume_ul"} <= fields


def test_sciex_shared_data_file_allowed(tmp_path):
    store = make_store(tmp_path)
    samples = [Sample(sample_name=n, data_file="batch1") for n in ("a", "b", "a")]
    store.create("S", "sciex_os", samples, Defaults(ms_method="m.msm"))
    errors = store.validate("S", max_injection_volume_ul=100).errors
    assert len(errors) == 1 and errors[0].row == 3 and errors[0].field == "sample_name"


def test_export_template_and_convert(tmp_path):
    store = make_store(tmp_path)
    # A header as exported from the user's own SCIEX OS (tab separated, different spelling).
    (store.root / "blank_batch.txt").write_text("Sample Name\tSample Type\tAcquisition Method\tVial Position\t"
                                                "Injection Volume (µL)\tData File\tBarcode ID\r\n", encoding="utf-8")
    samples = [Sample(sample_name="a", position="1", injection_volume_ul=3)]
    store.create("T", "sciex_os", samples, Defaults(ms_method="m.msm", data_file_pattern="{worklist}"))
    out = store.export("T", max_injection_volume_ul=100, template_path="blank_batch.txt", filename="T.txt")
    text = (store.root / "T.txt").read_bytes().decode("utf-8")
    assert text == ("Sample Name\tSample Type\tAcquisition Method\tVial Position\tInjection Volume (µL)\tData File\t"
                    "Barcode ID\r\na\tUnknown\tm.msm\t1\t3\tT\t\r\n")
    assert out["acquisition_started"] is False
    assert out["provenance_file"] == "T.provenance.json"
    # import it back and convert to Xcalibur
    wl, _ = store.import_file("T.txt", name="T2")
    assert store.samples(wl)[0].ms_method == "m.msm"
    wl.base[0].sample.ms_method = "C:\\methods\\m.meth"
    res = store.export("T2", max_injection_volume_ul=100, fmt="thermo_xcalibur", write_provenance=False)
    assert res["preview"][0] == "Bracket Type=4"
    assert res["relative_path"] == "T2_thermo_xcalibur.csv"


# ------------------------------------------------------------------ MCP round trip


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        outdir = Path(info["instrument"]["output_dir"])

        formats = (await client.call_tool("list_formats", {})).structured_content
        assert {f["format"] for f in formats["formats"]} == set(FORMATS)
        assert all(f["sources"] and f["verified"] for f in formats["formats"])

        wl = (await client.call_tool("create_worklist", {
            "name": "Plasma", "format": "agilent_masshunter",
            "samples": ["P01", "P02", "P03", {"sample_name": "P04", "sample_id": "X"}],
            "ms_method": "D:\\MassHunter\\methods\\Panel.m", "injection_volume_ul": 2,
            "position_pattern": "agilent_plate_well", "first_position": "P1-A1",
        })).structured_content
        assert wl["samples"][3]["position"] == "P1-A4"

        wl = (await client.call_tool("insert_qc_blanks", {
            "name": "Plasma", "blank_every_n": 2, "blank_position": "P2-A1", "qc_at_start": 1,
            "qc_position": "P2-A2", "randomize": True, "seed": 7,
        })).structured_content
        assert wl["randomisation_seed"] == 7 and wl["sample_count"] == 6

        await client.call_tool("add_samples", {"name": "Plasma", "samples": ["P05"]})

        report = (await client.call_tool("validate_worklist", {"name": "Plasma"})).structured_content
        assert report["valid"], report["errors"]

        out = (await client.call_tool("export_worklist", {"name": "Plasma"})).structured_content
        assert out["preview"][0].startswith("\ufeff") is False
        assert out["preview"][0].startswith("Sample Name,Barcode")
        assert (outdir / "Plasma_agilent_masshunter.csv").exists()

        again = await client.call_tool("export_worklist", {"name": "Plasma"}, raise_on_error=False)
        assert again.is_error and "already exists" in again.content[0].text

        bad = await client.call_tool("export_worklist", {"name": "Plasma", "filename": "../evil.csv"},
                                     raise_on_error=False)
        assert bad.is_error and "Refused" in bad.content[0].text

        imp = (await client.call_tool("import_worklist", {"path": "Plasma_agilent_masshunter.csv",
                                                          "name": "Copy"})).structured_content
        assert imp["format"] == "agilent_masshunter" and imp["sample_count"] == 8

        conv = await client.call_tool("export_worklist", {"name": "Copy", "format": "thermo_xcalibur"},
                                      raise_on_error=False)
        # a MassHunter '.m' method is not an Xcalibur '.meth' instrument method
        assert conv.is_error and "ms_method" in conv.content[0].text and ".meth" in conv.content[0].text

        listing = (await client.call_tool("list_worklists", {})).structured_content
        assert {d["name"] for d in listing["drafts"]} == {"Plasma", "Copy"}


async def test_injection_volume_limit_refuses():
    async with simulated_client(server, limits={"max_injection_volume_ul": 10}) as client:
        res = await client.call_tool("create_worklist", {
            "name": "Big", "format": "sciex_os", "samples": [{"sample_name": "a", "injection_volume_ul": 20}],
        }, raise_on_error=False)
        assert res.is_error and "max_injection_volume_ul" in res.content[0].text


def test_limit_check_direct():
    server.configure(simulate=True, limits={"max_injection_volume_ul": 10})
    try:
        with pytest.raises(SafetyLimitError):
            server.check("max_injection_volume_ul", 11)
    finally:
        server.configure(simulate=True, limits={})
        server.disconnect()


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        for t in ("list_formats", "validate_worklist", "import_worklist", "get_worklist", "list_worklists",
                  "get_connection_info", "reconnect"):
            assert t in names
        for t in ("create_worklist", "add_samples", "insert_qc_blanks", "export_worklist"):
            assert t not in names


def test_simulated_folder_removed_on_close():
    from labmcp_ms_worklist.simulator import simulated_store

    store = simulated_store()
    root = store.root
    store.write_file("x.csv", b"1", overwrite=False)
    store.close()
    assert not root.exists()
