"""SQLite transaction authority for runs, snapshots, seed state, and simulation writes."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes
from typing import Protocol
from uuid import uuid4

from .errors import Fault
from .fixture import Fixture, load_fixture
from .models import (
    ID, Key, PAYLOAD_FOR, AdvanceClock, AuditRecord, Catalog, EventPayload, Mode,
    Model, PublicEventType, ScenarioID, SeedState, SimulationEvent, SnapshotFrame, SnapshotRef,
    SupplySnapshot, event_target,
)
from .mcp_schema import MCP_SCHEMA
from .ops_events import record_window_applications
from .ops_schema import OPS_SCHEMA
from .simulator import Simulator
from .student_schema import STUDENT_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    run_id TEXT NOT NULL UNIQUE,
    scenario_id TEXT NOT NULL,
    data_mode TEXT NOT NULL CHECK (data_mode = 'SIMULATION'),
    fixture_version TEXT NOT NULL,
    fixture_json TEXT NOT NULL,
    catalog_json TEXT NOT NULL,
    seed_json TEXT NOT NULL,
    signals_revision INTEGER NOT NULL CHECK (signals_revision >= 0)
);
CREATE TABLE IF NOT EXISTS retired_runs (run_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    snapshot_id TEXT NOT NULL UNIQUE,
    frame_json TEXT NOT NULL,
    PRIMARY KEY (run_id, revision)
);
CREATE TRIGGER IF NOT EXISTS snapshots_append_only
BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT, 'snapshot rows are immutable'); END;
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    event_json TEXT NOT NULL,
    applied_revision INTEGER,
    applied_index INTEGER,
    removed_portions INTEGER CHECK (removed_portions >= 0),
    PRIMARY KEY (run_id, event_id),
    UNIQUE (run_id, applied_revision, applied_index),
    FOREIGN KEY (run_id, applied_revision) REFERENCES snapshots(run_id, revision),
    CHECK ((applied_revision IS NULL) = (applied_index IS NULL))
);
CREATE TABLE IF NOT EXISTS audit (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    audit_id TEXT NOT NULL,
    audit_json TEXT NOT NULL,
    PRIMARY KEY (run_id, audit_id)
);
CREATE TABLE IF NOT EXISTS receipts (
    actor_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    canonical_request TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (actor_id, scope, operation, key)
);
"""


class OpsActor(Model):
    principal_id: ID
    allowed_venue_ids: tuple[ID, ...]
    can_control_run: bool


OpsAuthority = OpsActor | Callable[[sqlite3.Connection, str], OpsActor]
WriteApproval = Callable[[sqlite3.Connection, str, Model], None]


def resolve_operator(actor: OpsAuthority, db: sqlite3.Connection, run_id: str) -> OpsActor:
    return actor(db, run_id) if callable(actor) else actor


class ResetRequest(Model):
    scenario_id: ScenarioID
    idempotency_key: Key


class ControlRequest(Model):
    run_id: ID
    expected_snapshot_id: ID
    event_type: PublicEventType
    payload: EventPayload
    idempotency_key: Key

    def model_post_init(self, _context):
        if type(self.payload) is not PAYLOAD_FOR[self.event_type]:
            raise ValueError("payload does not match the public event type")


class RunResult(Model):
    scenario_id: ScenarioID
    snapshot: SnapshotRef


@dataclass(frozen=True)
class LoggedEvent:
    event: SimulationEvent
    applied_revision: int
    removed_portions: int | None


class DataProvider(Protocol):
    def read(self, run_id: str, snapshot_id: str | None = None) -> tuple[SupplySnapshot, Catalog]: ...


class MockProvider:
    def __init__(self, store: "SimulationStore"):
        self.store = store

    def read(self, run_id: str, snapshot_id: str | None = None) -> tuple[SupplySnapshot, Catalog]:
        with self.store.transaction() as db:
            run = self.store._run(db, run_id)
            frame = self.store._frame(db, run_id, snapshot_id)
            return frame.supply, Catalog.model_validate_json(run["catalog_json"])


class SimulationStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def require_simulation(mode: Mode):
        if mode != "SIMULATION":
            raise Fault("MODE_DISABLED", "LIVE data mode is disabled for this release.")

    def bootstrap(self, mode: Mode) -> RunResult:
        self.require_simulation(mode)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.executescript(SCHEMA + STUDENT_SCHEMA + OPS_SCHEMA + MCP_SCHEMA)
        with self.transaction(write=True) as db:
            db.execute("INSERT OR IGNORE INTO student_secret VALUES (1, ?)", (token_bytes(32),))
            run = db.execute("SELECT * FROM runs WHERE singleton = 1").fetchone()
            if run is None:
                return self._new_run(db, "CLOSED_LOOP", load_fixture())
            self.require_simulation(run["data_mode"])
            return RunResult(scenario_id=run["scenario_id"], snapshot=self._frame(db, run["run_id"]).supply.ref)

    def _run(self, db: sqlite3.Connection, run_id: str | None = None) -> sqlite3.Row:
        row = db.execute("SELECT * FROM runs WHERE singleton = 1").fetchone()
        if row is None or (run_id is not None and run_id != row["run_id"]):
            raise Fault("NOT_FOUND", "Run not found.")
        self.require_simulation(row["data_mode"])
        return row

    @staticmethod
    def _frame(db: sqlite3.Connection, run_id: str, snapshot_id: str | None = None) -> SnapshotFrame:
        if snapshot_id is None:
            row = db.execute(
                "SELECT frame_json FROM snapshots WHERE run_id = ? ORDER BY revision DESC LIMIT 1", (run_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT frame_json FROM snapshots WHERE run_id = ? AND snapshot_id = ?", (run_id, snapshot_id),
            ).fetchone()
        if row is None:
            raise Fault("NOT_FOUND", "Snapshot not found.")
        return SnapshotFrame.model_validate_json(row["frame_json"])

    def current(self) -> RunResult:
        with self.transaction() as db:
            run = self._run(db)
            frame = self._frame(db, run["run_id"])
            return RunResult(scenario_id=run["scenario_id"], snapshot=frame.supply.ref)

    def seed_state(self, run_id: str) -> tuple[int, SeedState]:
        """Read immutable fixture records inside the supplying run's transaction."""
        with self.transaction() as db:
            run = self._run(db, run_id)
            return run["signals_revision"], SeedState.model_validate_json(run["seed_json"])

    def event_log(self, run_id: str) -> tuple[LoggedEvent, ...]:
        with self.transaction() as db:
            self._run(db, run_id)
            rows = db.execute(
                """SELECT event_json, applied_revision, removed_portions FROM events
                WHERE run_id = ? AND applied_revision IS NOT NULL ORDER BY applied_revision, applied_index""",
                (run_id,),
            )
            return tuple(LoggedEvent(
                SimulationEvent.model_validate_json(row["event_json"]), row["applied_revision"], row["removed_portions"],
            ) for row in rows)

    def _new_run(self, db: sqlite3.Connection, scenario_id: ScenarioID, fixture: Fixture) -> RunResult:
        run_id = f"r{uuid4().hex}"
        scenario = fixture.scenario(scenario_id)
        sim = Simulator(fixture, scenario, run_id)
        frame = sim.initial_frame()
        ref = frame.supply.ref
        seed = fixture.seed_state(ref, sim.windows[0], datetime.now(UTC))
        db.execute(
            """INSERT INTO runs (singleton, run_id, scenario_id, data_mode, fixture_version,
            fixture_json, catalog_json, seed_json, signals_revision) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, scenario_id, ref.data_mode, fixture.fixture_version, fixture.model_dump_json(),
             fixture.catalog(scenario).model_dump_json(), seed.model_dump_json(), seed.signals_revision),
        )
        self._insert_frame(db, frame)
        for event in fixture.boundaries(run_id, sim.windows[-1]):
            self._insert_event(db, event)
        return RunResult(scenario_id=scenario_id, snapshot=ref)

    @staticmethod
    def _insert_frame(db: sqlite3.Connection, frame: SnapshotFrame):
        ref = frame.supply.ref
        db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?)",
                   (ref.run_id, ref.revision, ref.snapshot_id, frame.model_dump_json()))

    @staticmethod
    def _insert_event(db: sqlite3.Connection, event: SimulationEvent):
        db.execute("INSERT INTO events (run_id, event_id, sequence, event_json) VALUES (?, ?, ?, ?)",
                   (event.run_id, event.event_id, event.sequence, event.model_dump_json()))

    @staticmethod
    def _saved(db: sqlite3.Connection, scope: tuple[str, str, str, str], digest: str,
               model: type[Model] = RunResult):
        receipt = db.execute(
            "SELECT * FROM receipts WHERE actor_id = ? AND scope = ? AND operation = ? AND key = ?", scope,
        ).fetchone()
        if receipt is None:
            return None
        if digest != receipt["canonical_request"]:
            raise Fault("IDEMPOTENCY_CONFLICT", "Idempotency key already used for another request.")
        return model.model_validate_json(receipt["result_json"])

    @staticmethod
    def _save(db: sqlite3.Connection, scope: tuple[str, str, str, str], digest: str, result: Model):
        db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?)", (*scope, digest, result.model_dump_json()))

    @staticmethod
    def _authorize(actor: OpsActor, fixture: Fixture, venue_id: str | None):
        allowed = set(actor.allowed_venue_ids)
        if venue_id is None:
            if actor.can_control_run and {row.venue_id for row in fixture.venues}.issubset(allowed):
                return
        elif venue_id in allowed:
            return
        raise Fault("FORBIDDEN", "Operator lacks permission for this simulation action.")

    def reset(self, request: ResetRequest, actor: OpsActor) -> RunResult:
        scope = (actor.principal_id, "", "reset", request.idempotency_key)
        digest = request.scenario_id
        with self.transaction(write=True) as db:
            current = self._run(db)
            fixture = load_fixture()
            self._authorize(actor, Fixture.model_validate_json(current["fixture_json"]), None)
            self._authorize(actor, fixture, None)
            replay = self._saved(db, scope, digest)
            if replay is not None:
                return replay
            previous = current["run_id"]
            db.execute("INSERT INTO retired_runs VALUES (?)", (previous,))
            db.execute("DELETE FROM receipts WHERE scope = ?", (previous,))
            db.execute("DELETE FROM runs WHERE run_id = ?", (previous,))
            result = self._new_run(db, request.scenario_id, fixture)
            self._save(db, scope, digest, result)
            return result

    def control(self, request: ControlRequest, actor: OpsActor) -> RunResult:
        scope = (actor.principal_id, request.run_id, "event", request.idempotency_key)
        with self.transaction(write=True) as db:
            current = self._run(db)
            fixture = Fixture.model_validate_json(current["fixture_json"])
            if request.run_id != current["run_id"]:
                if actor.can_control_run and {row.venue_id for row in fixture.venues}.issubset(actor.allowed_venue_ids):
                    if db.execute("SELECT 1 FROM retired_runs WHERE run_id = ?", (request.run_id,)).fetchone():
                        raise Fault("RUN_REPLACED", "This simulation run has been replaced.")
                raise Fault("NOT_FOUND", "Run not found.")

            sim = Simulator(fixture, fixture.scenario(current["scenario_id"]), request.run_id)
            frame = self._frame(db, request.run_id)
            ref = frame.supply.ref
            payload = request.payload
            if isinstance(payload, AdvanceClock):
                target = payload.to_clock_at.astimezone(fixture.starts_at.tzinfo)
                payload = AdvanceClock(to_clock_at=target)
                clock = target
            else:
                clock = ref.clock_at
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM events WHERE run_id = ?", (request.run_id,),
            ).fetchone()[0]
            event = SimulationEvent(
                event_id=f"{request.run_id}.manual{sequence}", run_id=request.run_id, scheduled_at=clock,
                sequence=sequence, event_type=request.event_type, target_id=event_target(payload), payload=payload,
            )
            venue_id = sim.affected_venue(event)
            self._authorize(actor, fixture, venue_id)
            digest = json.dumps((request.expected_snapshot_id, request.event_type, payload.model_dump(mode="json")),
                                sort_keys=True, separators=(",", ":"))
            replay = self._saved(db, scope, digest)
            if replay is not None:
                return replay
            if request.expected_snapshot_id != ref.snapshot_id:
                raise Fault("SNAPSHOT_CONFLICT", "Supply snapshot changed.", ref)

            scheduled = tuple(SimulationEvent.model_validate_json(row["event_json"]) for row in db.execute(
                "SELECT event_json FROM events WHERE run_id = ? AND applied_revision IS NULL", (request.run_id,),
            ))
            try:
                next_frame, applied = sim.transition(frame, event, scheduled)
            except Fault as error:
                raise Fault(error.code, str(error), ref) from error
            output = next_frame.supply.ref
            self._insert_frame(db, next_frame)
            self._insert_event(db, event)
            for order, entry in enumerate(applied):
                db.execute(
                    """UPDATE events SET applied_revision = ?, applied_index = ?, removed_portions = ?
                    WHERE run_id = ? AND event_id = ? AND applied_revision IS NULL""",
                    (output.revision, order, entry.removed_portions, output.run_id, entry.event.event_id),
                )
            record_window_applications(db, output, applied)
            audit = AuditRecord(
                run_id=output.run_id, audit_id=f"{output.run_id}.audit{output.revision}",
                action="SIM_CONTROL_APPLIED", actor_type="OPS", actor_id=actor.principal_id,
                result_code="OK", at=datetime.now(UTC), venue_id=venue_id, snapshot_id=output.snapshot_id,
            )
            db.execute("INSERT INTO audit VALUES (?, ?, ?)", (audit.run_id, audit.audit_id, audit.model_dump_json()))
            result = RunResult(scenario_id=current["scenario_id"], snapshot=output)
            self._save(db, scope, digest, result)
            return result
