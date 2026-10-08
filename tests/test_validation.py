# In plain English: these tests check that the service turns away bad requests
# at the door. They need no database, because a bad request is refused before
# any database work starts. They also check that the refusal message never
# repeats back what the caller sent.
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GOOD_ID = "11111111-1111-1111-1111-111111111111"


# In plain English: a complete, valid request to save an example. Each test
# below copies it and breaks exactly one thing.
def good_example():
    return {
        "assignment_id": GOOD_ID,
        "student_work": "The mitochondria makes energy for the cell.",
        "grade": "A",
        "reasoning": "Correct and clear.",
    }


# In plain English: a complete, valid search request, used the same way.
def good_search():
    return {"assignment_id": GOOD_ID, "query_text": "cells produce energy"}


# In plain English: checks the request was refused with 422 ("bad input") and
# that the reply only names the field and reason. It must not contain the
# "input" or "ctx" parts that would echo the caller's data back.
def assert_refused(response, field):
    assert response.status_code == 422
    body = response.json()
    assert list(body.keys()) == ["problems"]
    assert body["problems"][0]["field"] == f"body.{field}"
    for problem in body["problems"]:
        assert set(problem.keys()) == {"field", "reason"}


def test_save_rejects_bad_uuid():
    data = good_example()
    data["assignment_id"] = "not-a-uuid"
    assert_refused(client.post("/examples", json=data), "assignment_id")


def test_save_rejects_blank_student_work():
    data = good_example()
    data["student_work"] = "   "
    assert_refused(client.post("/examples", json=data), "student_work")


def test_save_rejects_oversize_student_work():
    data = good_example()
    data["student_work"] = "a" * 20001
    assert_refused(client.post("/examples", json=data), "student_work")


def test_save_rejects_oversize_grade():
    data = good_example()
    data["grade"] = "a" * 101
    assert_refused(client.post("/examples", json=data), "grade")


def test_save_rejects_extra_field():
    data = good_example()
    data["extra"] = "sneaky"
    assert_refused(client.post("/examples", json=data), "extra")


def test_save_rejects_missing_field():
    data = good_example()
    del data["reasoning"]
    assert_refused(client.post("/examples", json=data), "reasoning")


def test_search_rejects_bad_uuid():
    data = good_search()
    data["assignment_id"] = "not-a-uuid"
    assert_refused(client.post("/examples/search", json=data), "assignment_id")


def test_search_rejects_blank_query():
    data = good_search()
    data["query_text"] = "   "
    assert_refused(client.post("/examples/search", json=data), "query_text")


def test_search_rejects_limit_too_big():
    data = good_search()
    data["limit"] = 50
    assert_refused(client.post("/examples/search", json=data), "limit")


def test_search_rejects_limit_zero():
    data = good_search()
    data["limit"] = 0
    assert_refused(client.post("/examples/search", json=data), "limit")


def test_search_rejects_extra_field():
    data = good_search()
    data["extra"] = "sneaky"
    assert_refused(client.post("/examples/search", json=data), "extra")


# In plain English: the list address needs a real UUID too.
def test_list_rejects_bad_uuid():
    response = client.get("/examples", params={"assignment_id": "not-a-uuid"})
    assert response.status_code == 422
    assert list(response.json().keys()) == ["problems"]


# In plain English: the refusal must not repeat the caller's secret-looking
# text anywhere in the reply.
def test_refusal_does_not_echo_input():
    data = good_example()
    data["assignment_id"] = "SECRET-MARKER-12345"
    response = client.post("/examples", json=data)
    assert response.status_code == 422
    assert "SECRET-MARKER-12345" not in response.text
