"""Rule-based classifier: (filename, peek) -> type, week, lecture, timestamps, confidence, reason.

Signal strength, roughly:
  caption extension (.vtt/.srt)          0.95
  type keyword in the filename           0.85 (0.5 if keywords conflict, +0.1 if content agrees)
  .pptx with no keyword                  0.8
  speech-like or timestamped .txt        0.7-0.75
  landscape PDF / content markers only   0.55-0.7
  nothing matched                        0.2 -> "other"
Week/lecture found only in the content caps confidence at 0.7; slides, transcripts
and tutorials with no week or lecture at all are capped at 0.55 so they get reviewed.
For tutorials and solutions the week written in the file ("Workshop 4 (Week 5)") wins
over one in the filename: they're filed by the week they're taught, which the sheet
states, while filenames often carry the tutorial number or the lectures' week.
"""

from app.classify import patterns as p
from app.classify.peek import Peek
from app.classify.types import FileType, Guess

WEEKLY_TYPES = {FileType.SLIDES, FileType.TRANSCRIPT, FileType.TUTORIAL}
CONTENT_WEEK_TYPES = WEEKLY_TYPES | {FileType.SOLUTIONS}
TAUGHT_WEEK_TYPES = {FileType.TUTORIAL, FileType.SOLUTIONS}
TRANSCRIPT_HEAD_CHARS = 1500  # roughly the first minute of speech


def classify(filename: str, peek: Peek, subject_code: str | None = None) -> Guess:
    stem, ext = p.split_ext(filename)
    name = p.normalize(stem, subject_code)
    text = peek.text.lower()
    reasons: list[str] = []

    has_timestamps = detect_timestamps(ext, peek)
    ftype, confidence = _guess_type(name, ext, text, peek, has_timestamps, reasons)

    week = p.find_number(p.WEEK_RE, name, 52)
    lecture = p.find_number(p.LECTURE_RE, name, 99)
    if week is not None:
        reasons.append(f"week {week} from filename")
    if lecture is not None:
        reasons.append(f"lecture {lecture} from filename")

    if ftype in TAUGHT_WEEK_TYPES and week is not None and text:
        stated = p.find_number(p.WEEK_RE, p.normalize(text, subject_code), 52)
        if stated is not None and stated != week:
            reasons.append(f"week {stated} written in the file (filename says {week})")
            week = stated
            confidence = min(confidence, 0.7)

    if (week is None or lecture is None) and ftype in CONTENT_WEEK_TYPES and text:
        head = text[:TRANSCRIPT_HEAD_CHARS] if ftype == FileType.TRANSCRIPT else text
        head = p.normalize(head, subject_code)
        if week is None and (week := p.find_number(p.WEEK_RE, head, 52)) is not None:
            reasons.append(f"week {week} from content")
            confidence = min(confidence, 0.7)
        if lecture is None and (lecture := p.find_number(p.LECTURE_RE, head, 99)) is not None:
            reasons.append(f"lecture {lecture} from content")
            confidence = min(confidence, 0.7)

    if (tute := p.TUTORIAL_NUM_RE.search(name)) and week is None:
        reasons.append(f"tutorial {int(tute.group(1))} (not used as week)")
    if ftype == FileType.PAST_PAPER and (year := p.YEAR_RE.search(name)):
        reasons.append(f"year {year.group(0)}")

    if ftype in WEEKLY_TYPES and week is None and lecture is None:
        reasons.append("no week or lecture found")
        confidence = min(confidence, 0.55)

    if peek.error:
        reasons.append(peek.error)

    return Guess(
        type=ftype,
        week=week,
        lecture=lecture,
        has_timestamps=has_timestamps and ftype == FileType.TRANSCRIPT,
        confidence=round(confidence, 2),
        reason="; ".join(reasons),
    )


def detect_timestamps(ext: str, peek: Peek) -> bool:
    if ext in ("vtt", "srt"):
        return True
    if ext != "txt":
        return False
    stamped = sum(1 for line in peek.lines if p.TIMESTAMP_RE.search(line))
    return stamped >= p.MIN_TIMESTAMP_LINES


def looks_like_speech(text: str) -> bool:
    words = text.split()
    return len(words) >= p.MIN_SPEECH_WORDS and len(p.FILLER_RE.findall(text)) >= p.MIN_FILLERS


def _filename_hits(name: str) -> list[tuple[FileType, str]]:
    hits = []
    for ftype, regex in p.FILENAME_KEYWORDS:
        if match := regex.search(name):
            hits.append((ftype, match.group(0)))
    return hits


def _content_hits(text: str) -> dict[FileType, list[str]]:
    found = {}
    for ftype, min_hits, regex in p.CONTENT_MARKERS:
        markers = sorted({m.group(0) for m in regex.finditer(text)})
        if len(markers) >= min_hits:
            found[ftype] = markers
    return found


def _quote(words: list[str], limit: int = 3) -> str:
    return ", ".join(f"'{w}'" for w in words[:limit])


def _guess_type(
    name: str, ext: str, text: str, peek: Peek, has_timestamps: bool, reasons: list[str]
) -> tuple[FileType, float]:
    if ext in ("vtt", "srt"):
        reasons.append(f"caption file (.{ext})")
        return FileType.TRANSCRIPT, 0.95
    if ext == "txt" and has_timestamps and p.CUE_ARROW_RE.search(text):
        reasons.append("text file containing caption cues (-->)")
        return FileType.TRANSCRIPT, 0.9

    speech = ext == "txt" and looks_like_speech(text)
    content = _content_hits(text)
    hits = _filename_hits(name)

    if hits:
        winner, word = hits[0]
        confidence = 0.85
        reasons.append(f"'{word}' in filename")
        others = hits[1:]
        if others:
            reasons.append("also " + _quote([w for _, w in others]))
        if any(frozenset({winner, t}) not in p.COMPATIBLE for t, _ in others):
            reasons.append("conflicting keywords")
            confidence = 0.5

        # A .txt "lecture" file is a transcript, not slides.
        if ext == "txt" and winner == FileType.SLIDES:
            if has_timestamps or speech:
                reasons.append("text reads as speech" if speech else "text has timestamps")
                return FileType.TRANSCRIPT, 0.75
            return winner, 0.5

        if winner in content:
            reasons.append(f"content agrees ({_quote(content[winner])})")
            confidence = min(confidence + 0.1, 0.95)
        return winner, confidence

    if ext == "txt" and has_timestamps:
        reasons.append("text has timestamp lines")
        return FileType.TRANSCRIPT, 0.75
    if speech:
        reasons.append("text reads as speech (long, filler words)")
        return FileType.TRANSCRIPT, 0.7
    if ext == "pptx":
        reasons.append("PowerPoint file")
        return FileType.SLIDES, 0.8
    if ext == "ipynb":
        reasons.append("Jupyter notebook")
        return FileType.TUTORIAL, 0.75

    if ext in ("pdf", "pptx") and (code := p.LECTURE_RE.search(name)):
        reasons.append(f"lecture code '{code.group(0)}' in filename")
        return FileType.SLIDES, 0.75

    has_number = bool(p.WEEK_RE.search(name))
    if ext == "pdf" and peek.landscape:
        reasons.append("landscape pages")
        return FileType.SLIDES, 0.7 if has_number else 0.6

    if content:
        ftype = next(t for t in p.PRECEDENCE if t in content)
        reasons.append(f"content mentions {_quote(content[ftype])}")
        return ftype, min(0.5 + 0.05 * len(content[ftype]), 0.65)

    if ext == "pdf" and has_number:
        reasons.append("PDF with a week number")
        return FileType.SLIDES, 0.55

    reasons.append("no type keywords matched")
    return FileType.OTHER, 0.2
