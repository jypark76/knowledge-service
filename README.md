# Knowledge service

The memory of past grading for an AI assessment grader. It is one small
service in a larger set of microservices, built in Python and meant to run on
Kubernetes.

**Status: early work in progress.** Only the database setup exists so far.

## What it does

1. **Stores approved examples.** After an instructor approves a grade, the
   example (student work, grade and reasoning) is saved here along with an
   embedding, a list of 384 numbers describing what the text is about.
2. **Finds similar examples.** Given a new piece of student work, it returns
   the past examples closest in meaning, so grading stays consistent.

## What it owns

Its own Postgres database with the pgvector extension and a single
`examples` table. No other service reads that database directly. Other
services ask this service through its API.

The setup lives in [`db/init.sql`](db/init.sql). The table holds an example ID,
an assignment ID (a plain reference to another service), the student work, the
grade, the reasoning, the embedding and a timestamp. It stores no student
names.

## Planned API

| Call | What it does |
|---|---|
| `POST /examples` | Save an approved example and compute its embedding |
| `GET /examples?assignment_id=...` | List the examples for one assignment |
| `POST /examples/search` | Return the examples most similar to some text |
| `GET /health` | Report whether the service is alive |

## Security notes

- This repo is public. It never contains passwords, API keys or Kubernetes
  Secret files, not even templates.
- The service's database login can read and add examples but cannot update or
  delete them.
- The database password is set by hand and is never written into any file.
