import pytest

from app.main import bootstrap
from stario.testing import TestClient


@pytest.fixture
async def client():
    async with TestClient(bootstrap) as c:
        yield c
