# In plain English: this is the front door of the knowledge service. It answers
# "are you alive?" and "are you ready to work?", and it lets callers save a
# graded example and list the saved ones. Finding similar examples comes next.
from uuid import UUID

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db import database_is_ready
from app.examples import NewExample, list_examples, save_example

# Create the web application. The title and version show up on the automatic
# documentation page FastAPI builds at /docs.
app = FastAPI(title="Knowledge service", version="0.3.1")


# In plain English: when a request is turned away for bad input, FastAPI would
# normally repeat the bad value back to the caller. We don't want pieces of
# someone's data bouncing around in error messages (or ending up in logs). So
# this replaces that reply with a short one: only WHICH field was wrong and
# WHY, never what the caller typed. The status stays 422 ("bad input").
@app.exception_handler(RequestValidationError)
def bad_input(request: Request, error: RequestValidationError):
    problems = [
        {"field": ".".join(str(part) for part in item["loc"]), "reason": item["msg"]}
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"problems": problems})


# In plain English: a simple "are you alive?" check. If this address answers
# {"ok": true}, the service process is up. It does not look at the database.
# Kubernetes will later use it to decide whether to restart the service.
@app.get("/health")
def health():
    return {"ok": True}


# In plain English: an "are you ready to work?" check. It answers {"ok": true}
# only if the database is reachable too. If the database is down it answers
# with a plain 503 error and no details. Kubernetes will later use this to
# decide whether to send requests to this copy of the service.
@app.get("/ready")
def ready():
    if database_is_ready():
        return {"ok": True}
    return JSONResponse(status_code=503, content={"ok": False})


# In plain English: saves one new graded example. FastAPI first checks the
# request against the NewExample rules and turns bad requests away on its own
# (error 422) before any of our code runs. If the database has a problem, the
# caller gets a plain 503 with no details about why.
@app.post("/examples", status_code=201)
def create_example(example: NewExample):
    try:
        return {"example_id": save_example(example)}
    except psycopg.Error:
        return JSONResponse(status_code=503, content={"ok": False})


# In plain English: lists the saved examples for one assignment. The
# assignment_id must be a real UUID or FastAPI turns the request away (422).
@app.get("/examples")
def get_examples(assignment_id: UUID):
    try:
        return list_examples(assignment_id)
    except psycopg.Error:
        return JSONResponse(status_code=503, content={"ok": False})
