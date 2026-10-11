# In plain English: shared test helpers. The database guard lives here so every test
# file that touches a real database uses the same safety rule.
import os

import pytest


# In plain English: opt-in guard for tests that need a real database. With no database
# settings the tests quietly skip, so a normal run on a laptop still works. In the
# pipeline REQUIRE_DB=1 is set, and then a missing database is a failure, because a
# test that silently skips proves nothing. If the settings point at any database other
# than "knowledge_test", it refuses to run. The service login cannot delete, so these
# tests leave rows behind, which is only acceptable in a throwaway database.
@pytest.fixture
def require_test_database():
    if not os.environ.get("DB_HOST"):
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail("REQUIRE_DB is set but no database settings were given")
        pytest.skip("no test database configured")
    if os.environ.get("DB_NAME") != "knowledge_test":
        pytest.fail("refusing to run: DB_NAME must be 'knowledge_test', never a real database")


# In plain English: the shared service key for tests. The service refuses every call that does
# not carry a key, so the test setup configures one and hands each test client the matching
# header. The key is a made-up value that exists only in tests.
TEST_KEY = "test-only-key-0123456789abcdef0123456789"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_KEY}"}
os.environ["SERVICE_KEYS"] = TEST_KEY
