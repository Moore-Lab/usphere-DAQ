"""
daq_cli.py  —  usphere-DAQ terminal interface

Connects to a running daq_server.py and sends commands.

One-shot usage::

    python daq_cli.py ping
    python daq_cli.py start_recording
    python daq_cli.py start_recording --n_files 5 --basename coriolis
    python daq_cli.py stop_recording
    python daq_cli.py get_status
    python daq_cli.py inject CTRL_FPGA '{"gain_x": 10.5, "gain_y": 10.5}'
    python daq_cli.py clear_injection CTRL_FPGA
    python daq_cli.py list_injections
    python daq_cli.py last_file
    python daq_cli.py list_plugins

Interactive REPL::

    python daq_cli.py --interactive
    python daq_cli.py -i

Global options::

    --host HOST    server hostname (default: localhost)
    --rep PORT     REP port (default: 5552)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zmq_base import ModuleClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_reply(reply: dict) -> None:
    status = reply.get("status", "?")
    if status == "error":
        print(f"ERROR: {reply.get('message', reply)}")
    else:
        data = reply.get("data")
        if data is None:
            print("OK")
        elif isinstance(data, dict):
            print(json.dumps(data, indent=2, default=str))
        else:
            print(data)


def _client(host: str, rep_port: int) -> ModuleClient:
    return ModuleClient("daq", rep_port=rep_port, pub_port=rep_port + 1,
                        host=host, timeout_ms=5000)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _run_one(client: ModuleClient, tokens: list[str]) -> bool:
    if not tokens:
        return True

    cmd = tokens[0].lower()

    if cmd in ("quit", "exit", "q"):
        return False

    if cmd == "help":
        print(__doc__)
        return True

    if cmd == "ping":
        print("ONLINE" if client.ping() else "OFFLINE")
        return True

    if cmd == "get_state":
        _print_reply(client.send("get_state"))
        return True

    if cmd == "get_status":
        _print_reply(client.send("get_status"))
        return True

    if cmd == "start_recording":
        # Remaining tokens are key=value pairs
        kwargs: dict = {}
        for tok in tokens[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                try:
                    v = int(v)
                except ValueError:
                    try:
                        v = float(v)
                    except ValueError:
                        pass
                kwargs[k] = v
        _print_reply(client.send("start_recording", **kwargs))
        return True

    if cmd == "stop_recording":
        _print_reply(client.send("stop_recording"))
        return True

    if cmd == "last_file":
        _print_reply(client.send("last_file"))
        return True

    if cmd == "inject":
        # inject <module_name> <json_data>
        if len(tokens) < 3:
            print("Usage: inject <module_name> '<json_dict>'")
            return True
        module_name = tokens[1]
        try:
            data = json.loads(" ".join(tokens[2:]))
        except json.JSONDecodeError as exc:
            print(f"JSON parse error: {exc}")
            return True
        if not isinstance(data, dict):
            print("Data must be a JSON object (dict)")
            return True
        _print_reply(client.send("inject", module_name=module_name, data=data))
        return True

    if cmd == "clear_injection":
        kwargs = {}
        if len(tokens) >= 2:
            kwargs["module_name"] = tokens[1]
        _print_reply(client.send("clear_injection", **kwargs))
        return True

    if cmd == "list_injections":
        _print_reply(client.send("list_injections"))
        return True

    if cmd == "set_config":
        kwargs = {}
        for tok in tokens[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                try:
                    v = int(v)
                except ValueError:
                    try:
                        v = float(v)
                    except ValueError:
                        pass
                kwargs[k] = v
        _print_reply(client.send("set_config", **kwargs))
        return True

    if cmd == "get_config":
        _print_reply(client.send("get_config"))
        return True

    if cmd == "list_plugins":
        _print_reply(client.send("list_plugins"))
        return True

    # Fall through
    print(f"Sending raw command {cmd!r} ...")
    _print_reply(client.send(cmd))
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="usphere-DAQ terminal interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--rep",  type=int, default=5552)
    parser.add_argument("-i", "--interactive", action="store_true")
    parser.add_argument("command", nargs="*")
    args = parser.parse_args()

    client = _client(args.host, args.rep)

    if args.interactive or not args.command:
        print(f"daq-cli  connected to {args.host}:{args.rep}")
        print("Type 'help' for commands, 'quit' to exit.\n")
        while True:
            try:
                line = input("daq> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if not _run_one(client, line.split()):
                break
    else:
        _run_one(client, args.command)

    client.close()


if __name__ == "__main__":
    main()
