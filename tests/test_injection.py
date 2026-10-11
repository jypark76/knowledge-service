# In plain English: tests that feed text built to attack a database through every place this service
# takes text, and check that it is treated as plain text. The text is stored and read back exactly,
# never run as SQL. These tests need a real database (guarded like the other database tests).
#
# Why they exist: the code passes values to the database separately from the SQL (the %s spots),
# which is what stops SQL injection. Reading the code shows that today, but nothing would fail if a
# later change glued text into a SQL command. These tests would. They are the proof for the
# OWASP A05 Injection items in the coverage map.
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.consumer import handle_message
from app.db import connect
from app.main import app
from conftest import AUTH_HEADERS

pytestmark = pytest.mark.usefixtures("require_test_database")

client = TestClient(app, headers=AUTH_HEADERS)

# Text that would break or take over a SQL command if it were glued into one, plus the characters
# the database driver itself treats as placeholders (%s, %(name)s) and Python format fields.
HOSTILE = [
    "'; DROP TABLE examples; --",
    "x' OR '1'='1",
    '" OR ""="',
    "'); DELETE FROM examples; --",
    "Robert'); DROP TABLE examples;--",
    "\\'; SELECT pg_sleep(5); --",
    "100% sure %s %(name)s %d",
    "{0} {student_work} {VERSION_ONE_COLUMNS} ${1}",
    "$$ ; DROP TABLE examples ; $$",
    "’ OR ʼ1ʼ=ʼ1",
    "a\nb\r\nc'; --",
    "UNION SELECT example_id, assignment_id, student_work, grade, reasoning, 1.0 FROM examples",
]


def new_assignment():
    return str(uuid.uuid4())


def save(assignment_id, **change):
    body = {"assignment_id": assignment_id, "student_work": "An essay.", "grade": "A", "reasoning": "Fine."}
    body.update(change)
    return client.post("/examples", json=body)


def table_exists():
    with connect() as connection:
        return connection.execute("SELECT to_regclass('public.examples')").fetchone()[0] is not None


def count_for(assignment_id):
    with connect() as connection:
        return connection.execute(
            "SELECT count(*) FROM examples WHERE assignment_id = %s", (assignment_id,)
        ).fetchone()[0]


# In plain English: every hostile text goes in through every text field, comes back exactly as sent
# and is never run. The control: the same call with plain text works, so a refusal here would mean
# something other than the hostile text broke it.
@pytest.mark.parametrize("field", ["student_work", "grade", "reasoning"])
def test_hostile_text_is_stored_and_read_back_exactly(field):
    assert save(new_assignment()).status_code == 201  # control: plain text works
    assignment = new_assignment()
    for payload in HOSTILE:
        response = save(assignment, **{field: payload})
        assert response.status_code == 201, (field, payload, response.text)
    listed = client.get("/examples", params={"assignment_id": assignment}).json()
    assert sorted(item[field] for item in listed) == sorted(payload.strip() for payload in HOSTILE)
    assert count_for(assignment) == len(HOSTILE)
    assert table_exists()


# In plain English: text that looks like a command did not run: the table is still there, the other
# examples are still there, and nothing was deleted.
def test_destructive_looking_text_does_not_destroy_anything():
    keeper = new_assignment()
    assert save(keeper, student_work="Keep me.").status_code == 201
    for payload in HOSTILE:
        assert save(new_assignment(), student_work=payload).status_code == 201
    assert table_exists()
    assert count_for(keeper) == 1


# In plain English: a "always true" trick in the text does not widen what a search or a list returns.
# One assignment holds a trick example, another holds nothing, and the empty one must stay empty.
def test_an_always_true_trick_does_not_widen_a_list_or_a_search():
    holder, empty = new_assignment(), new_assignment()
    assert save(holder, student_work="x' OR '1'='1", grade="A' OR '1'='1").status_code == 201
    assert client.get("/examples", params={"assignment_id": empty}).json() == []
    for query in ("x' OR '1'='1", "' OR 1=1 --", "%"):
        found = client.post("/examples/search", json={"assignment_id": empty, "query_text": query})
        assert found.status_code == 200
        assert found.json() == []


# In plain English: hostile search text is just text to compare. It does not break the search.
def test_hostile_search_text_runs_normally():
    assignment = new_assignment()
    assert save(assignment, student_work="Plain essay about rivers.").status_code == 201
    for payload in HOSTILE:
        response = client.post("/examples/search", json={"assignment_id": assignment, "query_text": payload})
        assert response.status_code == 200, (payload, response.text)
        assert isinstance(response.json(), list)
    assert table_exists()


# In plain English: the places that must be a real ID (UUID) refuse anything else, so there is no
# way to smuggle a command into the query string, the search body or the source label. The refusal
# is a 422, and the reply never repeats what was sent.
@pytest.mark.parametrize("payload", ["11111111-1111-1111-1111-111111111111' OR '1'='1", "'; DROP TABLE examples; --", "1 OR 1=1"])
def test_ids_that_are_not_real_uuids_are_refused_without_being_repeated(payload):
    replies = [
        client.get("/examples", params={"assignment_id": payload}),
        client.post("/examples/search", json={"assignment_id": payload, "query_text": "q"}),
        save(new_assignment(), source_submission_id=payload),
        save(payload),
    ]
    for reply in replies:
        assert reply.status_code == 422
        assert "DROP TABLE" not in reply.text and "OR '1'" not in reply.text
    assert table_exists()


# In plain English: the same protection holds for messages that arrive through Kafka, which reach the
# database through the same save routine. A hostile example is saved once, stored exactly and run as
# nothing.
def test_hostile_text_in_a_kafka_message_is_stored_exactly():
    label, assignment = str(uuid.uuid4()), new_assignment()
    value = json.dumps({
        "schema_version": 1,
        "source_submission_id": label,
        "assignment_id": assignment,
        "student_work": HOSTILE[0],
        "grade": HOSTILE[1],
        "reasoning": HOSTILE[6],
    }).encode("utf-8")
    assert handle_message(label.encode(), value) == ("saved", None)
    stored = client.get("/examples", params={"assignment_id": assignment}).json()
    assert [(i["student_work"], i["grade"], i["reasoning"]) for i in stored] == [(HOSTILE[0], HOSTILE[1], HOSTILE[6])]
    assert table_exists()
