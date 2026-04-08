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


class AnalysisPlugin:
    """Interface that every analysis plugin must implement."""

    NAME: str = "Unnamed Plugin"
    DESCRIPTION: str = ""

    def create_widget(self, parent: QWidget | None = None) -> QWidget:
        """Return the QWidget to embed as a new tab in the DAQ GUI."""
        raise NotImplementedError

    def on_file_written(self, filepath: str) -> None:
        """Called each time the DAQ writes a new HDF5 file."""

    def teardown(self) -> None:
        """Called when the plugin is unloaded.  Clean up resources."""
