"""
Base class for DAQ analysis plugins.

Subclass ``AnalysisPlugin``, set NAME and DESCRIPTION, and implement
``create_widget()`` to define the plugin's tab UI.  Override
``on_file_written()`` to react to new data files during live recording.

The module must expose the subclass as ``Plugin`` so the discovery
machinery can find it.
"""

from __future__ import annotations

from PyQt5.QtWidgets import QWidget


class DAQController:
    """Facade for programmatic recording control, injected by PluginManager."""

    def read_config(self):
        """Return the current DAQConfig from the GUI."""
        raise NotImplementedError

    def start_recording(self, n_files: int = 1, basename: str | None = None):
        """Start a recording with the current GUI settings.

        *n_files* overrides the file count (1 = single acquisition).
        *basename* optionally overrides the output filename prefix.
        Returns immediately; recording runs in background.
        """
        raise NotImplementedError

    def stop_recording(self):
        """Request the current recording to stop."""
        raise NotImplementedError

    def is_recording(self) -> bool:
        raise NotImplementedError


class AnalysisPlugin:
    """Interface that every analysis plugin must implement."""

    NAME: str = "Unnamed Plugin"
    DESCRIPTION: str = ""

    daq: DAQController | None = None
    """Injected by the plugin manager after loading."""

    def create_widget(self, parent: QWidget | None = None) -> QWidget:
        """Return the QWidget to embed as a new tab in the DAQ GUI."""
        raise NotImplementedError

    def on_file_written(self, filepath: str) -> None:
        """Called each time the DAQ writes a new HDF5 file."""

    def teardown(self) -> None:
        """Called when the plugin is unloaded.  Clean up resources."""
