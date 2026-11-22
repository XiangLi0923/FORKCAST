"""Local three-role test host using the MCP Python SDK over stdio."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, stdio_client

from .mcp_access import ROLE_TOOLS
from .mcp_server import PROTOCOL


@asynccontextmanager
async def connect(database, token, *, mode="SIMULATION"):
    env = {"FORKCAST_DATABASE": str(Path(database).resolve()), "FORKCAST_DATA_MODE": mode}
    if token:
        env["FORKCAST_MCP_TOKEN"] = token
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "forkcast.mcp_server"], env=env)
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=10) as session:
            initialized = await session.initialize()
            if initialized.protocol_version != PROTOCOL or initialized.capabilities.tools is None:
                raise RuntimeError("Unexpected MCP protocol or missing tool capability.")
            yield session


async def call_checked(session, tools, name, arguments):
    result = await session.call_tool(name, arguments)
    envelope = result.structured_content
    if not isinstance(envelope, dict) or len(result.content) != 1 or result.content[0].type != "text":
        raise ValueError("Missing MCP response envelope.")
    if json.loads(result.content[0].text) != envelope or bool(result.is_error) == envelope["ok"]:
        raise ValueError("Inconsistent MCP response envelope.")
    if name in tools:
        Draft202012Validator(tools[name].output_schema).validate(envelope)
    return envelope


async def run(args):
    async with connect(os.getenv("FORKCAST_DATABASE", "forkcast.sqlite3"), os.getenv("FORKCAST_MCP_TOKEN")) as session:
        await session.send_ping()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        if set(tools) != set(ROLE_TOOLS[args.role]):
            raise SystemExit("This credential does not have the selected role's tool set.")
        if args.tool is None:
            print(json.dumps({"role": args.role, "protocol": PROTOCOL, "tools": list(tools)}, ensure_ascii=True))
        else:
            # The trusted operator pastes JSON and closes input. No local credential is accepted as a tool argument.
            arguments = json.load(sys.stdin)
            result = await call_checked(session, tools, args.tool, arguments)
            print(json.dumps(result, ensure_ascii=True))
            return 0 if result["ok"] else 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=ROLE_TOOLS, help="Assert the expected server-issued scope.")
    parser.add_argument("tool", nargs="?", help="Read the tool's JSON arguments from standard input.")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
