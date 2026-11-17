#!/usr/bin/env python3
"""Embed (or update) the acorn JavaScript parser inside workflow_watch.py.

Usage, from the repository root:

    npm pack acorn@<version>            # in an empty folder of your choice
    tar xzf acorn-<version>.tgz
    python3 tools/vendor_acorn.py <that folder>/package <version>

The script replaces the text of ACORN_JS and the licence comment above it.
It refuses a source that would break the Python string it goes into.
"""

import re
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "workflow_watch.py"
START = re.compile(r'^# acorn .*?\nACORN_JS = r"""', re.S | re.M)
END = '"""\n\n\nif __name__ == "__main__":'


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    package, version = Path(argv[1]), argv[2]
    source = (package / "dist" / "acorn.js").read_text(encoding="utf-8")
    licence = (package / "LICENSE").read_text(encoding="utf-8")
    if '"""' in source or source.endswith("\\"):
        print("acorn.js contains text that cannot go into a raw Python string.")
        return 1
    text = TARGET.read_text(encoding="utf-8")
    start = START.search(text)
    end = text.find(END)
    if not start or end < start.end():
        print("Markers not found in %s." % TARGET)
        return 1
    comment = "# acorn %s, https://github.com/acornjs/acorn, embedded unchanged by\n" % version
    comment += "# tools/vendor_acorn.py. Its licence:\n#\n"
    comment += "".join("#   " + line + "\n" if line.strip() else "#\n" for line in licence.splitlines())
    new = text[:start.start()] + comment + 'ACORN_JS = r"""' + source + text[end:]
    TARGET.write_text(new, encoding="utf-8")
    print("Embedded acorn %s (%d bytes) into %s." % (version, len(source), TARGET.name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
