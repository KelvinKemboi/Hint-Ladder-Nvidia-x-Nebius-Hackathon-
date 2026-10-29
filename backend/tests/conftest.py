import pytest
from fastapi.testclient import TestClient

from sparring_partner.api.sessions import get_orchestrator
from sparring_partner.config import Settings
from sparring_partner.main import app
from sparring_partner.orchestrator import Orchestrator
from sparring_partner.sandbox import LocalBackend, Sandbox
from sparring_partner.session import SessionStore

API_SETTINGS = Settings(_env_file=None, explore_max_examples=60, sandbox_timeout_s=60)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def make_client():
    """make(llm) -> (TestClient, Orchestrator) wired to the local sandbox."""
    clients = []

    def make(llm):
        orchestrator = Orchestrator(
            SessionStore(), llm, Sandbox(LocalBackend(), API_SETTINGS), API_SETTINGS
        )
        app.dependency_overrides[get_orchestrator] = lambda: orchestrator
        client = TestClient(app)
        client.__enter__()
        clients.append((client, orchestrator))
        return client, orchestrator

    yield make
    for client, orchestrator in clients:
        client.portal.call(orchestrator.wait_idle)  # let background work finish first
        client.__exit__(None, None, None)
    app.dependency_overrides.clear()
