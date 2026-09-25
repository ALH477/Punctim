# SPDX-License-Identifier: LGPL-3.0-only
"""Build a MinecraftTransport (and its Console) from `mc:` URI options or CLI flags."""
import os

from .. import transport as T
from .console import FifoConsole, RconConsole, SubprocessConsole
from .logtail import LogTail
from .rcon import RconClient
from .transport import MinecraftTransport


def _password(pass_file, pass_env):
    if pass_file:
        with open(pass_file) as fh:
            return fh.read().strip()
    env = pass_env or "MC_RCON_PASSWORD"
    pw = os.environ.get(env)
    if pw is None:
        raise T.MediumUnsupported(f"rcon needs pass_file= or ${env} (never put the password on argv)")
    return pw


def build(name, rcon=None, pass_file=None, pass_env=None, fifo=None, log=None, bot=None,
          egress=None, ns="dcf", poll_hz=4.0):
    """Returns (transport). Exactly one console source may be given; log= is required with
    fifo= (replies are read from the log) and sufficient alone for chat egress."""
    sources = [s for s in (rcon, fifo, bot) if s]
    if len(sources) > 1:
        raise T.MediumUnsupported("mc: give one of rcon=, fifo=, bot=")
    tail = LogTail(log) if log else None
    console = None
    if rcon:
        host, _, port = rcon.rpartition(":")
        if not host or not port.isdigit():
            raise T.MediumUnsupported("rcon= wants host:port")
        console = RconConsole(RconClient(host, int(port), _password(pass_file, pass_env)))
    elif fifo:
        if tail is None:
            raise T.MediumUnsupported("fifo= needs log= (replies are read from the server log)")
        console = FifoConsole(fifo, tail)
    elif bot:
        argv = bot if isinstance(bot, list) else [a for a in bot.split("|") if a]
        console = SubprocessConsole(argv)
    return MinecraftTransport(name, console=console, log_path=None, namespace=ns, egress=egress,
                              poll_hz=poll_hz, tail=tail)


def make_minecraft(name, g, direction):
    egress = g("egress")
    if egress is not None and egress not in ("console", "chat"):
        raise T.MediumUnsupported("egress= must be console or chat")
    return build(name, rcon=g("rcon"), pass_file=g("pass_file"), pass_env=g("pass_env"),
                 fifo=g("fifo"), log=g("log"), bot=g("bot"), egress=egress, ns=g("ns", "dcf"),
                 poll_hz=float(g("poll_hz", "4")))
