from __future__ import annotations

from datetime import datetime, timedelta, timezone

from interview_coach.flow_01_models import InterviewRecord, ReviewPlan


class SpacedRepetitionScheduler:
    def next_review(
        self,
        score: int,
        previous_record: InterviewRecord | None = None,
        reference_time: datetime | None = None,
    ) -> ReviewPlan:
        now = reference_time or datetime.now(timezone.utc)
        easiness = previous_record.easiness if previous_record else 2.5
        repetition_count = previous_record.repetition_count if previous_record else 0
        previous_interval = previous_record.interval_days if previous_record else 0

        if score <= 2:
            repetition_count = 0
            interval_days = 1
        elif score == 3:
            repetition_count = max(1, repetition_count)
            interval_days = 2
        elif repetition_count == 0:
            repetition_count = 1
            interval_days = 1
        elif repetition_count == 1:
            repetition_count = 2
            interval_days = 3
        else:
            repetition_count += 1
            multiplier = max(1.35, easiness - 0.1)
            interval_days = max(4, round(max(previous_interval, 3) * multiplier))

        easiness_delta = 0.1 - (5 - score) * (0.08 + (5 - score) * 0.02)
        easiness = max(1.3, round(easiness + easiness_delta, 2))
        due_at = now + timedelta(days=interval_days)
        return ReviewPlan(
            repetition_count=repetition_count,
            interval_days=interval_days,
            easiness=easiness,
            due_at=due_at,
        )
