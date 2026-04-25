"""
daq_server.py  —  usphere-DAQ ZMQ server

Owns the NI PXIe-6363 DAQ card and H5 file writing.
Exposes recording control and the inject API over ZMQ so that
usphere-CTRL, usphere-Q, and usphere-EXPT experiment scripts can add
their own metadata into every recorded H5 file without touching the
DAQ card or the H5 schema directly.

Run standalone::

    python daq_server.py                    # default ports 5552/5553
    python daq_server.py --no-gui

ZMQ commands (send to REP port 5552)
--------------------------------------
Built-in (from zmq_base):
    ping                        liveness check
    get_state                   current recorder state
    get_info                    port / module info

Recording:
    start_recording  [n_files] [basename] [sample_rate] [n_bits]
                     [output_dir] [device] [channels]
    stop_recording
    get_status                  → {recording, file_index, n_files, ...}
    last_file                   → {path}

Injection (key framework feature):
    inject           module_name data={..}  queue metadata for next H5 file(s)
    clear_injection  [module_name]          remove one or all injected modules
    list_injections                         → {module_name: [keys]}

Configuration:
    set_config       **kwargs               update DAQConfig fields
    get_config                              → current DAQConfig dict

Plugins:
    list_plugins                            → [plugin names]
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zmq_base import ModuleServer
from daq_core import DAQConfig, DAQRecorder, get_plugins

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DAQServer
# ---------------------------------------------------------------------------

class DAQServer(ModuleServer):
    """
    ZMQ server wrapping DAQRecorder.

    The server maintains a single DAQRecorder instance.  Recording is
    started/stopped via ZMQ commands; injected metadata is passed through
    to every H5 file written during the active recording session.
    """

    def __init__(
        self,
        rep_port: int = 5552,
        pub_port: int = 5553,
        publish_interval_s: float = 0.5,
    ) -> None:
        super().__init__(
            module_name="daq",
            rep_port=rep_port,
            pub_port=pub_port,
            publish_interval_s=publish_interval_s,
        )
        self._config = DAQConfig()
        self._recorder: DAQRecorder | None = None
        self._last_file: str | None = None
        self._file_index: int = 0
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------------
    # get_state — streamed by PUB loop
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        recording = self._recorder is not None and self._recorder.is_running()
        with self._status_lock:
            injected = (self._recorder.list_injected()
                        if self._recorder is not None else {})
        return {
            "recording":    recording,
            "file_index":   self._file_index,
            "n_files":      self._config.n_files,
            "output_dir":   self._config.output_dir,
            "basename":     self._config.basename,
            "sample_rate":  self._config.sample_rate,
            "last_file":    self._last_file,
            "injected":     list(injected.keys()),
        }

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def handle_command(self, cmd: str, args: dict) -> dict:
        try:
            return self._dispatch_daq(cmd, args)
        except Exception as exc:
            log.exception("Command %r raised", cmd)
            return {"status": "error", "message": str(exc)}

    def _dispatch_daq(self, cmd: str, args: dict) -> dict:

        # ---- Recording ----
        if cmd == "start_recording":
            return self._cmd_start(args)

        if cmd == "stop_recording":
            return self._cmd_stop()

        if cmd == "get_status":
            return {"status": "ok", "data": self.get_state()}

        if cmd == "last_file":
            return {"status": "ok", "data": {"path": self._last_file}}

        # ---- Injection ----
        if cmd == "inject":
            module_name = args.get("module_name")
            data = args.get("data")
            if not module_name or not isinstance(data, dict):
                return {"status": "error",
                        "message": "inject requires module_name (str) and data (dict)"}
            self._ensure_recorder()
            self._recorder.inject_module_data(module_name, data)
            return {"status": "ok"}

        if cmd == "clear_injection":
            self._ensure_recorder()
            self._recorder.clear_injected(args.get("module_name"))
            return {"status": "ok"}

        if cmd == "list_injections":
            self._ensure_recorder()
            return {"status": "ok", "data": self._recorder.list_injected()}

        # ---- Configuration ----
        if cmd == "set_config":
            valid = DAQConfig.__dataclass_fields__.keys()
            for k, v in args.items():
                if k in valid:
                    setattr(self._config, k, v)
            return {"status": "ok"}

        if cmd == "get_config":
            return {"status": "ok", "data": self._config.to_dict()}

        # ---- Plugins ----
        if cmd == "list_plugins":
            names = [getattr(p, "MODULE_NAME", type(p).__name__) for p in get_plugins()]
            return {"status": "ok", "data": names}

        return {"status": "error", "message": f"unknown command: {cmd!r}"}

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def _ensure_recorder(self) -> None:
        """Create a recorder if none exists (not recording yet)."""
        if self._recorder is None:
            self._recorder = DAQRecorder(
                config=self._config,
                on_status=lambda msg: log.info("[DAQ] %s", msg),
                on_file_written=self._on_file_written,
                on_finished=self._on_finished,
            )

    def _cmd_start(self, args: dict) -> dict:
        if self._recorder is not None and self._recorder.is_running():
            return {"status": "error", "message": "recording already in progress"}

        # Apply any one-shot config overrides from the command args
        valid = DAQConfig.__dataclass_fields__.keys()
        for k, v in args.items():
            if k in valid:
                setattr(self._config, k, v)

        self._file_index = 0
        self._recorder = DAQRecorder(
            config=self._config,
            on_status=lambda msg: log.info("[DAQ] %s", msg),
            on_file_written=self._on_file_written,
            on_finished=self._on_finished,
        )
        self._recorder.start()
        log.info("Recording started (%d files → %s)",
                 self._config.n_files, self._config.output_dir)
        return {"status": "ok"}

    def _cmd_stop(self) -> dict:
        if self._recorder is None or not self._recorder.is_running():
            return {"status": "error", "message": "not recording"}
        self._recorder.stop()
        return {"status": "ok"}

    def _on_file_written(self, path: Path) -> None:
        self._last_file = str(path)
        with self._status_lock:
            self._file_index += 1
        log.info("File written: %s", path)

    def _on_finished(self) -> None:
        log.info("Recording finished")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="usphere-DAQ ZMQ server")
    p.add_argument("--rep",    type=int, default=5552, help="REP port (default 5552)")
    p.add_argument("--pub",    type=int, default=5553, help="PUB port (default 5553)")
    p.add_argument("--no-gui", action="store_true",    help="headless mode (no Qt GUI)")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    args = _parse_args()

    server = DAQServer(rep_port=args.rep, pub_port=args.pub)
    server.start()
    log.info("daq server listening  REP=tcp://*:%d  PUB=tcp://*:%d", args.rep, args.pub)

    if args.no_gui:
        log.info("Running headless — Ctrl-C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
            if server._recorder and server._recorder.is_running():
                server._recorder.stop()
    else:
        try:
            from PyQt5.QtWidgets import QApplication
            import daq_gui as gui_mod
        except ImportError as exc:
            log.error("Cannot import GUI (%s) — rerun with --no-gui", exc)
            sys.exit(1)

        app = QApplication(sys.argv)
        window = gui_mod.DAQWindow(recorder=server._recorder, server=server)
        window.show()
        try:
            sys.exit(app.exec_())
        finally:
            server.stop()


if __name__ == "__main__":
    main()
