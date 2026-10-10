# In plain English: the reader. It takes approved examples off the Kafka topic
# "approved-examples" and saves them with the same save routine the web address uses.
# The message format is the shared contract in platform/contracts/approved-examples.md.
#
# The promise it keeps: a message is marked "done" (committed) only AFTER it has been
# dealt with, so a crash can never lose one. Dealt with means one of:
#   saved       a good new example was saved.
#   repeat      a good example that was already saved; nothing is saved twice.
#   dead_letter a message that can never succeed (not JSON, unknown version, a broken
#               rule, key not equal to the label, label used by a different example). It
#               is copied to the topic "approved-examples.dead-letter" with the reason
#               in a header, so it cannot block its lane, and a person can look at it.
# Temporary trouble (database down, Kafka down) is NOT dealt with: nothing is committed,
# the reader stops with an error, Kubernetes restarts it and the message is tried again.
#
# Nothing the student wrote is ever logged. Logs carry only the outcome and the offset.
import json
import logging
import os
import signal
import uuid
from typing import Literal

from pydantic import ValidationError

from app.examples import LabelUsedForDifferentExample, NewExample, save_example

TOPIC = "approved-examples"
DEAD_LETTER_TOPIC = "approved-examples.dead-letter"
GROUP_ID = "knowledge-service-examples"

log = logging.getLogger("knowledge-consumer")


# In plain English: the rules for one message. It is a NewExample (same limits, same
# text rules, no extra fields) plus the format version, and the label is required here
# because the label is what makes a delivery harmlessly repeatable.
class ApprovedExampleMessage(NewExample):
    schema_version: Literal[1]
    source_submission_id: uuid.UUID


# In plain English: decides what to do with one message and reports the outcome as a pair
# like ("saved", None) or ("dead_letter", "invalid_field"). It raises for temporary
# trouble (such as the database being down) so the caller does not commit.
# "save" is a parameter only so tests can pass a pretend one.
def handle_message(key, value, save=save_example):
    try:
        data = json.loads(value.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError, RecursionError):
        return "dead_letter", "invalid_json"
    if not isinstance(data, dict):
        return "dead_letter", "invalid_json"

    version = data.get("schema_version")
    if isinstance(version, bool) or version != 1:
        return "dead_letter", "unknown_schema_version"

    try:
        message = ApprovedExampleMessage.model_validate(data)
    except ValidationError:
        return "dead_letter", "invalid_field"

    try:
        key_label = uuid.UUID(key.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return "dead_letter", "key_mismatch"
    if key_label != message.source_submission_id:
        return "dead_letter", "key_mismatch"

    try:
        _, created = save(message)
    except LabelUsedForDifferentExample:
        return "dead_letter", "label_conflict"
    return ("saved" if created else "repeat"), None


# In plain English: the loop. It asks Kafka for one message at a time, deals with it, and
# only then commits. A bad message is copied to the dead-letter topic and that copy must
# be confirmed delivered BEFORE the commit. "consumer" and "producer" are parameters so
# tests can pass pretend ones. The loop ends when should_stop() says so. Any error ends
# it too, after closing the consumer, and the error goes up to the caller.
def run(consumer, producer, save=save_example, should_stop=lambda: False):
    consumer.subscribe([TOPIC])
    try:
        while not should_stop():
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                raise RuntimeError(f"Kafka error: {message.error()}")
            outcome, reason = handle_message(message.key(), message.value(), save=save)
            if outcome == "dead_letter":
                producer.produce(
                    DEAD_LETTER_TOPIC,
                    key=message.key(),
                    value=message.value(),
                    headers=[("error", reason.encode("utf-8"))],
                )
                if producer.flush(30) != 0:
                    raise RuntimeError("dead-letter copy was not delivered")
            consumer.commit(message=message, asynchronous=False)
            log.info("offset %s: %s%s", message.offset(), outcome, f" ({reason})" if reason else "")
    finally:
        consumer.close()


# In plain English: the Kafka settings. Commits are manual (the reader decides when a
# message is done), a brand-new group starts from the oldest message, and the generous
# poll interval allows for the slow embedding step on a long essay.
def consumer_settings(environment):
    bootstrap = environment.get("KAFKA_BOOTSTRAP")
    if not bootstrap:
        raise RuntimeError("Missing required setting: KAFKA_BOOTSTRAP")
    return {
        "bootstrap.servers": bootstrap,
        "group.id": GROUP_ID,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "max.poll.interval.ms": 600_000,
    }


# In plain English: starts the real reader. It stops cleanly when Kubernetes asks it to
# (SIGTERM), after finishing the message in hand.
def main():
    from confluent_kafka import Consumer, Producer

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = consumer_settings(os.environ)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    producer = Producer({"bootstrap.servers": settings["bootstrap.servers"]})
    run(Consumer(settings), producer, should_stop=lambda: bool(stopping))


if __name__ == "__main__":
    main()
