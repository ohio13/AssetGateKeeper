"""Open a URL after a short delay.

Used by the .bat launchers: the server needs a moment to bind its port before
the browser asks for the page, but the launcher must not block while waiting.
Run detached, e.g.

    start /b "" python.exe tools\\open_url_after.py 2000 http://127.0.0.1:8100/viewer

Arguments: <delay-ms> <url>
Exit code is 0 even if the browser cannot be launched - the URL is printed on
stderr so the launcher window always tells the user where to go.
"""

from __future__ import annotations

import sys
import time
import webbrowser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("usage: open_url_after.py <delay-ms> <url>", file=sys.stderr)
        return 2
    try:
        delay_ms = float(args[0])
    except ValueError:
        delay_ms = 0.0
    url = args[1]
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    try:
        webbrowser.open(url)
    except Exception:  # a headless or locked-down session must not fail loudly
        print(f"could not open a browser; visit {url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
