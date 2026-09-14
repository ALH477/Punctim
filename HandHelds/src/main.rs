// SPDX-License-Identifier: LGPL-3.0-only
// src/main.rs — DCF handheld framework (Nintendo DSi / Sony PSP crossplay)
// DeMoD LLC | LGPL-3.0-only
#![no_std]
#![no_main]
#![feature(alloc_error_handler, core_intrinsics)]

extern crate alloc;

// Certified DeModFrame + SuperPack wire quantum, sourced from the portable,
// host-tested core crate (see `core/`; also provides proto/routing/streamdb,
// the verified replacements for this file's inline envelope/Dijkstra/trie).
use dcf_handheld_core::{streamdb, wire};
use alloc::boxed::Box;
use alloc::string::{String, ToString};
use alloc::vec::Vec;
use alloc::vec;
use core::ffi::{c_char, c_int, c_long, c_uint, c_void, c_uchar};
use core::panic::PanicInfo;
use byteorder::{ByteOrder, LittleEndian};
use crc::Crc;
use heapless::consts::*;
use heapless::FnvIndexMap;
use lru::LruCache;
use core::num::NonZeroUsize;

// Platform-specific imports and defines
#[cfg(feature = "dsi")]
use libnds::prelude::*;
#[cfg(feature = "dsi")]
use libnds::console as nds_console;
#[cfg(feature = "dsi")]
use libnds::keyboard as nds_keyboard;
#[cfg(feature = "dsi")]
use libnds::bg;
#[cfg(feature = "dsi")]
use libnds::graphics;
#[cfg(feature = "dsi")]
use libnds::dma;
#[cfg(feature = "dsi")]
use libnds::touch;
#[cfg(feature = "dsi")]
use libnds::keys;
#[cfg(feature = "dsi")]
use libnds::power;

#[cfg(feature = "psp")]
use psp::sys::*;

// Common FFI for storage
extern "C" {
    fn fatInitDefault() -> c_int;  // Shared for DSi/SD; PSP uses sceIo
}

// DSi FFI for WiFi (from BlocksDS)
#[cfg(feature = "dsi")]
extern "C" {
    fn dswifi_init(mode: c_int) -> c_int;
    fn dswifi_connect_ap(ssid: *const c_char, wepkey: *const c_char) -> c_int;
    fn dswifi_bind_udp(port: c_uint) -> c_int;
    fn dswifi_send_udp(to: *const c_char, port: c_uint, data: *const c_void, len: c_uint) -> c_int;
    fn dswifi_recv_udp(buffer: *mut c_void, len: c_uint, from: *mut c_char, from_len: c_uint, port: *mut c_uint) -> c_int;
}

// PSP FFI for WiFi
#[cfg(feature = "psp")]
extern "C" {
    fn sceNetAdhocPdpCreate(mac: *const c_uchar, port: u16, bufsize: c_uint, unk: c_int) -> c_int;
    fn sceNetAdhocPdpSend(id: c_int, dmac: *const c_uchar, dport: u16, data: *const c_void, len: c_int, timeout: c_int, nonblock: c_int) -> c_int;
    fn sceNetAdhocPdpRecv(id: c_int, smac: *mut c_uchar, sport: *mut u16, data: *mut c_void, len: *mut c_int, timeout: c_int, nonblock: c_int) -> c_int;
}

// Constants
const WIFI_MODE_ADHOC: c_int = 1;
const SEEK_SET: c_int = 0;
const MAX_MSG_LEN: usize = 64;  // Reduced for minimal packets
const MAX_FILE_SIZE: usize = 4 * 1024;  // 4KB for images/assets
const MAX_PEERS: usize = 4;
const MAX_ROOMS: usize = 4;
const CACHE_SIZE: usize = 8;  // 8*512B=4KB cache
const RETRY_LIMIT: usize = 3;
const TIMEOUT_MS: u32 = 500;  // For ACKs

// StreamDB Constants
const DEFAULT_PAGE_RAW_SIZE: u64 = 512;  // Smaller for handhelds
const DEFAULT_PAGE_HEADER_SIZE: u64 = 8;  // crc (4) + len (4)
const CRC_HASHER: Crc<u32> = Crc::<u32>::new(&crc::CRC_32_ISO_HDLC);

// Allocator and Panic Handler
#[global_allocator]
static ALLOCATOR: libnds::allocator::DsAllocator = libnds::allocator::DsAllocator;

#[alloc_error_handler]
fn alloc_error(_layout: alloc::alloc::Layout) -> ! {
    unsafe { core::intrinsics::abort() }
}

#[panic_handler]
fn panic(_info: &PanicInfo) -> ! {
    unsafe { core::intrinsics::abort() }
}

// Fake UUID
static mut UUID_COUNTER: u128 = 0;
fn fake_uuid() -> u128 {
    unsafe {
        UUID_COUNTER += 1;
        UUID_COUNTER
    }
}

// Abstract Traits for Cross-Compatibility
pub trait Network {
    fn init(&mut self) -> Result<(), ()>;
    fn bind_udp(&mut self, port: u16) -> Result<c_int, ()>;
    fn send_udp(&self, to: &str, port: u16, data: &[u8]) -> Result<(), ()>;
    fn recv_udp(&self, buf: &mut [u8]) -> Result<(usize, heapless::String<U32>), ()>;
    fn discover_peers(&self, port: u16) -> heapless::Vec<heapless::String<U32>, MAX_PEERS>;
}

pub trait Storage {
    fn open(&mut self, path: &str, mode: &str) -> Result<c_int, ()>;
    fn close(&self, handle: c_int) -> Result<(), ()>;
    fn read(&self, handle: c_int, buf: &mut [u8], offset: u64) -> Result<usize, ()>;
    fn write(&mut self, handle: c_int, buf: &[u8], offset: u64) -> Result<usize, ()>;
}

pub trait Gui {
    fn init(&mut self) -> Result<(), ()>;
    fn clear_console(&self);
    fn print(&self, text: &str);
    fn poll_input(&mut self) -> Option<char>;
    fn display_image(&mut self, filename: &str) -> Result<(), ()>;
    fn wait_vblank(&self);
    fn is_lid_closed(&self) -> bool;
}

pub trait Game {
    fn init(&mut self);
    fn handle_message(&mut self, msg: &DcfMessage) -> Option<heapless::String<U32>>;
    fn update_gui(&mut self, gui: &mut dyn Gui);
    fn process_input(&mut self, input: char) -> Option<DcfMessage>;
}

// DSi Implementations
#[cfg(feature = "dsi")]
struct DsiNetwork;

#[cfg(feature = "dsi")]
impl Network for DsiNetwork {
    fn init(&mut self) -> Result<(), ()> {
        unsafe {
            if fatInitDefault() != 0 {
                return Err(());
            }
            if dswifi_init(WIFI_MODE_ADHOC) != 0 {
                return Err(());
            }
            if dswifi_connect_ap(cstr("DCFChat").as_ptr(), cstr("").as_ptr()) != 0 {
                return Err(());
            }
        }
        Ok(())
    }

    fn bind_udp(&mut self, port: u16) -> Result<c_int, ()> {
        let socket = unsafe { dswifi_bind_udp(port as c_uint) };
        if socket < 0 {
            Err(())
        } else {
            Ok(socket)
        }
    }

    fn send_udp(&self, to: &str, port: u16, data: &[u8]) -> Result<(), ()> {
        let ret = unsafe { dswifi_send_udp(cstr(to).as_ptr(), port as c_uint, data.as_ptr() as *const c_void, data.len() as c_uint) };
        if ret != 0 {
            Err(())
        } else {
            Ok(())
        }
    }

    fn recv_udp(&self, buf: &mut [u8]) -> Result<(usize, heapless::String<U32>), ()> {
        let mut from = [0u8; 16];
        let mut rport: c_uint = 0;
        let len = unsafe { dswifi_recv_udp(buf.as_mut_ptr() as *mut c_void, buf.len() as c_uint, from.as_mut_ptr() as *mut c_char, 16, &mut rport) } as usize;
        if len == 0 {
            Err(())
        } else {
            Ok((len, cstr_to_heapless(&from)))
        }
    }

    fn discover_peers(&self, port: u16) -> heapless::Vec<heapless::String<U32>, MAX_PEERS> {
        let mut peers = heapless::Vec::new();
        let hello = "HELLO".as_bytes();
        self.send_udp("255.255.255.255", port, hello).ok();
        let mut buf = [0u8; MAX_MSG_LEN];
        for _ in 0..10 {
            if let Ok((len, from)) = self.recv_udp(&mut buf) {
                if &buf[0..len] == b"HELLO_ACK" && !peers.contains(&from) {
                    peers.push(from).ok();
                }
            }
            self.wait_vblank();
        }
        peers
    }
}

#[cfg(feature = "dsi")]
#[derive(Clone, Copy)]
struct DsiStorage;

#[cfg(feature = "dsi")]
impl Storage for DsiStorage {
    fn open(&mut self, path: &str, mode: &str) -> Result<c_int, ()> {
        let handle = unsafe { fopen(cstr(path).as_ptr(), cstr(mode).as_ptr()) } as c_int;
        if handle < 0 {
            Err(())
        } else {
            Ok(handle)
        }
    }

    fn close(&self, handle: c_int) -> Result<(), ()> {
        let ret = unsafe { fclose(handle as *mut c_void) };
        if ret != 0 {
            Err(())
        } else {
            Ok(())
        }
    }

    fn read(&self, handle: c_int, buf: &mut [u8], offset: u64) -> Result<usize, ()> {
        let seek_ret = unsafe { fseek(handle as *mut c_void, offset as c_long, SEEK_SET) };
        if seek_ret != 0 {
            Err(())
        } else {
            let read = unsafe { fread(buf.as_mut_ptr() as *mut c_void, 1, buf.len() as c_uint, handle as *mut c_void) } as usize;
            if read != buf.len() {
                Err(())
            } else {
                Ok(read)
            }
        }
    }

    fn write(&mut self, handle: c_int, buf: &[u8], offset: u64) -> Result<usize, ()> {
        let seek_ret = unsafe { fseek(handle as *mut c_void, offset as c_long, SEEK_SET) };
        if seek_ret != 0 {
            Err(())
        } else {
            let written = unsafe { fwrite(buf.as_ptr() as *const c_void, 1, buf.len() as c_uint, handle as *mut c_void) } as usize;
            let flush_ret = unsafe { fflush(handle as *mut c_void) };
            if flush_ret != 0 || written != buf.len() {
                Err(())
            } else {
                Ok(written)
            }
        }
    }
}

#[cfg(feature = "dsi")]
struct DsiGui {
    keyboard: nds_keyboard::Keyboard,
    console: nds_console::Console,
    touch_pos: touch::TouchPosition,
    image_sprite: graphics::Sprite,
}

#[cfg(feature = "dsi")]
impl Gui for DsiGui {
    fn init(&mut self) -> Result<(), ()> {
        self.keyboard = nds_keyboard::keyboardInit(1);
        self.console = nds_console::consoleInit(0, 2, bg::BgType_Text4bpp, bg::BgSize_T_256x256, 31, 0, true, true);
        unsafe { bg::bgInit(3, bg::BgType_Bmp16, bg::BgSize_B16_256x256, 0, false); }
        unsafe { graphics::oamInit(0, graphics::SpriteColorFormat_Color256, false); }
        self.image_sprite = graphics::Sprite::new(graphics::SpriteSize_32x32, graphics::SpriteColorFormat_Color256, 0);
        unsafe { nds_keyboard::keyboardShow(); }
        Ok(())
    }

    fn clear_console(&self) {
        unsafe { nds_console::consoleClear(); }
    }

    fn print(&self, text: &str) {
        unsafe { nds_console::iprintf("%s\n", cstr(text).as_ptr()); }
    }

    fn poll_input(&mut self) -> Option<char> {
        keys::scanKeys();
        touch::touchRead(&mut self.touch_pos);
        if keys::keysDown() & keys::KEY_TOUCH != 0 {
            self.keyboard.get_key(self.touch_pos.px as i32, self.touch_pos.py as i32)
        } else {
            None
        }
    }

    fn display_image(&mut self, filename: &str) -> Result<(), ()> {
        let file_handle = unsafe { fopen(cstr(filename).as_ptr(), cstr("rb").as_ptr()) };
        if file_handle.is_null() {
            return Err(());
        }
        let mut buf = [0u8; 32*32*2]; // 16bpp BMP
        let size = unsafe { fread(buf.as_mut_ptr() as *mut c_void, 1, buf.len() as c_uint, file_handle) as usize };
        unsafe { fclose(file_handle); }
        if size != buf.len() {
            return Err(());
        }
        unsafe { dma::dmaCopyWords(3, buf.as_ptr() as *const c_void, graphics::VRAM_A as *mut c_void, buf.len() as u32); }
        self.image_sprite.setGfx(graphics::VRAM_A);
        self.image_sprite.setPos(100, 100);
        unsafe {
            graphics::oamSet(
                &mut self.image_sprite.oam,
                0,
                100,
                100,
                0,
                0,
                graphics::SpriteSize_32x32,
                graphics::SpriteColorFormat_Color256,
                graphics::VRAM_A,
                -1,
                false,
                false,
                false,
                false,
                false,
            );
            graphics::oamUpdate(&mut self.image_sprite.oam);
        }
        Ok(())
    }

    fn wait_vblank(&self) {
        swiWaitForVBlank();
    }

    fn is_lid_closed(&self) -> bool {
        power::isLidClosed()
    }
}

// PSP Implementations
#[cfg(feature = "psp")]
struct PspNetwork {
    pdp_id: c_int,
}

#[cfg(feature = "psp")]
impl Network for PspNetwork {
    fn init(&mut self) -> Result<(), ()> {
        unsafe {
            if sceNetInit(128 * 1024, 42, 4 * 1024, 42, 4 * 1024) < 0 {
                return Err(());
            }
            if sceNetAdhocInit() < 0 {
                return Err(());
            }
            if sceNetAdhocctlInit(128 * 1024, 42) < 0 {
                return Err(());
            }
            let group_name = cstr("DCFChat");
            if sceNetAdhocctlConnect(group_name.as_ptr()) < 0 {
                return Err(());
            }
        }
        Ok(())
    }

    fn bind_udp(&mut self, port: u16) -> Result<c_int, ()> {
        let mac = [0u8; 6]; // Broadcast or known MAC
        let pdp_id = unsafe { sceNetAdhocPdpCreate(mac.as_ptr(), port, 1024, 0) };
        if pdp_id < 0 {
            Err(())
        } else {
            self.pdp_id = pdp_id;
            Ok(pdp_id)
        }
    }

    fn send_udp(&self, to: &str, port: u16, data: &[u8]) -> Result<(), ()> {
        let to_mac = str_to_mac(to);
        let ret = unsafe {
            sceNetAdhocPdpSend(
                self.pdp_id,
                to_mac.as_ptr(),
                port,
                data.as_ptr() as *const c_void,
                data.len() as c_int,
                0,
                0,
            )
        };
        if ret < 0 {
            Err(())
        } else {
            Ok(())
        }
    }

    fn recv_udp(&self, buf: &mut [u8]) -> Result<(usize, heapless::String<U32>), ()> {
        let mut from_mac = [0u8; 6];
        let mut rport: u16 = 0;
        let mut len = buf.len() as c_int;
        let ret = unsafe {
            sceNetAdhocPdpRecv(
                self.pdp_id,
                from_mac.as_mut_ptr(),
                &mut rport,
                buf.as_mut_ptr() as *mut c_void,
                &mut len,
                0,
                0,
            )
        };
        if ret < 0 || len <= 0 {
            Err(())
        } else {
            Ok((len as usize, mac_to_str(&from_mac)))
        }
    }

    fn discover_peers(&self, port: u16) -> heapless::Vec<heapless::String<U32>, MAX_PEERS> {
        let mut peers = heapless::Vec::new();
        let hello = "HELLO".as_bytes();
        self.send_udp("broadcast", port, hello).ok();
        let mut buf = [0u8; MAX_MSG_LEN];
        for _ in 0..10 {
            if let Ok((len, from)) = self.recv_udp(&mut buf) {
                if &buf[0..len] == b"HELLO_ACK" && !peers.contains(&from) {
                    peers.push(from).ok();
                }
            }
            self.wait_vblank();
        }
        peers
    }
}

#[cfg(feature = "psp")]
#[derive(Clone, Copy)]
struct PspStorage;

#[cfg(feature = "psp")]
impl Storage for PspStorage {
    fn open(&mut self, path: &str, mode: &str) -> Result<c_int, ()> {
        let flags = if mode == "rb+" {
            PSP_O_RDWR
        } else if mode == "wb+" {
            PSP_O_WRONLY | PSP_O_CREAT | PSP_O_TRUNC
        } else {
            PSP_O_RDONLY
        };
        let handle = unsafe { sceIoOpen(cstr(path).as_ptr(), flags, 0777) };
        if handle < 0 {
            Err(())
        } else {
            Ok(handle)
        }
    }

    fn close(&self, handle: c_int) -> Result<(), ()> {
        let ret = unsafe { sceIoClose(handle) };
        if ret < 0 {
            Err(())
        } else {
            Ok(())
        }
    }

    fn read(&self, handle: c_int, buf: &mut [u8], offset: u64) -> Result<usize, ()> {
        let seek_ret = unsafe { sceIoLseek(handle, offset as i64, SEEK_SET) };
        if seek_ret < 0 {
            Err(())
        } else {
            let read = unsafe { sceIoRead(handle, buf.as_mut_ptr() as *mut c_void, buf.len() as c_int) } as usize;
            if read != buf.len() {
                Err(())
            } else {
                Ok(read)
            }
        }
    }

    fn write(&mut self, handle: c_int, buf: &[u8], offset: u64) -> Result<usize, ()> {
        let seek_ret = unsafe { sceIoLseek(handle, offset as i64, SEEK_SET) };
        if seek_ret < 0 {
            Err(())
        } else {
            let written = unsafe { sceIoWrite(handle, buf.as_ptr() as *const c_void, buf.len() as c_int) } as usize;
            if written != buf.len() {
                Err(())
            } else {
                Ok(written)
            }
        }
    }
}

#[cfg(feature = "psp")]
struct PspGui {
    input_buffer: heapless::String<U32>,
}

#[cfg(feature = "psp")]
impl PspGui {
    fn new() -> Self {
        Self {
            input_buffer: heapless::String::new(),
        }
    }
}

#[cfg(feature = "psp")]
impl Gui for PspGui {
    fn init(&mut self) -> Result<(), ()> {
        unsafe {
            sceGuInit();
            sceGuStart(GU_DIRECT, &mut [] as *mut u32);
            sceGuDrawBuffer(PSP_DISPLAY_PIXEL_FORMAT_8888, 0 as *mut c_void, 512);
            sceGuDispBuffer(480, 272, 0 as *mut c_void, 512);
            sceGuDisplay(true);
            sceGuFinish();
            pspDebugScreenInit();
            sceCtrlSetSamplingCycle(0);
            sceCtrlSetSamplingMode(PSP_CTRL_MODE_ANALOG);
        }
        Ok(())
    }

    fn clear_console(&self) {
        unsafe { pspDebugScreenClear(); }
    }

    fn print(&self, text: &str) {
        unsafe { pspDebugScreenPrintf(cstr("%s\n").as_ptr(), cstr(text).as_ptr()); }
    }

    fn poll_input(&mut self) -> Option<char> {
        let mut pad = SceCtrlData {
            time_stamp: 0,
            buttons: 0,
            lx: 0,
            ly: 0,
            rsrv: [0; 6],
        };
        unsafe { sceCtrlReadBufferPositive(&mut pad, 1); }
        if pad.buttons & PSP_CTRL_CROSS != 0 {
            self.input_buffer.push('x').ok();
            Some('x')
        } else if pad.buttons & PSP_CTRL_CIRCLE != 0 {
            self.input_buffer.push('o').ok();
            Some('o')
        } else if pad.buttons & PSP_CTRL_SQUARE != 0 {
            self.input_buffer.push('s').ok();
            Some('s')
        } else if pad.buttons & PSP_CTRL_TRIANGLE != 0 {
            self.input_buffer.push('t').ok();
            Some('t')
        } else if pad.buttons & PSP_CTRL_START != 0 {
            // Trigger send
            let input = self.input_buffer.clone();
            self.input_buffer.clear();
            if !input.is_empty() {
                input.chars().next()
            } else {
                None
            }
        } else {
            None
        }
    }

    fn display_image(&mut self, filename: &str) -> Result<(), ()> {
        let file_handle = unsafe { sceIoOpen(cstr(filename).as_ptr(), PSP_O_RDONLY, 0777) };
        if file_handle < 0 {
            return Err(());
        }
        let mut buf = [0u8; 32 * 32 * 4]; // 32x32 RGBA
        let size = unsafe { sceIoRead(file_handle, buf.as_mut_ptr() as *mut c_void, buf.len() as c_int) } as usize;
        unsafe { sceIoClose(file_handle); }
        if size != buf.len() {
            return Err(());
        }
        unsafe {
            sceGuStart(GU_DIRECT, &mut [] as *mut u32);
            sceGuTexImage(0, 32, 32, 32, buf.as_ptr() as *const c_void);
            sceGuTexFunc(GU_TFX_REPLACE, GU_RGBA);
            sceGuTexFilter(GU_NEAREST, GU_NEAREST);
            sceGuDrawArray(GU_SPRITES, GU_TEXTURE_32BITF | GU_VERTEX_32BITF | GU_TRANSFORM_2D, 2, 0 as *const c_void, 0 as *const c_void);
            sceGuFinish();
        }
        Ok(())
    }

    fn wait_vblank(&self) {
        unsafe { sceDisplayWaitVblankStart(); }
    }

    fn is_lid_closed(&self) -> bool {
        false // PSP has no lid
    }
}

// StreamDB Types (page constants are defined once near the top of the file).
#[derive(Clone, Copy)]
pub struct Config {
    page_size: u64,
    page_header_size: u64,
    page_cache_size: usize,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            page_size: DEFAULT_PAGE_RAW_SIZE,
            page_header_size: DEFAULT_PAGE_HEADER_SIZE,
            page_cache_size: CACHE_SIZE,
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct PageHeader {
    crc: u32,
    data_length: u32,
}

// The reverse-trie path index now lives in the host-tested core crate
// (`dcf_handheld_core::streamdb::PathTrie`). The original inline `ReverseTrieNode`
// was an infinitely-sized recursive type and could not compile.

#[derive(Clone, Debug)]
struct Document {
    id: u128,
    data: heapless::Vec<u8, MAX_FILE_SIZE>,
}

pub struct LibfatBackend<S: Storage> {
    storage: S,
    config: Config,
    documents: FnvIndexMap<u128, Document, U4>,
    path_trie: streamdb::PathTrie,
    page_cache: LruCache<u64, heapless::Vec<u8, MAX_FILE_SIZE>>,
    quick_mode: bool,
}

impl<S: Storage> LibfatBackend<S> {
    pub fn new(storage: S, config: Config) -> Self {
        Self {
            storage,
            config,
            documents: FnvIndexMap::new(),
            path_trie: streamdb::PathTrie::new(),
            page_cache: LruCache::new(NonZeroUsize::new(config.page_cache_size).unwrap()),
            quick_mode: false,
        }
    }

    fn get_document_id_by_path(&self, path: &str) -> Result<u128, ()> {
        self.path_trie.get(path).ok_or(())
    }
}

pub trait Database {
    fn write_document(&mut self, path: &str, data: &[u8]) -> Result<u128, ()>;
    fn get(&self, path: &str) -> Result<heapless::Vec<u8, MAX_FILE_SIZE>, ()>;
    fn delete(&mut self, path: &str) -> Result<(), ()>;
    fn search(&self, prefix: &str) -> Result<heapless::Vec<heapless::String<U32>, U8>, ()>;
    fn flush(&self) -> Result<(), ()>;
    fn set_quick_mode(&mut self, enabled: bool);
}

impl<S: Storage> Database for LibfatBackend<S> {
    fn write_document(&mut self, path: &str, data: &[u8]) -> Result<u128, ()> {
        if data.len() > MAX_FILE_SIZE {
            return Err(());
        }
        let id = fake_uuid();
        let mut doc_data = heapless::Vec::new();
        doc_data.extend_from_slice(data).ok();
        let doc = Document { id, data: doc_data };
        let mut buf = heapless::Vec::<u8, MAX_FILE_SIZE>::new();
        LittleEndian::write_u128(&mut buf, id).ok();
        LittleEndian::write_u32(&mut buf, data.len() as u32).ok();
        buf.extend_from_slice(data).ok();
        let crc = CRC_HASHER.checksum(&buf);
        let header = PageHeader {
            crc,
            data_length: data.len() as u32,
        };
        let mut final_buf = heapless::Vec::<u8, MAX_FILE_SIZE>::new();
        LittleEndian::write_u32(&mut final_buf, header.crc).ok();
        LittleEndian::write_u32(&mut final_buf, header.data_length).ok();
        final_buf.extend_from_slice(&buf).ok();
        let handle = self.storage.open("dcf.streamdb", "wb+")?;
        self.storage.write(handle, &final_buf, 0)?;
        self.storage.close(handle)?;
        self.documents.insert(id, doc).ok();
        if !self.path_trie.insert(path, id) {
            return Err(());
        }
        Ok(id)
    }

    fn get(&self, path: &str) -> Result<heapless::Vec<u8, MAX_FILE_SIZE>, ()> {
        let id = self.get_document_id_by_path(path)?;
        if let Some(doc) = self.documents.get(&id) {
            let mut buf = heapless::Vec::<u8, MAX_FILE_SIZE>::new();
            buf.resize(self.config.page_header_size as usize + 16 + doc.data.len(), 0).ok();
            let handle = self.storage.open("dcf.streamdb", "rb")?;
            let read = self.storage.read(handle, buf.as_mut_slice(), 0)?;
            self.storage.close(handle)?;
            if read != buf.len() {
                return Err(());
            }
            let crc = LittleEndian::read_u32(&buf[0..4]);
            let data_len = LittleEndian::read_u32(&buf[4..8]) as usize;
            let data = &buf[(self.config.page_header_size as usize + 16)..(self.config.page_header_size as usize + 16 + data_len)];
            if !self.quick_mode && CRC_HASHER.checksum(data) != crc {
                return Err(());
            }
            let mut res = heapless::Vec::new();
            res.extend_from_slice(data).ok();
            Ok(res)
        } else {
            Err(())
        }
    }

    fn delete(&mut self, path: &str) -> Result<(), ()> {
        let id = self.get_document_id_by_path(path)?;
        if self.documents.remove(&id).is_some() {
            self.path_trie.delete(path);
            let handle = self.storage.open("dcf.streamdb", "wb+")?;
            self.storage.write(handle, &[], 0)?;
            self.storage.close(handle)?;
            Ok(())
        } else {
            Err(())
        }
    }

    fn search(&self, prefix: &str) -> Result<heapless::Vec<heapless::String<U32>, U8>, ()> {
        Ok(self.path_trie.search(prefix))
    }

    fn flush(&self) -> Result<(), ()> {
        Ok(())
    }

    fn set_quick_mode(&mut self, enabled: bool) {
        self.quick_mode = enabled;
    }
}

pub struct StreamDb<S: Storage> {
    backend: LibfatBackend<S>,
    quick_mode: bool,
}

impl<S: Storage> StreamDb<S> {
    pub fn new(storage: S) -> Self {
        let config = Config::default();
        Self {
            backend: LibfatBackend::new(storage, config),
            quick_mode: false,
        }
    }
}

impl<S: Storage> Database for StreamDb<S> {
    fn write_document(&mut self, path: &str, data: &[u8]) -> Result<u128, ()> {
        self.backend.write_document(path, data)
    }

    fn get(&self, path: &str) -> Result<heapless::Vec<u8, MAX_FILE_SIZE>, ()> {
        self.backend.get(path)
    }

    fn delete(&mut self, path: &str) -> Result<(), ()> {
        self.backend.delete(path)
    }

    fn search(&self, prefix: &str) -> Result<heapless::Vec<heapless::String<U32>, U8>, ()> {
        self.backend.search(prefix)
    }

    fn flush(&self) -> Result<(), ()> {
        self.backend.flush()
    }

    fn set_quick_mode(&mut self, enabled: bool) {
        self.quick_mode = enabled;
        self.backend.set_quick_mode(enabled);
    }
}

// DCF Structures
#[derive(Clone, Default)]
struct DcfMessage {
    sender: heapless::String<U32>,
    recipient: heapless::String<U32>,
    data: heapless::Vec<u8, MAX_MSG_LEN>,
    timestamp: u64,
    sync: bool,
    sequence: u32,
    redundancy_path: heapless::String<U32>,
    group_id: heapless::String<U32>,
}

#[derive(Clone)]
struct DcfConfig {
    transport: heapless::String<U32>,
    host: heapless::String<U32>,
    port: u16,
    mode: heapless::String<U32>,
    node_id: heapless::String<U32>,
    peers: heapless::Vec<heapless::String<U32>, MAX_PEERS>,
    group_rtt_threshold: u32,
}

#[derive(Clone, Copy)]
enum Dir {
    Send,
    Receive,
}

// Tic-Tac-Toe Game Implementation
struct TicTacToe {
    board: heapless::Vec<heapless::String<U8>, U9>,
    is_my_turn: bool,
    my_symbol: char,
    input_buffer: heapless::String<U8>,
    game_over: bool,
}

impl TicTacToe {
    fn new(node_id: &heapless::String<U32>) -> Self {
        Self {
            board: (1..=9).map(|n| itoa_heapless(n)).collect(),
            is_my_turn: node_id == "host",
            my_symbol: if node_id == "host" { 'X' } else { 'O' },
            input_buffer: heapless::String::new(),
            game_over: false,
        }
    }
}

impl Game for TicTacToe {
    fn init(&mut self) {
        self.board = (1..=9).map(|n| itoa_heapless(n)).collect();
        self.input_buffer.clear();
        self.game_over = false;
    }

    fn handle_message(&mut self, msg: &DcfMessage) -> Option<heapless::String<U32>> {
        let data_str = heapless::String::<U32>::from_utf8(msg.data.clone()).ok()?;
        if data_str.starts_with("MOVE:") {
            let pos: usize = data_str[5..].parse().unwrap_or(9);
            if pos < 9 && !self.game_over {
                self.board[pos] = heapless::String::from(if self.my_symbol == 'X' { 'O' } else { 'X' });
                self.is_my_turn = true;
                if self.check_win(if self.my_symbol == 'X' { 'O' } else { 'X' }) {
                    self.game_over = true;
                    return Some("Opponent wins!".into());
                } else if self.check_draw() {
                    self.game_over = true;
                    return Some("Draw!".into());
                }
            }
        } else if data_str == "RESET" {
            self.init();
            return Some("Game reset".into());
        }
        None
    }

    fn update_gui(&mut self, gui: &mut dyn Gui) {
        gui.clear_console();
        for i in (0..9).step_by(3) {
            let line = [&self.board[i], &self.board[i + 1], &self.board[i + 2]]
                .iter()
                .map(|s| s.as_str())
                .collect::<heapless::Vec<_, U3>>()
                .join(" | ");
            gui.print(&line);
            if i < 6 {
                gui.print("-----");
            }
        }
        gui.print(&format!("Turn: {}", if self.is_my_turn { "You" } else { "Opponent" }));
    }

    fn process_input(&mut self, input: char) -> Option<DcfMessage> {
        if input == '\n' && !self.input_buffer.is_empty() && self.is_my_turn && !self.game_over {
            let parts: heapless::Vec<heapless::String<U8>, U2> = self.input_buffer.split(',').map(|s| s.into()).collect();
            if parts.len() == 2 {
                let row: usize = parts[0].parse().unwrap_or(3);
                let col: usize = parts[1].parse().unwrap_or(3);
                if row < 3 && col < 3 {
                    let pos = row * 3 + col;
                    if self.board[pos].as_str() != "X" && self.board[pos].as_str() != "O" {
                        self.board[pos] = heapless::String::from(self.my_symbol);
                        self.is_my_turn = false;
                        if self.check_win(self.my_symbol) {
                            self.game_over = true;
                            return Some(DcfMessage {
                                data: format_move(pos as u32).into(),
                                ..Default::default()
                            });
                        } else if self.check_draw() {
                            self.game_over = true;
                            return Some(DcfMessage {
                                data: format_move(pos as u32).into(),
                                ..Default::default()
                            });
                        }
                        self.input_buffer.clear();
                        return Some(DcfMessage {
                            data: format_move(pos as u32).into(),
                            ..Default::default()
                        });
                    }
                }
            }
            self.input_buffer.clear();
        } else if input == 'r' {
            self.init();
            return Some(DcfMessage {
                data: "RESET".into(),
                ..Default::default()
            });
        } else if self.input_buffer.len() < 8 {
            self.input_buffer.push(input).ok();
        }
        None
    }

    fn check_win(&self, player: char) -> bool {
        let p = heapless::String::from(player);
        (0..3).any(|i| self.board[i * 3..i * 3 + 3].iter().all(|c| *c == p))
            || (0..3).any(|i| [self.board[i], self.board[i + 3], self.board[i + 6]].iter().all(|c| *c == p))
            || [self.board[0], self.board[4], self.board[8]].iter().all(|c| *c == p)
            || [self.board[2], self.board[4], self.board[6]].iter().all(|c| *c == p)
    }

    fn check_draw(&self) -> bool {
        self.board.iter().all(|c| *c == "X".into() || *c == "O".into())
    }
}

// DCF Framework
struct DcfFramework<N: Network, S: Storage, G: Gui> {
    net: N,
    storage: S,
    gui: G,
    config: DcfConfig,
    middlewares: heapless::Vec<Box<dyn Fn(&DcfMessage, Dir) -> DcfMessage>, U4>,
    peer_groups: FnvIndexMap<heapless::String<U32>, heapless::Vec<heapless::String<U32>, MAX_PEERS>, U4>,
    metrics: FnvIndexMap<heapless::String<U32>, u32, U4>,
    db: StreamDb<S>,
    socket: c_int,
    current_room: heapless::String<U32>,
    chat_history: heapless::Vec<heapless::String<U32>, U8>,
    message_buffer: heapless::String<U32>,
    pending_acks: FnvIndexMap<u32, heapless::Vec<u8, MAX_MSG_LEN>, U4>,
    game: Option<Box<dyn Game>>,
    reasm: wire::Reassembler,
}

impl<N: Network, S: Storage + Clone, G: Gui> DcfFramework<N, S, G> {
    fn new(net: N, storage: S, gui: G, config: DcfConfig) -> Self {
        // The StreamDB and the framework each own a storage handle (the DSi/PSP
        // backends are zero-sized unit structs, so the clone is free).
        let db = StreamDb::new(storage.clone());
        let mut framework = Self {
            net,
            storage,
            gui,
            config,
            middlewares: heapless::Vec::new(),
            peer_groups: FnvIndexMap::new(),
            metrics: FnvIndexMap::new(),
            db,
            socket: -1,
            current_room: "default".into(),
            chat_history: heapless::Vec::new(),
            message_buffer: heapless::String::new(),
            pending_acks: FnvIndexMap::new(),
            game: None,
            reasm: wire::Reassembler::new(),
        };
        framework.game = Some(Box::new(TicTacToe::new(&framework.config.node_id)));
        framework
    }

    fn start(&mut self) -> Result<(), ()> {
        self.net.init()?;
        self.gui.init()?;
        // Fail fast if our wire codec disagrees with the certified mesh anchors.
        if !wire::selftest() {
            self.gui.print("WIRE SELFTEST FAILED");
            return Err(());
        }
        self.socket = self.net.bind_udp(self.config.port)?;
        self.load_config()?;
        self.join_room("tictactoe_room");
        self.group_peers();
        self.add_middleware(|msg, dir| {
            if dir == Dir::Receive && msg.data.starts_with(b"ACK:") {
                let seq: u32 = heapless::String::<U32>::from_utf8(msg.data[4..].to_vec()).unwrap_or_default().parse().unwrap_or(0);
                self.pending_acks.remove(&seq);
                return None;
            }
            Some(msg)
        });
        if let Some(game) = &mut self.game {
            game.init();
        }
        Ok(())
    }

    fn load_config(&mut self) -> Result<(), ()> {
        let config_data = self.db.get("/config")?;
        let config_str = heapless::String::<U32>::from_utf8(config_data).ok()?;
        let parts: heapless::Vec<heapless::String<U32>, U4> = config_str.split(';').map(|s| s.into()).collect();
        if parts.len() >= 3 {
            self.config.node_id = parts[0].clone();
            self.config.peers = parts[1].split(',').map(|s| s.into()).collect();
            self.current_room = parts[2].clone(); // current_room lives on the framework, not DcfConfig
        }
        Ok(())
    }

    fn save_config(&self) {
        let mut config_str = heapless::String::<U32>::new();
        config_str.push_str(&self.config.node_id).ok();
        config_str.push(';').ok();
        config_str.push_str(&self.config.peers.join(",")).ok();
        config_str.push(';').ok();
        config_str.push_str(&self.config.current_room).ok();
        self.db.write_document("/config", config_str.as_bytes()).ok();
    }

    fn join_room(&mut self, room: &str) {
        let mut msg = heapless::String::<U32>::new();
        msg.push_str("JOIN:").ok();
        msg.push_str(room).ok();
        self.send_raw(msg.as_bytes());
        self.current_room = room.into();
        self.chat_history.clear();
        self.gui.clear_console();
        self.save_config();
    }

    fn send_raw(&self, data: &[u8]) {
        for peer in &self.config.peers {
            self.net.send_udp(peer.as_str(), self.config.port, data).ok();
        }
    }

    /// 16-bit source id for the wire `src` field: the certified CRC-16 of our
    /// node id (the same hash DCF uses for channel rendezvous).
    fn node_src(&self) -> u16 {
        wire::crc16_ccitt(self.config.node_id.as_bytes())
    }

    /// Serialise `data` into certified `DeModFrame` `DATA` frames and put them on
    /// the wire, packing each adjacent pair into one 32-byte SuperPack (one
    /// datagram instead of two) for the lower-latency paired send. This is the
    /// on-air format shared with the rest of the Punctim mesh.
    fn send_framed(&self, data: &[u8], packet_id: u16) {
        let len = core::cmp::min(data.len(), wire::MAX_PAYLOAD);
        let mut frames = [[0u8; wire::FRAME_SIZE]; wire::MAX_FRAMES];
        let n = match wire::packetize(
            &data[..len],
            packet_id,
            self.node_src(),
            wire::BROADCAST,
            0,
            0, // msg_type_id (opaque to L2)
            0, // flags
            &mut frames,
        ) {
            Ok(n) => n,
            Err(_) => return,
        };
        let mut i = 0;
        while i < n {
            if i + 1 < n {
                if let Ok(sp) = wire::pack(&frames[i], &frames[i + 1]) {
                    self.send_raw(&sp);
                    i += 2;
                    continue;
                }
            }
            self.send_raw(&frames[i]);
            i += 1;
        }
    }

    fn send(&mut self, mut msg: DcfMessage) {
        msg.sequence = self.metrics.entry("sequence".into()).or_insert(0);
        msg = self.apply_middlewares(msg, Dir::Send).unwrap_or(msg);
        let mut buf = heapless::Vec::<u8, MAX_MSG_LEN>::new();
        LittleEndian::write_u32(&mut buf, msg.sequence).ok();
        LittleEndian::write_u64(&mut buf, msg.timestamp).ok();
        buf.extend_from_slice(msg.sender.as_bytes()).ok();
        buf.push(0).ok();
        buf.extend_from_slice(msg.recipient.as_bytes()).ok();
        buf.push(0).ok();
        buf.extend_from_slice(&msg.data).ok();
        buf.push(0).ok();
        buf.extend_from_slice(msg.redundancy_path.as_bytes()).ok();
        buf.push(0).ok();
        buf.extend_from_slice(msg.group_id.as_bytes()).ok();
        // On-air via the certified DeModFrame / SuperPack quantum.
        self.send_framed(&buf, (msg.sequence & 0x07FF) as u16);
        if msg.sync {
            self.pending_acks.insert(msg.sequence, buf).ok();
            *self.metrics.entry("sends".into()).or_insert(0) += 1;
        }
    }

    fn receive(&mut self) -> Option<DcfMessage> {
        let mut buf = [0u8; MAX_MSG_LEN];
        let (len, from) = self.net.recv_udp(&mut buf).ok()?;
        let raw = &buf[0..len];
        // DeModFrame / SuperPack path: reassemble framed game/chat traffic back
        // into the message envelope. Bootstrap control strings (JOIN/HELLO/PING/
        // ACK/FILE) stay plaintext and fall through unchanged.
        let mut env = [0u8; wire::MAX_PAYLOAD];
        let framed = if wire::is_superpack(raw) {
            match wire::unpack(raw) {
                Ok((fa, fb)) => {
                    let a = self.reasm.push(&fa, &mut env);
                    self.reasm.push(&fb, &mut env).or(a)
                }
                Err(_) => None,
            }
        } else if raw.len() == wire::FRAME_SIZE && wire::Frame::is_valid(raw) {
            self.reasm.push(raw, &mut env)
        } else {
            None
        };
        let data: &[u8] = match framed {
            Some(l) => &env[..l],
            None => raw,
        };
        if data.starts_with(b"JOIN:") {
            let room = heapless::String::<U32>::from_utf8(data[5..].to_vec()).ok()?;
            if !self.config.peers.contains(&from) {
                self.config.peers.push(from.clone()).ok();
                self.save_config();
            }
            return None;
        } else if data.starts_with(b"FILE:") {
            let parts = heapless::String::<U32>::from_utf8(data[5..].to_vec()).ok()?;
            let parts: heapless::Vec<heapless::String<U32>, U4> = parts.split(':').map(|s| s.into()).collect();
            let filename = parts.get(1)?.as_str();
            let size: usize = parts.get(2)?.parse().unwrap_or(0);
            if size > MAX_FILE_SIZE {
                return None;
            }
            let mut file_buf = [0u8; MAX_FILE_SIZE];
            let file_len = self.net.recv_udp(&mut file_buf).ok()?.0;
            if file_len != size {
                return None;
            }
            let handle = self.storage.open(filename, "wb+")?;
            self.storage.write(handle, &file_buf[0..file_len], 0)?;
            self.storage.close(handle)?;
            self.chat_history.push(format_str("Received file: ", filename)).ok();
            self.gui.display_image(filename).ok();
            return None;
        } else if data.starts_with(b"HELLO") {
            self.net.send_udp(from.as_str(), self.config.port, b"HELLO_ACK").ok();
            if !self.config.peers.contains(&from) {
                self.config.peers.push(from.clone()).ok();
                self.save_config();
            }
            return None;
        } else if data.starts_with(b"ACK:") {
            let seq: u32 = heapless::String::<U32>::from_utf8(data[4..].to_vec()).ok()?.parse().unwrap_or(0);
            self.pending_acks.remove(&seq);
            return None;
        } else {
            let mut cursor = 0;
            let sequence = LittleEndian::read_u32(&data[cursor..]);
            cursor += 4;
            let timestamp = LittleEndian::read_u64(&data[cursor..]);
            cursor += 8;
            let sender = cstr_to_heapless(&data[cursor..]);
            cursor += sender.len() + 1;
            let recipient = cstr_to_heapless(&data[cursor..]);
            cursor += recipient.len() + 1;
            let data_end = data[cursor..].iter().position(|&b| b == 0).unwrap_or(data.len() - cursor);
            let msg_data = heapless::Vec::from_slice(&data[cursor..cursor + data_end]).ok()?;
            cursor += data_end + 1;
            let redundancy_path = cstr_to_heapless(&data[cursor..]);
            cursor += redundancy_path.len() + 1;
            let group_id = cstr_to_heapless(&data[cursor..]);
            if group_id != self.current_room {
                return None;
            }
            let msg = DcfMessage {
                sender,
                recipient,
                data: msg_data,
                timestamp,
                sync: true,
                sequence,
                redundancy_path,
                group_id,
            };
            let processed = self.apply_middlewares(msg.clone(), Dir::Receive)?;
            if processed.sync {
                let ack = format_ack(processed.sequence);
                self.net.send_udp(from.as_str(), self.config.port, ack.as_bytes()).ok();
            }
            let msg_str = heapless::String::<U32>::from_utf8(processed.data.clone()).ok()?;
            self.chat_history.push(format_str(&format_str(&processed.sender, ": "), &msg_str)).ok();
            *self.metrics.entry("receives".into()).or_insert(0) += 1;
            self.db.write_document("/messages/received", data).ok();
            Some(processed)
        }
    }

    fn send_file(&mut self, filename: &str) -> Result<(), ()> {
        let handle = self.storage.open(filename, "rb")?;
        let mut buf = heapless::Vec::<u8, MAX_FILE_SIZE>::new();
        buf.resize(MAX_FILE_SIZE, 0).ok();
        let size = self.storage.read(handle, buf.as_mut_slice(), 0)?;
        self.storage.close(handle)?;
        let header = format_str("FILE:", &format_str(filename, &format_str(":", &itoa_heapless(size as u32))));
        self.send_raw(header.as_bytes());
        self.send_raw(&buf[0..size]);
        self.chat_history.push(format_str("Sent file: ", filename)).ok();
        self.gui.display_image(filename).ok();
        Ok(())
    }

    fn add_middleware<F: Fn(&DcfMessage, Dir) -> Option<DcfMessage> + 'static>(&mut self, f: F) {
        self.middlewares.push(Box::new(f)).ok();
    }

    fn apply_middlewares(&self, msg: DcfMessage, dir: Dir) -> Option<DcfMessage> {
        let mut current = Some(msg);
        for mw in &self.middlewares {
            current = (mw)(&current?, dir);
        }
        current
    }

    fn group_peers(&mut self) {
        let peers = self.net.discover_peers(self.config.port);
        for peer in &peers {
            let rtt = self.measure_rtt(peer.as_str());
            let group = if rtt < self.config.group_rtt_threshold {
                "low".into()
            } else {
                "high".into()
            };
            self.peer_groups
                .entry(group)
                .or_insert(heapless::Vec::new())
                .push(peer.clone())
                .ok();
            let mut rtt_buf = [0u8; 4];
            LittleEndian::write_u32(&mut rtt_buf, rtt);
            self.db.write_document(&peer_to_path(peer.as_str()), &rtt_buf).ok();
        }
        self.config.peers = peers;
        self.save_config();
    }

    fn measure_rtt(&self, peer: &str) -> u32 {
        let start = unsafe { swiGetTicks() };
        let ping = "PING".as_bytes();
        self.net.send_udp(peer, self.config.port, ping).ok();
        let mut buf = [0u8; MAX_MSG_LEN];
        for _ in 0..10 {
            if let Ok((len, from)) = self.net.recv_udp(&mut buf) {
                if from == peer && &buf[0..len] == b"PONG" {
                    let end = unsafe { swiGetTicks() };
                    return ((end - start) * 1000 / 32768) as u32; // DS ticks to ms
                }
            }
            self.gui.wait_vblank();
        }
        1000 // Timeout
    }

    fn heal(&mut self, failed_peer: &str) {
        let graph = self.build_graph();
        let (distances, predecessors) = dijkstra(&graph, &self.config.node_id);
        if let Some(path) = reconstruct_path(&predecessors, failed_peer) {
            if let Some(alt) = path.last() {
                self.config.peers.retain(|p| p.as_str() != failed_peer);
                if !self.config.peers.contains(alt) {
                    self.config.peers.push(alt.clone()).ok();
                }
                self.db.write_document("/state/heal", alt.as_bytes()).ok();
                self.save_config();
            }
        }
    }

    fn build_graph(&self) -> FnvIndexMap<(heapless::String<U32>, heapless::String<U32>), u32, U8> {
        let mut graph = FnvIndexMap::new();
        for peer1 in &self.config.peers {
            for peer2 in &self.config.peers {
                if peer1 != peer2 {
                    graph.insert((peer1.clone(), peer2.clone()), self.measure_rtt(peer2.as_str())).ok();
                }
            }
        }
        graph
    }

    fn db_insert(&mut self, path: &str, data: &[u8]) {
        self.db.write_document(path, data).ok();
    }

    fn db_query(&self, path: &str) -> Option<heapless::Vec<u8, MAX_FILE_SIZE>> {
        self.db.get(path).ok()
    }

    fn update_gui(&mut self) {
        if self.gui.is_lid_closed() {
            return;
        }
        self.gui.clear_console();
        if let Some(game) = &mut self.game {
            game.update_gui(&mut self.gui);
        }
        for msg in &self.chat_history {
            self.gui.print(msg.as_str());
        }
        if let Some(input) = self.gui.poll_input() {
            if let Some(game) = &mut self.game {
                if let Some(msg) = game.process_input(input) {
                    self.send(msg);
                }
            } else {
                self.message_buffer.push(input).ok();
                if input == '\n' && !self.message_buffer.is_empty() {
                    let msg = DcfMessage {
                        sender: self.config.node_id.clone(),
                        recipient: "".into(),
                        data: self.message_buffer.as_bytes().to_vec().into(),
                        timestamp: unsafe { swiGetTicks() },
                        sync: true,
                        sequence: 0,
                        redundancy_path: "".into(),
                        group_id: self.current_room.clone(),
                    };
                    self.send(msg);
                    self.message_buffer.clear();
                }
            }
        }
        // Virtual buttons for actions (join, file, reset)
        if keys::keysDown() & keys::KEY_X != 0 {
            self.send_file("asset.bmp").ok();
        }
        if keys::keysDown() & keys::KEY_Y != 0 {
            self.join_room("new_room");
        }
    }

    fn run(&mut self) {
        self.start().unwrap_or_else(|_| {
            self.gui.print("Failed to initialize");
            loop {
                self.gui.wait_vblank();
            }
        });
        let mut retry_timer = 0;
        loop {
            if let Some(msg) = self.receive() {
                if let Some(game) = &mut self.game {
                    if let Some(status) = game.handle_message(&msg) {
                        self.chat_history.push(status).ok();
                    }
                }
            }
            if retry_timer >= TIMEOUT_MS {
                for (seq, data) in &self.pending_acks {
                    self.send_framed(data, (*seq & 0x07FF) as u16);
                }
                retry_timer = 0;
            }
            retry_timer += 16; // ~60FPS
            self.update_gui();
            self.gui.wait_vblank();
        }
    }
}

// Dijkstra
fn dijkstra(
    graph: &FnvIndexMap<(heapless::String<U32>, heapless::String<U32>), u32, U8>,
    start: &heapless::String<U32>,
) -> (FnvIndexMap<heapless::String<U32>, u32, U8>, FnvIndexMap<heapless::String<U32>, heapless::String<U32>, U8>) {
    let mut distances = FnvIndexMap::new();
    let mut predecessors = FnvIndexMap::new();
    let mut queue = heapless::BinaryHeap::<(u32, heapless::String<U32>), heapless::Min, U8>::new();
    let nodes = graph.keys().map(|k| k.0.clone()).collect::<heapless::Vec<_, U8>>();
    for node in &nodes {
        distances.insert(node.clone(), u32::MAX).ok();
        predecessors.insert(node.clone(), heapless::String::new()).ok();
    }
    distances.insert(start.clone(), 0).ok();
    queue.push((0, start.clone())).ok();
    while let Some((dist, u)) = queue.pop() {
        for v in &nodes {
            if u != *v {
                let edge = (u.clone(), v.clone());
                if let Some(&weight) = graph.get(&edge) {
                    let alt = dist.saturating_add(weight);
                    if alt < *distances.get(v).unwrap_or(&u32::MAX) {
                        distances.insert(v.clone(), alt).ok();
                        predecessors.insert(v.clone(), u.clone()).ok();
                        queue.push((alt, v.clone())).ok();
                    }
                }
            }
        }
    }
    (distances, predecessors)
}

fn reconstruct_path(
    predecessors: &FnvIndexMap<heapless::String<U32>, heapless::String<U32>, U8>,
    target: &str,
) -> Option<heapless::Vec<heapless::String<U32>, U8>> {
    let mut path = heapless::Vec::<heapless::String<U32>, U8>::new();
    let mut u = target.into();
    while !u.is_empty() {
        path.push(u.clone()).ok();
        u = predecessors.get(&u)?.clone();
    }
    path.reverse();
    Some(path)
}

// Helpers
fn cstr_to_heapless(bytes: &[u8]) -> heapless::String<U32> {
    let mut s = heapless::String::new();
    for &b in bytes.iter().take_while(|&&b| b != 0) {
        s.push(b as char).ok();
    }
    s
}

fn cstr(s: &str) -> heapless::Vec<u8, U32> {
    let mut v = heapless::Vec::new();
    v.extend_from_slice(s.as_bytes()).ok();
    v.push(0).ok();
    v
}

fn itoa_heapless(mut n: u32) -> heapless::String<U32> {
    let mut s = heapless::String::new();
    if n == 0 {
        s.push('0').ok();
        return s;
    }
    let mut digits = heapless::Vec::<char, U32>::new();
    while n > 0 {
        digits.push(((n % 10) as u8 + b'0') as char).ok();
        n /= 10;
    }
    while let Some(d) = digits.pop() {
        s.push(d).ok();
    }
    s
}

fn format_str(a: &str, b: &str) -> heapless::String<U32> {
    let mut s = heapless::String::new();
    s.push_str(a).ok();
    s.push_str(b).ok();
    s
}

fn format_move(pos: u32) -> heapless::String<U32> {
    let mut s = heapless::String::new();
    s.push_str("MOVE:").ok();
    s.push_str(&itoa_heapless(pos)).ok();
    s
}

fn format_ack(seq: u32) -> heapless::String<U32> {
    let mut s = heapless::String::new();
    s.push_str("ACK:").ok();
    s.push_str(&itoa_heapless(seq)).ok();
    s
}

fn str_to_mac(s: &str) -> [u8; 6] {
    let parts: heapless::Vec<heapless::String<U8>, U6> = s.split(':').map(|s| s.into()).collect();
    let mut mac = [0u8; 6];
    if parts.len() == 6 {
        for (i, part) in parts.iter().enumerate() {
            mac[i] = part.parse::<u8>().unwrap_or(0);
        }
    }
    mac
}

fn mac_to_str(mac: &[u8; 6]) -> heapless::String<U32> {
    let mut s = heapless::String::new();
    for i in 0..6 {
        s.push_str(&itoa_heapless(mac[i] as u32)).ok();
        if i < 5 {
            s.push(':').ok();
        }
    }
    s
}

fn peer_to_path(peer: &str) -> heapless::String<U32> {
    let mut s = heapless::String::new();
    s.push_str("/peers/").ok();
    s.push_str(peer).ok();
    s
}

#[cfg(feature = "dsi")]
#[no_mangle]
pub extern "C" fn main() -> ! {
    libnds::init();
    unsafe { libnds::ipc::Ipc::init(); }

    let config = DcfConfig {
        transport: "udp".into(),
        host: "0.0.0.0".into(),
        port: 50051,
        mode: "p2p".into(),
        node_id: "host".into(),
        peers: heapless::Vec::from_iter(vec!["192.168.1.2:50051".into()]),
        group_rtt_threshold: 50,
    };

    let mut framework = DcfFramework::new(DsiNetwork, DsiStorage, DsiGui {
        keyboard: nds_keyboard::Keyboard::default(),
        console: nds_console::Console::default(),
        touch_pos: touch::TouchPosition::default(),
        image_sprite: graphics::Sprite::new(graphics::SpriteSize_32x32, graphics::SpriteColorFormat_Color256, 0),
    }, config);
    framework.run();
}

#[cfg(feature = "psp")]
#[no_mangle]
pub extern "C" fn main() -> ! {
    unsafe {
        psp::module_info!("DCF Framework", 0, 1, 1);
        psp::setup_callbacks();
    }

    let config = DcfConfig {
        transport: "udp".into(),
        host: "0.0.0.0".into(),
        port: 50051,
        mode: "p2p".into(),
        node_id: "client".into(),
        peers: heapless::Vec::from_iter(vec!["mac:00:00:00:00:00:01".into()]),
        group_rtt_threshold: 50,
    };

    let mut framework = DcfFramework::new(PspNetwork { pdp_id: -1 }, PspStorage, PspGui::new(), config);
    framework.run();
}
