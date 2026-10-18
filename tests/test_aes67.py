"""Checks the AES67 SDP / SAP output and the aes67 settings validation.

    python tests/test_aes67.py
"""
import copy
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cueplayer import aes67  # noqa: E402
from cueplayer.config import DEFAULTS, Settings  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


def raises(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    return None


BASE = DEFAULTS["aes67"]
GM = "00-1D-C1-FF-FE-12-34-56"

# ---- helpers ----
check("clock id formatting", aes67.clock_id_str(0x001DC1FFFE123456) == GM, aes67.clock_id_str(0x001DC1FFFE123456))
check("unset clock id -> None", aes67.clock_id_str(0) is None and aes67.clock_id_str(2 ** 64 - 1) is None)
check("default multicast from host", aes67.default_multicast("10.20.30.40") == "239.69.30.40")
check("default multicast fallback", aes67.default_multicast("bogus") == "239.69.0.1")
check("1 ms stereo L24 = 288 bytes", aes67.payload_bytes(2, 24, 1) == 288)
check("125 us = 6 samples", aes67.samples_per_packet(0.125) == 6)

# ---- SDP ----
cfg = dict(aes67.validate(dict(BASE, enabled=True, multicast="239.69.1.2", ptp_domain=3)),
           source_ip="10.0.1.2", session_name="East LED audio")
sdp = aes67.build_sdp(cfg, GM, version=7)
lines = sdp.split("\r\n")
check("SDP uses CRLF and ends with one", sdp.endswith("\r\n") and "\n" not in sdp.replace("\r\n", ""))
check("SDP version line first", lines[0] == "v=0", lines[0])
check("origin carries source + version", lines[1].startswith("o=- ") and lines[1].endswith(" 7 IN IP4 10.0.1.2"), lines[1])
for want in ("s=East LED audio", "c=IN IP4 239.69.1.2/32", "t=0 0", "m=audio 5004 RTP/AVP 96",
             "a=rtpmap:96 L24/48000/2", "a=ptime:1", "a=mediaclk:direct=0", "a=recvonly",
             "a=framecount:48", "a=source-filter: incl IN IP4 239.69.1.2 10.0.1.2",
             f"a=ts-refclk:ptp=IEEE1588-2008:{GM}:3"):
    check(f"SDP has {want!r}", want in lines, sdp)
check("session id stable", aes67.build_sdp(cfg, GM, 7) == sdp)
check("m= line after session lines", lines.index("m=audio 5004 RTP/AVP 96") > lines.index("t=0 0"))

mono = aes67.build_sdp(dict(cfg, channels=1, bit_depth=16, ptime_ms=0.25), GM)
check("mono L16 rtpmap", "a=rtpmap:96 L16/48000/1" in mono, mono)
check("fractional ptime", "a=ptime:0.25" in mono and "a=framecount:12" in mono, mono)
check("newline in session name stripped", "\ns=" not in aes67.build_sdp(dict(cfg, session_name="a\nb"), GM).replace("\r\n", ""))

r = aes67.resolve(dict(BASE, enabled=True), "Atrium")
check("resolve fills multicast + name", r["multicast"].startswith("239.69.") and r["session_name"] == "Atrium audio", r)
socket.inet_aton(r["source_ip"])  # a valid IPv4 address

# ---- SAP ----
pkt = aes67.sap_packet(sdp, "10.0.1.2")
check("SAP v1 announce header", pkt[0] == 0x20 and pkt[1] == 0, pkt[:2])
check("SAP origin address", pkt[4:8] == socket.inet_aton("10.0.1.2"))
check("SAP payload type + SDP", pkt[8:].startswith(b"application/sdp\x00" + b"v=0"))
dele = aes67.sap_packet(sdp, "10.0.1.2", delete=True)
check("SAP deletion flag", dele[0] == 0x24 and dele[2:4] == pkt[2:4])

# ---- validation ----
check("defaults validate", aes67.validate(copy.deepcopy(BASE)) == BASE, aes67.validate(copy.deepcopy(BASE)))
check("off by default", BASE["enabled"] is False)
v = aes67.validate(dict(BASE, port="5006", ptime_ms="0.125", channels="1"))
check("strings coerced", v["port"] == 5006 and v["ptime_ms"] == 0.125 and v["channels"] == 1, v)
check("ptime 1 stays an int", aes67.validate(dict(BASE, ptime_ms="1"))["ptime_ms"] == 1)
for bad, why in ((dict(multicast="10.0.0.1"), "unicast address"), (dict(multicast="239.1"), "malformed address"),
                 (dict(multicast="224.0.0.5"), "reserved group"), (dict(multicast="224.2.127.254"), "SAP group"),
                 (dict(port=5005), "odd port"), (dict(port=80), "privileged port"), (dict(channels=8), "8 channels"),
                 (dict(bit_depth=20), "20-bit"), (dict(ptime_ms=2), "2 ms ptime"), (dict(ptp_domain=200), "domain 200"),
                 (dict(ttl=0), "ttl 0"), (dict(port="abc"), "non-numeric port"), (dict(interface="eth0; rm"), "bad interface")):
    check(f"rejects {why}", raises(lambda: aes67.validate(dict(BASE, **bad))) is not None)
msg = raises(lambda: aes67.validate(dict(BASE, port=5005, ttl=0)))
check("all errors reported together", msg and "port" in msg and "ttl" in msg, msg)

with tempfile.TemporaryDirectory() as d:
    s = Settings(Path(d))
    err = raises(lambda: s.update({"aes67": {"enabled": True, "multicast": "10.1.1.1"}, "ndi": {"name": "X"}}))
    check("Settings.update rejects bad aes67", err is not None)
    check("rejected update changes nothing", s.get()["aes67"]["enabled"] is False and s.get()["ndi"]["name"] == "CuePlayer")
    changed = s.update({"aes67": {"enabled": True, "port": "5008"}})
    check("good update applied + normalised", changed == {"aes67"} and s.get()["aes67"]["port"] == 5008, (changed, s.get()["aes67"]))

print("all passed" if not failures else f"{failures} failed")
sys.exit(1 if failures else 0)
