# Knowledge service

The memory of past grading for an AI assessment grader. It is one small
service in a larger set of microservices, built in Python and meant to run on
Kubernetes.

**Status: work in progress.** The API, the tests and the local Kubernetes setup
exist. The AWS setup (RDS, ECR, EKS) is not written yet.

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

## API

| Call | What it does |
|---|---|
| `POST /examples` | Save an approved example and compute its embedding |
| `GET /examples?assignment_id=...` | List the examples for one assignment (newest first, up to 100) |
| `POST /examples/search` | Return the examples most similar in meaning to some text, inside one assignment |
| `GET /health` | Report whether the service is alive |
| `GET /ready` | Report whether the service can reach its database |

Bad input is refused with a 422 that names the field and the reason but never
repeats what the caller sent.

## Tests

```
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -v
```

The same tests run on every pull request, and the `main` branch rules require
them to pass before a merge.

## Running on Kubernetes

The cluster files live in `k8s/`:

- `k8s/base/` holds what every environment shares (the Deployment and Service).
- `k8s/overlays/local/` holds the laptop settings, including a Postgres pod.
- `k8s/deploy.sh` looks at which cluster `kubectl` points at and applies the
  matching settings. It refuses any cluster it does not recognise.

```
bash k8s/deploy.sh --preview   # show what would be deployed
bash k8s/deploy.sh             # deploy
```

First time on a cluster: create the Secret by hand (the script prints the
command), then run `deploy.sh`. On its first start the database sets itself up
from `db/init.sql` and gives the `knowledge_app` login its password from the
Secret. To start the database from scratch:

```
kubectl delete -n knowledge deployment/knowledge-db pvc/knowledge-db-data
bash k8s/deploy.sh
```

The embedding model loads on the first save or search call, not at startup, so
the first call is slower. If it is ever preloaded at startup, add a
`startupProbe` to the Deployment so a slow start is not mistaken for a crash.

On AWS the database is RDS, so the Postgres pod is not used there.

## Security notes

- This repo is public. It never contains passwords, API keys or Kubernetes
  Secret files, not even templates.
- The service's database login can read and add examples but cannot update or
  delete them.
- The database passwords live only in a Kubernetes Secret that is created by
  hand with a command. They are never written into any file.
