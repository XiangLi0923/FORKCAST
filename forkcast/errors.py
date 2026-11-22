"""M1 business errors shared by the store and the HTTP adapter."""

from .models import SnapshotRef


class Fault(Exception):
    def __init__(self, code: str, message: str, snapshot: SnapshotRef | None = None, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.snapshot = snapshot
        self.details = details
