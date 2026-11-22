"""Student application service. All state changes use the simulation store's transaction."""

import base64
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, digest
from secrets import token_urlsafe
from uuid import uuid4

from .decision_engine import Evaluation, evaluate, recheck_selection, replan, restore_evaluation
from .decision_models import DecisionInput, DecisionRequest, Selection
from .errors import Fault
from .feedback_ledger import effective_demo_declarations
from .fixture import Fixture
from .models import Catalog, SeedState, SnapshotRef, SupplySnapshot
from .store import OpsActor, OpsAuthority, SimulationStore, WriteApproval, resolve_operator
from .student_dining import decode_cursor, encode_cursor, project_dining
from .student_models import (
    FEEDBACK_CHANNELS, ConsentRequest, DemoEnrollmentRequest, DiningQuery, FeedbackRequest, OpenSession, RecommendRequest,
    RecommendationRecord, ReplanRequest, RunRequest, SessionInfo, StudentReply,
)


def _encoded(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _secret(db: sqlite3.Connection) -> bytes:
    return db.execute("SELECT secret FROM student_secret WHERE singleton=1").fetchone()[0]


def _hash(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


def _material(parts) -> bytes:
    return json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()


def _canonical(request: RunRequest) -> str:
    return sha256(_material(request.model_dump(mode="json", exclude={"idempotency_key"}))).hexdigest()


def _session_token(db, run_id: str) -> str:
    payload = f"{run_id}.{token_urlsafe(32)}"
    signature = _encoded(digest(_secret(db), b"session:" + payload.encode("ascii"), "sha256"))
    return f"{payload}.{signature}"


def _signed_run(db, token: str | None) -> str | None:
    if not token or len(token) > 256 or not token.isascii() or token.count(".") != 2:
        return None
    payload, signature = token.rsplit(".", 1)
    expected = _encoded(digest(_secret(db), b"session:" + payload.encode("ascii"), "sha256"))
    return payload.split(".", 1)[0] if compare_digest(signature, expected) else None


def _reply(ref: SnapshotRef, **fields) -> StudentReply:
    return StudentReply(snapshot=ref, data=fields)


StudentAuthority = str | None | Callable[[sqlite3.Connection, str], str]


class StudentService:
    def __init__(self, store: SimulationStore, *, now=lambda: datetime.now(UTC)):
        self.store = store
        self.now = now

    def _run(self, db, run_id: str, signed_run: str | None = None):
        current = self.store._run(db)
        if current["run_id"] == run_id:
            return current
        if signed_run == run_id and db.execute("SELECT 1 FROM retired_runs WHERE run_id=?", (run_id,)).fetchone():
            ref = self.store._frame(db, current["run_id"]).supply.ref
            raise Fault("RUN_REPLACED", "This simulation run has been replaced.", ref)
        raise Fault("NOT_FOUND", "Run not found.")

    def _identity(self, db, run_id: str, token: StudentAuthority):
        if callable(token):
            principal = token(db, run_id)
            run = self._run(db, run_id)
            session = db.execute(
                "SELECT * FROM student_sessions WHERE run_id=? AND principal_id=?", (run_id, principal),
            ).fetchone()
            if session is None:
                raise Fault("UNAUTHENTICATED", "The bound student session is no longer active.")
            return run, session
        signed_run = _signed_run(db, token)
        if signed_run is None:
            raise Fault("UNAUTHENTICATED", "A student session is required.")
        run = self._run(db, run_id, signed_run)
        if signed_run != run_id:
            raise Fault("UNAUTHENTICATED", "The session is not active in this run.")
        session = db.execute(
            "SELECT * FROM student_sessions WHERE run_id=? AND token_digest=?", (run_id, _hash(token)),
        ).fetchone()
        if session is None:
            raise Fault("UNAUTHENTICATED", "The student session is no longer active.")
        return run, session

    def open_session(self, request: OpenSession, token: str | None) -> tuple[StudentReply, str | None]:
        with self.store.transaction(write=True) as db:
            run = self._run(db, request.run_id, _signed_run(db, token))
            ref = self.store._frame(db, run["run_id"]).supply.ref
            demo_token = request.demo_enrollment_token
            if demo_token is None and _signed_run(db, token) == request.run_id:
                existing = db.execute(
                    "SELECT * FROM student_sessions WHERE run_id=? AND token_digest=?", (request.run_id, _hash(token)),
                ).fetchone()
                if existing is not None:
                    info = SessionInfo(session_kind=existing["session_kind"], share_for_ops=bool(existing["share_for_ops"]),
                                       consent_revision=existing["consent_revision"])
                    return _reply(ref, **info.model_dump()), None
            now = self.now()
            if demo_token is not None:
                ticket = db.execute(
                    "SELECT * FROM demo_enrollments WHERE run_id=? AND token_digest=?",
                    (request.run_id, _hash(demo_token)),
                ).fetchone()
                if (ticket is None or ticket["consumed_at"] is not None
                        or datetime.fromisoformat(ticket["expires_at"]) <= now):
                    raise Fault("FORBIDDEN", "Invalid, expired, or used demonstration enrollment.", ref)
                db.execute("UPDATE demo_enrollments SET consumed_at=? WHERE token_digest=?",
                           (now.isoformat(), ticket["token_digest"]))
            kind = "DEMO" if demo_token is not None else "USABILITY_TEST"
            new_token = _session_token(db, request.run_id)
            db.execute(
                """INSERT INTO student_sessions
                (run_id,principal_id,token_digest,session_kind,share_for_ops,consent_revision,updated_at)
                VALUES (?,?,?,?,0,0,?)""",
                (request.run_id, f"u{uuid4().hex}", _hash(new_token), kind, now.isoformat()),
            )
            return _reply(ref, session_kind=kind, share_for_ops=False, consent_revision=0), new_token

    def issue_enrollment(self, request: DemoEnrollmentRequest, actor: OpsActor) -> StudentReply:
        with self.store.transaction(write=True) as db:
            run = self._run(db, request.run_id)
            fixture = Fixture.model_validate_json(run["fixture_json"])
            self.store._authorize(actor, fixture, None)
            scope = actor.principal_id, request.run_id, "demo-enrollment", request.idempotency_key
            canonical = _canonical(request)
            # The receipt contains expiration only. Replay derives the original credential without storing it.
            token = _encoded(digest(_secret(db), b"enrollment:" + _material(scope), "sha256"))
            receipt = self.store._saved(db, scope, canonical, StudentReply)
            if receipt is None:
                ref = self.store._frame(db, request.run_id).supply.ref
                expiry = (self.now() + timedelta(seconds=120)).isoformat()
                db.execute("INSERT INTO demo_enrollments VALUES (?,?,?,NULL)", (_hash(token), request.run_id, expiry))
                receipt = _reply(ref, expires_at=expiry)
                self.store._save(db, scope, canonical, receipt)
            return _reply(receipt.snapshot, **receipt.data, demo_enrollment_token=token)

    def snapshot(self, request: RunRequest, token: str | None) -> StudentReply:
        with self.store.transaction() as db:
            self._identity(db, request.run_id, token)
            ref = self.store._frame(db, request.run_id).supply.ref
            return _reply(ref, **ref.model_dump(mode="json"))

    def dining(self, request: DiningQuery, token: StudentAuthority, actor: OpsAuthority | None = None) -> StudentReply:
        with self.store.transaction() as db:
            if actor is not None:
                actor = resolve_operator(actor, db, request.run_id)
            run = self._identity(db, request.run_id, token)[0] if actor is None else self._run(db, request.run_id)
            snapshot_id, venue_id, limit, after = request.snapshot_id, request.venue_id, request.limit, None
            if request.cursor is not None:
                bound_run, bound_snapshot, bound_venue, bound_limit, after = decode_cursor(_secret(db), request.cursor)
                if bound_run != request.run_id or any(
                    given != bound for field, given, bound in (
                        ("snapshot_id", snapshot_id, bound_snapshot), ("venue_id", venue_id, bound_venue),
                        ("limit", limit, bound_limit),
                    ) if field in request.model_fields_set
                ):
                    raise Fault("INVALID_INPUT", "Dining options cursor does not match the requested page.")
                snapshot_id, venue_id, limit = bound_snapshot, bound_venue, bound_limit
            snapshot, catalog = self._supplies(db, run, snapshot_id)
            allowed = {row.venue_id for row in catalog.venues}
            if actor is not None:
                allowed &= set(actor.allowed_venue_ids)
                if venue_id is not None and venue_id not in allowed:
                    raise Fault("FORBIDDEN", "Operator has no access to this venue.", snapshot.ref)
                catalog = catalog.model_copy(update={
                    "venues": tuple(row for row in catalog.venues if row.venue_id in allowed),
                    "bundles": tuple(row for row in catalog.bundles if row.venue_id in allowed),
                })
            elif venue_id is not None and venue_id not in allowed:
                raise Fault("INVALID_INPUT", "Unknown venue.", snapshot.ref)
            limit = 20 if limit is None else limit
            venues, options, last = project_dining(snapshot, catalog, venue_id=venue_id, after=after, limit=limit)
            cursor = encode_cursor(_secret(db), (request.run_id, snapshot.ref.snapshot_id, venue_id, limit, last)) if last else None
            return _reply(snapshot.ref, venues=venues, options=options, next_cursor=cursor)

    def _supplies(self, db, run, snapshot_id: str | None = None):
        snapshot = self.store._frame(db, run["run_id"], snapshot_id).supply
        catalog = Catalog.model_validate_json(run["catalog_json"])
        return snapshot, catalog

    @staticmethod
    def _window(run, snapshot: SupplySnapshot):
        if snapshot.active_window_id is None:
            return None
        fixture = Fixture.model_validate_json(run["fixture_json"])
        return next(window for window in fixture.scoped_windows(run["run_id"]) if window.window_id == snapshot.active_window_id)

    @staticmethod
    def _record(db, run_id: str, principal: str, recommendation_id: str) -> RecommendationRecord:
        row = db.execute(
            """SELECT record_json FROM recommendation_records
               WHERE run_id=? AND principal_id=? AND recommendation_id=?""",
            (run_id, principal, recommendation_id),
        ).fetchone()
        if row is None:
            raise Fault("NOT_FOUND", "Recommendation not found.")
        return RecommendationRecord.model_validate_json(row["record_json"])

    @staticmethod
    def _persist(db, evaluation: Evaluation, principal: str, effective_max: int, previous: str | None = None):
        body = evaluation.body
        result = body.bind(f"rec{uuid4().hex}")
        snapshot = evaluation.inputs.snapshot
        record = RecommendationRecord(
            recommendation_id=result.recommendation_id, run_id=snapshot.ref.run_id, principal_id=principal,
            snapshot=snapshot.ref, origin_window_id=snapshot.active_window_id,
            normalized_request=evaluation.inputs.request, explicit_max_options=evaluation.explicit_max_options is not None,
            effective_max_options=effective_max, deadline_at=evaluation.deadline_at,
            selectable_bundle_ids=result.selectable_bundle_ids, rules_version=result.rules_version,
            config_version=result.config_version, result=result, previous_recommendation_id=previous,
        )
        db.execute("INSERT INTO recommendation_records VALUES (?,?,?,?)",
                   (record.recommendation_id, record.run_id, principal, record.model_dump_json()))
        return result

    def _restore(self, db, run, record: RecommendationRecord, catalog: Catalog) -> Evaluation:
        snapshot = self.store._frame(db, run["run_id"], record.snapshot.snapshot_id).supply
        inputs = DecisionInput(request=record.normalized_request, snapshot=snapshot,
                               catalog=catalog, window=self._window(run, snapshot))
        return restore_evaluation(
            inputs, record.result, deadline_at=record.deadline_at,
            explicit_max_options=record.effective_max_options if record.explicit_max_options else None,
            effective_max_options=record.effective_max_options,
        )

    def recommend(self, request: RecommendRequest, token: StudentAuthority) -> StudentReply:
        with self.store.transaction(write=True) as db:
            run, student = self._identity(db, request.run_id, token)
            snapshot, catalog = self._supplies(db, run, request.snapshot_id)
            try:
                evaluation = evaluate(DecisionInput(request=request.request, catalog=catalog, snapshot=snapshot,
                                                    window=self._window(run, snapshot)))
            except Fault as error:
                raise Fault(error.code, str(error), snapshot.ref) from error
            result = self._persist(db, evaluation, student["principal_id"], request.request.max_options)
            return _reply(snapshot.ref, **result.model_dump(mode="json"))

    @staticmethod
    def _selection(db, run_id: str, principal: str, window: str | None) -> Selection | None:
        row = db.execute(
            """SELECT event_type, bundle_id, window_id FROM feedback_events
               WHERE run_id=? AND principal_id=? AND channel='INTENT'
               ORDER BY (window_id IS ?) DESC, commit_seq DESC LIMIT 1""",
            (run_id, principal, window),
        ).fetchone()
        if row is None or row["event_type"] == "INTENT_CLEARED":
            return None
        return Selection(bundle_id=row["bundle_id"], window_id=row["window_id"])

    def replan(self, request: ReplanRequest, token: StudentAuthority) -> StudentReply:
        with self.store.transaction(write=True) as db:
            run, student = self._identity(db, request.run_id, token)
            principal = student["principal_id"]
            record = self._record(db, request.run_id, principal, request.previous_recommendation_id)
            snapshot, catalog = self._supplies(db, run)
            prior = self._restore(db, run, record, catalog)
            selected = self._selection(db, request.run_id, principal, snapshot.active_window_id)
            switched = replan(prior, snapshot, self._window(run, snapshot), selected=selected,
                              max_options=request.max_options, catalog=catalog)
            saved = self._persist(db, switched.evaluation, principal, switched.evaluation.explicit_max_options or 4,
                                  previous=record.recommendation_id)
            result = switched.bind(record.recommendation_id, saved.recommendation_id)
            return _reply(snapshot.ref, **result.model_dump(mode="json"))

    def feedback_scope(self, db, request: FeedbackRequest, principal: str, snapshot: SupplySnapshot, catalog: Catalog):
        if request.recommendation_id is not None:
            record = self._record(db, request.run_id, principal, request.recommendation_id)
            if request.bundle_id is not None and request.bundle_id not in record.selectable_bundle_ids:
                raise Fault("INVALID_INPUT", "This meal was not offered by the recommendation.", snapshot.ref)
            return record
        if not any(dish.dish_id == request.dish_id for dish in catalog.dishes):
            raise Fault("INVALID_INPUT", "Unknown dish.", snapshot.ref)
        return None

    def feedback(
        self, request: FeedbackRequest, token: StudentAuthority, *, approval: WriteApproval | None = None,
    ) -> StudentReply:
        with self.store.transaction(write=True) as db:
            run, student = self._identity(db, request.run_id, token)
            snapshot, catalog = self._supplies(db, run)
            principal = student["principal_id"]
            record = self.feedback_scope(db, request, principal, snapshot, catalog)

            scope = principal, request.run_id, "student-feedback", request.idempotency_key
            canonical = _canonical(request)
            replay = self.store._saved(db, scope, canonical, StudentReply)
            if replay is not None:
                return replay
            if approval is not None:
                approval(db, principal, request)
            kind = request.event_type
            if kind == "INTENT_SELECTED":
                evaluation = self._restore(db, run, record, catalog)
                recheck_selection(evaluation, request.bundle_id, snapshot, self._window(run, snapshot), catalog=catalog)
            window = record.origin_window_id if record is not None else snapshot.active_window_id
            if window is None:
                raise Fault("INVALID_INPUT", "This action needs a meal window.", snapshot.ref)

            channel = FEEDBACK_CHANNELS.get(kind)
            demo = student["session_kind"] == "DEMO"
            share_applied = demo and bool(student["share_for_ops"]) and channel is not None
            seed_size = SeedState.model_validate_json(run["seed_json"]).signals_revision
            seq = db.execute(
                "SELECT COALESCE(MAX(commit_seq), ?) + 1 FROM feedback_events WHERE run_id=?",
                (seed_size, request.run_id),
            ).fetchone()[0]
            event_id = f"fb{uuid4().hex}"
            db.execute(
                """INSERT INTO feedback_events
                (commit_seq,event_id,run_id,principal_id,window_id,occurred_at,stream,event_type,channel,
                 recommendation_id,bundle_id,dish_id,consent_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (seq, event_id, request.run_id, principal, window, snapshot.ref.clock_at.isoformat(),
                 "SIMULATED" if demo else "USABILITY_TEST", kind, channel,
                 request.recommendation_id, request.bundle_id, request.dish_id, student["consent_revision"]),
            )
            signals = run["signals_revision"]
            if share_applied:
                signals += 1
                db.execute("UPDATE runs SET signals_revision=? WHERE run_id=?", (signals, request.run_id))
            result = _reply(snapshot.ref, event_id=event_id, event_type=kind, window_id=window,
                            share_applied=share_applied, signals_revision=signals)
            self.store._save(db, scope, canonical, result)
            return result

    def consent(self, request: ConsentRequest, token: str | None) -> StudentReply:
        with self.store.transaction(write=True) as db:
            run, student = self._identity(db, request.run_id, token)
            ref = self.store._frame(db, request.run_id).supply.ref
            principal = student["principal_id"]
            scope = principal, request.run_id, "student-consent", request.idempotency_key
            canonical = _canonical(request)
            replay = self.store._saved(db, scope, canonical, StudentReply)
            if replay is not None:
                return replay
            revision, signals = student["consent_revision"], run["signals_revision"]
            if revision != request.expected_consent_revision:
                raise Fault("SNAPSHOT_CONFLICT", "The consent setting has changed.", ref)
            if bool(student["share_for_ops"]) != request.share_for_ops:
                revision += 1
                db.execute(
                    "UPDATE student_sessions SET share_for_ops=?,consent_revision=?,updated_at=? "
                    "WHERE run_id=? AND principal_id=?",
                    (request.share_for_ops, revision, self.now().isoformat(), request.run_id, principal),
                )
                if student["session_kind"] == "DEMO" and not request.share_for_ops:
                    signals += 1
                    db.execute("UPDATE runs SET signals_revision=? WHERE run_id=?", (signals, request.run_id))
            result = _reply(ref, share_for_ops=request.share_for_ops, consent_revision=revision, signals_revision=signals)
            self.store._save(db, scope, canonical, result)
            return result

    def effective_demo_signals(self, run_id: str) -> tuple[int, tuple[dict, ...]]:
        """Private read of the current ledger, including fixture declarations."""
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            clock = self.store._frame(db, run_id).supply.ref.clock_at
            return run["signals_revision"], effective_demo_declarations(db, run, clock)
