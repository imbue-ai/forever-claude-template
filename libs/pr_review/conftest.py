import pytest
from flask.testing import FlaskClient

from pr_review.runner import app


@pytest.fixture
def client() -> FlaskClient:
    app.config.update(TESTING=True)
    return app.test_client()
