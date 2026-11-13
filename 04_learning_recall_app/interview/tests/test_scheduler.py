from datetime import datetime, timezone

from interview_coach.flow_07_scheduler import SpacedRepetitionScheduler


def test_low_score_repeats_quickly() -> None:
    scheduler = SpacedRepetitionScheduler()
    now = datetime(2026, 4, 16, tzinfo=timezone.utc)

    plan = scheduler.next_review(score=2, previous_record=None, reference_time=now)

    assert plan.interval_days == 1
    assert plan.repetition_count == 0
    assert plan.due_at > now


def test_high_score_grows_interval() -> None:
    scheduler = SpacedRepetitionScheduler()
    now = datetime(2026, 4, 16, tzinfo=timezone.utc)

    first = scheduler.next_review(score=5, previous_record=None, reference_time=now)
    second = scheduler.next_review(
        score=5,
        previous_record=type(
            "PreviousRecord",
            (),
            {
                "easiness": first.easiness,
                "repetition_count": 2,
                "interval_days": 3,
            },
        )(),
        reference_time=now,
    )

    assert first.interval_days == 1
    assert second.interval_days >= 4
    assert second.repetition_count == 3
