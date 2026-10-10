#!/usr/bin/env bash
# In plain English: this proves the database upgrade really works on a database that
# ALREADY holds data, which is the case that matters. It:
#   1. starts a throwaway Postgres built the old way (db/init.sql only, no changes yet),
#   2. saves one example in it, like the live database has,
#   3. runs the Liquibase change files against it,
#   4. checks the old example survived and the new column and rule are in place,
#   5. runs Liquibase a second time and checks it changed nothing,
#   6. throws the throwaway database away.
# The password is made up fresh for each run and never stored anywhere.
#
# Needs Docker. Usage:  bash db/check-upgrade.sh
set -euo pipefail

# Git Bash on Windows rewrites folder paths in docker commands unless told not to.
export MSYS_NO_PATHCONV=1

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
postgres_image="pgvector/pgvector:0.8.7-pg17"
liquibase_image="liquibase/liquibase:4.31.1"
name="upgrade-check-db"
database="upgrade_check"
password="$(openssl rand -hex 12)"

# Always remove the throwaway database, even if a check fails.
cleanup() {
  docker rm -f "$name" > /dev/null || true
}
trap cleanup EXIT

# Runs one SQL statement as the admin and prints only the answer.
sql() {
  docker exec -i "$name" psql -U postgres -d "$database" -v ON_ERROR_STOP=1 -tA -c "$1"
}

# Stops with a clear message if a check does not match what was expected.
expect() {
  if [ "$3" != "$2" ]; then
    echo "FAILED: $1 (expected '$2', got '$3')" >&2
    exit 1
  fi
  echo "ok: $1"
}

# Runs Liquibase against the throwaway database. It shares the database's own
# network, so "localhost" is the database on any computer.
migrate() {
  docker run --rm --network "container:$name" \
    -v "$here/changelog:/liquibase/changelog:ro" \
    -e LIQUIBASE_COMMAND_URL="jdbc:postgresql://localhost:5432/$database" \
    -e LIQUIBASE_COMMAND_USERNAME=postgres \
    -e LIQUIBASE_COMMAND_PASSWORD="$password" \
    "$liquibase_image" \
    --search-path=/liquibase/changelog --changelog-file=changelog-root.yaml update
}

echo "Starting a throwaway database built the old way (init.sql only)..."
docker run -d --name "$name" \
  -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB="$database" \
  -v "$here/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro" \
  "$postgres_image" > /dev/null

# Wait until the first-start script has finished AND the real server answers.
ready=""
for _ in $(seq 1 60); do
  if docker logs "$name" 2>&1 | grep -q "init process complete" \
    && docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -d "$database" > /dev/null; then
    ready="yes"
    break
  fi
  sleep 2
done
[ -n "$ready" ] || { echo "The throwaway database did not become ready." >&2; exit 1; }

echo "Before the upgrade:"
expect "the label column is not there yet" "0" \
  "$(sql "SELECT count(*) FROM information_schema.columns WHERE table_name = 'examples' AND column_name = 'source_submission_id'")"
sql "INSERT INTO examples (assignment_id, student_work, grade, reasoning, embedding) VALUES (gen_random_uuid(), 'old essay', 'B', 'old reasoning', array_fill(0.1::real, ARRAY[384])::vector)" > /dev/null
expect "one old example is saved" "1" "$(sql "SELECT count(*) FROM examples")"

echo "Running the upgrade (first time)..."
migrate

echo "After the upgrade:"
expect "the label column is there" "1" \
  "$(sql "SELECT count(*) FROM information_schema.columns WHERE table_name = 'examples' AND column_name = 'source_submission_id'")"
expect "the old example survived, with an empty label" "1" \
  "$(sql "SELECT count(*) FROM examples WHERE student_work = 'old essay' AND source_submission_id IS NULL")"
expect "the uniqueness rule exists" "1" \
  "$(sql "SELECT count(*) FROM pg_indexes WHERE indexname = 'examples_source_submission_id_key'")"
expect "Liquibase logged exactly one change" "1" "$(sql "SELECT count(*) FROM databasechangelog")"

echo "Checking the new rule behaves:"
label="11111111-1111-4111-8111-111111111111"
sql "INSERT INTO examples (assignment_id, student_work, grade, reasoning, embedding, source_submission_id) VALUES (gen_random_uuid(), 'new essay', 'A', 'new reasoning', array_fill(0.2::real, ARRAY[384])::vector, '$label')" > /dev/null
echo "ok: an example with a label can be saved"
if sql "INSERT INTO examples (assignment_id, student_work, grade, reasoning, embedding, source_submission_id) VALUES (gen_random_uuid(), 'duplicate', 'A', 'dup', array_fill(0.2::real, ARRAY[384])::vector, '$label')" > /dev/null 2>&1; then
  echo "FAILED: a second example with the same label was accepted" >&2
  exit 1
fi
echo "ok: a second example with the same label is refused"
sql "INSERT INTO examples (assignment_id, student_work, grade, reasoning, embedding) VALUES (gen_random_uuid(), 'another unlabelled', 'C', 'other', array_fill(0.3::real, ARRAY[384])::vector)" > /dev/null
echo "ok: any number of unlabelled examples can exist"

echo "Running the upgrade again (should change nothing)..."
before="$(sql "SELECT count(*) FROM examples")"
migrate
expect "Liquibase still has exactly one change logged" "1" "$(sql "SELECT count(*) FROM databasechangelog")"
expect "no examples were added or lost" "$before" "$(sql "SELECT count(*) FROM examples")"

echo "All upgrade checks passed."
