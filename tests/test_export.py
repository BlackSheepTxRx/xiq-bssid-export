"""Offline checks for BSSID selection, status, sorting, and .env lookup."""

import os

import xiq_bssid_export as exp

BASE = "aa:bb:cc:00:00:60"
API_MAC = "aa:bb:cc:00:00:64"
FIRST = "aa:bb:cc:00:00:64"
SECOND = "aa:bb:cc:00:00:65"


def _ap(site: str = "Alpha", name: str = "AP-EXAMPLE-01") -> dict:
    return {
        "id": 1,
        "site": site,
        "building": "Main",
        "floor": "1",
        "hostname": name,
        "serial_number": "XXXXXXXXXXXXXX",
        "product_type": "AP_EXAMPLE",
        "connected": "true",
        "location_source": "tree",
    }


def _radio(mac: str = API_MAC, wlans: list | None = None) -> dict:
    if wlans is None:
        wlans = [
            {"ssid": "EXAMPLE-SSID-2", "bssid": "AABBCC000065", "ssid_status": "OPEN", "network_policy_name": "Example"},
            {"ssid": "EXAMPLE-SSID-1", "bssid": "AABBCC000064", "ssid_status": "OPEN", "network_policy_name": "Example"},
        ]
    return {"name": "wifi1", "mac_address": mac, "wlans": wlans}


def _by_label(rows):
    return {row["Inferred Subinterface"]: row for row in rows}


def test_offset_math_from_api_mac():
    choices = exp.parse_interface_list(["wifi1.0", "wifi1.1", "wifi1.2"])
    rows, has_radio = exp.rows_for_ap(_ap(), [_radio()], choices, set())
    assert has_radio
    found = _by_label(rows)

    base = found["wifi1.0"]
    assert base["Radio MAC"] == BASE
    assert base["BSSID"] == BASE
    assert base["SSID"] == ""
    assert base["Status"] == "base"

    first = found["wifi1.1"]
    assert first["BSSID"] == FIRST
    assert first["SSID"] == "EXAMPLE-SSID-1"
    assert first["Status"] == "confirmed"

    second = found["wifi1.2"]
    assert second["BSSID"] == SECOND
    assert second["SSID"] == "EXAMPLE-SSID-2"
    assert second["Status"] == "confirmed"


def test_unconfirmed_bss_leaves_bssid_blank():
    choices = exp.parse_interface_list(["wifi1.3"])
    rows, _ = exp.rows_for_ap(_ap(), [_radio()], choices, set())
    assert len(rows) == 1
    assert rows[0]["BSSID"] == ""
    assert rows[0]["Radio MAC"] == BASE
    assert rows[0]["Status"] == "not-reported"


def test_wlan_bssids_outside_the_block_count_as_a_mismatch():
    weird = _radio(
        mac=API_MAC,
        wlans=[{"ssid": "EXAMPLE-SSID-1", "bssid": "aa:bb:cc:11:22:33"}],
    )
    assert exp.ap_has_bssid_pattern_mismatch([weird]) is True
    assert exp.ap_has_bssid_pattern_mismatch([_radio()]) is False

    rows, with_radio, mismatches = exp.build_rows(
        [_ap()],
        {1: [weird]},
        exp.parse_interface_list(["wifi1.0"]),
        set(),
    )
    assert with_radio == 1
    assert mismatches == [("AP-EXAMPLE-01", "AP_EXAMPLE")]
    assert rows[0]["Status"] == "base"
    assert rows[0]["BSSID"] == BASE


def test_offset_one_through_three_is_unexpected_mac():
    radio = _radio(mac="aa:bb:cc:00:00:61", wlans=[])
    rows, _ = exp.rows_for_ap(
        _ap(),
        [radio],
        exp.parse_interface_list(["wifi1.0", "wifi1.1"]),
        set(),
    )
    assert [row["Status"] for row in rows] == ["unexpected-mac", "unexpected-mac"]
    assert all(row["BSSID"] == "" and row["Radio MAC"] == "" for row in rows)


def test_unassigned_sorts_after_zeta():
    choices = exp.parse_interface_list(["wifi1.0"])
    radios = [_radio(mac=BASE, wlans=[])]
    access_points = [
        _ap("(unassigned)", "AP-UNASSIGNED"),
        _ap("Zeta", "AP-ZETA"),
        _ap("Alpha", "AP-ALPHA"),
    ]
    for index, ap in enumerate(access_points, start=1):
        ap["id"] = index
    radio_map = {ap["id"]: radios for ap in access_points}
    rows, _, _ = exp.build_rows(access_points, radio_map, choices, set())
    assert [row["Site"] for row in rows] == ["Alpha", "Zeta", "(unassigned)"]


def test_dotenv_is_found_beside_the_script_when_cwd_differs(tmp_path, monkeypatch):
    script_dir = tmp_path / "script"
    work_dir = tmp_path / "work"
    script_dir.mkdir()
    work_dir.mkdir()
    (script_dir / ".env").write_text("XIQ_API_TOKEN=script-token\n", encoding="utf-8")
    monkeypatch.chdir(work_dir)
    monkeypatch.delenv("XIQ_API_TOKEN", raising=False)
    exp.load_dotenv(script_dir=str(script_dir))
    assert os.environ["XIQ_API_TOKEN"] == "script-token"

    (work_dir / ".env").write_text("XIQ_API_TOKEN=cwd-token\n", encoding="utf-8")
    monkeypatch.delenv("XIQ_API_TOKEN", raising=False)
    exp.load_dotenv(script_dir=str(script_dir))
    assert os.environ["XIQ_API_TOKEN"] == "cwd-token"
