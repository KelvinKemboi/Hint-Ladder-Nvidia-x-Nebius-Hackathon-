from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sparring_partner import __version__
from sparring_partner.config import get_settings

app = FastAPI(title="Sparring Partner", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
