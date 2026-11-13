"""Micro-batch factory: cognitive mix, schema rejection, checkpointing, backoff."""
from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from engine import quiz_factory as factory
from engine.gemini_batch import BatchError, BatchGemini
from tests.bank_fixtures import (
    FakeClient, add_card, batch_payload, make_db, sqlite_rows,
)


# --------------------------------------------------------------------------- mix
def test_rotation_is_exactly_the_requested_mix_and_has_no_adjacency():
    """The 20-slot plan reproduces 30/35/20/15 and never repeats a type."""
    counts = {name: factory.ROTATION.count(name) for name in factory.COGNITIVE_TYPES}
    assert counts == {"INTUITION": 6, "CALCULATION_STEP": 7,
                      "ADVERSARIAL_TRAP": 4, "SYSTEM_APPLICATION": 3}
    assert all(left != right for left, right in zip(factory.ROTATION, factory.ROTATION[1:]))


def test_plan_types_wraps_the_rotation_without_adjacent_duplicates():
    """Any five consecutive slots form a legal batch, including across the wrap."""
    planned = factory.plan_types(5, 18)
    assert planned == ["ADVERSARIAL_TRAP", "CALCULATION_STEP", "INTUITION",
                       "CALCULATION_STEP", "ADVERSARIAL_TRAP"]
    assert all(left != right for left, right in zip(planned, planned[1:]))


# ------------------------------------------------------------------ validation
def test_validate_questions_normalises_scratchpad_and_dedupe_key():
    """A valid batch is cleaned, and only the derivation type demands paper."""
    target = factory.Target("Algebra", "maths", "maths/algebra.pdf")
    expected = factory.plan_types(5, 0)
    cleaned = factory.validate_questions(batch_payload(5), target, expected)
    assert len(cleaned) == 5
    # Legal drift is preserved; the plan only shapes the prompt.
    assert [item["cognitive_type"] for item in cleaned] == \
        ["INTUITION", "CALCULATION_STEP", "ADVERSARIAL_TRAP", "SYSTEM_APPLICATION", "INTUITION"]
    assert cleaned[0]["requires_scratchpad"] == 0
    assert cleaned[1]["requires_scratchpad"] == 1
    assert json.loads(cleaned[0]["options_json"])[0] == "$x^{0}$"
    assert all(len(item["dedupe_key"]) == 64 for item in cleaned)


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.update(questions=p["questions"][:3]), "4-6 questions"),
    (lambda p: p["questions"][0].update(options=["a", "a", "b", "c"]), "duplicate options"),
    (lambda p: p["questions"][0].update(question_md="Solve \\(x^2\\)"), "$"),
    (lambda p: p["questions"][0].update(cognitive_type="VIBES"), "cognitive_type"),
    (lambda p: p["questions"][0].update(correct_index=7), "correct_index"),
    (lambda p: p["questions"][0].update(difficulty_tier=9), "difficulty_tier"),
    (lambda p: p["questions"][1].update(question_md=p["questions"][0]["question_md"]),
     "repeats"),
    (lambda p: p["questions"][0].update(options=["only", "three", "here"]), "four options"),
])
def test_validate_questions_rejects_broken_batches(mutate, message):
    """Every schema violation raises before anything reaches the bank."""
    payload = batch_payload(5)
    mutate(payload)
    target = factory.Target("Algebra", "maths", "maths/algebra.pdf")
    with pytest.raises(factory.QuizSchemaError) as error:
        factory.validate_questions(payload, target, factory.plan_types(5, 0))
    assert message.lower() in str(error.value).lower()


def test_validate_questions_rejects_a_batch_without_desk_free_questions():
    """A commute session must still be possible after every batch."""
    payload = batch_payload(4)
    payload["questions"] = [payload["questions"][1]]  # CALCULATION_STEP only, but count
    payload["questions"] = [dict(payload["questions"][0], cognitive_type="CALCULATION_STEP")
                            for _ in range(4)]
    for index, question in enumerate(payload["questions"]):
        question["question_md"] = f"Derive step {index}: $\\int x^{index} dx$"
    with pytest.raises(factory.QuizSchemaError) as error:
        factory.validate_questions(payload, factory.Target("Algebra"), ["CALCULATION_STEP"])
    assert "desk-free" in str(error.value)


def test_dedupe_key_ignores_case_and_punctuation_noise():
    """Re-runs cannot duplicate a question because the key is normalised."""
    assert factory.dedupe_key("Algebra", "What is  $x^2$  ?") == \
        factory.dedupe_key("Algebra", "  what is $x^2$?  ")
    assert factory.dedupe_key("Algebra", "What is $x^2$?") != \
        factory.dedupe_key("Geometry", "What is $x^2$?")


def test_build_prompt_states_the_plan_and_never_uses_latex_bracket_delimiters():
    """The prompt carries the concept, the exact plan, and the JSON contract."""
    types = factory.plan_types(4, 0)
    prompt = factory.build_prompt(factory.Target("Algebra", "maths", "maths/algebra.pdf"),
                                  [("maths/algebra.pdf", "Matrix text")], types)
    assert "Concept: Algebra" in prompt
    assert prompt.count("questions") >= 2
    for name in types:
        assert name in prompt
    assert "Note excerpts:" in prompt and "Matrix text" in prompt
    assert "\\[" not in prompt.split("Never use")[0]


# ------------------------------------------------------------------- execution
def test_factory_checkpoints_each_batch_and_stops_at_target(tmp_path):
    """A run commits the validated batch immediately and honours the target."""
    path = make_db(tmp_path)
    add_card(path)
    client = FakeClient(batch_payload(5))
    studied = factory.QuizFactory(
        path=str(path), client=client,
        inventory=[{"path": "x", "relative": "maths/algebra.pdf", "folder": "maths",
                    "title": "algebra", "bytes": 10}],
        chunk_provider=lambda target: [("maths/algebra.pdf", "Matrix rank")],
        pause=0, sleep=lambda seconds: None,
    )
    report = studied.run(5)
    assert report.inserted == 5 and report.batches == 1
    assert report.finished_bank == 5
    rows = sqlite_rows(
        path,
        "SELECT concept_id,cognitive_type,requires_scratchpad,status,batch_id FROM quiz_bank",
    )
    assert {row["cognitive_type"] for row in rows} == set(factory.COGNITIVE_TYPES)
    assert all(row["status"] == "STAGED" for row in rows)
    assert all(row["batch_id"] for row in rows)


def test_second_run_same_batch_is_counted_as_duplicate_not_stored_twice(tmp_path):
    """INSERT OR IGNORE plus the dedupe key make generation idempotent."""
    path = make_db(tmp_path)
    add_card(path)
    inventory = [{"path": "x", "relative": "maths/algebra.pdf", "folder": "maths",
                  "title": "algebra", "bytes": 10}]
    client = FakeClient(batch_payload(5))
    studied = factory.QuizFactory(path=str(path), client=client, inventory=inventory,
                                  chunk_provider=lambda target: [("a", "text")],
                                  pause=0, sleep=lambda seconds: None)
    studied.run(5)
    second = studied.run(10, max_batches=1)
    assert second.duplicates == 5 and second.inserted == 0
    assert len(sqlite_rows(path, "SELECT id FROM quiz_bank")) == 5


def test_a_schema_error_is_reported_and_nothing_is_committed(tmp_path):
    """A malformed model answer costs one batch, never a corrupted bank."""
    path = make_db(tmp_path)
    add_card(path)
    client = FakeClient({"questions": batch_payload(3)["questions"]})
    studied = factory.QuizFactory(
        path=str(path), client=client,
        inventory=[{"path": "x", "relative": "maths/algebra.pdf", "folder": "maths",
                    "title": "algebra", "bytes": 10}],
        chunk_provider=lambda target: [("a", "text")],
        pause=0, sleep=lambda seconds: None,
    )
    report = studied.run(5, max_batches=1)
    assert report.rejected == 1 and report.inserted == 0
    assert "4-6 questions" in report.errors[0]
    assert sqlite_rows(path, "SELECT id FROM quiz_bank") == []


def test_a_client_failure_backs_off_then_keeps_going(tmp_path):
    """A BatchError is recorded, the worker waits, and the next batch still runs."""
    path = make_db(tmp_path)
    add_card(path)
    client = FakeClient(BatchError("rate limited"), batch_payload(4))
    slept: list[float] = []
    studied = factory.QuizFactory(
        path=str(path), client=client,
        inventory=[{"path": "x", "relative": "maths/algebra.pdf", "folder": "maths",
                    "title": "algebra", "bytes": 10}],
        chunk_provider=lambda target: [("a", "text")],
        pause=0, sleep=slept.append,
    )
    report = studied.run(4, max_batches=2)
    assert report.rejected == 1 and report.inserted == 4
    assert slept and slept[0] > 0


def test_dry_run_builds_prompts_without_calling_the_model(tmp_path):
    """``--dry-run`` must be safe to run without an API key."""
    path = make_db(tmp_path)
    add_card(path)
    client = FakeClient(batch_payload(4))
    studied = factory.QuizFactory(
        path=str(path), client=client,
        inventory=[{"path": "x", "relative": "maths/algebra.pdf", "folder": "maths",
                    "title": "algebra", "bytes": 10}],
        chunk_provider=lambda target: [("a", "text")],
        pause=0, sleep=lambda seconds: None,
    )
    report = studied.run(5, max_batches=1, dry_run=True)
    assert client.calls == [] and report.inserted == 5
    assert sqlite_rows(path, "SELECT id FROM quiz_bank") == []


# --------------------------------------------------------------- batch client
def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.invalid", code, "boom", headers,
                                  io.BytesIO(b'{"error":{"message":"nope"}}'))


def _response(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_batch_client_retries_429_with_full_jitter_then_succeeds():
    """The delay is bounded by the exponential window and honours Retry-After."""
    answers = [_http_error(429, "3"), _http_error(429), _response('{"questions":[]}')]
    delays: list[float] = []

    def transport(model, body):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = BatchGemini(api_key="k", models=("primary",), sleep=delays.append,
                         transport=transport, max_attempts=3)
    assert client.json("prompt") == {"questions": []}
    assert len(delays) == 2
    assert 3.0 <= delays[0] <= 3.6            # Retry-After plus ≤0.5 s jitter
    assert 0.0 <= delays[1] <= min(45.0, 2.0 * 2)


def test_batch_client_falls_back_to_the_lite_model_on_400():
    """An unknown primary model switches to the fallback instead of retrying it."""
    seen: list[str] = []

    def transport(model, body):
        seen.append(model)
        if model == "primary":
            raise _http_error(400)
        return _response('{"questions":[{"a":1}]}')

    client = BatchGemini(api_key="k", models=("primary", "lite"), sleep=lambda s: None,
                         transport=transport, max_attempts=2)
    assert client.json("prompt") == {"questions": [{"a": 1}]}
    assert seen == ["primary", "lite"]


def test_batch_client_stops_on_bad_credentials_without_looping():
    """401/403 is a configuration error, not something a retry can fix."""
    calls: list[str] = []

    def transport(model, body):
        calls.append(model)
        raise _http_error(401)

    client = BatchGemini(api_key="k", models=("primary", "lite"), sleep=lambda s: None,
                         transport=transport, max_attempts=3)
    with pytest.raises(BatchError) as error:
        client.json("prompt")
    assert "credentials" in str(error.value)
    assert calls == ["primary"]


def test_batch_client_parses_fenced_json_and_rejects_garbage():
    """Models sometimes wrap JSON in prose; the extractor handles the common case."""
    client = BatchGemini(api_key="k", models=("primary",), sleep=lambda s: None,
                         transport=lambda model, body: _response(
                             'Here you go:\n```json\n{"questions": [1]}\n```'))
    assert client.json("prompt") == {"questions": [1]}
    broken = BatchGemini(api_key="k", models=("primary",), sleep=lambda s: None,
                         transport=lambda model, body: _response("not json at all"))
    with pytest.raises(BatchError):
        broken.json("prompt")


def test_batch_client_requires_an_api_key():
    """No key means a clear error before any network attempt."""
    client = BatchGemini(api_key="", transport=lambda model, body: pytest.fail("called"))
    with pytest.raises(BatchError) as error:
        client.json("prompt")
    assert "GEMINI_API_KEY" in str(error.value)
