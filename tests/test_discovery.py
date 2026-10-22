"""Address handling for adding players by IP/hostname, and mDNS address ranking.

    python tests/test_discovery.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cueplayer.common import normalize_url  # noqa: E402
from cueplayer.discovery import best_addresses  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


for given, want in [("10.0.0.21", "http://10.0.0.21:8080"), ("led1.local", "http://led1.local:8080"),
                    ("10.0.0.21:9000", "http://10.0.0.21:9000"), ("http://10.0.0.21:8080/", "http://10.0.0.21:8080"),
                    ("http://10.0.0.21/api/x", "http://10.0.0.21:8080"), ("https://master.example", "https://master.example"),
                    ("fe80::1", "http://[fe80::1]:8080"), ("  10.0.0.21  ", "http://10.0.0.21:8080"), ("", "")]:
    check(f"normalize {given!r}", normalize_url(given) == want, normalize_url(given))

check("same subnet first, loopback last",
      best_addresses(["127.0.0.1", "192.168.1.121", "10.0.10.103"], ["10.0.10.5"]) == ["10.0.10.103", "192.168.1.121", "127.0.0.1"])
check("link-local after private", best_addresses(["169.254.1.1", "192.168.1.9"], ["172.16.0.2"]) == ["192.168.1.9", "169.254.1.1"])

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
