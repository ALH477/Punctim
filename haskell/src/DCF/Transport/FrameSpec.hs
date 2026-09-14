-- SPDX-License-Identifier: LGPL-3.0-only
{-|
Module      : DCF.Transport.FrameSpec
Description : DeMoD 17-byte transport frame codec — Haskell reference implementation
Copyright  : (c) DeMoD LLC
License    : LGPL-3.0
Maintainer  : info@demodllc.example

The canonical Haskell implementation of the DeMoD wire quantum.

Wire layout (17 bytes = 136 bits, all multi-byte fields big-endian):

  Byte   Field        Notes
  ─────  ───────────  ──────────────────────────────────────────────────────
   0     sync         Fixed 0xD3 — first validity gate
   1     flags        [7:4] version (4-bit) | [3:0] frame type (4-bit)
   2-3   seq          Big-endian u16, rolling counter
   4-5   src_id       Big-endian u16, source node
   6-7   dst_id       Big-endian u16, 0xFFFF = broadcast
   8-11  payload      4 raw application bytes
  12-14  timestamp    24-bit big-endian µs offset, wraps ~16.7 s
  15-16  crc16        CRC-CCITT(poly=0x1021, init=0xFFFF) over bytes [0..14]

Cross-language parity:
  C        transport/dcf_frame.h  dcf_frame_encode / dcf_frame_decode
  Haskell  DCF.Transport.Frame    encodeFrame / decodeFrame  ← this file
  Lisp     punctim.lisp         encode-dcf-frame / decode-dcf-frame
  Rust     dcf/rust/src/frame.rs  Frame::encode / Frame::decode
  Python   wirelab_core.py        encode / decode

-}
module DCF.Transport.FrameSpec
  ( -- * Types
    FrameType(..)
  , Frame(..)
  , FrameError(..)
    -- * Constants
  , frameSize
  , syncByte
  , broadcast
    -- * Codec
  , encodeFrame
  , decodeFrame
  , isValidFrame
    -- * CRC
  , crc16ccitt
    -- * Reference test vector
  , exampleFrame
  , exampleFrameBytes
  ) where

import Prelude hiding (seq)
import Data.Bits (Bits, shiftL, shiftR, (.|.), (.&.), xor)
import Data.Word (Word8, Word16, Word32)

-- ── Constants ──────────────────────────────────────────────────────────────

frameSize :: Int
frameSize = 17

syncByte :: Word8
syncByte = 0xD3

broadcast :: Word16
broadcast = 0xFFFF

crcCover :: Int
crcCover = 15

-- ── Frame type ─────────────────────────────────────────────────────────────

data FrameType = Data | Ack | Beacon | Ctrl
  deriving (Eq, Show, Read, Enum, Bounded)

frameTypeToNibble :: FrameType -> Word8
frameTypeToNibble Data   = 0
frameTypeToNibble Ack    = 1
frameTypeToNibble Beacon = 2
frameTypeToNibble Ctrl   = 3

nibbleToFrameType :: Word8 -> Maybe FrameType
nibbleToFrameType n = case n .&. 0x0F of
  0 -> Just Data
  1 -> Just Ack
  2 -> Just Beacon
  3 -> Just Ctrl
  _ -> Nothing

-- ── Frame (decoded, host-order fields) ─────────────────────────────────────

data Frame = Frame
  { version      :: !Word8    -- ^ 4-bit, 0–15
  , frameType    :: !FrameType
  , seq          :: !Word16   -- ^ rolling counter
  , srcId        :: !Word16   -- ^ source node
  , dstId        :: !Word16   -- ^ dest node (broadcast = 0xFFFF)
  , payload      :: !(Word8, Word8, Word8, Word8)
  , timestampUs  :: !Word32   -- ^ 24-bit µs (top byte always 0)
  } deriving (Eq, Show)

-- ── Decode error ───────────────────────────────────────────────────────────

data FrameError
  = BadLength
  | BadSync
  | BadCrc
  | UnknownType !Word8
  deriving (Eq, Show)

-- ── CRC-CCITT ──────────────────────────────────────────────────────────────
--
-- Poly 0x1021, init 0xFFFF.
-- Identical algorithm in C (dcf_crc16), Rust (crc16_ccitt), Python (crc16_ccitt).
-- All four implementations must return the same value for the same input.

crc16ccitt :: [Word8] -> Word16
crc16ccitt = go 0xFFFF
  where
    go crc []     = crc
    go crc (b:bs) = go (step8 (crc `xor` (fromIntegral b `shiftL` 8))) bs

step8 :: Word16 -> Word16
step8 crc = foldl (\c _ -> if c .&. 0x8000 /= 0
                            then (c `shiftL` 1) `xor` 0x1021
                            else c `shiftL` 1) crc [(0::Int)..7]

-- ── Encode ────────────────────────────────────────────────────────────────
--
-- Serialise a Frame into exactly 17 bytes (big-endian).
-- CRC is computed and appended automatically.

encodeFrame :: Frame -> [Word8]
encodeFrame f = take frameSize $ buf ++ [hi crc, lo crc]
  where
    buf = [ syncByte
          , ((version f .&. 0x0F) `shiftL` 4) .|. frameTypeToNibble (frameType f)
          , hi (seq f), lo (seq f)
          , hi (srcId f), lo (srcId f)
          , hi (dstId f), lo (dstId f)
          , a, b, c, d
          , fromIntegral ((timestampUs f `shiftR` 16) .&. 0xFF)
          , fromIntegral ((timestampUs f `shiftR`  8) .&. 0xFF)
          , fromIntegral ( timestampUs f               .&. 0xFF)
          ]
    (a, b, c, d) = payload f
    crc = crc16ccitt (take crcCover buf)

hi, lo :: (Integral a, Bits a, Num b) => a -> b
hi w = fromIntegral (w `shiftR` 8)
lo w = fromIntegral (w .&. 0xFF)

-- ── Decode ────────────────────────────────────────────────────────────────
--
-- Parse a 17-byte wire buffer.
-- Returns Left on invalid sync, CRC mismatch, or unknown frame type.

decodeFrame :: [Word8] -> Either FrameError Frame
decodeFrame buf
  | length buf /= frameSize = Left BadLength
  | head buf /= syncByte    = Left BadSync
  | crc16ccitt (take crcCover buf) /= storedCrc = Left BadCrc
  | otherwise = case nibbleToFrameType (buf !! 1 .&. 0x0F) of
      Nothing -> Left (UnknownType (buf !! 1 .&. 0x0F))
      Just ft -> Right Frame
        { version     = (buf !! 1 `shiftR` 4) .&. 0x0F
        , frameType   = ft
        , seq         = makeW16 (buf !! 2) (buf !! 3)
        , srcId       = makeW16 (buf !! 4) (buf !! 5)
        , dstId       = makeW16 (buf !! 6) (buf !! 7)
        , payload     = (buf !! 8, buf !! 9, buf !! 10, buf !! 11)
        , timestampUs = makeW24 (buf !! 12) (buf !! 13) (buf !! 14)
        }
  where
    storedCrc = makeW16 (buf !! 15) (buf !! 16)

makeW16 :: Word8 -> Word8 -> Word16
makeW16 hi8 lo8 = fromIntegral hi8 `shiftL` 8 .|. fromIntegral lo8

makeW24 :: Word8 -> Word8 -> Word8 -> Word32
makeW24 a b c = fromIntegral a `shiftL` 16 .|. fromIntegral b `shiftL` 8 .|. fromIntegral c

-- ── Validate (non-destructive) ─────────────────────────────────────────────

isValidFrame :: [Word8] -> Bool
isValidFrame buf
  | length buf /= frameSize = False
  | head buf /= syncByte    = False
  | otherwise = crc16ccitt (take crcCover buf) == makeW16 (buf !! 15) (buf !! 16)

-- ── Reference test vector ─────────────────────────────────────────────────
--
-- Matches the C dcf_frame.h reference, the Rust Frame::tests::reference_frame,
-- and the Python wirelab_core anchors.
--   version=1, FData, seq=1, src=1, dst=0xFFFF, payload=0xDEADBEEF, ts=0

exampleFrame :: Frame
exampleFrame = Frame
  { version     = 1
  , frameType   = Data
  , seq         = 1
  , srcId       = 1
  , dstId       = broadcast
  , payload     = (0xDE, 0xAD, 0xBE, 0xEF)
  , timestampUs = 0
  }

-- | The encoded bytes of 'exampleFrame', for cross-language pinning tests.
--   CRC computed as 0x42DD over bytes [0..14].
exampleFrameBytes :: [Word8]
exampleFrameBytes = encodeFrame exampleFrame