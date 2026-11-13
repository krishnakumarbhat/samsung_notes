"""Interleaving, weakness reinforcement and domain coverage tests."""
from __future__ import annotations

import random

import pytest

from engine import db, interleaving
from engine import flow_scheduler as scheduler
from tests.bank_fixtures import add_question, make_db, sqlite_rows

DOMAINS = [
    "maths/03. Algebra",
    "maths/08. Probability  Statistics",
    "Mlaidlgenai_robo/DL",
    "Mlaidlgenai_robo/Robotics",
    "Mlaidlgenai_robo/Os",
    "Thoughts/Health wealth",
]


def seed_domains(path, per_domain: int = 4, *, start: int = 0) -> None:
    """Insert ``per_domain`` questions per domain, cycling cognitive types."""
    for domain_index, folder in enumerate(DOMAINS):
        for index in range(per_domain):
            add_question(
                path,
                concept=f"{folder} concept {index}",
                folder=folder,
                cognitive=interleaving.ROTATION[(domain_index + index) % 4],
                text=f"Question {start + domain_index * per_domain + index} in {folder}",
            )


def test_domain_of_splits_broad_roots_and_keeps_small_ones():
    """ML/AI/Robotics and maths use their subject folder as the domain."""
    assert interleaving.domain_of("maths/03. Algebra/03.2 Linear Algebra") == "maths/03. Algebra"
    assert interleaving.domain_of("Mlaidlgenai_robo/DL/Genai llm") == "Mlaidlgenai_robo/DL"
    assert interleaving.domain_of("Thoughts/Done") == "Thoughts"
    assert interleaving.domain_of("") == "Uncategorized"


def test_build_session_covers_five_domains_without_adjacent_repeats(tmp_path):
    """A 50-question session must interleave domains instead of blocking them."""
    path = make_db(tmp_path)
    seed_domains(path, per_domain=12)
    queue = scheduler.build_queue(limit=50, path=str(path))["questions"]
    assert len(queue) == 50
    domains = [interleaving.domain_of(item["topic_folder"]) for item in queue]
    assert len(set(domains)) >= 5
    assert all(left != right for left, right in zip(domains, domains[1:]))


def test_build_session_reports_domain_distribution(tmp_path):
    """The queue payload exposes the served domain mix for the UI."""
    path = make_db(tmp_path)
    seed_domains(path, per_domain=3)
    payload = scheduler.build_queue(limit=18, path=str(path))
    assert sum(payload["domains"].values()) == payload["count"]
    assert len(payload["domains"]) == len(DOMAINS)


def test_build_session_serves_a_single_domain_rather_than_starving(tmp_path):
    """One remaining domain still fills the requested queue."""
    path = make_db(tmp_path)
    for index in range(6):
        add_question(path, concept=f"only {index}", folder="maths",
                     cognitive=interleaving.ROTATION[index % 4],
                     text=f"Only domain question {index}")
    queue = scheduler.build_queue(limit=6, path=str(path))["questions"]
    assert len(queue) == 6


def test_weakness_variant_is_same_concept_and_lands_8_to_12_later(tmp_path):
    """A missed question schedules a different-angle variant in the same concept."""
    path = make_db(tmp_path)
    source = add_question(path, concept="Eigenvalues", folder="maths/03. Algebra",
                          cognitive="CALCULATION_STEP", text="Compute the eigenvalues.")
    add_question(path, concept="Eigenvalues", folder="maths/03. Algebra",
                 cognitive="INTUITION", text="What does an eigenvector mean geometrically?")
    result = interleaving.register_weakness(
        "session-a", source, path=str(path), rng=random.Random(4))
    assert result["question"] is not None
    assert result["question"]["id"] != source
    assert result["question"]["concept_id"] == "Eigenvalues"
    assert result["question"]["cognitive_type"] != "CALCULATION_STEP"
    assert 8 <= result["insert_after"] <= 12


def test_weakness_is_reused_not_duplicated_within_a_session(tmp_path):
    """Two misses on the same concept keep one reinforcement slot."""
    path = make_db(tmp_path)
    source = add_question(path, concept="Rank", folder="maths", cognitive="CALCULATION_STEP",
                          text="Find the rank.")
    add_question(path, concept="Rank", folder="maths", cognitive="INTUITION",
                 text="Why is rank a dimension?")
    first = interleaving.register_weakness("s", source, path=str(path))
    second = interleaving.register_weakness("s", source, path=str(path))
    assert second["reused"] is True
    assert first["weakness_id"] == second["weakness_id"]
    rows = sqlite_rows(path, "SELECT COUNT(*) AS n FROM session_weakness")
    assert rows[0]["n"] == 1


def test_correct_second_pass_resolves_and_failure_marks_priority(tmp_path):
    """The loop closes on success and stays high priority after a second miss."""
    path = make_db(tmp_path)
    source = add_question(path, concept="Chain rule", folder="maths",
                          cognitive="CALCULATION_STEP", text="Differentiate the chain.")
    variant = add_question(path, concept="Chain rule", folder="maths",
                           cognitive="INTUITION", text="Why multiply the derivatives?")
    interleaving.register_weakness("s", source, path=str(path))
    result = interleaving.resolve_weakness(variant, True, path=str(path))
    assert result["resolved"] is True and result["priority"] == 0

    interleaving.register_weakness("s2", source, path=str(path))
    failure = interleaving.resolve_weakness(variant, False, path=str(path))
    assert failure["priority"] == 1
    assert "Chain rule" in interleaving.priority_concepts(path=str(path))


def test_weakness_without_any_variant_is_a_no_op(tmp_path):
    """A concept with a single question cannot invent a variant."""
    path = make_db(tmp_path)
    source = add_question(path, concept="Singleton", folder="maths", text="Only question.")
    result = interleaving.register_weakness("s", source, path=str(path))
    assert result["question"] is None
    assert result["weakness_id"] is None
