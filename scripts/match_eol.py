"""Make every edited file's line endings match what git has in HEAD.

`Path.write_text` on Windows translates ``\\n`` to ``\\r\\n``, so touching a
three-line paragraph in an LF file rewrote all of it. That is how a one-line doc
change became a 70-line diff, and a diff nobody can read is a diff nobody
reviews. The reverse also bites: writing ``\\r\\n`` into a CRLF file is fine, but
writing LF into it is just as noisy.

So: read the blob git actually has, take its dominant ending, and rewrite the
working copy to match. Files are left byte-identical except where the content
really changed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def staged_bytes(path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout


def dominant(data: bytes) -> bytes:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    return b"\r\n" if crlf > lf else b"\n"


changed = subprocess.run(
    ["git", "diff", "--name-only"], cwd=REPO, capture_output=True, check=True
).stdout.decode("utf-8")
targets = [line for line in changed.splitlines() if line and not line.startswith("data/")]

for name in targets:
    head = staged_bytes(name)
    want = dominant(head)
    path = REPO / name
    current = path.read_bytes()
    # Normalise to LF first so the comparison ignores mixed remnants, then
    # re-expand with the convention HEAD uses.
    normalised = current.replace(b"\r\n", b"\n")
    fixed = normalised.replace(b"\n", want)
    if fixed == current:
        print(f"ok        {name}  ({'CRLF' if want == b'\r\n' else 'LF'})")
        continue
    path.write_bytes(fixed)
    print(f"rewritten {name}  ({'CRLF' if want == b'\r\n' else 'LF'})")
