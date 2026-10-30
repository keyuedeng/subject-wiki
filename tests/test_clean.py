from app.transcripts.clean import CleanedWindow, Flag, check, request_text
from app.transcripts.glossary import build_glossary

GLOSSARY = ["UML", "GRASP", "Creator", "Information Expert", "MonopolyGame"]
RAW = (
    "Um, so, uh, the the you mel diagram shows uh the create a pattern. Make sure you know "
    "the creator pattern because, uh, this will be on the exam."
)


def result(clean: str, flags=()) -> CleanedWindow:
    return CleanedWindow(clean_text=clean, flags=list(flags))


HINT = Flag(
    type="exam_hint",
    reason="Says it is examined",
    quote="Make sure you know the creator pattern because, uh, this will be on the exam",
)


def test_good_clean_is_accepted():
    clean = ("The UML diagram shows the Creator pattern. Make sure you know the Creator pattern "
             "because this will be on the exam.")  # fmt: skip
    out = check(RAW, result(clean, [HINT]), GLOSSARY)
    assert out.status == "cleaned" and out.clean_text == clean
    assert out.flags[0]["quote_found"] is True


def test_added_content_is_rejected_and_raw_kept():
    clean = ("The UML diagram shows the Creator pattern, which assigns object creation to the "
             "class that aggregates, contains or closely uses the created objects.")  # fmt: skip
    out = check(RAW, result(clean), GLOSSARY)
    assert out.status == "rejected" and out.clean_text == RAW
    assert "added words" in out.note


def test_dropping_an_exam_hint_is_rejected():
    out = check(RAW, result("The UML diagram shows the Creator pattern.", [HINT]), GLOSSARY)
    assert out.status == "rejected" and out.note == "cleaning dropped an exam hint"


def test_empty_clean_of_real_speech_is_rejected():
    assert check(RAW, result(""), GLOSSARY).status == "rejected"
    assert check("Um, uh, OK.", result(""), GLOSSARY).status == "cleaned"  # only fillers


def test_invented_quotes_are_marked():
    fake = Flag(type="admin", reason="x", quote="the assignment is due on Friday")
    out = check(RAW, result("The UML diagram.", [fake]), GLOSSARY)
    assert out.flags[0]["quote_found"] is False


def test_request_puts_shared_parts_first():
    text = request_text("the window", GLOSSARY, "previous end.", ["mel"])
    assert text.index("Glossary") < text.index("previous end.") < text.index("the window")
    assert text.startswith("Glossary (terms from this lecture's slides):\nUML, GRASP")
    assert "unsure about: mel" in text


def test_glossary_from_slides_is_deterministic_and_ranked():
    slides = [
        {"title": "GRASP: Creator", "notes": "Creator assigns creation in the UML design.",
         "content_json": {"content_markdown": "MonopolyGame creates Board. Use GRASP.",
                          "visuals": [{"description": "UML class diagram",
                                       "key_facts": ["MonopolyGame 1 — 1 Board"]}]}},
        {"title": "Information Expert (or Expert)", "notes": None,
         "content_json": {"content_markdown": "Assign to the Information Expert. getSquare()",
                          "visuals": []}},
    ]  # fmt: skip
    g = build_glossary(slides, "SWEN30006")
    assert g == build_glossary(slides, "SWEN30006")
    for term in ("GRASP", "Creator", "UML", "MonopolyGame", "Information Expert", "getSquare",
                 "SWEN30006"):  # fmt: skip
        assert term in g, term
    assert g.index("GRASP") < g.index("SWEN30006")  # more frequent first
    assert "The" not in g and "Use" not in g


def test_analogy_is_its_own_flag_and_worked_example_is_narrow():
    from app.transcripts.clean import CLEAN_PROMPT, CleanedWindow

    flag = {"type": "analogy", "reason": "kitchen comparison", "quote": "like a restaurant"}
    window = CleanedWindow.model_validate({"clean_text": "x", "flags": [flag]})
    assert window.flags[0].type == "analogy"
    assert "step by step to a specific case" in CLEAN_PROMPT
    assert "Not: naming an example" in CLEAN_PROMPT
