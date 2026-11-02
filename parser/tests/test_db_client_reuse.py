"""Regression: db.table() must reuse one schema client (fd leak -> Errno 8 mid-ingest)."""
from tinderbox import db


class _FakeSchema:
    def table(self, name):
        return name


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def schema(self, name):
        self.calls += 1
        return _FakeSchema()


class _Cfg:
    schema = "tinderbox"


def test_table_reuses_schema_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(db, "_client", fake)
    monkeypatch.setattr(db, "_config", _Cfg())
    monkeypatch.setattr(db, "_schema_client", None)
    for _ in range(50):
        assert db.table("messages") == "messages"
    assert fake.calls == 1


def test_http2_goaway_is_transient():
    from tinderbox.ingest.retry import is_transient
    exc = Exception("<ConnectionTerminated error_code:0, last_stream_id:19999, additional_data:None>")
    assert is_transient(exc)
