# In plain English: this is the front door of the knowledge service. Right now
# it does only one thing: it answers "are you alive?". The real features (save
# an example, find similar examples) come in later steps.
from fastapi import FastAPI

# Create the web application. The title and version show up on the automatic
# documentation page FastAPI builds at /docs.
app = FastAPI(title="Knowledge service", version="0.1.0")


# In plain English: a simple "are you alive?" check. If this address answers
# {"ok": true}, the service is up. Kubernetes will later call this same address
# to decide whether to keep the service running or restart it.
@app.get("/health")
def health():
    return {"ok": True}
