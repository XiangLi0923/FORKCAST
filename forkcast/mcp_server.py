"""MCP 2025-11-25 over local UTF-8 newline-delimited stdio."""

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from .errors import Fault
from .mcp_contracts import TOOLS
from .mcp_gateway import Gateway, internal_fault
from .store import SimulationStore

PROTOCOL = "2025-11-25"
MAX_MESSAGE = 1024 * 1024


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Session:
    def __init__(self, gateway):
        self.gateway = gateway
        self.handshake = False
        self.initialized = False

    def receive(self, message):
        request_id = None
        try:
            method, params, request_id = self.parse(message)
            if request_id is None:
                if method == "notifications/initialized" and self.handshake:
                    self.initialized = True
                return None
            result = self.call(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ProtocolError as error:
            return self.error(request_id, error.code, str(error))
        except Exception:
            return self.error(request_id, -32603, str(internal_fault()))

    @staticmethod
    def parse(message):
        try:
            data = json.loads(message.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError(-32700, "Parse error.") from None
        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or not isinstance(data.get("method"), str):
            raise ProtocolError(-32600, "Invalid request.")
        if "id" in data and type(data["id"]) not in (int, str):
            raise ProtocolError(-32600, "Invalid request ID.")
        return data["method"], data.get("params", {}), data.get("id")

    def call(self, method, params):
        if not isinstance(params, dict):
            raise ProtocolError(-32602, "Invalid request parameters.")
        if method == "ping":
            return {}
        if method == "initialize":
            client = params.get("clientInfo")
            if (self.handshake or not isinstance(params.get("protocolVersion"), str)
                    or not isinstance(params.get("capabilities"), dict) or not isinstance(client, dict)
                    or not isinstance(client.get("name"), str) or not isinstance(client.get("version"), str)):
                raise ProtocolError(-32602, "Invalid initialize request.")
            self.handshake = True
            return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "FORKCAST", "version": "0.3.0"}}
        if not self.initialized:
            raise ProtocolError(-32600, "Complete MCP initialization first.")
        if method == "tools/list":
            if set(params) - {"_meta"}:
                raise ProtocolError(-32602, "Tool listing is not paginated.")
            return {"tools": self.gateway.discover()}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if (not isinstance(name, str) or not isinstance(args, dict)
                    or set(params) - {"name", "arguments", "_meta"}):
                raise ProtocolError(-32602, "Malformed tool request.")
            if name not in TOOLS:
                self.gateway.count_unknown_call()
                raise ProtocolError(-32602, "Unknown tool.")
            return self.gateway.invoke(name, args)
        raise ProtocolError(-32601, "Method not found.")

    @staticmethod
    def error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def reject_constant(value):
    raise ValueError("JSON constants are invalid")


def main():
    mode = os.getenv("FORKCAST_DATA_MODE", "SIMULATION")
    store = SimulationStore(Path(os.getenv("FORKCAST_DATABASE", "forkcast.sqlite3")))
    try:
        store.bootstrap(mode)
        startup_fault = None
    except Fault as error:
        logging.getLogger(__name__).warning("MCP startup rejected: %s", error.code)
        startup_fault = error
    except sqlite3.Error:
        logging.getLogger(__name__).warning("MCP startup rejected: DEPENDENCY_UNAVAILABLE")
        startup_fault = Fault("DEPENDENCY_UNAVAILABLE", "The local store could not be opened.")
    session = Session(Gateway(store, os.getenv("FORKCAST_MCP_TOKEN"), startup_fault))
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    while line := source.readline(MAX_MESSAGE + 1):
        if len(line) > MAX_MESSAGE:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_MESSAGE + 1)
            reply = session.error(None, -32600, "MCP message exceeds the local size limit.")
        else:
            reply = session.receive(line)
        if reply is not None:
            try:
                sink.write(json.dumps(reply, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n")
                sink.flush()
            except BrokenPipeError:
                return


if __name__ == "__main__":
    main()
