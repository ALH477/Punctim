# SPDX-License-Identifier: LGPL-3.0-only
#
# Certifies DCF::Medium (the Tier-B digital medium codecs: stream, hex, udp_proto,
# udp_bare, l2eth) byte-for-byte against the cross-language golden vectors in
# Documentation/medium_vectors.json. Core-module only (JSON::PP + Test::More).
#   prove -l t/medium.t        (from the perl/ directory)
#   PUNCTIM_VECTORS=/dir prove -l t/medium.t   (read <dir>/medium_vectors.json)

use strict;
use warnings;
use Test::More;
use JSON::PP qw(decode_json);
use FindBin;
use lib "$FindBin::Bin/../lib";
use DCF::Frame qw(crc16 encode FRAME_SIZE);
use DCF::SuperPack qw(unpack_super SUPER_LEN);
use DCF::Medium qw(:all);

my @candidates = (
    (defined $ENV{PUNCTIM_VECTORS} ? ("$ENV{PUNCTIM_VECTORS}/medium_vectors.json") : ()),
    "$FindBin::Bin/../../Documentation/medium_vectors.json",
    "$FindBin::Bin/../../python/MCP/medium_vectors.json",
);
my ($path) = grep { -e $_ } @candidates;
BAIL_OUT("medium_vectors.json not found (run gen_medium_vectors.py)") unless $path;

open my $fh, '<:raw', $path or BAIL_OUT("open $path: $!");
my $mv = do { local $/; decode_json(<$fh>) };
close $fh;

my $an  = $mv->{anchors};
my $fam = $mv->{families};

sub hexes { [ map { unpack('H*', $_) } @_ ] }
sub bins  { map { pack('H*', $_) } @{ $_[0] } }

# u64 -> 16 lowercase hex digits (native integer or Math::BigInt)
sub u64_hex {
    my ($v) = @_;
    if (ref $v) {
        (my $h = $v->as_hex) =~ s/\A0x//;
        return ('0' x (16 - length $h)) . $h;
    }
    return unpack('H16', pack('Q>', $v));
}
# 16 hex digits -> u64 as the module represents it (exact beyond 2^53)
sub u64_from_hex {
    my ($h) = @_;
    return unpack('Q>', pack('H16', $h)) if DCF::Medium::HAS_U64;
    require Math::BigInt;
    return Math::BigInt->from_hex($h);
}

# ── anchors ────────────────────────────────────────────────────────────────────────
is($an->{crc_123456789}, 0x29B1, 'anchor CRC("123456789") = 0x29B1');
is(crc16("123456789"), $an->{crc_123456789}, 'CRC("123456789") matches the anchor');
is(crc16("\0" x 15), $an->{crc_zero15}, 'CRC(0^15) = 0x4EC3 matches the anchor');
is($an->{frame_len}, FRAME_SIZE, 'frame_len 17');
is($an->{proto_header_len}, PROTO_HEADER_LEN, 'proto_header_len 17');
is($an->{msg_frame}, MSG_FRAME, 'MSG_FRAME 12');
is($an->{super_len}, SUPER_LEN, 'super_len 32');
is($an->{l2_hdr}, L2_HDR, 'l2_hdr 2');
is(unpack('H*', l2_filler()), $an->{l2_filler}, 'l2 filler = encode(DATA, 0, 0, 0, 00000000, 0)');

# ── basis frames ───────────────────────────────────────────────────────────────────
for my $b (@{ $mv->{basis} }) {
    my $f = encode(version => 1, type => $b->{type}, seq => $b->{seq}, src => $b->{src},
                   dst => $b->{dst}, payload => pack('H8', $b->{payload}), ts_us => $b->{ts});
    is(unpack('H*', $f), $b->{hex}, "basis $b->{id} encodes byte-identically");
    ok(gate($f), "basis $b->{id} passes the gate");
}

# ── stream ─────────────────────────────────────────────────────────────────────────
for my $c (@{ $fam->{stream}{cases} }) {
    my $in = pack('H*', $c->{input});
    my ($fr, $sk, $tail) = stream_decode($in);
    is_deeply(hexes(@$fr), $c->{frames}, "stream $c->{name}: frames");
    is($sk, $c->{skipped_bytes}, "stream $c->{name}: skipped_bytes");
    is($tail, $c->{tail_bytes}, "stream $c->{name}: tail_bytes");
    ok(!grep({ !gate($_) } @$fr), "stream $c->{name}: every frame passes the gate");

    # chunking-invariance law: every chunk size gives the same frames/skipped/tail
    my $chunk_ok = 1;
    for my $step (1 .. 35) {
        my $sc = DCF::Medium::StreamScanner->new;
        my @got;
        for (my $i = 0; $i < length $in; $i += $step) {
            push @got, $sc->feed(substr($in, $i, $step));
        }
        my $want = join ',', @{ $c->{frames} };
        $chunk_ok = 0 unless join(',', @{ hexes(@got) }) eq $want
            && $sc->skipped_bytes == $c->{skipped_bytes}
            && $sc->frames_out == @{ $c->{frames} }
            && length($sc->flush) == $c->{tail_bytes};
    }
    ok($chunk_ok, "stream $c->{name}: scanner chunk-invariant (1..35-byte chunks)");

    # lossless: decode(encode(frames)) == frames, nothing skipped, no tail
    my $enc = stream_encode([ bins($c->{frames}) ]);
    my ($rf, $rs, $rt) = stream_decode($enc);
    ok(join(',', @{ hexes(@$rf) }) eq join(',', @{ $c->{frames} }) && $rs == 0 && $rt == 0,
       "stream $c->{name}: encode lossless");
    if ($c->{name} =~ /\A(?:aligned_1|aligned_3|all_six)\z/) {
        is(unpack('H*', $enc), $c->{input}, "stream $c->{name}: encode == input");
    }
}

# ── hex ────────────────────────────────────────────────────────────────────────────
for my $c (@{ $fam->{hex}{cases} }) {
    is(hex_encode([ bins($c->{frames}) ]), $c->{text}, "hex $c->{name}: encode");
    my ($fr, $bad) = hex_decode($c->{decode_input});
    is_deeply(hexes(@$fr), $c->{decoded}, "hex $c->{name}: decoded frames");
    is($bad, $c->{bad_lines}, "hex $c->{name}: bad_lines");
    my ($cf, $cb) = hex_decode($c->{text});
    ok(join(',', @{ hexes(@$cf) }) eq join(',', @{ $c->{decoded} }) && $cb == 0,
       "hex $c->{name}: canonical text is a decode fixed point");
}

# ── udp_proto ──────────────────────────────────────────────────────────────────────
{
    my $up = $fam->{udp_proto};
    is($up->{header_len}, PROTO_HEADER_LEN, 'udp_proto header_len 17');
    is($up->{msg_frame}, MSG_FRAME, 'udp_proto msg_frame 12');
    is_deeply($up->{types}, \%MSG_TYPES, 'udp_proto msg_type registry 1..12');

    my $golden;
    for my $c (@{ $up->{cases} }) {
        my $n  = $c->{name};
        my $ts = u64_from_hex($c->{ts_hex});           # exact: ts can exceed 2^53
        is("$ts", "$c->{ts}", "udp_proto $n: ts_hex == ts");
        my $pl = pack('H*', $c->{payload});
        my $dg = proto_encode($c->{type}, $c->{seq}, $ts, $pl);
        is(unpack('H*', $dg), $c->{datagram}, "udp_proto $n: encode");

        my $want = pack('H*', $c->{datagram});
        my ($t, $seq, $dts, $dpl) = proto_decode($want);
        ok($t == $c->{type} && $seq == $c->{seq} && u64_hex($dts) eq $c->{ts_hex} && $dpl eq $pl,
           "udp_proto $n: decode to (type, seq, ts, payload)");

        my $fr = proto_frame_decode($want);
        is(defined $fr ? 1 : 0, $c->{accept_as_frame} ? 1 : 0, "udp_proto $n: accept_as_frame");
        if (defined $fr) {
            is(unpack('H*', $fr), $c->{payload}, "udp_proto $n: carried frame");
            is(unpack('H*', proto_frame_encode($fr, $c->{seq}, $ts)), $c->{datagram},
               "udp_proto $n: frame round-trips to 34 B");
        }
        $golden = $want if $n eq 'go_golden';
    }
    ok(defined $golden, 'udp_proto go_golden case present');
    for my $bad ("\0" x 16, substr($golden, 0, length($golden) - 1)) {
        ok(!eval { proto_decode($bad); 1 }, 'udp_proto rejects a short header / payload_len overrun');
        ok(!defined proto_frame_decode($bad), 'udp_proto frame decode refuses it too');
    }
}

# ── udp_bare ───────────────────────────────────────────────────────────────────────
for my $c (@{ $fam->{udp_bare}{cases} }) {
    my @fs = bins($c->{frames});
    is_deeply(hexes(bare_encode(\@fs)), $c->{datagrams}, "udp_bare $c->{name}: encode");
    my @back = map { bare_decode(pack('H*', $_)) } @{ $c->{datagrams} };
    is_deeply(hexes(@back), $c->{frames}, "udp_bare $c->{name}: decode lossless");
    is_deeply(hexes(bare_encode(\@fs, 0)), $c->{frames}, "udp_bare $c->{name}: pair=0 sends raw");
    if ($c->{name} eq 'frames_2') {
        my $tam = pack('H*', $c->{datagrams}[0]);
        substr($tam, 7, 1) = chr(ord(substr($tam, 7, 1)) ^ 1);
        is(scalar(my @r = bare_decode($tam)), 0, 'udp_bare rejects a tampered SuperPack');
    }
}
is(scalar(my @r33 = bare_decode("\0" x 33)), 0, 'udp_bare rejects a 33-byte datagram');
is(scalar(my @r16 = bare_decode("\0" x 16)), 0, 'udp_bare rejects a 16-byte datagram');

# ── l2eth ──────────────────────────────────────────────────────────────────────────
{
    my $l2 = $fam->{l2eth};
    is($l2->{hdr}, L2_HDR, 'l2eth hdr 2');
    is(unpack('H*', l2_filler()), $l2->{filler}, 'l2eth filler');
    ok(gate(l2_filler()), 'l2eth filler passes the gate');
    for my $c (@{ $l2->{cases} }) {
        my @fs = bins($c->{frames});
        is(unpack('H*', l2_batch(\@fs)), $c->{payload}, "l2eth $c->{name}: batch");
        my $pl = pack('H*', $c->{payload});
        is_deeply(hexes(l2_unbatch($pl)), $c->{frames}, "l2eth $c->{name}: unbatch lossless");
        is_deeply(hexes(l2_unbatch($pl . ("\0" x 7))), $c->{frames},
                  "l2eth $c->{name}: min-size padding ignored");
        ok(!eval { l2_unbatch(substr($pl, 0, length($pl) - 1)); 1 },
           "l2eth $c->{name}: truncated batch rejected");
        if (@fs % 2) {
            my (undef, $b) = unpack_super(substr($pl, -SUPER_LEN));
            is(unpack('H*', $b), $l2->{filler}, "l2eth $c->{name}: odd tail paired with the filler");
        }
    }
    ok(!eval { l2_unbatch("\0"); 1 }, 'l2eth short batch rejected');
    is(l2_capacity(1500), 92, 'l2_capacity(1500) = 92');
    is(l2_capacity(9000), 562, 'l2_capacity(9000) = 562');
}

done_testing();
