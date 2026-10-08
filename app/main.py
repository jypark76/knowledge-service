# In plain English: this is the front door of the knowledge service. Right now
# it answers two questions: "are you alive?" and "are you ready to work?". The
# real features (save an example, find similar examples) come in later steps.
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.db import database_is_ready

# Create the web application. The title and version show up on the automatic
# documentation page FastAPI builds at /docs.
app = FastAPI(title="Knowledge service", version="0.2.0")


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
