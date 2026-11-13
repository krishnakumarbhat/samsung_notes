"""Background question factory tests: offline safety and bounded refills."""
from __future__ import annotations

import random

from engine import background_question_factory as factory
from tests.bank_fixtures import add_question, make_db


class FakeReport:
    def __init__(self, inserted: int, errors=None):
        self.inserted = inserted
        self.errors = errors or []


class FakeFactory:
    def __init__(self, inserted: int = 5, errors=None):
        self.inserted = inserted
        self.errors = errors
        self.calls = []

    def bank_size(self) -> int:
        return 10

    def run(self, target_pool, **kwargs):
        self.calls.append({"target": target_pool, **kwargs})
        return FakeReport(self.inserted, self.errors)


def test_offline_probe_never_constructs_a_factory(tmp_path, monkeypatch):
    """No internet means no model client, no request, no status surprise."""
    path = make_db(tmp_path)
    created = []

    def exploding_factory(**kwargs):
        created.append(kwargs)
        raise AssertionError("factory must not be built offline")

    monkeypatch.setattr(factory, "STATUS_FILE", tmp_path / "status.json")
    result = factory.generate_once(path=str(path), probe=lambda: False,
                                   factory_factory=exploding_factory)
    assert result["online"] is False
    assert result["inserted"] == 0
    assert created == []


def test_full_buffer_skips_generation(tmp_path, monkeypatch):
    """A buffer already at target is a no-op even when online."""
    path = make_db(tmp_path)
    for index in range(3):
        add_question(path, concept=f"c{index}", text=f"Question {index}")
    monkeypatch.setattr(factory, "STATUS_FILE", tmp_path / "status.json")
    result = factory.generate_once(path=str(path), target=2, probe=lambda: True,
                                   factory_factory=FakeFactory)
    assert result["inserted"] == 0
    assert "already full" in result["message"]


def test_online_pass_calls_the_factory_once_and_reports_inserts(tmp_path, monkeypatch):
    """A deficit produces one bounded run and a published status file."""
    path = make_db(tmp_path)
    add_question(path, concept="c", text="Only question")
    monkeypatch.setattr(factory, "STATUS_FILE", tmp_path / "status.json")
    fake = FakeFactory(inserted=5)
    result = factory.generate_once(path=str(path), target=20, probe=lambda: True,
                                   factory_factory=lambda **kwargs: fake,
                                   rng=random.Random(1), sleep=lambda _s: None)
    assert result["inserted"] == 5
    assert result["staged"] == 6
    assert len(fake.calls) == 1
    assert fake.calls[0]["per_batch"] == 5
    assert factory.read_status()["staged"] == 6


def test_repeated_empty_runs_back_off_without_raising(tmp_path, monkeypatch):
    """A rate-limited model produces retries and a recorded error, not a crash."""
    path = make_db(tmp_path)
    add_question(path, concept="c", text="Only question")
    monkeypatch.setattr(factory, "STATUS_FILE", tmp_path / "status.json")
    fake = FakeFactory(inserted=0, errors=["rate limited"])
    slept = []
    result = factory.generate_once(path=str(path), target=20, probe=lambda: True,
                                   factory_factory=lambda **kwargs: fake,
                                   rng=random.Random(2), sleep=slept.append,
                                   max_attempts=3)
    assert result["inserted"] == 0
    assert len(fake.calls) == 3
    assert len(slept) == 3
    assert "rate limited" in result["errors"]
