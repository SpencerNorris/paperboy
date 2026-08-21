import json
from pathlib import Path

from paperboy.web.wayback import cdx_timestamp_to_iso, parse_cdx_rows

FX = Path("tests/fixtures/web/wayback_cdx.json")


def test_parses_recorded_fixture_into_rows():
    payload = json.loads(FX.read_text())
    rows = parse_cdx_rows(payload)
    assert len(rows) == 2
    assert rows[0]["timestamp"] == "20190301120000"
    assert rows[0]["original"] == "http://t.me/s/durov"
    assert rows[0]["statuscode"] == "200"
    assert rows[0]["digest"] == "AAAABBBBCCCCDDDD"


def test_header_only_or_empty_payload_yields_no_rows():
    assert parse_cdx_rows([]) == []
    assert parse_cdx_rows([["urlkey", "timestamp", "original"]]) == []


def test_cdx_timestamp_to_iso():
    assert cdx_timestamp_to_iso("20190301120000") == "2019-03-01T12:00:00+00:00"


def test_cdx_timestamp_to_iso_returns_none_on_malformed_input():
    assert cdx_timestamp_to_iso("not-a-timestamp") is None
    assert cdx_timestamp_to_iso("2019") is None
