# SPDX-License-Identifier: LGPL-3.0-only
package DCF::Medium;

# DCF-Medium — the digital medium codecs (Tier B: stream, hex, udp_proto, udp_bare,
# l2eth), byte-identical to the canonical Python reference python/MCP/mediumlab_core.py
# and certified against Documentation/medium_vectors.json (spec:
# Documentation/DCF_MEDIUM_SPEC.md).
#
# A medium codec is a deterministic pair
#     encode : [frame]        -> representation
#     decode : representation -> ([frame], diagnostics)
# that carries the 17-byte DeModFrame quantum over one medium WITHOUT parsing it beyond
# the frame gate (sync 0xD3 + version nibble 1 + CRC-16/CCITT-FALSE). Media are
# transports *beneath* the quantum, so the 246-vector wire certificate is untouched.
#
#   stream     .dcf file / stdio: concatenated frames; decode is a byte-wise resync scan
#   hex        34 lowercase hex chars + "\n" per frame; tolerant line parser
#   udp_proto  ProtoMessage [type u8][seq u32][ts u64][len u32][payload] (big-endian);
#              one frame = msg_type FRAME (12), len 17 -> a 34-byte datagram
#   udp_bare   consecutive pairs -> one 32-byte SuperPack, lone trailing frame raw 17 B
#   l2eth      [n_frames u16 BE][SuperPack * ceil(n/2)], odd tail paired with the filler
#
# Frames are 17-byte binary strings. A u64 timestamp is a native integer on perls with
# 64-bit integers, or a Math::BigInt (accepted everywhere, returned on 32-bit perls).

use strict;
use warnings;
use Config;
use Exporter 'import';
use DCF::Frame qw(crc16 encode SYNC VERSION FRAME_SIZE CRC_COVER);
use DCF::SuperPack qw(pack_super unpack_super is_superpack SUPER_LEN);

our $VERSION = '0.3.0';
our @EXPORT_OK = qw(
    gate
    stream_encode stream_decode
    hex_encode hex_decode
    proto_encode proto_decode proto_frame_encode proto_frame_decode
    bare_encode bare_decode
    l2_filler l2_capacity l2_batch l2_unbatch
    PROTO_HEADER_LEN MSG_FRAME L2_HDR %MSG_TYPES
);
our %EXPORT_TAGS = (all => \@EXPORT_OK);

use constant {
    PROTO_HEADER_LEN => 17,
    MSG_FRAME        => 12,
    L2_HDR           => 2,     # the n_frames u16
};
use constant HAS_U64 => (($Config{uvsize} || 4) >= 8) ? 1 : 0;

# The ProtoMessage msg_type registry (mirrors mediumlab_core.MSG_TYPES).
our %MSG_TYPES = (
    POSITION => 1, AUDIO => 2, GAME_EVENT => 3, STATE_SYNC => 4, RELIABLE => 5,
    ACK => 6, PING => 7, PONG => 8, GAME_DCF => 9, TEXT_DCF => 10, MESH => 11,
    FRAME => 12,
);

# ── the frame gate (the existing validity rule; media never parse further) ─────────
# True iff $w is a valid DeModFrame: 17 bytes, sync 0xD3, version nibble 1, and
# CRC-16/CCITT-FALSE over bytes [0..14] == bytes [15..16] (big-endian).
sub gate {
    my ($w) = @_;
    return 0 unless defined $w && length($w) == FRAME_SIZE;
    my ($s, $f) = unpack('C2', $w);
    return 0 unless $s == SYNC && ($f >> 4) == VERSION;
    return crc16($w, CRC_COVER) == unpack('n', substr($w, 15, 2)) ? 1 : 0;
}

sub _need17 {
    for (@_) {
        die "need " . FRAME_SIZE . "-byte frames, got " . length($_) . "\n"
            unless length($_) == FRAME_SIZE;
    }
}

# ══ stream (.dcf file, stdio) ═══════════════════════════════════════════════════════
# Concatenate 17-byte frames (the .dcf / stdio representation).
sub stream_encode {
    my ($frames) = @_;
    _need17(@$frames);
    return join('', @$frames);
}

# Byte-wise resync scan of $$bufref from offset $i. Pushes gated frames onto @$out and
# returns ($i, $skipped) with length - $i < 17. At offset i: if the 17-byte window passes
# the gate, emit it and i += 17; else i += 1, skipped += 1.
sub _scan {
    my ($bufref, $i, $skipped, $out) = @_;
    my $last = length($$bufref) - FRAME_SIZE;        # highest offset with a full window
    my $sync = chr(SYNC);
    while ($i <= $last) {
        if (substr($$bufref, $i, 1) ne $sync) {
            # fast-forward to the next 0xD3 that still has a full window (pure speed-up:
            # every skipped offset would fail the gate's sync test anyway)
            my $j = index($$bufref, $sync, $i + 1);
            if ($j < 0 || $j > $last) {
                $skipped += $last + 1 - $i;
                $i = $last + 1;
                last;
            }
            $skipped += $j - $i;
            $i = $j;
            next;
        }
        my $w = substr($$bufref, $i, FRAME_SIZE);
        if (gate($w)) {
            push @$out, $w;
            $i += FRAME_SIZE;
        } else {
            $i++;
            $skipped++;
        }
    }
    return ($i, $skipped);
}

# Byte-wise resync decode of a whole stream. Returns (\@frames, $skipped_bytes,
# $tail_bytes): skipped_bytes counts offsets rejected by the gate; the trailing < 17
# bytes that can never hold a frame are tail_bytes (not counted as skipped).
sub stream_decode {
    my ($buf) = @_;
    my @frames;
    my ($i, $skipped) = _scan(\$buf, 0, 0, \@frames);
    return (\@frames, $skipped, length($buf) - $i);
}

# ══ hex (text lines) ═════════════════════════════════════════════════════════════════
# One frame per line: 34 lowercase hex characters + "\n".
sub hex_encode {
    my ($frames) = @_;
    _need17(@$frames);
    return join('', map { unpack('H*', $_) . "\n" } @$frames);
}

# Parse hex lines -> (\@frames, $bad_lines). Lines are split on "\n"; leading/trailing
# space, tab, CR, VT, FF are stripped; blank lines and lines starting with "#" are
# skipped (not counted); uppercase is accepted; any other line that is not exactly 34
# hex digits counts as a bad line. Frames are returned raw (NOT gated).
sub hex_decode {
    my ($text) = @_;
    my (@frames, $bad);
    $bad = 0;
    for my $line (split /\n/, $text, -1) {
        (my $s = $line) =~ s/\A[ \t\r\x0B\f]+//;
        $s =~ s/[ \t\r\x0B\f]+\z//;
        next if $s eq '' || substr($s, 0, 1) eq '#';
        if ($s =~ /\A[0-9A-Fa-f]{34}\z/) {
            push @frames, pack('H34', $s);
        } else {
            $bad++;
        }
    }
    return (\@frames, $bad);
}

# ══ udp dialect "proto" (ProtoMessage envelope) ══════════════════════════════════════
# Byte-identical to python/dcf/proto.py, go/node/proto.go, rust/src/lib.rs,
# C_SDK/node/dcf_proto.h:
#   [0] msg_type u8 | [1:5] sequence u32 | [5:13] timestamp u64 | [13:17] payload_len u32

# u64 -> 8 big-endian bytes (native integer or Math::BigInt).
sub _u64_be {
    my ($v) = @_;
    if (ref $v) {
        die "timestamp must be u64\n" if $v->is_neg;
        (my $h = $v->as_hex) =~ s/\A0x//;
        die "timestamp must be u64\n" if length($h) > 16;
        return pack('H16', ('0' x (16 - length $h)) . $h);
    }
    die "timestamp must be u64\n" if $v < 0;
    return pack('Q>', $v) if HAS_U64;
    require Math::BigInt;
    return _u64_be(Math::BigInt->new("$v"));
}

# 8 big-endian bytes -> u64 (native integer, or Math::BigInt on 32-bit perls).
sub _u64_from_be {
    my ($b) = @_;
    return unpack('Q>', $b) if HAS_U64;
    require Math::BigInt;
    return Math::BigInt->from_hex(unpack('H16', $b));
}

# Serialize one ProtoMessage (17-byte big-endian header + payload). $seq wraps modulo
# 2^32; $type must be u8 and $ts u64.
sub proto_encode {
    my ($type, $seq, $ts, $payload) = @_;
    $payload = '' unless defined $payload;
    die "msg_type must be u8\n" unless defined $type && $type >= 0 && $type <= 0xFF;
    return pack('C N', $type, $seq & 0xFFFFFFFF) . _u64_be(defined $ts ? $ts : 0)
         . pack('N', length $payload) . $payload;
}

# Parse one ProtoMessage -> ($type, $seq, $ts, $payload). Dies on a short header or a
# payload_len that overruns the datagram. Bytes after payload_len are ignored.
sub proto_decode {
    my ($dg) = @_;
    die "message shorter than 17-byte header\n" if length($dg) < PROTO_HEADER_LEN;
    my ($type, $seq) = unpack('C N', $dg);
    my $ts = _u64_from_be(substr($dg, 5, 8));
    my $plen = unpack('N', substr($dg, 13, 4));
    die "payload length exceeds message size\n" if length($dg) - PROTO_HEADER_LEN < $plen;
    return ($type, $seq, $ts, substr($dg, PROTO_HEADER_LEN, $plen));
}

# One frame as a 34-byte ProtoMessage(MSG_FRAME, seq, ts, len 17, frame); ts defaults
# to 0 (the deterministic default).
sub proto_frame_encode {
    my ($frame, $seq, $ts) = @_;
    _need17($frame);
    return proto_encode(MSG_FRAME, $seq, defined $ts ? $ts : 0, $frame);
}

# The carried frame, or undef unless msg_type == 12 and payload_len == 17 (types 1..11
# are adapter envelopes, not frames on this medium). Not gated — the caller gates.
sub proto_frame_decode {
    my ($dg) = @_;
    my ($type, undef, undef, $payload) = eval { proto_decode($dg) };
    return undef unless defined $type;
    return undef unless $type == MSG_FRAME && length($payload) == FRAME_SIZE;
    return $payload;
}

# ══ udp dialect "bare" (17-B frame / 32-B SuperPack per datagram) ═══════════════════
# Datagrams for a frame sequence: with $pair (default on), consecutive pairs become one
# 32-byte SuperPack and a lone trailing frame goes raw (17 B); without, every frame goes
# raw. Dies if a frame to be paired fails the SuperPack checks.
sub bare_encode {
    my ($frames, $pair) = @_;
    $pair = 1 unless defined $pair;
    return @$frames unless $pair;
    my @out;
    my $i = 0;
    for (; $i + 1 < @$frames; $i += 2) {
        push @out, pack_super($frames->[$i], $frames->[$i + 1]);
    }
    push @out, $frames->[$i] if $i < @$frames;
    return @out;
}

# Frames carried by one bare datagram: a valid 32-byte SuperPack -> its 2 frames; a
# 17-byte datagram -> (it) (not gated); anything else -> ().
sub bare_decode {
    my ($dg) = @_;
    if (length($dg) == SUPER_LEN && is_superpack($dg)) {
        my @f = eval { unpack_super($dg) };
        return @f == 2 ? @f : ();
    }
    return length($dg) == FRAME_SIZE ? ($dg) : ();
}

# ══ l2eth (raw-L2 Ethernet payload; byte-identical to hydramodem/dcf-tools/snake_l2.h) ═
# The canonical zero filler frame (a valid DATA DeModFrame with all application fields
# 0); pairs an odd trailing frame; the receiver discards it using n_frames.
sub l2_filler {
    return encode(version => VERSION, type => 0, seq => 0, src => 0, dst => 0,
                  payload => "\0\0\0\0", ts_us => 0);
}

# Number of DeModFrames that fit one Ethernet payload of the given MTU.
sub l2_capacity {
    my ($mtu) = @_;
    return 0 if $mtu < L2_HDR;
    return int(($mtu - L2_HDR) / SUPER_LEN) * 2;
}

# Batch frames into one Ethernet payload: [n_frames u16 BE][SuperPack * ceil(n/2)],
# the odd tail paired with l2_filler().
sub l2_batch {
    my ($frames) = @_;
    my $n = scalar @$frames;
    die "too many frames for one batch\n" if $n > 0xFFFF;
    my $out = pack('n', $n);
    my $filler = l2_filler();
    for (my $i = 0; $i < $n; $i += 2) {
        $out .= pack_super($frames->[$i], $i + 1 < $n ? $frames->[$i + 1] : $filler);
    }
    return $out;
}

# Split an Ethernet payload back into its frames (bit-exact). Dies on a short/truncated
# batch or a corrupt SuperPack; trailing bytes after the last SuperPack (Ethernet
# minimum-size padding) are ignored.
sub l2_unbatch {
    my ($buf) = @_;
    die "short batch\n" if length($buf) < L2_HDR;
    my $n = unpack('n', $buf);
    my $npairs = int(($n + 1) / 2);
    die "truncated batch\n" if length($buf) < L2_HDR + $npairs * SUPER_LEN;
    my @frames;
    my $off = L2_HDR;
    for (1 .. $npairs) {
        my ($a, $b) = unpack_super(substr($buf, $off, SUPER_LEN));
        $off += SUPER_LEN;
        push @frames, $a;
        push @frames, $b if @frames < $n;
    }
    return @frames;
}

# ══ incremental stream decoder ══════════════════════════════════════════════════════
# feed($chunk) returns the frames completed so far and keeps a carry of at most 16
# bytes, so any chunking yields exactly stream_decode()'s frames and skipped_bytes;
# flush() returns (and clears) the final tail bytes.
package DCF::Medium::StreamScanner;

sub new {
    my ($class) = @_;
    return bless { carry => '', skipped_bytes => 0, frames_out => 0 }, $class;
}

sub feed {
    my ($self, $chunk) = @_;
    my $buf = $self->{carry} . $chunk;
    my @frames;
    my ($i, $skipped) = DCF::Medium::_scan(\$buf, 0, $self->{skipped_bytes}, \@frames);
    $self->{skipped_bytes} = $skipped;
    $self->{carry} = substr($buf, $i);
    $self->{frames_out} += @frames;
    return @frames;
}

sub flush {
    my ($self) = @_;
    my $tail = $self->{carry};
    $self->{carry} = '';
    return $tail;
}

sub skipped_bytes { $_[0]{skipped_bytes} }
sub frames_out    { $_[0]{frames_out} }
sub pending       { length $_[0]{carry} }    # bytes carried (a possible frame prefix), <= 16

1;
