# SPDX-License-Identifier: LGPL-3.0-only
"""A stand-in for minecraft/tools/bot/dcf_bot.js in tests: the same JSON-lines console
protocol, executing commands over RCON against the FakeServer and streaming the server's
chat lines as {"chat": ...} — so SubprocessConsole's reply/chat routing is exercised for real."""
import json
import os
import sys
import threading

PYDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PYDIR)
from dcf.minecraft.logtail import LogTail  # noqa: E402
from dcf.minecraft.rcon import RconClient  # noqa: E402


def main():
    port, password, log = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    rc = RconClient("127.0.0.1", port, password)
    out_lock = threading.Lock()

    def emit(obj):
        with out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    tail = LogTail(log).start()
    tail.subscribe(lambda line, m: emit({"chat": m}) if "[CHAT]" in line else None)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        reply = rc.exec(req["cmd"])
        emit({"reply": reply if req.get("reply") else ""})
    tail.stop()


if __name__ == "__main__":
    main()
