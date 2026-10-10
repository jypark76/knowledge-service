# Starting a new service from this template

This repo is the pattern for every other microservice in the grading platform. It
holds a working service (FastAPI, its own Postgres database, tests, a pipeline,
Kubernetes files and a deploy script). A new service starts as a copy and then
changes the parts that belong to the knowledge service.

This repo is marked as a GitHub template repository. That setting adds a **Use this
template** button on its front page. The button creates a brand new repo with the
same files and a clean history, not linked to this one. The template copies files
only. Repo settings (branch rules, required checks, secret scanning) are NOT copied
and must be set up again for each new repo (step 3).

## Step 1: create the repo and rename

1. Click **Use this template**, then **Create a new repository**. Name it
   `<name>-service`, for example `assessment-service`. Keep it **public**.
2. Clone it. Keep this guide open from the template repo on GitHub. In your new
   clone, delete `NEW_SERVICE.md` and the paragraph in `README.md` titled "Starting a
   new service from this repo". They describe the template, and the rename below would
   turn them into nonsense.
3. Rename. The knowledge service's name appears in 11 forms
   (`knowledge-service`, `knowledge-db`, `knowledge_app`, `knowledge_test`, the
   `knowledge` namespace and database, and so on), and all of them are the word
   `knowledge` plus a suffix. One command changes every one (it was tried on a copy
   of this repo and the renamed copy passed its tests). Replace `assessment` with your
   service's word, in lowercase and with no dashes:

   ```
   grep -rlI "knowledge" --exclude-dir=.git --exclude-dir=.venv . | xargs sed -i 's/knowledge/assessment/g; s/Knowledge/Assessment/g'
   ```

4. Check nothing was missed. This should print nothing:

   ```
   grep -rIi "knowledge" --exclude-dir=.git --exclude-dir=.venv .
   ```

## Step 2: replace the parts that belong to the knowledge service

The rename only changes names. These parts hold the knowledge service's actual job
and must be rewritten for the new service:

| File | What to do |
|---|---|
| `db/init.sql` | Replace the `examples` table and its indexes. Keep the pattern: a login created with no password, and only the permissions the service needs |
| `app/examples.py` | Replace the request shapes and the save, list and search functions. Keep the null-character check |
| `app/main.py` | Replace the routes. Keep `/health`, `/ready`, the 422 handler and the `safe_reason` and `safe_field` helpers |
| `app/embeddings.py`, `requirements.txt`, `Dockerfile` | Delete if the service has no AI model. Then also remove `fastembed`, the model download line and `FASTEMBED_CACHE_PATH` from the Dockerfile |
| `tests/test_validation.py`, `test_database.py`, `test_vectors.py`, `test_search_settings.py`, `test_embeddings_lock.py` | Rewrite for the new routes and data. Keep the guards and the controls (see the rules below) |
| `k8s/base/deployment.yaml` | Set memory and CPU for the new service. The knowledge service needs room for its model |
| `README.md` | Rewrite the description, the API table and the known limits |

### If the service has no database

Delete these, then fix the pieces that point at them:

- `app/db.py` and the `/ready` check on the database (keep `/ready`, but make it check what the service really needs)
- `db/`, `k8s/overlays/local/db.yaml`, `networkpolicy.yaml`, `set-app-password.sh` and the `DB_*` settings in `configmap.yaml`
- the `database-tests` job in `.github/workflows/tests.yml` and `tests/test_database.py`
- in `k8s/deploy.sh`: the `knowledge-db-init` ConfigMap step and the Secret check, if the service has no Secret
- in `tests/test_k8s_manifests.py`: the `MADE_OUTSIDE` list and the rules that mention the database pod

## Step 3: one-time GitHub setup for the new repo

Do this before any real work, so every pull request is protected from the start.

1. **Two rulesets on the default branch** (Settings, then Rules, then Rulesets):
   - `main: tests must pass`: require a pull request and the status checks
     `tests`, `database-tests`, `kubernetes-checks` and `image-build`. No bypass for anyone.
     A check only appears in the picker after it has run once, so add them after the
     first pull request.
   - `main: review required`: one approval required, with the repository admin on the
     bypass list ("For pull requests only").
2. **Secret scanning and push protection:** Settings, then Security. Turn them on.
3. Leave **Template repository** unchecked on the new repo. It is not a template.
4. Do NOT add any Kubernetes Secret files to the repo, not even templates.

## Step 4: the order of work

The same order that finally worked on the knowledge service. The deploy path comes
before the real features.

1. Commit the `.gitignore` first.
2. Smallest runnable service: only `/health` and `/ready`. Build and run the image.
3. Pipeline and rulesets. **Prove the gate** with a deliberately broken pull request.
4. Deploy path: `k8s/base`, the local overlay and `deploy.sh`. Create the Secret by
   hand. Deploy the bare service and watch it become ready.
5. If it owns data: `db/init.sql` and the first-start password script together, then
   prove the limited login: it connects, changing or deleting rows is refused, and so is creating tables.
6. Add the real routes one at a time, each with strict input rules and a test that
   breaks it on purpose. Redeploy after each one.
7. Run the clean-room test: wipe the database disk and the deployments, redeploy from
   the repo plus the hand-made Secret, and read the log for the setup lines. Do not
   delete any pod during the test.
8. Harden: the NetworkPolicy (it can only be proven on a cluster that enforces
   policies, such as EKS with network policy on), and a second review pass.

## Step 5: connecting the service to others through Kafka

Do this only after the service works on its own. These are the rules the knowledge and
assessment services follow. The `platform` repo holds Kafka itself, the topics and the
written message contracts.

**Write the contract first.** Before any code, add `contracts/<topic>.md` in the `platform`
repo: the topic, the key, the exact fields, the limits at both ends, what the sender must
do, what the reader does and every reason code the reader can put on a dead-letter message.
No extra fields; a change means a new `schema_version`. Add the topic and its dead-letter
topic to the topic job. Platform tests fail if a topic has no contract or a reason code is
not written down. Each service adds a test that its own limits never exceed the other
end's, and, when a limit comes from a route constant, a test that the message limit is
that same constant and not a copy of the number.

**A program that sends (the outbox idea).**

- The request that causes the message (an approval, a new submission) only saves a row. It
  never talks to Kafka, so a Kafka outage cannot stop the service.
- A separate program, started with its own command from the same image, loops: it asks
  the database what is waiting, publishes it, and adds a row to an append-only "sent"
  table (a numbered Liquibase change) ONLY after Kafka's delivery callback confirms it.
  `flush()` returning zero is not proof. A refusal arrives only through the callback.
- Anything not confirmed stays waiting and is sent again. If a message can be lost after
  it was delivered (the receiver sets it aside, or the topic's retention ends), resend after
  a long wait and cap the number of sends, then leave it for a person.
- Build the message with the same rules as the contract and skip a row that cannot be
  built, once, by its ID, so a pile of bad rows cannot hold up the good ones. Never log
  student text.
- Use `acks=all` and the idempotent producer setting. Delivery is at-least-once, so say
  that, never exactly-once.

**A program that reads.**

- Commit an offset only AFTER the message has been dealt with. A good message is saved. A
  repeat changes nothing. A normal race (the other side moved on) is dropped and logged by
  ID only. A message that can never succeed (not JSON, unknown version, a broken rule, key
  not equal to the label, a save the database refuses for that message alone) is copied to
  the dead-letter topic with a reason in an `error` header, the copy is confirmed
  delivered, and only then is the original committed. Temporary trouble (database or Kafka
  down) commits nothing and stops the program so it is tried again.
- Make repeats harmless where the data lands: a unique label with `ON CONFLICT DO NOTHING`
  and a re-read, or an operation that already answers a repeat with the saved result. Prove
  it with a planted-collision test and a threaded race test.
- Pin `confluent-kafka` and check the wheel installs in the slim image.

**Deploying them.** Each program is its own Deployment from the same image with
`command: ["python", "-m", "app.<name>"]`, no port, no probes and no Service. Give it the
`deploy-phase: service` label (so it rolls out after the migration), add it to the
checker's locked-down list, add its pod label to the database door rule, put
`KAFKA_BOOTSTRAP` in the settings ConfigMap, and keep to one copy until it needs more.

**Testing them.**

- Use a pretend consumer and producer. The pretend consumer must fail the test if the
  program polls after its messages ran out, or a broken loop hangs the test for ever. The
  pretend producer must report a refusal through the callback while `flush()` still says
  zero, as real Kafka does.
- Test the real entry point too: run `main()` with a pretend Kafka library and stop it the
  way Kubernetes does. A deployed pod once crashed because only the inner function had been
  tested with the arguments that `main()` passed.
- Break each rule on purpose, one at a time (skip the delivery check, skip the key check,
  send the wrong rows, commit too early), and confirm a test fails. Keep the script out of
  the repo or in the scratchpad.
- Prove it live with Kafka's own console producer and consumer: a good message, the same
  one again, bad JSON, an unknown version, a clash. Stop the program, post a message,
  start it again and check nothing was lost and the lag is zero. Stop Kafka, do the
  normal action, start Kafka, and check the message arrives.

**A key on the routes.** A service that other services call should require a shared bearer
key from a Kubernetes Secret on every route except `/health` and `/ready`: the same fixed
401 for every failure, 503 and never open when no key is configured, keys compared in
constant time, several keys allowed so one can be replaced without downtime. Find the
routes by asking the app in the test so a new route cannot be forgotten.

## The rules that keep the checks honest

- The repo alone must rebuild everything. Anything done by hand and not written in the
  repo is a bug. The only exception is the Secret.
- A check must be able to fail, and you must have seen it fail. Each gate is proven
  with a deliberately broken pull request: grant `DELETE` in `init.sql`, remove the
  namespace from the generated ConfigMap, delete the model download line from the
  Dockerfile.
- A test that expects a failure first proves the success case in the same test.
- Probe every place caller data can appear (values, field names, query parameters) with
  a secret-looking marker, and check it never comes back.
- Tests that need a database refuse to run unless it is named `<name>_test`, and CI sets
  a flag so a skipped test counts as a failure.
- The pipeline job with the database runs every test file, never a list of "database
  files". The job without a database skips those tests quietly and still passes, so a
  file missing from a list runs nowhere. This happened on the assessment service: five
  test files never ran in the pipeline until a review caught it. After adding a test
  file, open the job log and check the test count went up.
- Once a database holds real data, every change to it is a numbered file in
  `db/changelog`, applied by the Liquibase job that `deploy.sh` runs. Never run SQL by
  hand against a database that matters. `db/init.sql` stays as the first version of
  the table. Prove each change with `db/check-upgrade.sh`, which upgrades a database
  that already holds data and checks the data survives and a second run changes nothing.
  The migration job must follow the same rules as the service: a pinned image, a
  normal user, a memory limit, and its password from the Secret.
- Run every new pipeline check on your own computer before the first pipeline run. In
  Git Bash on Windows put `MSYS_NO_PATHCONV=1` in front of `docker` commands that
  contain paths.
- Read the namespace of every object in the deploy preview, not just the names.
- Comments claim only what the code does. If a fix has a ceiling, say where it is.
- Review comments arrive after the pull request is open. Fix them on the same branch,
  test first, deploy the new version when code changed, and say the pull request is updated.
  A fix that is merged before its last commit is pushed needs a new branch.

## Before you open a pull request

- Ask: what did I do by hand that is not in the repo?
- Run the clean-room test.
- Run the full test suite and the Kubernetes checks on your computer.
- Get a second review pass on the diff.
