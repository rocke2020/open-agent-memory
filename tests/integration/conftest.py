"""Explicit synthetic model identities for isolated database tests."""

import pytest


@pytest.fixture
def synthetic_model_environment(monkeypatch):
    for name, value in {
        "VLM_MODEL": "synthetic-vlm",
        "VLM_BASE_URL": "https://unused.example.test/v1",
        "VLM_API_KEY": "synthetic",
        "OAM_EMBEDDING_MODEL": "synthetic-embedding",
        "OAM_EMBEDDING_URL": "https://unused.example.test/v1",
        "OAM_EMBEDDING_KEY": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
