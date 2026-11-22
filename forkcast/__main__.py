"""Start the local M1 API on loopback."""

import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="FORKCAST M1 simulation HTTP service")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    uvicorn.run("forkcast.http:from_env", factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
