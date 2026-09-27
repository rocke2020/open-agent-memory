from threading import Event

import httpx
import pytest
from test_runtime_models import runtime_config

from oam.models.runtime import ModelDispatchUnavailable, RuntimeModels


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(httpx.ConnectTimeout, "released"), (httpx.ReadTimeout, "unknown"), (None, "released")],
)
def test_model_permit_tracks_remote_completion_at_http_boundary(failure, expected):
    events = []

    class Permit:
        permit_id = "00000000-0000-0000-0000-000000000001"

        def dispatched(self):
            events.append("dispatch")

        def complete(self):
            events.append("released")

        def uncertain(self):
            events.append("unknown")

    class Admission:
        def acquire_model(self, kind, *, background, timeout):
            assert (kind, background) == ("llm", True)
            assert 0 < timeout <= 2
            return Permit()

    def respond(request):
        events.append("http")
        if failure:
            raise failure("synthetic transport failure", request=request)
        return httpx.Response(500, json={"error": "synthetic terminal response"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        models = RuntimeModels(
            runtime_config(),
            {"MODEL_URL": "https://synthetic.test", "MODEL_KEY": "synthetic"},
            http,
            background=True,
            admission=Admission(),
        )
        with pytest.raises(failure or httpx.HTTPStatusError):
            models.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=2
            )
    assert events == ["dispatch", "http", expected]


def test_stopping_worker_does_not_dispatch_another_model_call():
    stopped = Event()
    stopped.set()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("model dispatched"))
    ) as http:
        models = RuntimeModels(
            runtime_config(),
            {"MODEL_URL": "https://synthetic.test", "MODEL_KEY": "synthetic"},
            http,
            background=True,
            stop_event=stopped,
        )
        with pytest.raises(ModelDispatchUnavailable):
            models.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )


def test_capacity_timeout_before_dispatch_is_zero_call():
    from oam.capacity import CapacityUnavailable

    class FullAdmission:
        def acquire_model(self, *args, **kwargs):
            raise CapacityUnavailable("full")

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("model dispatched"))
    ) as http:
        models = RuntimeModels(
            runtime_config(),
            {"MODEL_URL": "https://synthetic.test", "MODEL_KEY": "synthetic"},
            http,
            admission=FullAdmission(),
        )
        with pytest.raises(ModelDispatchUnavailable):
            models.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )


def test_admission_database_failure_is_predispatch_unavailability(monkeypatch):
    import psycopg

    from oam.capacity import PostgresCapacity

    capacity = PostgresCapacity(runtime_config(), "synthetic-unused-dsn")

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("synthetic database outage")

    monkeypatch.setattr(capacity, "_execute", unavailable)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("model dispatched"))
    ) as http:
        models = RuntimeModels(
            runtime_config(),
            {"MODEL_URL": "http://synthetic.test", "MODEL_KEY": "synthetic"},
            http,
            admission=capacity,
        )
        with pytest.raises(ModelDispatchUnavailable):
            models.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )


def test_failure_after_http_completion_is_not_reported_as_zero_dispatch():
    from oam.capacity import CapacityUnavailable

    class Permit:
        permit_id = "00000000-0000-0000-0000-000000000001"

        def dispatched(self):
            pass

        def complete(self):
            raise CapacityUnavailable("synthetic post-dispatch persistence failure")

    class Admission:
        def acquire_model(self, *args, **kwargs):
            return Permit()

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    ) as http:
        models = RuntimeModels(
            runtime_config(),
            {"MODEL_URL": "http://synthetic.test", "MODEL_KEY": "synthetic"},
            http,
            admission=Admission(),
        )
        with pytest.raises(CapacityUnavailable) as failure:
            models.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )
        assert not isinstance(failure.value, ModelDispatchUnavailable)


def test_dispatch_and_cleanup_database_failure_does_not_leak_local_capacity():
    import psycopg

    class Permit:
        permit_id = "00000000-0000-0000-0000-000000000001"

        def dispatched(self):
            raise psycopg.OperationalError("synthetic dispatch outage")

        def complete(self):
            raise psycopg.OperationalError("synthetic cleanup outage")

    class Admission:
        def acquire_model(self, *args, **kwargs):
            return Permit()

    config = runtime_config()
    config = config.model_copy(
        update={
            "worker": config.worker.model_copy(update={"max_model_calls": 1}),
            "provider": config.provider.model_copy(update={"llm_concurrency": 1}),
        }
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("model dispatched during outage"))
    ) as http:
        models = RuntimeModels(
            config,
            {"MODEL_URL": "http://synthetic.test", "MODEL_KEY": "synthetic"},
            http,
            background=True,
            admission=Admission(),
        )
        for _ in range(2):
            with pytest.raises(psycopg.OperationalError, match="cleanup outage"):
                models.generate(
                    "extraction",
                    system="s",
                    user="u",
                    max_output_tokens=1024,
                    timeout_seconds=0.05,
                )
