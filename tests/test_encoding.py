# In plain English: tests about how this service reads and writes text, so what it sends out means the
# same thing to every reader and odd characters are never mishandled.
#
# Part 1: every reply is JSON, labelled as JSON, and decodes to exactly what was stored. Text that
# looks like web page code (<script>) is only ever data inside that JSON.
# Part 2: unusual characters (invisible marks, terminal escape codes, text-direction tricks, the
# highest Unicode character, a half-written character) either round-trip exactly or are refused with
# a normal 422. They never cause a crash (a 500) or a changed value.
# Part 3: a Kafka message that says the same field twice is refused, because different programs read
# duplicates differently (some keep the first, some the last), and so does a message with NaN or
# Infinity, which JSON does not allow but Python accepts. A half-written character or a null inside a
# message is refused as a bad field instead of crashing the reader.
# (OWASP A05: CWE-115, CWE-116 and CWE-159.)
import uuid

import pytest
from fastapi.testclient import TestClient

from app.consumer import handle_message
from app.main import app
from conftest import AUTH_HEADERS
from test_consumer import fake_save_new, good_value

bare = TestClient(app, headers=AUTH_HEADERS)

# Text that is easy to mishandle. Each one is either stored exactly or refused with a 422.
SPECIALS = [
    "<script>alert(1)</script>",
    "</textarea><img src=x onerror=alert(1)>",
    "&amp; &lt; &#60; %3Cscript%3E",
    "\\u0041 \\n \\x41 \\\\",
    "\x1b[31mred\x1b[0m terminal escape",
    "‮evil‬ text-direction override",
    "﻿starts with a byte order mark",
    "\x7f delete character",
    "line separator paragraph",
    "\U0010ffff highest character",
    "tab\tinside and \x0b vertical tab",
    "emoji 👩‍💻 and combining é",
]


def body(**change):
    data = {"assignment_id": str(uuid.uuid4()), "student_work": "An essay.", "grade": "A", "reasoning": "Fine."}
    data.update(change)
    return data


# ---------- Part 1: every reply is JSON ----------

def assert_json_reply(response):
    assert response.headers["content-type"].split(";")[0] == "application/json", response.headers["content-type"]
    return response.json()  # decodes, or the test fails


def test_error_replies_are_labelled_json_and_decode():
    for reply in (
        bare.get("/examples"),
        bare.post("/examples", json={"nope": "<script>"}),
        bare.get("/nothing-here"),
        TestClient(app).get("/examples"),
        TestClient(app).get("/health"),
    ):
        assert_json_reply(reply)


@pytest.mark.usefixtures("require_test_database")
def test_success_replies_are_labelled_json_and_html_like_text_stays_data():
    assignment = str(uuid.uuid4())
    saved = bare.post("/examples", json=body(assignment_id=assignment, student_work="<script>alert(1)</script>"))
    assert saved.status_code == 201
    assert_json_reply(saved)
    listed = bare.get("/examples", params={"assignment_id": assignment})
    assert assert_json_reply(listed)[0]["student_work"] == "<script>alert(1)</script>"
    searched = bare.post("/examples/search", json={"assignment_id": assignment, "query_text": "<script>"})
    assert_json_reply(searched)


# ---------- Part 2: unusual characters ----------

@pytest.mark.usefixtures("require_test_database")
@pytest.mark.parametrize("special", SPECIALS)
def test_unusual_characters_are_stored_exactly_or_refused_never_a_crash(special):
    assignment = str(uuid.uuid4())
    for field in ("student_work", "reasoning"):
        reply = bare.post("/examples", json=body(assignment_id=assignment, **{field: special}))
        assert reply.status_code in (201, 422), (field, special, reply.status_code, reply.text)
        if reply.status_code == 201:
            stored = bare.get("/examples", params={"assignment_id": assignment}).json()
            assert special.strip() in [item[field] for item in stored], (field, special)


@pytest.mark.usefixtures("require_test_database")
def test_a_half_written_character_and_a_null_are_refused_with_a_normal_422():
    for escaped in ('"\\ud800"', '"a\\u0000b"', '"\\udfff"'):
        raw = ('{"assignment_id": "%s", "student_work": %s, "grade": "A", "reasoning": "r"}' % (uuid.uuid4(), escaped)).encode()
        reply = bare.post("/examples", content=raw, headers={"Content-Type": "application/json"})
        assert reply.status_code == 422, (escaped, reply.status_code)
        assert_json_reply(reply)
    control = bare.post("/examples", json=body())
    assert control.status_code == 201


def test_a_request_that_is_not_valid_utf8_or_not_json_is_a_client_error_not_a_crash():
    for raw in (b'{"student_work": "\xff\xfe"}', b"{not json", b"", b"[1,2,3]", b'"just a string"'):
        reply = bare.post("/examples", content=raw, headers={"Content-Type": "application/json"})
        assert reply.status_code in (400, 422), (raw, reply.status_code)  # the framework answers 400 for undecodable text
        assert_json_reply(reply)
        assert "not json" not in reply.text and "just a string" not in reply.text


# ---------- Part 3: ambiguous or damaged Kafka messages ----------

def message(text):
    data = good_value()
    return data["source_submission_id"].encode(), text.replace("LABEL", data["source_submission_id"]).replace("ASSIGN", str(uuid.uuid4())).encode()


def test_a_message_that_repeats_a_field_is_refused_as_invalid_json():
    for text in (
        '{"schema_version":1,"source_submission_id":"LABEL","assignment_id":"ASSIGN","student_work":"a","grade":"A","grade":"F","reasoning":"r"}',
        '{"schema_version":1,"schema_version":2,"source_submission_id":"LABEL","assignment_id":"ASSIGN","student_work":"a","grade":"A","reasoning":"r"}',
        '{"schema_version":1,"source_submission_id":"LABEL","source_submission_id":"LABEL","assignment_id":"ASSIGN","student_work":"a","grade":"A","reasoning":"r"}',
    ):
        key, value = message(text)
        assert handle_message(key, value, save=fake_save_new) == ("dead_letter", "invalid_json"), text
    control_key, control = message('{"schema_version":1,"source_submission_id":"LABEL","assignment_id":"ASSIGN","student_work":"a","grade":"A","reasoning":"r"}')
    assert handle_message(control_key, control, save=fake_save_new) == ("saved", None)


def test_a_message_with_nan_or_infinity_is_refused_as_invalid_json():
    for token in ("NaN", "Infinity", "-Infinity"):
        text = '{"schema_version":1,"source_submission_id":"LABEL","assignment_id":"ASSIGN","student_work":"a","grade":%s,"reasoning":"r"}' % token
        key, value = message(text)
        assert handle_message(key, value, save=fake_save_new) == ("dead_letter", "invalid_json"), token


def test_nested_repeats_are_refused_too():
    text = '{"schema_version":1,"source_submission_id":"LABEL","assignment_id":"ASSIGN","student_work":{"a":1,"a":2},"grade":"A","reasoning":"r"}'
    key, value = message(text)
    assert handle_message(key, value, save=fake_save_new) == ("dead_letter", "invalid_json")


# A half-written character or a null inside a Kafka message must be refused as a bad field. If it slipped
# through, saving it would crash the reader (the database driver cannot encode it) and the same message
# would crash it again after every restart.
def test_a_half_written_character_or_a_null_in_a_message_is_a_bad_field_not_a_crash():
    for escaped in ("a\\ud800b", "\\udfff", "a\\u0000b"):
        text = '{"schema_version":1,"source_submission_id":"LABEL","assignment_id":"ASSIGN","student_work":"%s","grade":"A","reasoning":"r"}' % escaped
        key, value = message(text)
        assert handle_message(key, value, save=fake_save_new) == ("dead_letter", "invalid_field"), escaped
