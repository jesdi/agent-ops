"""Unlocked ticket 04 case: a large saved answers file still reads back."""
from tests.test_web_answers_acceptance import REQ, answers_req, rig, saved
from tests.webfakes import HEADERS


def test_large_saved_file_still_comes_back(tmp_path):
    big = {"format.note": "x" * (70 * 1024)}
    _, client = rig(tmp_path, answers_req(), saved(big))
    assert client.get(REQ, headers=HEADERS).json()["answers"] == big
