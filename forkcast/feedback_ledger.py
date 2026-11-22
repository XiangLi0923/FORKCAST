"""Private view of the feedback ledger shared by student and OPS services."""

from datetime import datetime
from itertools import chain
from sqlite3 import Connection, Row

from .models import SeedState
from .student_models import FEEDBACK_CHANNELS


def effective_demo_declarations(
    db: Connection, run: Row, clock_at: datetime, window_id: str | None = None,
) -> tuple[dict, ...]:
    """Read current, authorized declarations using the caller's transaction."""
    run_id = run["run_id"]
    seed = SeedState.model_validate_json(run["seed_json"])
    seed_subjects = {(subject.run_id, subject.principal_id, subject.consent_revision) for subject in seed.sessions}
    seeded = (
        event.model_dump(mode="json") for event in seed.feedback
        if event.run_id == run_id and (window_id is None or event.window_id == window_id)
        and (event.run_id, event.principal_id, event.consent_revision) in seed_subjects
        and event.occurred_at <= clock_at
    )
    committed = db.execute(
        """SELECT f.event_id,f.principal_id,f.run_id,f.window_id,f.occurred_at,f.commit_seq,f.stream,f.event_type,
                  f.consent_revision,f.recommendation_id,f.bundle_id,f.dish_id
           FROM feedback_events f JOIN student_sessions s
             ON s.run_id=f.run_id AND s.principal_id=f.principal_id
           WHERE f.run_id=? AND (? IS NULL OR f.window_id=?)
             AND f.stream='SIMULATED' AND s.session_kind='DEMO'
             AND s.share_for_ops=1 AND f.consent_revision=s.consent_revision
           AND f.channel IS NOT NULL""",
        (run_id, window_id, window_id),
    )
    reached = (dict(event) for event in committed if datetime.fromisoformat(event["occurred_at"]) <= clock_at)
    latest = {}
    for event in chain(seeded, reached):
        key = event["principal_id"], event["window_id"], FEEDBACK_CHANNELS[event["event_type"]]
        previous = latest.get(key)
        if previous is None or event["commit_seq"] > previous["commit_seq"]:
            latest[key] = event
    return tuple(
        event for event in sorted(latest.values(), key=lambda row: row["commit_seq"])
        if event["event_type"] in ("PREFERENCE", "INTENT_SELECTED", "DINED")
    )
