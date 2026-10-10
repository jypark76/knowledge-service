# In plain English: these tests run the real service against a real Postgres
# database, so they check the parts the other tests cannot: saving, listing and
# searching for real, and that the service's limited login really cannot change
# or delete anything.
#
# SAFETY: the service's login cannot delete, so these tests cannot clean up after
# themselves. Every test adds rows with a brand new random assignment ID and
# leaves them behind. That is fine for a throwaway database and a disaster for a
# real one. So the tests refuse to run unless the database is named
# "knowledge_test". The pipeline creates a fresh one for every run and throws it
# away afterwards. To run these on your own computer, start a temporary Postgres
# container yourself (see the README) and point the DB_* settings at it.
import os
import random
import threading
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import examples as examples_module
from app.db import connect
from app.embeddings import embed_text
from app.examples import NewExample, _vector_text, save_example
from app.main import app

client = TestClient(app)


# In plain English: runs before every test in this file. With no database
# settings at all, the tests quietly skip, so a normal run on your laptop still
# works. In the pipeline REQUIRE_DB=1 is set, and then a missing database is a
# failure, because a test that silently skips proves nothing. If the settings
# point at any database other than "knowledge_test", it refuses to run.
@pytest.fixture(autouse=True)
def require_test_database():
    if not os.environ.get("DB_HOST"):
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail("REQUIRE_DB is set but no database settings were given")
        pytest.skip("no test database configured")
    if os.environ.get("DB_NAME") != "knowledge_test":
        pytest.fail("refusing to run: DB_NAME must be 'knowledge_test', never a real database")


# In plain English: saves one example through the real web address and returns
# its ID. It also checks the save worked.
def save(assignment_id, student_work, grade="A", reasoning="Correct."):
    response = client.post(
        "/examples",
        json={
            "assignment_id": assignment_id,
            "student_work": student_work,
            "grade": grade,
            "reasoning": reasoning,
        },
    )
    assert response.status_code == 201
    return response.json()["example_id"]


# In plain English: a brand new assignment ID, so one test's rows never mix
# with another test's.
def new_assignment():
    return str(uuid.uuid4())


# In plain English: with the right password the service says it is ready.
def test_ready_with_a_working_login():
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# In plain English: the ready check must be able to fail. With a wrong password
# it must answer 503 and say nothing about why. The test first proves the right
# password works, so a 503 can only be caused by the wrong password and not by a
# database that was never reachable at all.
def test_ready_fails_quietly_with_a_wrong_password(monkeypatch):
    assert client.get("/ready").status_code == 200
    monkeypatch.setenv("DB_PASSWORD", "not-the-password")
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"ok": False}


# In plain English: a saved example is really in the database, with its 384
# meaning numbers.
def test_save_stores_a_384_number_embedding():
    example_id = save(new_assignment(), "The mitochondria makes energy for the cell.")
    with connect() as connection:
        row = connection.execute(
            "SELECT vector_dims(embedding) FROM examples WHERE example_id = %s",
            (uuid.UUID(example_id),),
        ).fetchone()
    assert row[0] == 384


# In plain English: listing returns what was saved, leaves out the numbers, and
# returns nothing for a different assignment.
def test_list_returns_saved_examples_only_for_that_assignment():
    assignment = new_assignment()
    example_id = save(assignment, "Plants make food from sunlight.", "B")
    listed = client.get("/examples", params={"assignment_id": assignment}).json()
    assert [item["example_id"] for item in listed] == [example_id]
    assert "embedding" not in listed[0]
    other = client.get("/examples", params={"assignment_id": new_assignment()}).json()
    assert other == []


# In plain English: the point of the whole service. A question about cells and
# energy must find the mitochondria example before the sunlight one.
def test_search_ranks_the_closest_meaning_first():
    assignment = new_assignment()
    mitochondria = save(assignment, "The mitochondria makes energy for the cell.", "A")
    sunlight = save(assignment, "Plants make food from sunlight.", "B")
    results = client.post(
        "/examples/search",
        json={"assignment_id": assignment, "query_text": "cells produce energy"},
    ).json()
    assert [item["example_id"] for item in results] == [mitochondria, sunlight]
    assert results[0]["similarity"] > results[1]["similarity"]


# In plain English: when an assignment has its own examples, search stays inside it.
# Two assignments hold near identical text, and a search in one must never return the
# other's example. (An assignment with NO examples is different: it falls back to the
# others, and the tests at the end of this file cover that.)
def test_search_never_returns_another_assignments_examples():
    first = new_assignment()
    second = new_assignment()
    mine = save(first, "The mitochondria makes energy for the cell.")
    theirs = save(second, "The mitochondria makes energy for the cell.")
    results = client.post(
        "/examples/search",
        json={"assignment_id": first, "query_text": "cells produce energy"},
    ).json()
    assert [item["example_id"] for item in results] == [mine]
    assert theirs not in [item["example_id"] for item in results]


# In plain English: the service's login can read and add but can never change or
# remove an example. First it proves the test really is running as that limited
# login and that reading works, so the refusals below mean "not allowed" and not
# "login missing". Then it tries to change and to delete, and both must be
# refused, and the example must still be intact.
def test_service_login_cannot_update_or_delete():
    assignment = new_assignment()
    save(assignment, "The mitochondria makes energy for the cell.", "A")
    assignment_uuid = uuid.UUID(assignment)

    with connect() as connection:
        who = connection.execute("SELECT current_user").fetchone()[0]
        count = connection.execute(
            "SELECT count(*) FROM examples WHERE assignment_id = %s", (assignment_uuid,)
        ).fetchone()[0]
    assert who == "knowledge_app"
    assert count == 1

    for statement in (
        "DELETE FROM examples WHERE assignment_id = %s",
        "UPDATE examples SET grade = 'F' WHERE assignment_id = %s",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect() as connection:
                connection.execute(statement, (assignment_uuid,))

    listed = client.get("/examples", params={"assignment_id": assignment}).json()
    assert len(listed) == 1
    assert listed[0]["grade"] == "A"


# In plain English: why search needs pgvector's "keep looking" mode. The search
# index (HNSW) only looks at about 40 candidates across ALL assignments and filters
# afterwards. So a small assignment surrounded by many closer rows from another
# assignment comes back empty. This test builds exactly that situation: 60 close
# rows in one assignment and one far row in another. The ordinary assignment index
# would normally rescue the query, so the filter is written in a way that index
# cannot be used, which leaves the HNSW index as the only fast path. It checks the
# plan really uses the HNSW index, shows that WITHOUT iterative scan the small
# assignment is lost, and that WITH it the row is found.
def test_filtered_index_search_needs_iterative_scan_to_find_a_small_assignment():
    crowded = new_assignment()
    small = new_assignment()
    close_vector = _vector_text(embed_text("cells produce energy"))
    far_vector = _vector_text(embed_text("Plants make food from sunlight."))
    insert = (
        "INSERT INTO examples (assignment_id, student_work, grade, reasoning, embedding) "
        "VALUES (%s, %s, 'A', 'r', %s::vector)"
    )
    # 60 close rows, each nudged by a tiny different amount. Identical copies make
    # the index build a strange graph, so every row gets its own small difference.
    close_numbers = embed_text("cells produce energy")
    noise = random.Random(1234)
    with connect() as connection:
        for _ in range(60):
            nudged = [number + noise.uniform(-0.01, 0.01) for number in close_numbers]
            connection.execute(insert, (uuid.UUID(crowded), "close", _vector_text(nudged)))
        connection.execute(insert, (uuid.UUID(small), "far", far_vector))

    query = (
        "SELECT example_id FROM examples "
        f"WHERE assignment_id::text = '{small}' "
        f"ORDER BY embedding <=> '{close_vector}'::vector LIMIT 3"
    )

    def run(iterative):
        with connect() as connection:
            connection.execute("SET LOCAL enable_seqscan = off")
            # Look at only 10 candidates instead of the usual 40, so the effect
            # shows up reliably with just 60 close rows.
            connection.execute("SET LOCAL hnsw.ef_search = 10")
            if iterative:
                connection.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            plan = "\n".join(row[0] for row in connection.execute("EXPLAIN " + query).fetchall())
            rows = connection.execute(query).fetchall()
        return plan, rows

    plan, rows = run(iterative=False)
    assert "examples_embedding_idx" in plan
    assert rows == []

    plan, rows = run(iterative=True)
    assert "examples_embedding_idx" in plan
    assert len(rows) == 1


# In plain English: the tests below cover the fallback: what a search does when the
# assignment asked about has no examples of its own. That search looks across EVERY
# example in the database, including the ones other tests saved. So the history and
# geography topics are used by only one test each, and the checks look at the example's
# text and flags, not its id, which keeps them reliable on a database that still holds
# rows from earlier runs.
def search(assignment_id, query_text, limit=None, fallback=False):
    body = {"assignment_id": assignment_id, "query_text": query_text}
    if limit is not None:
        body["limit"] = limit
    if fallback:
        body["fallback_to_all"] = True
    return client.post("/examples/search", json=body).json()


# In plain English: the cold-start case. History 102 is brand new and has no examples,
# but History 101 has one. A reworded question about the French Revolution must still
# find History 101's example, say it came from elsewhere, and score it high. For
# reference the real model scores this pair at about 0.91, and about 0.40 against
# unrelated biology.
def test_new_assignment_borrows_examples_from_another_assignment():
    history_101 = new_assignment()
    history_102 = new_assignment()
    save(history_101, "The French Revolution began in 1789.", "A")

    results = search(history_102, "When did the French Revolution start?", fallback=True)

    assert results[0]["student_work"] == "The French Revolution began in 1789."
    assert results[0]["same_assignment"] is False
    assert results[0]["assignment_id"] != history_102
    assert results[0]["similarity"] > 0.8
    assert all(item["same_assignment"] is False for item in results)


# In plain English: the old rule. Biology 101 holds the BETTER match for the question
# (score about 0.88). Biology 102 holds its own, weaker example (about 0.69). Because
# Biology 102 has an example of its own, only that one may come back, flagged true, and
# Biology 101's better match must be ignored.
def test_assignment_with_its_own_examples_ignores_better_matches_elsewhere():
    biology_101 = new_assignment()
    biology_102 = new_assignment()
    save(biology_101, "The mitochondria makes energy for the cell.", "A")
    own = save(biology_102, "Plants make food from sunlight.", "B")

    results = search(biology_102, "cells produce energy", fallback=True)

    assert [item["example_id"] for item in results] == [own]
    assert results[0]["same_assignment"] is True
    assert results[0]["assignment_id"] == biology_102
    assert results[0]["similarity"] < 0.8


# In plain English: the fallback respects the limit and ranks best first. Geography 101
# holds three river examples and Geography 102 holds none. Asking for 2 about the
# longest river in Africa must return exactly 2, with the Nile example first (about
# 0.90, against about 0.63 for the Amazon one), all flagged false.
def test_fallback_respects_the_limit_and_ranks_best_first():
    geography_101 = new_assignment()
    geography_102 = new_assignment()
    save(geography_101, "The Nile is the longest river in Africa.", "A")
    save(geography_101, "The Amazon carries more water than any other river.", "B")
    save(geography_101, "Rivers flow downhill toward the sea.", "C")

    results = search(geography_102, "Which river is the longest in Africa?", limit=2, fallback=True)

    assert len(results) == 2
    assert results[0]["student_work"] == "The Nile is the longest river in Africa."
    assert results[0]["similarity"] > 0.8
    assert results[0]["similarity"] >= results[1]["similarity"]
    assert all(item["same_assignment"] is False for item in results)


# In plain English: the wall, tested for real. An assignment ID the service has never seen
# must get NOTHING back unless the caller explicitly asks to borrow, even though another
# assignment holds a perfect match. With the flag on, the same search borrows it and
# says so.
def test_unknown_assignment_gets_nothing_unless_the_caller_asks_to_borrow():
    other = new_assignment()
    unknown = new_assignment()
    save(other, "The Magna Carta was signed in 1215.", "A")

    without_flag = search(unknown, "When was the Magna Carta signed?")
    with_flag = search(unknown, "When was the Magna Carta signed?", fallback=True)

    assert without_flag == []
    assert with_flag[0]["student_work"] == "The Magna Carta was signed in 1215."
    assert with_flag[0]["same_assignment"] is False


# In plain English: the service's login is limited in what it can BUILD, not only in
# what it can change. First it proves the test is running as that limited login and that
# connecting works, so the refusal below means "not allowed" and not "login missing". Then
# it tries to create a table, which a login that may only read and add rows must be refused.
def test_limited_login_connects_but_cannot_create_tables():
    with connect() as connection:
        who = connection.execute("SELECT current_user").fetchone()[0]
    assert who == "knowledge_app"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect() as connection:
            connection.execute("CREATE TABLE should_not_exist (id int)")


# ---------------------------------------------------------------------------
# The source label: saving the same approved example twice stores it once
# ---------------------------------------------------------------------------

CONFLICT = {"problem": "This source_submission_id is already used for a different example"}
WORK = "The mitochondria makes energy for the cell."


# In plain English: saves an example that carries a label, and returns the whole
# reply without judging it, so a test can look at the status code.
def post_labelled(assignment, label, work=WORK, grade="A", reasoning="Correct."):
    return client.post(
        "/examples",
        json={
            "assignment_id": assignment,
            "student_work": work,
            "grade": grade,
            "reasoning": reasoning,
            "source_submission_id": label,
        },
    )


# In plain English: every saved example that carries this label, read straight
# from the database.
def rows_with_label(label):
    with connect() as connection:
        return connection.execute(
            "SELECT example_id, student_work, grade FROM examples WHERE source_submission_id = %s",
            (uuid.UUID(label),),
        ).fetchall()


# In plain English: a labelled example is saved with its label in the database.
def test_a_labelled_example_is_saved_with_its_label():
    label = str(uuid.uuid4())
    response = post_labelled(new_assignment(), label)
    assert response.status_code == 201
    rows = rows_with_label(label)
    assert [str(row[0]) for row in rows] == [response.json()["example_id"]]


# In plain English: sending the same label with the same content again (Kafka can
# deliver a message twice) returns the original example with a 200 and saves
# nothing new. The first send is the control and answers 201.
def test_the_same_label_and_content_returns_the_original():
    assignment, label = new_assignment(), str(uuid.uuid4())
    first = post_labelled(assignment, label)
    assert first.status_code == 201

    repeat = post_labelled(assignment, label)
    assert repeat.status_code == 200
    assert repeat.json() == first.json()
    assert len(rows_with_label(label)) == 1


# In plain English: spotting a repeat must happen BEFORE the slow step. Turning the
# essay into 384 numbers is the expensive part, so a repeat, and a clash, must not
# pay for it. The first save is the control and does the work once.
def test_a_repeat_does_not_redo_the_slow_embedding(monkeypatch):
    calls = []
    real_embed = examples_module.embed_text
    monkeypatch.setattr(
        examples_module, "embed_text", lambda text: (calls.append(text), real_embed(text))[1]
    )
    assignment, label = new_assignment(), str(uuid.uuid4())

    assert post_labelled(assignment, label).status_code == 201
    assert len(calls) == 1
    assert post_labelled(assignment, label).status_code == 200
    assert post_labelled(assignment, label, grade="C").status_code == 409
    assert len(calls) == 1


# In plain English: the same label with DIFFERENT content is refused with fixed
# wording that repeats none of it, and the original stays as it was. A different
# essay, grade, reasoning and assignment are each tried. The first save is the control.
def test_the_same_label_with_different_content_is_refused_and_changes_nothing():
    assignment, label = new_assignment(), str(uuid.uuid4())
    assert post_labelled(assignment, label).status_code == 201

    clashes = [
        post_labelled(assignment, label, work="SECRET-MARKER a different essay."),
        post_labelled(assignment, label, grade="SECRET-MARKER-C"),
        post_labelled(assignment, label, reasoning="SECRET-MARKER different reasons."),
        post_labelled(new_assignment(), label),
    ]
    for response in clashes:
        assert response.status_code == 409
        assert response.json() == CONFLICT
        assert "SECRET-MARKER" not in response.text

    rows = rows_with_label(label)
    assert len(rows) == 1
    assert rows[0][1] == WORK and rows[0][2] == "A"


# In plain English: examples WITHOUT a label are never merged, even when identical.
# That is how saving worked before, and it must not change. Two labels that differ
# are two different examples too.
def test_unlabelled_examples_and_different_labels_are_all_kept():
    assignment = new_assignment()
    body = {"assignment_id": assignment, "student_work": WORK, "grade": "A", "reasoning": "Correct."}
    first = client.post("/examples", json=body)
    second = client.post("/examples", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() != second.json()

    one, two = str(uuid.uuid4()), str(uuid.uuid4())
    assert post_labelled(assignment, one).status_code == 201
    assert post_labelled(assignment, two).status_code == 201
    listed = client.get("/examples", params={"assignment_id": assignment}).json()
    assert len(listed) == 4


# In plain English: a stand-in for a database connection that acts exactly like the
# real one, except that just before the service's first INSERT it lets a competing
# save go first. That is the moment a real race would hit: the service has checked
# "is this label taken?", found nothing, and is about to write, when someone else's
# row lands in between.
class RacingConnection:
    def __init__(self, real, before_insert, state):
        self.real, self.before_insert, self.state = real, before_insert, state

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *details):
        return self.real.__exit__(*details)

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("INSERT") and not self.state["fired"]:
            self.state["fired"] = 1
            self.before_insert()
        return self.real.execute(sql, params)


# In plain English: plants the collision. The first INSERT the service makes is
# preceded by the competitor, once. The competitor's own save uses the normal path.
def plant_collision(monkeypatch, competitor):
    real_connect = examples_module.connect
    state = {"fired": 0}
    monkeypatch.setattr(
        examples_module, "connect", lambda: RacingConnection(real_connect(), competitor, state)
    )
    return state


def competing_save(assignment, label, grade="A"):
    return save_example(
        NewExample(
            assignment_id=assignment,
            student_work=WORK,
            grade=grade,
            reasoning="Correct.",
            source_submission_id=label,
        )
    )


# In plain English: the SAME example lands first. The service's own write finds the
# label taken, looks again, sees its own example already saved and answers 200 with
# the competitor's ID, as for a repeat. There is still exactly one row.
def test_a_save_that_loses_the_race_to_the_same_example_is_treated_as_a_repeat(monkeypatch):
    assignment, label = new_assignment(), str(uuid.uuid4())
    winner = []
    state = plant_collision(monkeypatch, lambda: winner.append(competing_save(assignment, label)))

    response = post_labelled(assignment, label)

    assert state["fired"] == 1
    assert response.status_code == 200
    assert response.json() == {"example_id": winner[0][0]}
    assert len(rows_with_label(label)) == 1


# In plain English: a DIFFERENT example lands first. The service looks again and
# answers 409. The competitor's example is the one that stays, and there is one row.
def test_a_save_that_loses_the_race_to_a_different_example_gets_a_conflict(monkeypatch):
    assignment, label = new_assignment(), str(uuid.uuid4())
    state = plant_collision(monkeypatch, lambda: competing_save(assignment, label, grade="B"))

    response = post_labelled(assignment, label, grade="A")

    assert state["fired"] == 1
    assert response.status_code == 409
    assert response.json() == CONFLICT
    rows = rows_with_label(label)
    assert len(rows) == 1 and rows[0][2] == "B"


# In plain English: eight copies of the same message arrive at the same instant.
# Exactly one is saved (201), the other seven are recognised (200) and all eight get
# the same example ID. One row exists afterwards. This is a real race, not a planted one.
def test_eight_identical_saves_at_once_store_one_example():
    assignment, label = new_assignment(), str(uuid.uuid4())
    barrier = threading.Barrier(8)
    results = [None] * 8

    def worker(index):
        local_client = TestClient(app)
        barrier.wait()
        try:
            results[index] = local_client.post(
                "/examples",
                json={
                    "assignment_id": assignment,
                    "student_work": WORK,
                    "grade": "A",
                    "reasoning": "Correct.",
                    "source_submission_id": label,
                },
            )
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for result in results:
        assert not isinstance(result, Exception), result
    assert sorted(result.status_code for result in results) == [200] * 7 + [201]
    assert len({result.json()["example_id"] for result in results}) == 1
    assert len(rows_with_label(label)) == 1


# In plain English: long reasoning really is saved. A 6,000 character reasoning (past the
# old limit of 5,000) and the longest allowed, 10,000, both go in and come back whole.
def test_long_reasoning_is_saved_whole():
    assignment = new_assignment()
    for length in (6_000, 10_000):
        reasoning = "r" * length
        response = client.post(
            "/examples",
            json={"assignment_id": assignment, "student_work": f"Essay {length}", "grade": "A", "reasoning": reasoning},
        )
        assert response.status_code == 201, length
    stored = {item["student_work"]: len(item["reasoning"])
              for item in client.get("/examples", params={"assignment_id": assignment}).json()}
    assert stored == {"Essay 6000": 6_000, "Essay 10000": 10_000}
