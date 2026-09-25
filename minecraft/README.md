# DCF-Minecraft

Command blocks, redstone, a Paper plugin, a Fabric mod and a vanilla Bedrock client exchanging
ordinary 17-byte `DeModFrame`s with any Punctim peer. The world holds a **conforming register**
(34 nibbles as barrel item counts / redstone-wire power), pinned by
[`Documentation/minecraft_vectors.json`](../Documentation/minecraft_vectors.json) and specified by
[`Documentation/DCF_MINECRAFT_SPEC.md`](../Documentation/DCF_MINECRAFT_SPEC.md). The old
[`MineCraft/`](../MineCraft/) datapack is a non-conforming redstone demo; this directory replaces it.

| path | what |
|---|---|
| `datapack/gen_datapack.py` | the vanilla datapack: register, `dcf:tx`/`dcf:rx_commit`, self-announcing `DCF TX` egress, `dcf:selftest` |
| `core/` | loader-agnostic Java: the certified codec (from `../java`, unmodified) + `DcfUdpNode` (both UDP dialects) |
| `paper/` | the Paper plugin (`/dcf …`, `BlockRedstoneEvent` strobe, main-thread world writes) |
| `fabric/` | the Fabric mod (also runs in a single-player world's integrated server); Gradle/Loom |
| `build.sh` | plain-javac build of `build/dcf-minecraft-core.jar` and `build/dcf-minecraft-paper.jar` |
| `tools/prism_test.py` | client-in-the-loop test with a Prism Launcher instance (`--launch … --world …`) |
| `tools/devserver_test.py` | Oligarchy's real Paper 26.2 stack as your own user + the plugin, driven over its stdin console |
| `tools/real_server_roundtrip.py` | any server jar + RCON, headless |
| `tools/bot/` | a Mineflayer operator bot as a console (`punctim mc --bot …`) for LAN worlds |

The sidecar is `python/dcf/minecraft/` — `punctim mc …` and the `mc:` medium URI (Python-only).

## Quick start

```sh
# vanilla: datapack + sidecar over RCON (or --fifo … --log …, or --log … --egress chat for single-player)
python3 minecraft/datapack/gen_datapack.py --out ~/server/world/datapacks/dcf --origin 0 64 0
python3 python/punctim.py mc --rcon 127.0.0.1:25575 --password-file ~/.rcon --peer 127.0.0.1:7777/proto
# in game:  /function dcf:build   /function dcf:build_loopback   /function dcf:selftest

# modded
JAVA_HOME=<jdk21> PAPER_JDK=<jdk25> minecraft/build.sh          # -> build/dcf-minecraft-{core,paper}.jar
JAVA_HOME=<jdk25> gradle -p minecraft/fabric build                 # Gradle 9 + a Java-25 JVM to RUN Loom 1.18 (the mod targets 21)
                                                                   # -> fabric/build/libs/dcf-minecraft-fabric-*.jar

# your own client (this host): egress via the client log, ingress via the Fabric mod
python3 minecraft/tools/prism_test.py --instance 1.21.11
python3 minecraft/tools/prism_test.py --instance poo --mod minecraft/fabric/build/libs/dcf-minecraft-fabric-1.0.0.jar
# a real Paper 26.2 server (Oligarchy's dev runner, as you)
python3 minecraft/tools/devserver_test.py --join 1.21.11

# vanilla Bedrock: in the world, /connect 127.0.0.1:19134 ; command block: say DCF d31312340001ffffdeadbeefab12cd24c0
python3 python/punctim.py mc --bedrock-ws 127.0.0.1:19134 --peer 127.0.0.1:7777/proto -v
```

Peers carry a dialect: `/proto` (ProtoMessage MSG_FRAME=12 — `python/dcf_node.py`, Go, Rust, C,
`punctim io`) or `/bare` (17-B / 32-B SuperPack — the Hermes agent `matrix-bridge/mesh_mcp.py`,
JS, web). Events ride DCF-Game EVENT on `mc-world` (0xD952), chat rides DCF-Text on `mc-chat`
(0xE624); Hermes listens on `duet`, so point it at `mc-chat` (`DCF_CHANNEL=mc-chat`) to chat.

## Tests

```sh
python3 python/MCP/gen_minecraft_vectors.py /tmp/mv.json && diff /tmp/mv.json Documentation/minecraft_vectors.json
cd python && python3 -m unittest tests.test_minecraft_vectors tests.test_minecraft_datapack \
                                 tests.test_minecraft_sidecar tests.test_bedrock_ws -v
javac -d /tmp/j java/com/demod/dcf/*.java  # (minus Networking.java) then:
java -cp /tmp/j com.demod.dcf.MinecraftCertify Documentation/minecraft_vectors.json
minecraft/build.sh --core-only                                   # + CoreSelfTest loopback
```

CI: `certify-minecraft` in `.github/workflows/wire-certify.yml` (vectors, Python suites, javac jars).
The Fabric jar needs Gradle 9, a Java-25 JVM and network (Loom 1.18 fetches Minecraft + Yarn); `nix shell nixpkgs#gradle_9` works. Built by hand / on `workflow_dispatch`, not in the PR lane.

## Licensing

`LGPL-3.0-only` throughout; jars embed only `com.demod.dcf.*`. Compiled against the Paper API (MIT),
Fabric Loader/API (Apache-2.0) and Minecraft (proprietary, never vendored) — see
[`../LICENSING.md`](../LICENSING.md).
