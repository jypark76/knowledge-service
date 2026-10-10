# In plain English: tests for the reader that takes approved examples off Kafka. Most use
# fakes (a pretend Kafka and a pretend save), so they need no Kafka and no database. They
# check the two things that matter most: what happens to each kind of message, and that a
# message is marked "done" only AFTER it has been dealt with. The last tests use the real
# database (guarded like test_database.py) to check a good message is really saved and a
# repeat is recognised.
import json
import uuid

import psycopg
import pytest

from app import consumer
from app.consumer import DEAD_LETTER_TOPIC, TOPIC, handle_message, run
from app.examples import LabelUsedForDifferentExample


def good_value(**change):
    data = {
        "schema_version": 1,
        "source_submission_id": str(uuid.uuid4()),
        "assignment_id": str(uuid.uuid4()),
        "student_work": "An essay.",
        "grade": "A",
        "reasoning": "Correct.",
    }
    data.update(change)
    return data


def encode(data):
    return json.dumps(data).encode("utf-8")


def keyed(data):
    return data["source_submission_id"].encode(), encode(data)


def fake_save_new(example):
    return str(uuid.uuid4()), True


def fake_save_repeat(example):
    return str(uuid.uuid4()), False


def never_save(example):
    raise AssertionError("a bad message must never reach the save")


# ---------- handle_message: what happens to each kind of message ----------

def test_a_good_new_message_is_saved():
    data = good_value()
    seen = []

    def save(example):
        seen.append(example)
        return str(uuid.uuid4()), True

    assert handle_message(*keyed(data), save=save) == ("saved", None)
    assert str(seen[0].source_submission_id) == data["source_submission_id"]
    assert seen[0].student_work == "An essay."


def test_a_repeat_is_recognised_and_not_an_error():
    assert handle_message(*keyed(good_value()), save=fake_save_repeat) == ("repeat", None)


def _without(field):
    data = good_value()
    key = data["source_submission_id"].encode()
    del data[field]
    return key, encode(data)


# name -> (key, value, expected reason code)
def bad_cases():
    other_key = str(uuid.uuid4()).encode()
    return {
        "invalid_json": (other_key, b"{not json", "invalid_json"),
        "not_utf8": (other_key, b"\xff\xfe", "invalid_json"),
        "not_an_object": (other_key, b"[1, 2]", "invalid_json"),
        "empty_value": (other_key, None, "invalid_json"),
        "unknown_version": (*keyed(good_value(schema_version=2)), "unknown_schema_version"),
        "missing_version": (*_without("schema_version"), "unknown_schema_version"),
        "version_true_is_not_one": (*keyed(good_value(schema_version=True)), "unknown_schema_version"),
        "empty_grade": (*keyed(good_value(grade="")), "invalid_field"),
        "reasoning_too_long": (*keyed(good_value(reasoning="r" * 10_001)), "invalid_field"),
        "bad_uuid": (*keyed(good_value(assignment_id="nope")), "invalid_field"),
        "extra_field": (*keyed(good_value(student_name="Ann")), "invalid_field"),
        "null_character": (*keyed(good_value(student_work="a\x00b")), "invalid_field"),
        "missing_label": (*_without("source_submission_id"), "invalid_field"),
        "key_mismatch": (other_key, encode(good_value()), "key_mismatch"),
        "key_missing": (None, encode(good_value()), "key_mismatch"),
    }


@pytest.mark.parametrize("name", sorted(bad_cases()))
def test_a_bad_message_is_dead_lettered_with_its_code_and_never_saved(name):
    key, value, reason = bad_cases()[name]
    assert handle_message(key, value, save=never_save) == ("dead_letter", reason)


def test_the_longest_allowed_message_is_accepted():
    data = good_value(student_work="w" * 20_000, grade="g" * 100, reasoning="r" * 10_000)
    assert handle_message(*keyed(data), save=fake_save_new) == ("saved", None)


def test_a_label_used_for_a_different_example_is_dead_lettered():
    def save(example):
        raise LabelUsedForDifferentExample()

    assert handle_message(*keyed(good_value()), save=save) == ("dead_letter", "label_conflict")


# A save the database refuses for this message alone (data it cannot store, a rule it
# enforces) would fail the same way every time, so it is dead-lettered instead of
# blocking its lane forever.
@pytest.mark.parametrize("error", [psycopg.DataError("bad data"), psycopg.IntegrityError("rule")])
def test_a_save_the_database_refuses_for_this_message_is_dead_lettered(error):
    def save(example):
        raise error

    assert handle_message(*keyed(good_value()), save=save) == ("dead_letter", "save_rejected")


def test_a_database_failure_is_not_swallowed():
    def save(example):
        raise psycopg.OperationalError("down")

    with pytest.raises(psycopg.OperationalError):
        handle_message(*keyed(good_value()), save=save)


def test_the_reason_never_contains_the_student_text():
    data = good_value(student_work="SECRET-MARKER", grade="")
    assert "SECRET-MARKER" not in repr(handle_message(*keyed(data), save=never_save))


# ---------- run: the loop and the order of "done" ----------

class FakeMessage:
    def __init__(self, key, value, offset=0, error=None):
        self._key, self._value, self._offset, self._error = key, value, offset, error

    def key(self):
        return self._key

    def value(self):
        return self._value

    def offset(self):
        return self._offset

    def error(self):
        return self._error


class FakeConsumer:
    def __init__(self, messages, log):
        self.messages = list(messages)
        self.log = log
        self.subscribed = None
        self.closed = False

    def subscribe(self, topics):
        self.subscribed = topics

    def poll(self, timeout):
        # A loop that keeps polling after the messages ran out would hang a test forever.
        assert self.messages, "polled after the messages ran out"
        return self.messages.pop(0)

    def commit(self, message=None, asynchronous=True):
        assert asynchronous is False
        self.log.append(("commit", message.offset()))

    def close(self):
        self.closed = True


class FakeProducer:
    # "rejected" pretends the broker refused the copy: the delivery callback gets an
    # error, yet flush() still reports nothing left in the queue, as real Kafka does.
    def __init__(self, log, left_over=0, rejected=False):
        self.log = log
        self.left_over = left_over
        self.rejected = rejected
        self.callbacks = []

    def produce(self, topic, key=None, value=None, headers=None, on_delivery=None):
        self.log.append(("produce", topic, key, value, headers))
        self.callbacks.append(on_delivery)

    def flush(self, timeout=None):
        self.log.append(("flush",))
        for callback in self.callbacks:
            if callback is not None:
                callback("broker said no" if self.rejected else None, None)
        return self.left_over


# Runs the loop until the fake has handed over all its messages.
def run_with(messages, save, left_over=0):
    log = []
    fake = FakeConsumer(messages, log)
    run(fake, FakeProducer(log, left_over), save=save, should_stop=lambda: not fake.messages)
    return fake, log


def test_the_loop_subscribes_to_the_agreed_topic():
    fake, _ = run_with([], fake_save_new)
    assert fake.subscribed == [TOPIC] == ["approved-examples"]
    assert fake.closed


def test_a_message_is_committed_only_after_it_is_saved():
    order = []

    def save(example):
        order.append("saved")
        return str(uuid.uuid4()), True

    class Recording(FakeConsumer):
        def commit(self, message=None, asynchronous=True):
            order.append("commit")
            super().commit(message, asynchronous)

    log = []
    fake = Recording([FakeMessage(*keyed(good_value()), 7)], log)
    run(fake, FakeProducer(log), save=save, should_stop=lambda: not fake.messages)
    assert order == ["saved", "commit"]
    assert log == [("commit", 7)]


def test_a_database_failure_means_no_commit_and_the_loop_stops():
    def save(example):
        raise psycopg.OperationalError("down")

    log = []
    fake = FakeConsumer([FakeMessage(*keyed(good_value()), 3)], log)
    with pytest.raises(psycopg.OperationalError):
        run(fake, FakeProducer(log), save=save, should_stop=lambda: False)
    assert log == []
    assert fake.closed


def test_a_bad_message_is_copied_to_the_dead_letter_topic_before_the_commit():
    _, log = run_with([FakeMessage(b"k", b"{oops", 5)], never_save)
    assert [entry[0] for entry in log] == ["produce", "flush", "commit"]
    _, topic, key, value, headers = log[0]
    assert topic == DEAD_LETTER_TOPIC == "approved-examples.dead-letter"
    assert key == b"k" and value == b"{oops"
    assert dict(headers)["error"] == b"invalid_json"
    assert log[2] == ("commit", 5)


def test_a_dead_letter_that_is_not_delivered_is_not_committed():
    log = []
    fake = FakeConsumer([FakeMessage(b"k", b"{oops", 5)], log)
    with pytest.raises(RuntimeError):
        run(fake, FakeProducer(log, left_over=1), save=never_save, should_stop=lambda: False)
    assert ("commit", 5) not in log


# flush() only says how many copies are still queued. A refused copy is reported through the
# delivery callback, so a refusal must stop the reader before the commit.
def test_a_dead_letter_the_broker_refused_is_not_committed():
    log = []
    fake = FakeConsumer([FakeMessage(b"k", b"{oops", 5)], log)
    with pytest.raises(RuntimeError):
        run(fake, FakeProducer(log, rejected=True), save=never_save, should_stop=lambda: False)
    assert ("commit", 5) not in log


def test_a_repeat_is_committed_and_nothing_is_dead_lettered():
    _, log = run_with([FakeMessage(*keyed(good_value()), 1)], fake_save_repeat)
    assert log == [("commit", 1)]


def test_messages_are_handled_in_order_each_committed_in_turn():
    _, log = run_with(
        [
            FakeMessage(*keyed(good_value()), 1),
            FakeMessage(b"bad", b"{", 2),
            FakeMessage(*keyed(good_value()), 3),
        ],
        fake_save_new,
    )
    assert [entry for entry in log if entry[0] == "commit"] == [("commit", 1), ("commit", 2), ("commit", 3)]


def test_a_kafka_error_stops_the_loop_without_a_commit():
    log = []
    fake = FakeConsumer([FakeMessage(None, None, 9, error="broker gone")], log)
    with pytest.raises(RuntimeError):
        run(fake, FakeProducer(log), save=never_save, should_stop=lambda: False)
    assert log == []


# ---------- the settings ----------

def test_the_group_and_manual_commit_settings():
    settings = consumer.consumer_settings({"KAFKA_BOOTSTRAP": "kafka:9092"})
    assert settings["group.id"] == "knowledge-service-examples"
    assert settings["enable.auto.commit"] is False
    assert settings["auto.offset.reset"] == "earliest"
    assert settings["bootstrap.servers"] == "kafka:9092"
    assert settings["max.poll.interval.ms"] >= 300_000


def test_a_missing_bootstrap_setting_stops_early():
    with pytest.raises(RuntimeError):
        consumer.consumer_settings({})


# ---------- with the real database ----------

@pytest.mark.usefixtures("require_test_database")
def test_a_good_message_is_really_saved_then_recognised_as_a_repeat():
    from app.db import connect

    data = good_value()
    key, value = keyed(data)
    assert handle_message(key, value) == ("saved", None)
    assert handle_message(key, value) == ("repeat", None)
    with connect() as connection:
        count = connection.execute(
            "SELECT count(*) FROM examples WHERE source_submission_id = %s",
            (data["source_submission_id"],),
        ).fetchone()[0]
    assert count == 1


@pytest.mark.usefixtures("require_test_database")
def test_the_same_label_with_different_content_is_a_label_conflict():
    data = good_value()
    key = data["source_submission_id"].encode()
    assert handle_message(key, encode(data)) == ("saved", None)
    assert handle_message(key, encode({**data, "grade": "F"})) == ("dead_letter", "label_conflict")
