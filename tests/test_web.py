"""End-to-end through FastAPI: create a subject, upload, re-upload, edit, move, delete."""


def create_subject(client) -> str:
    r = client.post("/subjects", data={"name": "Machine Learning", "code": "COMP30027",
                                       "semester": "2026 S2", "weeks": "12"},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def upload(client, sid, name, data=b"pdf bytes", **form):
    r = client.post(f"/api/subjects/{sid}/files", files={"file": (name, data)}, data=form)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow(client, storage):
    sid = create_subject(client)
    assert "Machine Learning" in client.get("/").text

    first = upload(client, sid, "COMP30027_Week3_Lecture5.pdf")
    assert first["outcome"] == "stored"
    assert (first["file"]["type"], first["file"]["week"], first["file"]["lecture"]) == (
        "slides",
        3,
        5,
    )
    assert upload(client, sid, "COMP30027_Week3_Lecture5.pdf")["outcome"] == "duplicate"
    assert upload(client, sid, "Lecture 6.mp4", b"video")["outcome"] == "rejected"
    assert len(client.get(f"/api/subjects/{sid}/files").json()) == 1

    for view in ("board", "table"):
        page = client.get(f"/subjects/{sid}?view={view}")
        assert page.status_code == 200
        assert "COMP30027_Week3_Lecture5.pdf" in page.text

    fid = first["file"]["id"]
    row = client.patch(f"/files/{fid}", data={"type": "tutorial", "view": "row"})
    assert row.status_code == 200 and 'value="tutorial" selected' in row.text
    row = client.patch(f"/files/{fid}", data={"week": "", "view": "row"})
    assert client.get(f"/api/subjects/{sid}/files").json()[0]["week"] is None
    assert client.patch(f"/files/{fid}", data={"week": "99", "view": "row"}).status_code == 422

    board = client.post(f"/files/{fid}/move", data={"slot": "lectures", "week": "7"})
    assert board.status_code == 200
    # The re-rendered board (lectures regroup), with the chip now in week 7's lectures.
    assert 'class="week-grid"' in board.text and f'data-file-id="{fid}"' in board.text
    bad = client.post(f"/files/{fid}/move", data={"slot": "lectures"})
    assert bad.status_code == 422  # weekly slot without a week
    moved = client.get(f"/api/subjects/{sid}/files").json()[0]
    assert (moved["type"], moved["week"], moved["reviewed"]) == ("slides", 7, True)

    assert client.delete(f"/files/{fid}?view=table").status_code == 200
    assert client.get(f"/api/subjects/{sid}/files").json() == []
    assert not storage.objects


def test_drop_onto_slot_via_api(client):
    sid = create_subject(client)
    body = upload(client, sid, "scan.pdf", slot="tutorials", week="5")
    assert (body["file"]["type"], body["file"]["week"]) == ("tutorial", 5)


def test_create_subject_validation(client):
    r = client.post("/subjects", data={"name": "  ", "weeks": "12"})
    assert r.status_code == 422


def test_json_api_patch(client):
    sid = create_subject(client)
    fid = upload(client, sid, "IMG_1.pdf")["file"]["id"]
    r = client.patch(f"/api/files/{fid}", json={"type": "reading", "week": 2})
    assert r.status_code == 200
    body = r.json()
    assert (body["type"], body["week"], body["included"], body["needs_review"]) == (
        "reading",
        2,
        False,
        False,
    )
    assert client.patch(f"/api/files/{fid}", json={"type": None}).status_code == 422


def test_subjects_page_shows_week_completeness(client, session_factory):
    from app.db.models import File

    sid = create_subject(client)
    upload(client, sid, "COMP30027_Week1_Lecture1.pdf", b"slides 1")
    upload(client, sid, "COMP30027_Week1_Lecture1.vtt", b"WEBVTT\n")
    upload(client, sid, "Week 1 Tutorial.pdf", b"tute 1")
    assert "0 of 12 weeks complete" in client.get("/").text  # lectures not processed yet
    with session_factory() as db:
        for f in db.query(File).filter(File.type.in_(["slides", "transcript"])):
            f.status = "parsed"
        db.commit()
    page = client.get("/").text
    assert "1 of 12 weeks complete" in page
    assert 'aria-valuenow="1"' in page


def test_board_pairs_slides_and_transcript(client):
    sid = create_subject(client)
    upload(client, sid, "COMP30027_Week2_Lecture3_NaiveBayes.pdf", b"slides")
    upload(client, sid, "lecture-03-week-2.vtt", b"WEBVTT\n")
    board = client.get(f"/subjects/{sid}/view/board").text
    week2 = board.split(">Week 2<", 1)[1].split(">Week 3<", 1)[0]
    # Deck and transcript side by side in week 2's lecture row: no transcript gap left.
    assert "Naive Bayes" in week2 and "lecture-03-week-2.vtt" in week2
    assert "Drop transcript" not in week2 and "Drop slides" not in week2


def test_invalid_subject_reopens_dialog(client):
    r = client.post("/subjects", data={"name": " ", "code": "X1", "weeks": "12"})
    assert r.status_code == 422
    assert "data-open-on-load" in r.text and 'value="X1"' in r.text


def test_board_columns_gaps_and_whole_subject(client):
    sid = create_subject(client)
    upload(client, sid, "COMP30027_Week2_Lecture3_NaiveBayes.pdf", b"slides")
    upload(client, sid, "Workshop_02.pdf", b"tute", slot="tutorials", week="2")
    upload(client, sid, "Partial_Solution_02.pdf", b"sol", slot="tutorials", week="2")
    board = client.get(f"/subjects/{sid}/view/board").text
    for col in ("Lecture", "Lecture transcript", "Tutorial", "Other"):
        assert f"<div>{col}</div>" in board
    week2 = board.split(">Week 2<", 1)[1].split(">Week 3<", 1)[0]
    assert "1 missing" in week2 and "Drop transcript" in week2 and "Drop tutorial" not in week2
    # Questions and solutions in one tutorial card.
    assert "Questions</span> · Workshop_02.pdf" in week2
    assert "Solutions</span> · Partial_Solution_02.pdf" in week2
    # Empty weeks collapse to one line; nothing says solutions are missing.
    assert board.count("Nothing uploaded") == 11 and "olutions missing" not in board
    assert "Still to add:" in board and "transcripts for week 2" in board
    assert "Not uploaded. It sets the topic list for the wiki." in board
    assert "1 of 12 weeks have lectures · 0 transcripts · 1 tutorial" in board


def test_review_table_filters_and_checks(client):
    sid = create_subject(client)
    upload(client, sid, "COMP30027_Week3_Lecture5.pdf", b"slides")
    low = upload(client, sid, "IMG_2041.pdf", b"mystery")["file"]
    assert low["needs_review"]
    table = client.get(f"/subjects/{sid}?view=table").text
    assert "All · 2" in table and "Needs a check · 1" in table and "Slides · 1" in table
    assert '<option value="slides" selected>Lecture slides</option>' in table
    assert "Uploaded · not parsed yet" in table and "Looks right" in table
    only = client.get(f"/subjects/{sid}/view/table?filter=review").text
    assert "IMG_2041.pdf" in only and "COMP30027_Week3_Lecture5.pdf" not in only
    row = client.patch(f"/files/{low['id']}", data={"reviewed": "true", "view": "row"}).text
    assert "Looks right" not in row and "Needs a check" not in row


def test_failed_files_show_their_error_on_hover(client, session_factory):
    import uuid

    from app.db.models import File

    sid = create_subject(client)
    fid = upload(client, sid, "COMP30027_Week3_Lecture5.pdf")["file"]["id"]
    with session_factory() as db:
        f = db.get(File, uuid.UUID(fid))
        f.status = "failed"
        f.status_detail = ("DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes\n"
                           "[SQL: INSERT INTO slides ...]")  # fmt: skip
        db.commit()
    reason = 'title="DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes"'
    board = client.get(f"/subjects/{sid}/view/board").text
    assert "Parse failed" in board and reason in board and "INSERT INTO" not in board
    assert reason in client.get(f"/subjects/{sid}/view/table").text
