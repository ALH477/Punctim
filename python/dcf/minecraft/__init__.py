# SPDX-License-Identifier: LGPL-3.0-only
"""DCF-Minecraft sidecar: drive the conforming DeModFrame register of a Minecraft world
(Documentation/DCF_MINECRAFT_SPEC.md) from outside the game — over RCON, the server console
FIFO / stdin pipe, a Mineflayer bot, or just the client's chat log — and bridge it to any
Punctim UDP peer.  Stdlib only.  `punctim mc` is the CLI; `mc:` is the medium URI."""
from .transport import MinecraftTransport  # noqa: F401
