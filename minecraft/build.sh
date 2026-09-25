#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (C) 2026 DeMoD LLC.
#
# Build the DCF-Minecraft jars with plain javac (no Gradle, hermetic given the jars below):
#   build/dcf-minecraft-core.jar    the certified codec + Game/Text/McEvent + the UDP node (Java 21)
#   build/dcf-minecraft-paper.jar   the Paper plugin (Java 25: Paper 26.2's API is Java-25 bytecode)
# The Fabric mod is Gradle/Loom only: `gradle -p minecraft/fabric build` (see README.md).
#
#   minecraft/build.sh [--core-only] [--paper-lib DIR]
#
# JDKs: $JAVA_HOME (or javac on PATH) must be >= 21 for the core; the plugin needs a JDK >= 25,
# taken from $PAPER_JDK, else $JAVA_HOME if new enough, else a Nix-store openjdk-25.
# Paper API + Adventure jars are fetched into --paper-lib (default build/paper-lib) unless present
# ($PAPER_API_JAR overrides the API jar, e.g. from a Nix fixed-output fetch).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$HERE/build"
CORE_ONLY=0
LIB="$OUT/paper-lib"
while [ $# -gt 0 ]; do
  case "$1" in
    --core-only) CORE_ONLY=1; shift ;;
    --paper-lib) LIB="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PAPER_API_VERSION="${PAPER_API_VERSION:-26.2.build.121-stable}"   # == Oligarchy's papermc.nix pin
PAPER_REPO="https://repo.papermc.io/repository/maven-public/io/papermc/paper/paper-api"
MAVEN="https://repo.papermc.io/repository/maven-public"   # proxies Maven Central + BungeeCord
# groupPath artifact version — what javac needs to resolve Paper API signatures (Adventure & co.)
PAPER_DEPS="net/kyori/adventure-api 5.2.0
net/kyori/adventure-key 5.2.0
net/kyori/examination-api 1.3.0
net/kyori/examination-string 1.3.0
net/kyori/option 1.1.0
org/jspecify/jspecify 1.0.0
com/google/guava/guava 33.6.0-jre
org/joml/joml 1.10.8
net/md-5/bungeecord-chat 1.21-R0.2-deprecated+build.21"

jdk_major() { "$1" -version 2>&1 | sed -n 's/^javac \([0-9]*\).*/\1/p'; }
JAVAC="${JAVA_HOME:+$JAVA_HOME/bin/javac}"; JAVAC="${JAVAC:-$(command -v javac || true)}"
[ -n "$JAVAC" ] || { echo "no javac (set JAVA_HOME)" >&2; exit 1; }
JAR="$(dirname "$JAVAC")/jar"
[ "$(jdk_major "$JAVAC")" -ge 21 ] || { echo "core needs JDK >= 21, have $(jdk_major "$JAVAC")" >&2; exit 1; }

rm -rf "$OUT/core" "$OUT/paper"
mkdir -p "$OUT/core" "$OUT/paper" "$LIB"

# ── core: the certified codec (unmodified, from java/) + minecraft/core ───────────────────
CODEC=$(ls "$ROOT"/java/com/demod/dcf/*.java | grep -v -e Networking.java -e Certify.java)
"$JAVAC" -Xlint:all,-options --release 21 -d "$OUT/core" $CODEC "$HERE"/core/src/com/demod/dcf/mc/*.java
"$JAR" --create --file "$OUT/dcf-minecraft-core.jar" -C "$OUT/core" .
# the core's own loopback proof (no network beyond localhost)
"$(dirname "$JAVAC")/java" -cp "$OUT/core" com.demod.dcf.mc.CoreSelfTest
echo "built $OUT/dcf-minecraft-core.jar"
[ "$CORE_ONLY" = 1 ] && exit 0

# ── paper plugin ───────────────────────────────────────────────────────────────────────────
PJAVAC="${PAPER_JDK:+$PAPER_JDK/bin/javac}"
if [ -z "$PJAVAC" ] && [ "$(jdk_major "$JAVAC")" -ge 25 ]; then PJAVAC="$JAVAC"; fi
if [ -z "$PJAVAC" ]; then
  for d in /nix/store/*-openjdk-25*/bin/javac; do [ -x "$d" ] && PJAVAC="$d" && break; done
fi
[ -n "$PJAVAC" ] && [ "$(jdk_major "$PJAVAC")" -ge 25 ] || {
  echo "the Paper plugin needs a JDK >= 25 (Paper 26.2 API is Java-25 bytecode): set PAPER_JDK" >&2; exit 1; }

API="${PAPER_API_JAR:-$LIB/paper-api-$PAPER_API_VERSION.jar}"
if [ ! -f "$API" ]; then
  echo "fetching paper-api $PAPER_API_VERSION" >&2
  curl -sfL -o "$API" "$PAPER_REPO/$PAPER_API_VERSION/paper-api-$PAPER_API_VERSION.jar"
fi
CP="$API"
while read -r gp ver; do
  [ -n "$gp" ] || continue
  art="${gp##*/}"; f="$LIB/$art-$ver.jar"
  if [ ! -f "$f" ]; then
    echo "fetching $art $ver" >&2
    curl -sfL -o "$f" "$MAVEN/$gp/$ver/$art-$ver.jar"
  fi
  CP="$CP:$f"
done <<< "$PAPER_DEPS"

"$PJAVAC" -Xlint:all,-options,-deprecation,-classfile --release 25 -cp "$CP:$OUT/core" -d "$OUT/paper" \
  "$HERE"/paper/src/com/demod/dcf/paper/*.java
cp -r "$OUT/core/." "$OUT/paper/"                    # one self-contained jar: plugin + core + codec
cp "$HERE"/paper/resources/plugin.yml "$HERE"/paper/resources/config.yml "$OUT/paper/"
"$(dirname "$PJAVAC")/jar" --create --file "$OUT/dcf-minecraft-paper.jar" -C "$OUT/paper" .
echo "built $OUT/dcf-minecraft-paper.jar (plugin.yml api-version $(sed -n "s/^api-version: '\(.*\)'/\1/p" "$HERE"/paper/resources/plugin.yml))"
