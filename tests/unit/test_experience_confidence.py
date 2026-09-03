from __future__ import annotations

from datetime import datetime, timezone

from trajweave.experience.confidence import compute_confidence

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def test_reference_example_from_brief():
    # S=5, C=1, N=6, P=2, recent -> ~0.84 (brief's 0.81 is illustrative)
    c = compute_confidence(
        support=5, contradiction=1, ambiguous=0, occurrences=6, projects=2,
        last_seen_at="2026-08-30T00:00:00+00:00", now=NOW,
    )
    assert c.support_ratio == 5 / 6
    assert c.recurrence == 1.0
    assert c.cross_project == 0.5
    assert c.recency == 1.0
    assert c.score == 0.84


def test_contradictions_lower_the_score():
    kw = dict(ambiguous=0, occurrences=4, projects=1,
              last_seen_at="2026-08-20T00:00:00+00:00", now=NOW)
    clean = compute_confidence(support=4, contradiction=0, **kw)
    mixed = compute_confidence(support=3, contradiction=1, **kw)
    assert mixed.score < clean.score


def test_recency_buckets():
    base = dict(support=3, contradiction=0, ambiguous=0, occurrences=3, projects=1)
    fresh = compute_confidence(last_seen_at="2026-08-15T00:00:00+00:00", now=NOW, **base)
    stale = compute_confidence(last_seen_at="2025-01-01T00:00:00+00:00", now=NOW, **base)
    assert fresh.recency == 1.0
    assert stale.recency == 0.1
    assert fresh.score > stale.score


def test_no_evidence_is_zero_ratio_not_crash():
    c = compute_confidence(
        support=0, contradiction=0, ambiguous=2, occurrences=2, projects=1,
        last_seen_at=None, now=NOW,
    )
    assert c.support_ratio == 0.0
    assert 0.0 <= c.score <= 1.0


def test_deterministic():
    args = dict(support=4, contradiction=1, ambiguous=1, occurrences=6, projects=3,
                last_seen_at="2026-08-01T00:00:00+00:00", now=NOW)
    assert compute_confidence(**args).score == compute_confidence(**args).score
