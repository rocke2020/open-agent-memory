"""Index-time ANN admission owns its transaction-local search budget."""

import json
from pathlib import Path
from types import SimpleNamespace

from psycopg.pq import TransactionStatus

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.indexing.identity import index_generation
from oam.indexing.links import propose_semantic_links


class LinkConnection:
    """Database protocol double; it does not simulate vector selection."""

    def __init__(self):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)
        self.settings = {}
        self.row = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, parameters=None, **kwargs):
        if "set_config('hnsw.iterative_scan'" in statement:
            self.settings["hnsw.iterative_scan"] = parameters[-2]
            self.settings["hnsw.max_scan_tuples"] = parameters[-1]
        self.row = (False,) * 5 if "bool_or" in statement else (0,)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


def test_link_search_uses_index_budget_independently_of_recall_budget(tmp_path):
    path = tmp_path / "override.yaml"
    path.write_text(
        json.dumps(
            {
                "index": {
                    "semantic_link_ann_iterative_scan": "strict_order",
                    "semantic_link_ann_scan_max_tuples": 73,
                },
                "search": {"ann_scan_max_tuples": 19},
            }
        )
    )
    config = load_config(path).config
    authority = AuthorizedUser("key", "tenant", "worker", "user", frozenset())
    connection = LinkConnection()

    propose_semantic_links(connection, authority, config, "generation", ["seed"])

    assert connection.settings == {
        "hnsw.iterative_scan": "strict_order",
        "hnsw.max_scan_tuples": "73",
    }


def test_index_link_budget_changes_generation_but_recall_budget_does_not():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    environment = {
        "VLM_MODEL": "synthetic",
        "VLM_BASE_URL": "http://models.example.test/v1",
        "VLM_API_KEY": "synthetic",
        "OAM_EMBEDDING_MODEL": "synthetic",
        "OAM_EMBEDDING_URL": "http://embedding.example.test/v1",
        "OAM_EMBEDDING_KEY": "synthetic",
    }
    generation = index_generation(config, environment)
    index_changed = config.model_copy(
        update={"index": config.index.model_copy(update={"semantic_link_ann_scan_max_tuples": 73})}
    )
    recall_changed = config.model_copy(
        update={"search": config.search.model_copy(update={"ann_scan_max_tuples": 19})}
    )

    assert index_generation(index_changed, environment) != generation
    assert index_generation(recall_changed, environment) == generation
