"""Reading an answer's markdown out of a structured response while it streams in."""

import json

from app.qa.stream import partial_string, trim_partial_marker


def test_partial_string_decodes_what_has_arrived():
    full = json.dumps({"status": "answered", "answer_markdown": 'Say "B" — é\nthen [S1].'})
    assert partial_string(full, "answer_markdown") == 'Say "B" — é\nthen [S1].'
    # Every prefix decodes to a prefix of the value.
    value = 'Say "B" — é\nthen [S1].'
    for i in range(len(full)):
        got = partial_string(full[:i], "answer_markdown")
        assert got is None or value.startswith(got)


def test_partial_string_waits_for_a_whole_escape():
    assert partial_string('{"answer_markdown": "a\\', "answer_markdown") == "a"
    assert partial_string('{"answer_markdown": "a\\u00', "answer_markdown") == "a"
    assert partial_string('{"answer_markdown": "a\\ud83d', "answer_markdown") == "a"
    assert partial_string('{"answer_markdown": "a\\ud83d\\ude00b', "answer_markdown") == "a😀b"


def test_partial_string_before_the_field_starts():
    assert partial_string('{"status": "answ', "answer_markdown") is None


def test_trim_partial_marker():
    assert trim_partial_marker("Creator [S1][S") == "Creator [S1]"
    assert trim_partial_marker("Creator [S1, S") == "Creator "
    assert trim_partial_marker("Creator [") == "Creator "
    assert trim_partial_marker("Creator [S1] done") == "Creator [S1] done"
