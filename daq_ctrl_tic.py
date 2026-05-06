"""
daq_ctrl_tic.py

DAQ plugin: reads Edwards TIC pressure data from the CTRL ZMQ server.

CTRL owns the TIC serial connection and caches readings in its poll thread.
This plugin queries that cache via a ZMQ REQ socket (get_tic command) so the
DAQ never opens its own serial connection to the TIC.

The H5 schema is unchanged from the legacy daq_edwards_tic plugin:
  MODULE_NAME = "TIC"
  keys        = "APGX" (mbar), "WRG" (mbar)

Prerequisites on the CTRL side
-------------------------------
- ctrl_server.py must be running (REP port 5550 by default)
- TIC polling must be active (start_tic_poll command, or --tic-poll CLI flag)
  Without an active poll the cache is empty and nan values are written.
"""

from __future__ import annotations

import json

MODULE_NAME = "TIC"
DEVICE_NAME = "Edwards TIC (via CTRL ZMQ)"

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "ctrl_host",
        "label":   "CTRL host",
        "type":    "text",
        "default": "localhost",
        "tooltip": "Hostname of the usphere-CTRL ZMQ server",
    },
    {
        "key":     "ctrl_rep_port",
        "label":   "CTRL REP port",
        "type":    "text",
        "default": "5550",
        "tooltip": "REP socket port of ctrl_server.py (default 5550)",
    },
]

KEY_APGX = "APGX"
KEY_WRG  = "WRG"

DEFAULTS: dict = {
    KEY_APGX: 0.0,
    KEY_WRG:  0.0,
}


def read(config: dict) -> dict:
    """
    Query the CTRL ZMQ server for the latest cached TIC readings.

    Returns
    -------
    dict  {"APGX": float_mbar, "WRG": float_mbar}
          Values are nan when CTRL has no reading yet.

    Raises
    ------
    RuntimeError  pyzmq not installed, or CTRL returned an error status
    TimeoutError  CTRL server did not respond within 2 s
    """
    try:
        import zmq
    except ImportError:
        raise RuntimeError("pyzmq not installed — run: pip install pyzmq")

    host     = str(config.get("ctrl_host", "localhost"))
    rep_port = int(config.get("ctrl_rep_port", 5550))

    ctx  = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 2000)
    sock.setsockopt(zmq.SNDTIMEO, 2000)
    sock.connect(f"tcp://{host}:{rep_port}")
    try:
        sock.send(json.dumps({"cmd": "get_tic", "args": {}}).encode())
        raw = sock.recv()
    except zmq.Again:
        raise TimeoutError(
            f"CTRL server ({host}:{rep_port}) did not respond within 2 s — "
            "is ctrl_server.py running?"
        )
    finally:
        sock.close()

    reply = json.loads(raw)
    if reply.get("status") != "ok":
        raise RuntimeError(f"CTRL get_tic error: {reply.get('message', reply)}")

    data = reply.get("data", {})
    if "error" in data:
        raise RuntimeError(f"CTRL TIC error: {data['error']}")

    # CTRL keys (mod_edwards_tic): wrg_mbar, apgx_mbar
    # DAQ H5 keys (legacy):        WRG,      APGX
    nan = float("nan")
    apgx = data.get("apgx_mbar", nan)
    wrg  = data.get("wrg_mbar",  nan)

    return {
        KEY_APGX: float(apgx) if apgx is not None else nan,
        KEY_WRG:  float(wrg)  if wrg  is not None else nan,
    }


def test(config: dict) -> tuple[bool, str]:
    """Connect to CTRL, fetch TIC readings, report. Safe to call from a thread."""
    try:
        values = read(config)
        apgx   = values[KEY_APGX]
        wrg    = values[KEY_WRG]
        apgx_s = f"{apgx:.3e} mbar" if apgx == apgx else "no reading (TIC poll not running?)"
        wrg_s  = f"{wrg:.3e} mbar"  if wrg  == wrg  else "no reading (TIC poll not running?)"
        return True, f"OK — APGX: {apgx_s}  |  WRG: {wrg_s}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
