# In plain English: search must switch on pgvector's "keep looking" mode
# (iterative scan) before it asks the database anything. Without it, a small
# assignment surrounded by many closer rows from other assignments can come back
# with too few results. This test needs no database: a recording stand-in for the
# connection captures what the search sends, in order.
import uuid

import app.examples as examples
from app.examples import SearchRequest, search_examples


class RecordingConnection:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *details):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return self

    def fetchall(self):
        return []


def test_search_turns_on_iterative_scan_before_it_queries(monkeypatch):
    recorder = RecordingConnection()
    monkeypatch.setattr(examples, "connect", lambda: recorder)
    monkeypatch.setattr(examples, "embed_text", lambda text: [0.0] * 384)

    search_examples(SearchRequest(assignment_id=uuid.uuid4(), query_text="cells produce energy"))

    assert recorder.statements[0] == "SET LOCAL hnsw.iterative_scan = strict_order"
    assert recorder.statements[1].lstrip().startswith("SELECT")
