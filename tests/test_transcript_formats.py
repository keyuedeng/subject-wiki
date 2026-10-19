from app.transcripts.formats import parse_transcript
from app.transcripts.windows import OTHER_SPEAKER, make_windows, sentences

VTT = b"""WEBVTT
Kind: captions

00:00:00.449 --> 00:00:04.489 align:start position:0%
<v Speaker 0>For the system to start recording and also for people

NOTE CONF {"raw":[87,91,99,99,99,99,99,99,99,99]}

00:00:04.489 --> 00:00:05.730
<v Speaker 0>to come to the class.

NOTE CONF {"raw":[99,99,99,99,99]}

00:00:07.760 --> 00:00:10.539
<v Speaker 0>So today we cover the create a pattern. It

NOTE CONF {"raw":[97,94,99,99,99,30,20,40,99]}

00:00:10.630 --> 00:00:11.439
<v Speaker 0>matters.

00:00:12.000 --> 00:00:13.000
<v Speaker 1>Is it on the exam?
"""

SRT = b"""1
00:00:01,000 --> 00:00:04,000
Welcome to lecture five.

2
00:00:04,500 --> 00:00:08,250
Today: <i>Information Expert</i>.
"""

TXT_STAMPED = b"""[00:00:05] Okay let's start with low coupling.
[00:01:10] So last week we saw the creator pattern
and it assigns creation.
[00:02:30] Right, high cohesion next.
"""

TXT_PLAIN = b"All right, OK. Um, everyone can hear me well? Good. This is week two, lecture two."


def test_vtt_cues_speakers_settings_and_confidence():
    t = parse_transcript(VTT, "vtt")
    assert t.has_timestamps and len(t.cues) == 5
    first = t.cues[0]
    assert first.text == "For the system to start recording and also for people"
    assert (first.start_ms, first.end_ms, first.speaker) == (449, 4489, "Speaker 0")
    assert t.cues[2].low_conf == ["create", "a", "pattern"]  # scored 30/20/40
    assert t.cues[4].speaker == "Speaker 1"


def test_vtt_confidence_skipped_when_word_counts_disagree():
    t = parse_transcript(
        b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nTwo words\n\nNOTE CONF {"raw":[1]}\n', "vtt"
    )
    assert t.cues[0].low_conf == []


def test_srt():
    t = parse_transcript(SRT, "srt")
    assert t.has_timestamps
    assert [(c.text, c.start_ms, c.end_ms) for c in t.cues] == [
        ("Welcome to lecture five.", 1000, 4000),
        ("Today: Information Expert.", 4500, 8250),
    ]


def test_timestamped_txt_with_continuation_lines():
    t = parse_transcript(TXT_STAMPED, "txt")
    assert t.has_timestamps
    assert [c.start_ms for c in t.cues] == [5000, 70000, 150000]
    assert t.cues[1].text == "So last week we saw the creator pattern and it assigns creation."
    assert t.cues[0].end_ms == 70000  # ends where the next line starts


def test_plain_txt_has_no_timestamps():
    t = parse_transcript(TXT_PLAIN, "txt")
    assert not t.has_timestamps
    assert len(t.cues) == 1 and t.cues[0].start_ms is None


def test_sentences_join_cues_and_split_inside_them():
    ss = sentences(parse_transcript(VTT, "vtt"))
    texts = [s.text for s in ss]
    assert texts[0] == "For the system to start recording and also for people to come to the class."
    assert texts[1] == "So today we cover the create a pattern."
    assert texts[2] == "It matters."  # starts inside one cue, ends in the next
    assert ss[0].start_ms == 449 and ss[0].end_ms == 5730
    assert ss[1].low_conf == ["create", "a", "pattern"]
    assert ss[3].other_speaker and texts[3] == "Is it on the exam?"


def test_other_speaker_is_marked_in_window_text():
    windows = make_windows(parse_transcript(VTT, "vtt"))
    assert f"{OTHER_SPEAKER} Is it on the exam?" in windows[-1].text


def _timed_vtt(n_sentences: int, words: int = 20, seconds: float = 8.0) -> bytes:
    cues, t = ["WEBVTT", ""], 0.0
    for i in range(n_sentences):
        text = " ".join(f"w{i}x{j}" for j in range(words - 1)) + " end."

        def stamp(x):
            return f"00:{int(x // 60):02d}:{x % 60:06.3f}"

        cues += [f"{stamp(t)} --> {stamp(t + seconds)}", text, ""]
        t += seconds
    return "\n".join(cues).encode()


def test_timed_windows_respect_size_limits():
    windows = make_windows(parse_transcript(_timed_vtt(60), "vtt"))
    for w in windows[:-1]:
        seconds = (w.end_ms - w.start_ms) / 1000
        assert 30 <= seconds <= 60 and 100 <= w.words <= 150, (seconds, w.words)
    assert [w.idx for w in windows] == list(range(len(windows)))


def test_untimed_windows_use_word_counts():
    text = " ".join(f"Sentence number {i} has a few more words in it." for i in range(80))
    windows = make_windows(parse_transcript(text.encode(), "txt"))
    assert all(w.start_ms is None for w in windows)
    assert all(100 <= w.words <= 150 for w in windows[:-1])


def test_unpunctuated_speech_still_splits():
    cues = ["WEBVTT", ""]
    for i in range(40):
        cues += [
            f"00:00:{i:02d}.000 --> 00:00:{i:02d}.900",
            "and then we keep talking about it",
            "",
        ]
    windows = make_windows(parse_transcript("\n".join(cues).encode(), "vtt"))
    assert len(windows) >= 2  # no punctuation at all, still more than one window
