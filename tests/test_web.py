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
    assert 'class="weeks"' in board.text and f'data-file-id="{fid}"' in board.text
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


def test_subjects_page_shows_week_completeness(client):
    sid = create_subject(client)
    upload(client, sid, "COMP30027_Week1_Lecture1.pdf", b"slides 1")
    upload(client, sid, "COMP30027_Week1_Lecture1.vtt", b"WEBVTT\n")
    upload(client, sid, "Week 1 Tutorial.pdf", b"tute 1")
    page = client.get("/").text
    assert "1 of 12 weeks complete" in page
    assert 'aria-valuenow="1"' in page


def test_board_pairs_slides_and_transcript(client):
    sid = create_subject(client)
    upload(client, sid, "COMP30027_Week2_Lecture3_NaiveBayes.pdf", b"slides")
    upload(client, sid, "lecture-03-week-2.vtt", b"WEBVTT\n")
    board = client.get(f"/subjects/{sid}/view/board").text
    assert '<details class="lecture">' in board
    assert "Naive Bayes" in board and "2 files" in board


def test_invalid_subject_reopens_dialog(client):
    r = client.post("/subjects", data={"name": " ", "code": "X1", "weeks": "12"})
    assert r.status_code == 422
    assert "data-open-on-load" in r.text and 'value="X1"' in r.text
