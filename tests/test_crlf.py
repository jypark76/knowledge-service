# In plain English: tests that carriage-return and line-feed characters in anything a caller or a
# Kafka producer sends can never split a web reply header or forge a new line in a log.
#
# Why it matters: a line break inside a header lets an attacker add headers of their own (for
# example a cookie), and a line break inside a log message lets them write fake log lines that
# look like real events. Both are old, well-known attacks (OWASP A05, CWE-93 and CWE-113, and
# log forging, CWE-117).
#
# Part 1 sends line breaks through every place the web service takes text and checks no reply
# header ever carries them. Part 2 sends hostile Kafka keys and values through the reader and
# checks no log line carries them. Part 3 checks the Kafka key must be a UUID written the normal way:
# Python's own UUID reader also accepts odd forms (a trailing newline, spaces, a plus sign, a
# 0x prefix), and one of those would otherwise be copied into a log line.
import json
import logging
import uuid
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.consumer import handle_message, run
from app.main import app
from conftest import AUTH_HEADERS
from test_consumer import FakeConsumer, FakeMessage, FakeProducer, fake_save_new, good_value

MARKER = "CRLF-MARKER"
# A header-splitting attempt and a log-forging attempt in one string.
ATTACK = f"x\r\nSet-Cookie: pwned=1\r\nX-{MARKER}: 1\r\n2026-10-10 INFO offset 0: saved {MARKER}"
UUID_QUIRKS = [
    "1234567812345678123456781234567\n",  # 31 digits and a newline: Python reads this as a UUID
    "12345678-1234-5678-1234-56781234567\r",
    " 1234567812345678123456781234567",
    "+1234567812345678123456781234567",
    "0x234567812345678123456781234567",
    "{12345678-1234-5678-1234-567812345678}",
    "urn:uuid:12345678-1234-5678-1234-567812345678",
    "12345678123456781234567812345678",  # no hyphens
]

bare = TestClient(app, headers=AUTH_HEADERS)


# In plain English: no reply header may contain a line break, a cookie we did not set, or any of the
# attacker's text.
def assert_headers_clean(response):
    for name, value in response.headers.items():
        assert "\r" not in name and "\n" not in name and "\r" not in value and "\n" not in value, (name, value)
        assert MARKER.lower() not in name.lower() and MARKER not in value, (name, value)
        assert "pwned" not in value, (name, value)
    assert "set-cookie" not in response.headers


# ---------- Part 1: reply headers ----------

def test_line_breaks_in_requests_never_reach_reply_headers_on_the_refusal_paths():
    replies = [
        bare.get("/examples", params={"assignment_id": ATTACK}),
        bare.get("/examples", params={"x": ATTACK}),  # the required assignment_id is missing
        bare.post("/examples", json={"assignment_id": ATTACK, "student_work": ATTACK, "grade": ATTACK, "reasoning": ATTACK}),
        bare.post("/examples/search", json={"assignment_id": ATTACK, "query_text": ATTACK}),
        bare.post("/examples", json={"nope": ATTACK}),
        bare.get("/" + quote(ATTACK)),
        TestClient(app).get("/examples", params={"assignment_id": ATTACK}),  # no key at all
        TestClient(app, headers={"Authorization": "Bearer %0d%0aSet-Cookie:%20pwned=1"}).get("/examples"),
    ]
    for reply in replies:
        assert_headers_clean(reply)
    assert [r.status_code for r in replies[:6]] == [422, 422, 422, 422, 422, 404]


@pytest.mark.usefixtures("require_test_database")
def test_line_breaks_in_saved_and_searched_text_never_reach_reply_headers():
    assignment = str(uuid.uuid4())
    saved = bare.post("/examples", json={"assignment_id": assignment, "student_work": ATTACK, "grade": "A", "reasoning": ATTACK})
    assert saved.status_code == 201  # control: the text is accepted as plain text
    assert_headers_clean(saved)
    listed = bare.get("/examples", params={"assignment_id": assignment})
    assert_headers_clean(listed)
    assert listed.json()[0]["student_work"] == ATTACK
    searched = bare.post("/examples/search", json={"assignment_id": assignment, "query_text": ATTACK})
    assert searched.status_code == 200
    assert_headers_clean(searched)


# ---------- Part 2: log lines ----------

def all_log_text(caplog):
    return [record.getMessage() for record in caplog.records]


def assert_logs_clean(caplog):
    for line in all_log_text(caplog):
        assert "\r" not in line and "\n" not in line, repr(line)
        assert MARKER not in line, repr(line)
        assert "Set-Cookie" not in line, repr(line)


def run_reader(messages, caplog):
    log = []
    fake = FakeConsumer(messages, log)
    with caplog.at_level(logging.DEBUG):
        run(fake, FakeProducer(log), save=fake_save_new, should_stop=lambda: not fake.messages)
    return log


def test_hostile_keys_and_values_never_put_line_breaks_into_the_log(caplog):
    good = good_value()
    messages = [FakeMessage(key.encode(), json.dumps(good_value(source_submission_id=key)).encode(), index)
                for index, key in enumerate(UUID_QUIRKS)]
    messages += [
        FakeMessage(ATTACK.encode(), ATTACK.encode(), 20),
        FakeMessage(None, None, 21),
        FakeMessage(b"\xff\xfe\r\n", b"\xff\xfe\r\n", 22),
        FakeMessage(good["source_submission_id"].encode(), json.dumps(good_value(student_work=ATTACK, grade=ATTACK)).encode(), 23),
        FakeMessage(good["source_submission_id"].encode(), json.dumps(good).encode(), 24),
    ]
    run_reader(messages, caplog)
    assert caplog.records, "the reader logged nothing, so this test would pass by accident"
    assert_logs_clean(caplog)


# ---------- Part 3: the key must be a UUID written the normal way ----------

@pytest.mark.parametrize("quirk", UUID_QUIRKS)
def test_a_key_that_is_not_a_normally_written_uuid_is_refused_even_when_python_would_read_it(quirk):
    data = good_value(source_submission_id=quirk)
    outcome = handle_message(quirk.encode(), json.dumps(data).encode(), save=fake_save_new)
    assert outcome[0] == "dead_letter", (quirk, outcome)


def test_the_normal_form_still_works_in_lower_and_upper_case():
    data = good_value()
    label = data["source_submission_id"]
    assert handle_message(label.encode(), json.dumps(data).encode(), save=fake_save_new) == ("saved", None)
    assert handle_message(label.upper().encode(), json.dumps(data).encode(), save=fake_save_new) == ("saved", None)
