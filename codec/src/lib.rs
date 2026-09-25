// SPDX-License-Identifier: LGPL-3.0-only
#[path = "../frame.rs"]
mod frame;

pub use frame::{crc16_ccitt, Frame, FrameError, FrameType, BROADCAST, FRAME_SIZE, SYNC_BYTE};

pub mod audio;
pub mod fec;
pub mod game;
pub mod hydrapack;
pub mod medium;
pub mod mesh;
pub mod modulation;
pub mod monitor;
pub mod pipe;
pub mod pipemulti;
pub mod qkd;
pub mod snake;
pub mod sstv;
pub mod superpack;
pub mod text;