import pytest
from rest_framework.test import APIClient


@pytest.fixture
def api_client():
    """DRF APIClient - JSON 요청(PATCH 등)에 사용"""
    return APIClient()