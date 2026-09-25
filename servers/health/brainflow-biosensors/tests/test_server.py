import asyncio
import time

import numpy as np
import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_brainflow.driver import (
    BiosensorBoard,
    NumpyDsp,
    band_power,
    brainflow_error,
    downsample,
    input_params,
    railed_percent,
    resolve_board,
    welch_psd,
)
from labmcp_brainflow.server import server
from labmcp_brainflow.simulator import FAKE_BOARD_ID, FakeBoardShim, FakeLibrary


def _brainflow_available() -> bool:
    try:
        from labmcp_brainflow.driver import BrainFlowLibrary

        lib = BrainFlowLibrary()
        lib.board_descr(-1)
        return True
    except Exception:
        return False


HAVE_BRAINFLOW = _brainflow_available()
SIMULATORS = ["fake", pytest.param("brainflow", marks=pytest.mark.skipif(not HAVE_BRAINFLOW, reason="no BrainFlow"))]


def fake_board() -> BiosensorBoard:
    return BiosensorBoard(FakeBoardShim(seed=1), FakeLibrary("test"), FAKE_BOARD_ID, "FAKE_EEG")


# ------------------------------------------------------------------ board table / params


def test_resolve_board_aliases_names_and_ids():
    assert resolve_board("cyton").enum_name == "CYTON_BOARD"
    assert resolve_board("CYTON_DAISY_BOARD").board_id == 2
    assert resolve_board("muse_s_athena").board_id == 67
    assert resolve_board("0").enum_name == "CYTON_BOARD"
    assert resolve_board(None).enum_name == "SYNTHETIC_BOARD"
    with pytest.raises(InstrumentConnectionError, match="Unknown board"):
        resolve_board("not_a_board")


def test_address_maps_to_the_right_input_param():
    assert input_params(resolve_board("cyton"), "/dev/ttyUSB0", {}) == {"serial_port": "/dev/ttyUSB0"}
    assert input_params(resolve_board("muse_2"), "00:55:DA:B0:12:34", {"serial_number": "Muse-1234"}) == {
        "mac_address": "00:55:DA:B0:12:34",
        "serial_number": "Muse-1234",
    }
    assert input_params(resolve_board("unicorn"), "UN-2019.05.08", {})["serial_number"] == "UN-2019.05.08"
    wifi = input_params(resolve_board("cyton_wifi"), "192.168.4.1", {})
    assert wifi == {"ip_address": "192.168.4.1", "ip_port": 6789}
    assert input_params(resolve_board("ganglion"), None, {"other_info": "fw:2"}) == {"other_info": "fw:2"}


def test_address_errors_are_explained():
    with pytest.raises(InstrumentConnectionError, match="needs --address"):
        input_params(resolve_board("cyton"), None, {})
    with pytest.raises(InstrumentConnectionError, match="does not take an --address"):
        input_params(resolve_board("synthetic"), "COM3", {})
    with pytest.raises(InstrumentConnectionError, match="master_board"):
        input_params(resolve_board("playback"), "/tmp/x.csv", {})


def test_brainflow_error_hint():
    err = brainflow_error(Exception("UNABLE_TO_OPEN_PORT_ERROR:2 unable to prepare streaming session"), "connecting")
    assert "serial port could not be opened" in str(err)


# ------------------------------------------------------------------ DSP


def test_welch_psd_finds_alpha_peak_and_band_power():
    fs = 250
    t = np.arange(fs * 8) / fs
    x = 20 * np.sin(2 * np.pi * 10 * t)
    freqs, psd = welch_psd(x, fs)
    assert abs(freqs[np.argmax(psd)] - 10) < 0.6
    # sine of amplitude A has power A^2/2 = 200 uV^2
    assert band_power(freqs, psd, 8, 13) == pytest.approx(200, rel=0.1)


def test_railed_percent_flat_line_and_small_signal():
    assert railed_percent(np.zeros(100)) == 100.0
    assert railed_percent(np.sin(np.arange(100.0)) * 50) < 1.0


def test_numpy_avg_band_powers_alpha_dominant():
    fs = 250
    t = np.arange(fs * 4) / fs
    data = np.vstack([np.zeros_like(t), 20 * np.sin(2 * np.pi * 10 * t), 15 * np.sin(2 * np.pi * 11 * t)])
    avg, _ = NumpyDsp().avg_band_powers(data, [1, 2], fs)
    assert sum(avg) == pytest.approx(1.0)
    assert avg[2] > 0.9  # alpha


def test_downsample_bounds_length():
    assert len(downsample(np.arange(1000.0), 50)) == 50
    assert downsample(np.arange(5.0), 50) == [0.0, 1.0, 2.0, 3.0, 4.0]


# ------------------------------------------------------------------ driver on the fake board


def test_fake_board_streams_and_places_markers():
    b = fake_board()
    assert b.sampling_rate() == 250
    assert [c.name for c in b.channels("eeg")][:2] == ["Fp1", "Fp2"]
    b.start_stream(10000)
    time.sleep(0.3)
    b.insert_marker(3)
    time.sleep(0.2)
    rec = b.latest(0.4)  # the most recent 0.4 s, read non-destructively from the stream
    assert rec.data.shape == (14, 100)
    assert 3.0 in rec.data[13]
    assert b.acquire(0.2).data.shape[1] == 50  # acquire() collects *new* data
    b.close()


def test_fake_board_error_replies():
    b = fake_board()
    with pytest.raises(InstrumentProtocolError, match="start_streaming first"):
        b.insert_marker(1)
    b.start_stream(1000)
    with pytest.raises(InstrumentProtocolError, match="reserved"):
        b.insert_marker(0)
    with pytest.raises(InstrumentProtocolError, match="no ppg channels"):
        b.channels("ppg")
    b.close()
    # a raw BrainFlow-style error from the handle is translated
    shim = FakeBoardShim()
    with pytest.raises(InstrumentProtocolError, match="STREAM_THREAD_IS_NOT_RUNNING"):
        shim.prepare_session()
        shim.stop_stream()


def test_fake_ring_buffer_is_bounded():
    shim = FakeBoardShim()
    shim.prepare_session()
    shim.start_stream(100)
    shim._t0 -= 2.0  # pretend 2 s have passed (500 samples)
    assert shim.get_board_data_count() == 100


@pytest.mark.skipif(not HAVE_BRAINFLOW, reason="no BrainFlow")
def test_real_brainflow_open_error_is_helpful(tmp_path):
    server.configure(simulate=False, address=str(tmp_path / "no-such-port"), options={"board": "cyton"})
    info = server._connection_info()
    assert info["connected"] is False
    assert "UNABLE_TO_OPEN_PORT_ERROR" in info["error"]
    server.configure(simulate=True, address="", options={})


def test_real_mode_missing_address_is_explained():
    if not HAVE_BRAINFLOW:
        pytest.skip("no BrainFlow")
    server.configure(simulate=False, address="", options={"board": "cyton_daisy"})
    info = server._connection_info()
    assert "needs --address" in info["error"]
    server.configure(simulate=True, options={})


# ------------------------------------------------------------------ MCP round trips


@pytest.mark.parametrize("simulator", SIMULATORS)
async def test_tools_via_mcp(simulator, tmp_path):
    async with simulated_client(server, options={"simulator": simulator}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        boards = (await client.call_tool("list_supported_boards", {})).structured_content["result"]
        assert any(b["alias"] == "cyton" and b["address"] == "serial_port" for b in boards)

        board = (await client.call_tool("get_board_info", {})).structured_content
        assert board["sampling_rate_hz"] == 250
        assert board["exg_unit"] == "uV"
        n_exg = len(board["channels"]["exg"])

        csv = tmp_path / "rec.csv"
        rec = (
            await client.call_tool("record", {"duration_s": 1.0, "max_points": 25, "save_path": str(csv)})
        ).structured_content
        assert 200 <= rec["n_samples"] <= 260
        assert len(rec["channels"]) == n_exg
        assert all(len(v) <= 25 for v in rec["traces"].values())
        assert csv.read_text().splitlines()[0].startswith("package_num")

        raw = tmp_path / "rec.brainflow.csv"
        rec = (
            await client.call_tool(
                "record", {"duration_s": 0.5, "include_traces": False, "save_path": str(raw), "save_format": "brainflow"}
            )
        ).structured_content
        assert rec["traces"] is None and raw.exists()

        state = (await client.call_tool("start_streaming", {"buffer_duration_s": 60})).structured_content
        assert state["streaming"] is True

        async def mark_later():
            await asyncio.sleep(0.6)
            return await client.call_tool("insert_marker", {"value": 42})

        rec_task = asyncio.create_task(client.call_tool("record", {"duration_s": 1.5, "include_traces": False}))
        await mark_later()
        rec = (await rec_task).structured_content
        assert [m["value"] for m in rec["markers"]] == [42.0]
        assert 0.2 < rec["markers"][0]["time_s"] < 1.5

        bp = (await client.call_tool("get_band_powers", {"window_s": 4})).structured_content
        assert sum(bp["average_relative"].values()) == pytest.approx(1.0, abs=1e-6)
        assert len(bp["channels"]) == n_exg

        q = (await client.call_tool("get_signal_quality", {"window_s": 3})).structured_content
        assert sum(q["summary"].values()) == n_exg
        assert q["summary"].get("line_noise", 0) >= 1  # both simulators contain mains-frequency signals

        cfg = (await client.call_tool("configure_board", {"command": "x1060110X"})).structured_content
        assert cfg["reply"] == "Config:x1060110X"

        stopped = (await client.call_tool("stop_streaming", {})).structured_content
        assert stopped["streaming"] is False

        log = (await client.call_tool("get_command_log", {"limit": 50})).data
        assert any("insert_marker 42" in entry["data"] for entry in log)


async def test_fake_simulator_signal_quality_flags_fp2():
    async with simulated_client(server, options={"simulator": "fake"}) as client:
        q = (await client.call_tool("get_signal_quality", {"window_s": 2})).structured_content
        verdicts = {c["name"]: c["verdict"] for c in q["channels"]}
        assert verdicts["Fp2"] == "line_noise"
        assert verdicts["O1"] == "ok"
        assert q["dominant_mains_hz"] == 50
        bp = (await client.call_tool("get_band_powers", {"window_s": 4})).structured_content
        o1 = next(c for c in bp["channels"] if c["name"] == "O1")
        assert abs(o1["peak_frequency_hz"] - 10) < 1.0
        assert bp["average_relative"]["alpha"] == max(bp["average_relative"].values())


async def test_marker_without_stream_is_refused():
    async with simulated_client(server, options={"simulator": "fake"}) as client:
        with pytest.raises(Exception, match="start_streaming"):
            await client.call_tool("insert_marker", {"value": 1})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True, options={"simulator": "fake"}) as client:
        names = await tool_names(client)
        assert {"record", "get_band_powers", "get_signal_quality", "list_supported_boards"} <= names
        assert not names & {"start_streaming", "stop_streaming", "insert_marker", "configure_board"}
        assert "reconnect" in names  # safety tools stay available


async def test_record_duration_limit():
    async with simulated_client(server, limits={"max_record_duration_s": 2}, options={"simulator": "fake"}) as client:
        with pytest.raises(Exception, match="max_record_duration_s"):
            await client.call_tool("record", {"duration_s": 5})
        with pytest.raises(Exception, match="max_record_duration_s"):
            await client.call_tool("get_band_powers", {"window_s": 10})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_record_duration_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_record_duration_s", 5)
    server.configure(simulate=True, limits={})


@pytest.mark.parametrize("simulator", SIMULATORS)
async def test_missing_preset_is_explained(simulator):
    async with simulated_client(server, options={"simulator": simulator}) as client:
        with pytest.raises(Exception, match="no 'ancillary' preset"):
            await client.call_tool("get_board_info", {"preset": "ancillary"})
        info = (await client.call_tool("get_board_info", {})).structured_content
        assert "ancillary" not in info["presets"]
