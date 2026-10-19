"""Read VTT, SRT and TXT transcripts into ordered cues. Pure.

- VTT: `<v Speaker N>` voice tags give the speaker; Echo360's `NOTE CONF {"raw": [...]}`
  after a cue gives one recognition confidence (0-100) per word, and words below
  LOW_CONFIDENCE are kept as hints for the cleaning pass.
- SRT: numbered cues with `HH:MM:SS,mmm --> HH:MM:SS,mmm`.
- TXT: lines starting with a timestamp (`[00:12:34] text` or `00:12:34 text`) when at
  least MIN_TIMESTAMP_LINES lines have one; otherwise one untimed block.
"""

import json
import re
from dataclasses import dataclass, field

LOW_CONFIDENCE = 50
MIN_TIMESTAMP_LINES = 3

_TIME = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
CUE_TIMES_RE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
TXT_STAMP_RE = re.compile(rf"^\s*\[?{_TIME}\]?\s*[-–:]?\s*(.*)$")
VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
TAG_RE = re.compile(r"</?[^>]+>")
CONF_RE = re.compile(r"^NOTE\s+CONF\s+(\{.*\})\s*$")
WORD_EDGE_RE = re.compile(r"^\W+|\W+$")


@dataclass
class Cue:
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    speaker: str | None = None
    low_conf: list[str] = field(default_factory=list)


@dataclass
class Transcript:
    cues: list[Cue]
    has_timestamps: bool


def _ms(h, m, s, frac) -> int:
    frac = (frac or "0").ljust(3, "0")[:3]
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac)


def _clean_line(text: str) -> tuple[str, str | None]:
    speaker = None
    if m := VOICE_RE.search(text):
        speaker = m.group(1).strip()
    return " ".join(TAG_RE.sub("", text).split()), speaker


def parse_transcript(data: bytes, ext: str) -> Transcript:
    text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    if ext == "vtt" or text.lstrip().startswith("WEBVTT"):
        return _parse_cues(text, vtt=True)
    if ext == "srt" or ("-->" in text and CUE_TIMES_RE.search(text.split("\n\n", 2)[0] + "\n")):
        return _parse_cues(text, vtt=False)
    return _parse_txt(text)


def _parse_cues(text: str, vtt: bool) -> Transcript:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if vtt and (m := CONF_RE.match(lines[0].strip())):
            if cues:
                _attach_confidence(cues[-1], m.group(1))
            continue
        if lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "Kind:", "Language:")):
            continue
        time_at = next((i for i, ln in enumerate(lines) if CUE_TIMES_RE.match(ln)), None)
        if time_at is None:
            continue  # stray text without a timing line
        g = CUE_TIMES_RE.match(lines[time_at]).groups()
        body, speaker = [], None
        for ln in lines[time_at + 1 :]:
            cleaned, who = _clean_line(ln)
            speaker = speaker or who
            if cleaned:
                body.append(cleaned)
        if body:
            cues.append(Cue(" ".join(body), _ms(*g[:4]), _ms(*g[4:]), speaker))
    return Transcript(cues, has_timestamps=bool(cues))


def _attach_confidence(cue: Cue, raw_json: str) -> None:
    try:
        scores = json.loads(raw_json).get("raw") or []
    except (ValueError, AttributeError):
        return
    words = cue.text.split()
    if len(scores) != len(words):
        return  # can't line scores up with words; skip rather than guess
    for word, score in zip(words, scores, strict=True):
        bare = WORD_EDGE_RE.sub("", word)
        if bare and score < LOW_CONFIDENCE and bare not in cue.low_conf:
            cue.low_conf.append(bare)


def _parse_txt(text: str) -> Transcript:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    stamped = [(TXT_STAMP_RE.match(ln), ln) for ln in lines]
    if sum(1 for m, _ in stamped if m and m.group(5).strip()) >= MIN_TIMESTAMP_LINES:
        cues: list[Cue] = []
        for m, ln in stamped:
            if m:
                cues.append(Cue(m.group(5).strip(), _ms(*m.groups()[:4])))
            elif cues:  # continuation of the previous stamped line
                cues[-1].text += " " + ln.strip()
        for cue, nxt in zip(cues, cues[1:], strict=False):
            cue.end_ms = nxt.start_ms
        if cues:
            cues[-1].end_ms = cues[-1].start_ms
        return Transcript([c for c in cues if c.text], has_timestamps=True)
    body = " ".join(" ".join(lines).split())
    return Transcript([Cue(body)] if body else [], has_timestamps=False)
