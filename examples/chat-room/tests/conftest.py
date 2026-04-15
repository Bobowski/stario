import main
import pytest

from stario.testing import TestClient


@pytest.fixture
async def client():
    async with TestClient(main.bootstrap) as c:
        yield c
