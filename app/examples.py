# In plain English: this file holds everything about saving and listing graded
# examples. First come the "shapes" of the data we accept (with strict rules, so
# bad input is turned away at the door). Then come two functions that talk to
# the database: one that saves an example and one that lists them.
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db import connect
from app.embeddings import embed_text


# In plain English: the rules for a request to save a new example. Every field
# is required and must be plain text of a sensible size. "forbid" means any
# extra field someone sneaks in is rejected instead of silently ignored.
class NewExample(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    assignment_id: UUID
    student_work: str = Field(min_length=1, max_length=20000)
    grade: str = Field(min_length=1, max_length=100)
    reasoning: str = Field(min_length=1, max_length=5000)


# In plain English: the rules for a "find similar examples" request. We need the
# assignment to look inside, the text to compare, and optionally how many
# matches to return (1 to 10, default 3). Extra fields are rejected.
class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    assignment_id: UUID
    query_text: str = Field(min_length=1, max_length=20000)
    limit: int = Field(default=3, ge=1, le=10)


# In plain English: turns the 384 numbers into the text form the database
# understands, like "[0.1,0.2,...]". The database then reads it as a vector.
def _vector_text(numbers):
    return "[" + ",".join(str(number) for number in numbers) + "]"


# In plain English: saves one new example. It first works out the 384 meaning
# numbers from the student's work, then adds ONE new row. It never changes or
# removes existing rows. The values are handed over separately from the SQL
# (the %s spots), so nothing typed by a user can ever become part of the SQL
# command itself. Returns the id of the new row.
def save_example(example):
    embedding = _vector_text(embed_text(example.student_work))
    with connect() as connection:
        row = connection.execute(
            "INSERT INTO examples "
            "(assignment_id, student_work, grade, reasoning, embedding) "
            "VALUES (%s, %s, %s, %s, %s::vector) RETURNING example_id",
            (
                example.assignment_id,
                example.student_work,
                example.grade,
                example.reasoning,
                embedding,
            ),
        ).fetchone()
    return str(row[0])


# In plain English: lists the saved examples for one assignment, newest first,
# up to 100. The 384 meaning numbers are left out because callers have no use
# for them. This only reads; it changes nothing.
def list_examples(assignment_id):
    with connect() as connection:
        rows = connection.execute(
            "SELECT example_id, assignment_id, student_work, grade, reasoning, "
            "created_at FROM examples WHERE assignment_id = %s "
            "ORDER BY created_at DESC LIMIT 100",
            (assignment_id,),
        ).fetchall()
    return [
        {
            "example_id": str(row[0]),
            "assignment_id": str(row[1]),
            "student_work": row[2],
            "grade": row[3],
            "reasoning": row[4],
            "created_at": row[5].isoformat(),
        }
        for row in rows
    ]


# In plain English: finds the saved examples whose MEANING is closest to the
# given text, but only inside the one assignment asked about. It turns the text
# into 384 numbers, then lets the database rank saved examples by how close
# their numbers are ("cosine distance", using the fast index). Each result gets
# a similarity score: 1 means practically identical, lower means less alike.
# This only reads; it changes nothing.
def search_examples(request):
    embedding = _vector_text(embed_text(request.query_text))
    with connect() as connection:
        rows = connection.execute(
            "SELECT example_id, student_work, grade, reasoning, "
            "1 - (embedding <=> %s::vector) AS similarity "
            "FROM examples WHERE assignment_id = %s "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (embedding, request.assignment_id, embedding, request.limit),
        ).fetchall()
    return [
        {
            "example_id": str(row[0]),
            "student_work": row[1],
            "grade": row[2],
            "reasoning": row[3],
            "similarity": round(float(row[4]), 4),
        }
        for row in rows
    ]
