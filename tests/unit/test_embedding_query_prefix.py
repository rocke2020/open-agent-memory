"""Query task formatting is configured once and never changes document inputs."""

import json
from contextlib import contextmanager

import httpx
import pytest
from test_query_time_recall import CONFIG, DatedRepository

from oam.config import ConfigurationError, load_config
from oam.contracts.search import SearchRequest
from oam.indexing.identity import index_generation
from oam.models.runtime import RuntimeModels
from oam.paths import default_config_path
from oam.retrieval.service import RecallService


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("prefix", ["", "Instruct: Retrieve relevant evidence.\nQuery: "])
def test_fresh_config_formats_only_recall_embedding_input(tmp_path, mode, prefix):
    path = tmp_path / "runtime.yaml"
    path.write_text(json.dumps({"models": {"embedding": {"query_prefix": prefix}}}))
    selected = load_config(path)
    assert (selected.sha256 != load_config(CONFIG_PATH).sha256) == bool(prefix)
    environment = {
        "OAM_EMBEDDING_MODEL": "synthetic",
        "OAM_EMBEDDING_URL": "http://model.test",
        "OAM_EMBEDDING_KEY": "synthetic",
        "VLM_MODEL": "synthetic",
        "VLM_BASE_URL": "http://model.test",
        "VLM_API_KEY": "synthetic",
    }
    assert index_generation(selected.config, environment) == index_generation(CONFIG, environment)
    requests = []
    query = "What changed about the apple harvest in 2025?"

    class Repository(DatedRepository):
        def lexical(self, text, limit):
            assert text == query
            return super().lexical(text, limit)

        def anchors(self, text, limit):
            assert text == query
            return super().anchors(text, limit)

    class Service(RecallService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            assert request.query == query
            yield Repository(request)

    def respond(request):
        body = json.loads(request.content)
        requests.append(body["input"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1.0] * selected.config.index.embedding_dimensions}
                ],
                "usage": {"prompt_tokens": 9, "total_tokens": 9},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        models = RuntimeModels(selected.config, environment, client)
        service = Service(selected.config, None, models, generation_id="generation")
        result = service.search(object(), SearchRequest(query=query, recall_level=mode))
        models.embed(("Unchanged indexed document.",), timeout_seconds=1)
    assert requests == [[prefix + query], ["Unchanged indexed document."]]
    assert "Apple harvest was good." in result["context"]


@pytest.mark.parametrize("prefix", [None, 1, True, [], {}])
def test_embedding_query_prefix_rejects_non_text(tmp_path, prefix):
    path = tmp_path / "runtime.yaml"
    path.write_text(json.dumps({"models": {"embedding": {"query_prefix": prefix}}}))
    with pytest.raises(ConfigurationError):
        load_config(path)


CONFIG_PATH = default_config_path()
