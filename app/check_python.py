"""Used by RUN.bat: is this Python ready for the tool? Exit 0 = yes, otherwise prints what is missing.

A script file instead of `python -c "..."`: Windows PowerShell 5.1 drops the double quotes inside
a -c argument, the check then failed with a SyntaxError and every Python looked "not installed".
"""
import importlib.util
import sys

NEEDED = {
    "google.genai": "google-genai",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "edge_tts": "edge-tts",
    "faster_whisper": "faster-whisper",
    "numpy": "numpy",
    "tkinter": "tcl/tk (tick it in the Python installer)",
}


def main() -> int:
    missing = []
    for module, package in NEEDED.items():
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(package)
        except (ImportError, ValueError):  # "google.genai" raises when "google" itself is missing
            missing.append(package)
    if sys.version_info < (3, 10):
        missing.append(f"Python 3.10 or newer (this is {sys.version.split()[0]})")
    if missing:
        print("missing: " + ", ".join(missing))
        return 1
    print("OK " + sys.version.split()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
