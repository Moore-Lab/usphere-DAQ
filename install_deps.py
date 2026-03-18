#!/usr/bin/env python3
"""
install_deps.py

Sets up a virtual environment (.venv) and installs all usphere-DAQ
dependencies from requirements.txt.

Usage
-----
    python install_deps.py            # create .venv and install deps
    python install_deps.py --no-venv  # install into the active environment
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"


def run(cmd: list, **kwargs):
    print(f"  > {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Install usphere-DAQ dependencies")
    parser.add_argument(
        "--no-venv",
        action="store_true",
        help="Install into the current Python environment instead of a new .venv",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  usphere-DAQ  —  dependency installer")
    print("=" * 60)

    if not REQUIREMENTS.exists():
        print(f"ERROR: {REQUIREMENTS} not found.")
        sys.exit(1)

    if args.no_venv:
        pip = [sys.executable, "-m", "pip"]
        print(f"\nInstalling into: {sys.executable}\n")
    else:
        # Create virtual environment if it doesn't already exist
        if not VENV_DIR.exists():
            print(f"\nCreating virtual environment at: {VENV_DIR}\n")
            run([sys.executable, "-m", "venv", str(VENV_DIR)])
        else:
            print(f"\nUsing existing virtual environment: {VENV_DIR}\n")

        # Resolve the pip inside the venv
        if sys.platform == "win32":
            pip = [str(VENV_DIR / "Scripts" / "python.exe"), "-m", "pip"]
        else:
            pip = [str(VENV_DIR / "bin" / "python"), "-m", "pip"]

    # Upgrade pip first
    print("Upgrading pip…")
    run([*pip, "install", "--upgrade", "pip"])

    # Install requirements
    print(f"\nInstalling from {REQUIREMENTS.name}…\n")
    run([*pip, "install", "-r", str(REQUIREMENTS)])

    print("\n" + "=" * 60)
    print("  Installation complete.")
    if not args.no_venv:
        if sys.platform == "win32":
            activate = VENV_DIR / "Scripts" / "activate"
            print(f"\n  To activate the environment:")
            print(f"      {activate}")
            print(f"  or in bash:")
            print(f"      source {activate.as_posix()}")
        else:
            print(f"\n  To activate:")
            print(f"      source {VENV_DIR}/bin/activate")
        print(f"\n  Then run the GUI:")
        print(f"      python daq_gui.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
