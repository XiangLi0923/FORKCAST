"""Provision and revoke a host credential from a trusted local console."""

import argparse
import os
import sys
from getpass import getpass
from pathlib import Path
from secrets import compare_digest

from .errors import Fault
from .http import operator_credential_from_env
from .mcp_access import ROLE_TOOLS, HostAccess
from .store import SimulationStore


def operator():
    credential = operator_credential_from_env()
    proof = getpass("OPS credential: ")
    if credential is None or not compare_digest(proof.encode(), credential.token.encode()):
        raise Fault("UNAUTHENTICATED", "An existing OPS credential is required.")
    return credential.actor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("bind", "revoke"))
    parser.add_argument("role", choices=ROLE_TOOLS)
    args = parser.parse_args()
    try:
        store = SimulationStore(Path(os.getenv("FORKCAST_DATABASE", "forkcast.sqlite3")))
        store.bootstrap(os.getenv("FORKCAST_DATA_MODE", "SIMULATION"))
        access = HostAccess(store)
        identity = {"cookie": getpass("Browser forkcast_student cookie: ")} if args.role == "STUDENT" else {"actor": operator()}
        if args.operation == "bind":
            print(access.bind(store.current().snapshot.run_id, args.role, **identity))
        else:
            access.revoke(getpass("Host credential to revoke: "), **identity)
            print("Host credential revoked.")
    except Fault as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
