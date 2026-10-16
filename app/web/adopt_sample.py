"""Sample script for the adopt wizard: a bass-shaker + subwoofer preset written as literal `minidsp -f` commands.

It is the expanded command block from the device notes (the shell loops written out). The text lives next to this
module in ``adopt_sample.txt`` and is identical to the test fixture ``tests/fixtures/pi_notes_commands.txt`` (a test
keeps the two in step).
"""

from pathlib import Path

SAMPLE_PATH = Path(__file__).with_name("adopt_sample.txt")
ADOPT_SAMPLE = SAMPLE_PATH.read_text(encoding="utf-8")
