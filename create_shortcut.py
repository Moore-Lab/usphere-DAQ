"""
Run once to create a Windows desktop shortcut for daq_gui.py.

    python create_shortcut.py

Requires: pywin32  (pip install pywin32)
"""

import os
import sys
from pathlib import Path

try:
    from win32com.client import Dispatch
except ImportError:
    print("pywin32 is required.  Install it with:\n  pip install pywin32")
    sys.exit(1)

# Paths
PROJECT_DIR = Path(__file__).resolve().parent
SCRIPT = PROJECT_DIR / "daq_gui.py"
ICON = PROJECT_DIR / "assets" / "uDAQ_logo.ico"
DESKTOP = Path(os.environ.get("USERPROFILE", "~")) / "Desktop"
SHORTCUT_PATH = DESKTOP / "usphere DAQ.lnk"

# Prefer pythonw.exe (no console window); fall back to python.exe
pythonw = Path(sys.executable).parent / "pythonw.exe"
python_exe = str(pythonw if pythonw.exists() else sys.executable)

shell = Dispatch("WScript.Shell")
shortcut = shell.CreateShortCut(str(SHORTCUT_PATH))
shortcut.TargetPath = python_exe
shortcut.Arguments = f'"{SCRIPT}"'
shortcut.WorkingDirectory = str(PROJECT_DIR)
shortcut.Description = "usphere DAQ Control"

# .lnk only supports .ico; use the python exe icon as fallback
ico_path = PROJECT_DIR / "assets" / "logo.ico"
if ico_path.exists():
    shortcut.IconLocation = str(ico_path)
else:
    shortcut.IconLocation = python_exe

shortcut.save()
print(f"Shortcut created → {SHORTCUT_PATH}")
