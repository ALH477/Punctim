// SPDX-License-Identifier: LGPL-3.0-or-later
// Copyright (C) 2025 DeMoD LLC
//! StreamDb - A lightweight, embedded database for storing and retrieving binary streams via paths.
//! ## Overview
//! StreamDb is a reliable, performant, and simple key-value store where keys are string paths
//! (supporting prefix searches via a compressed reverse trie) and values are binary streams (documents up to 256MB by default).
//! It uses a paged storage system with chaining for large documents. The database is thread-safe, supports
//! multiple readers/single writer per path, and includes optimizations like LRU caching for pages and paths,
//! QuickAndDirtyMode for skipping CRC on reads, and free page management with first-fit LIFO strategy and
//! automatic consolidation. Versioning supports basic retention (keep 2 versions, free on 3rd), with garbage
//! collection during flush. Uses i64 for page IDs (exceeding spec's i32) for scalability.
//!# Copyright (c) 2025 DeMoD LLC
//! ## Adaptability for Multiple Use Cases and Platforms
//! - **Use Cases**:
//!   - **Embedded Systems**: Configurable page and cache sizes for low memory; optional no-mmap mode.
//!   - **Server Applications**: Thread-safe with lock hierarchy; tunable for high concurrency.
//!   - **Mobile/Desktop Apps**: Cross-platform; lightweight dependencies.
//!   - **In-Memory Testing/Volatile Storage**: MemoryBackend with compressed trie for consistent search.
//!   - **Custom Storage**: DatabaseBackend trait for pluggable backends (e.g., S3).
//!   - **Large-Scale Databases**: i64 page IDs support >2B pages; configurable max sizes.
//!   - **Read-Heavy Workloads**: Quick mode for 10x faster reads; LRU page cache.
//!   - **Write-Heavy Workloads**: Batched file growth, LIFO free page reuse, preemptive growth.
//!   - **Versioned Data**: MVCC-like retention; extensible for snapshots.
//! - **Platforms**:
//!   - **Cross-Compilation**: Supports Rust targets; conditional no-mmap for wasm.
//!   - **File System Tuning**: Configurable page size (e.g., 4KB default, 8KB for NVMe).
//!   - **No-Mmap Fallback**: Uses standard seek/read/write.
//!   - **Threading**: parking_lot locks for efficiency across OS.
//! - **Extensibility**:
//!   - **Config**: Tune page size, caches, versions, mmap.
//!   - **Traits**: Database for ops, DatabaseBackend for storage.
//!   - **Recovery**: Scans orphans, repairs chains, rebuilds indexes.
//!   - **Error Handling**: Checks bounds, CRC, versions; custom recovery.
//!   - **Maintenance**: Flush, stats, GC, cache metrics.
//! - **Dependencies**: uuid, crc, memmap2 (optional), byteorder, parking_lot, lru, snappy, futures.

use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, BufWriter, Cursor, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};
use parking_lot::{Mutex, RwLock as PlRwLock};
use memmap2::{MmapMut, MmapOptions};
use byteorder::{LittleEndian, ReadBytesExt, WriteBytesExt};
use uuid::Uuid;
use crc::Crc as CrcLib;
use crc::CRC_32_ISO_HDLC;
use lru::LruCache;
use snappy;
use futures::future::Future;

const MAGIC: [u8; 8] = [0x55, 0xAA, 0xFE, 0xED, 0xFA, 0xCE, 0xDA, 0x7A];
const DEFAULT_PAGE_RAW_SIZE: u64 = 8192; // CHANGED: 8KB for NVMe alignment
const DEFAULT_PAGE_HEADER_SIZE: u64 = 32; // u32 crc (4) + i32 ver (4) + i64 prev/next (8+8) + u8 flags (1) + i32 len (4) + u8[3] pad (3) = 32 for alignment
const FREE_LIST_HEADER_SIZE: u64 = 12; // i64 next (8) + i32 used (4)
const FREE_LIST_ENTRIES_PER_PAGE: usize = ((DEFAULT_PAGE_RAW_SIZE - DEFAULT_PAGE_HEADER_SIZE - FREE_LIST_HEADER_SIZE) / 8) as usize; // (8192-32-12)/8 = 1011
const DEFAULT_MAX_DB_SIZE: u64 = 8000 * 1024 * 1024 * 1024;
const DEFAULT_MAX_PAGES: i64 = i64::MAX;
const DEFAULT_MAX_DOCUMENT_SIZE: u64 = 256 * 1024 * 1024;
const BATCH_GROW_PAGES: u64 = 16;
const DEFAULT_PAGE_CACHE_SIZE: usize = 2048; // CHANGED: Doubled for prefetch
const DEFAULT_VERSIONS_TO_KEEP: i32 = 2;
const MAX_CONSECUTIVE_EMPTY_FREE_LIST: u64 = 5; // Preemptive growth threshold

// Flags for page types
const FLAG_DATA_PAGE: u8 = 0b00000001;
const FLAG_TRIE_PAGE: u8 = 0b00000010;
const FLAG_FREE_LIST_PAGE: u8 = 0b00000100;
const FLAG_INDEX_PAGE: u8 = 0b00001000;

#[derive(Clone, Copy, Debug)]
struct VersionedLink {
    page_id: i64,
    version: i32,
}

#[derive(Debug)]
struct DatabaseHeader {
    magic: [u8; 8],
    index_root: VersionedLink,
    path_lookup_root: VersionedLink,
    free_list_root: VersionedLink,
}

#[derive(Debug, Clone)]
struct PageHeader {
    crc: u32,
    version: i32,
    prev_page_id: i64,
    next_page_id: i64,
    flags: u8,
    data_length: i32,
    padding: [u8; 3], // Align to 32 bytes
}

#[derive(Debug)]
struct FreeListPage {
    next_free_list_page: i64,
    used_entries: i32,
    free_page_ids: Vec<i64>,
}

#[derive(Debug)]
struct ReverseTrieNode {
    edge: String, // Store path segment for compression
    parent_index: i64,
    self_index: i64,
    document_id: Option<Uuid>,
    children: HashMap<char, i64>,
}

#[derive(Clone, Debug)]
struct Document {
    id: Uuid,
    first_page_id: i64,
    current_version: i32,
    paths: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Config {
    page_size: u64,
    page_header_size: u64,
    max_db_size: u64,
    max_pages: i64,
    max_document_size: u64,
    page_cache_size: usize,
    versions_to_keep: i32,
    use_mmap: bool,
    use_compression: bool, // ADDED: Toggle snappy compression
}

impl Default for Config {
    fn default() -> Self {
        Self {
            page_size: DEFAULT_PAGE_RAW_SIZE,
            page_header_size: DEFAULT_PAGE_HEADER_SIZE,
            max_db_size: DEFAULT_MAX_DB_SIZE,
            max_pages: DEFAULT_MAX_PAGES,
            max_document_size: DEFAULT_MAX_DOCUMENT_SIZE,
            page_cache_size: DEFAULT_PAGE_CACHE_SIZE,
            versions_to_keep: DEFAULT_VERSIONS_TO_KEEP,
            use_mmap: true,
            use_compression: true, // ADDED: Default on
        }
    }
}

#[derive(Debug, Default)]
pub struct CacheStats {
    hits: usize,
    misses: usize,
}

pub trait Database {
    fn write_document(&mut self, path: &str, data: &mut dyn Read) -> io::Result<Uuid>;
    fn get(&self, path: &str) -> io::Result<Vec<u8>>;
    fn get_quick(&self, path: &str, quick: bool) -> io::Result<Vec<u8>>;
    fn get_id_by_path(&self, path: &str) -> io::Result<Option<Uuid>>;
    fn delete(&mut self, path: &str) -> io::Result<()>;
    fn delete_by_id(&mut self, id: Uuid) -> io::Result<()>;
    fn bind_to_path(&mut self, id: Uuid, path: &str) -> io::Result<()>;
    fn unbind_path(&mut self, id: Uuid, path: &str) -> io::Result<()>;
    fn search(&self, prefix: &str) -> io::Result<Vec<String>>;
    fn list_paths(&self, id: Uuid) -> io::Result<Vec<String>>;
    fn flush(&self) -> io::Result<()>;
    fn calculate_statistics(&self) -> io::Result<(i64, i64)>;
    fn set_quick_mode(&mut self, enabled: bool);
    fn snapshot(&self) -> io::Result<Self> where Self: Sized;
    fn get_cache_stats(&self) -> io::Result<CacheStats>;
    fn get_stream(&self, path: &str) -> io::Result<impl Iterator<Item = io::Result<Vec<u8>>>>;
    fn get_async(&self, path: &str) -> impl Future<Output = io::Result<Vec<u8>>>;
}

pub trait DatabaseBackend {
    fn write_document(&mut self, data: &mut dyn Read) -> io::Result<Uuid>;
    fn read_document(&self, id: Uuid) -> io::Result<Vec<u8>>;
    fn read_document_quick(&self, id: Uuid, quick: bool) -> io::Result<Vec<u8>>;
    fn delete_document(&mut self, id: Uuid) -> io::Result<()>;
    fn bind_path_to_document(&mut self, path: &str, id: Uuid) -> io::Result<Uuid>;
    fn get_document_id_by_path(&self, path: &str) -> io::Result<Uuid>;
    fn search_paths(&self, prefix: &str) -> io::Result<Vec<String>>;
    fn list_paths_for_document(&self, id: Uuid) -> io::Result<Vec<String>>;
    fn count_free_pages(&self) -> io::Result<i64>;
    fn get_info(&self, id: Uuid) -> io::Result<String>;
    fn delete_paths_for_document(&mut self, id: Uuid) -> io::Result<()>;
    fn remove_from_index(&mut self, id: Uuid) -> io::Result<()>;
    fn get_cache_stats(&self) -> io::Result<CacheStats>;
    fn get_stream(&self, id: Uuid) -> io::Result<impl Iterator<Item = io::Result<Vec<u8>>>>;
}

// Varint helpers for optimized serialization
fn write_varint<W: Write>(writer: &mut W, mut value: u64) -> io::Result<()> {
    loop {
        if value < 0x80 {
            writer.write_u8(value as u8)?;
            break;
        }
        writer.write_u8((value as u8 & 0x7F) | 0x80)?;
        value >>= 7;
    }
    Ok(())
}

fn read_varint<R: Read>(reader: &mut R) -> io::Result<u64> {
    let mut value = 0u64;
    let mut shift = 0u32;
    loop {
        let byte = reader.read_u8()?;
        value |= ((byte & 0x7F) as u64) << shift;
        shift += 7;
        if byte & 0x80 == 0 {
            break;
        }
        if shift > 63 {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "Varint too large"));
        }
    }
    Ok(value)
}

fn zigzag_encode(value: i64) -> u64 {
    ((value << 1) ^ (value >> 63)) as u64
}

fn zigzag_decode(value: u64) -> i64 {
    ((value >> 1) as i64) ^ -((value & 1) as i64)
}

// In-Memory Backend with compressed trie for consistent search
struct MemoryBackend {
    documents: Mutex<HashMap<Uuid, Vec<u8>>>,
    path_to_id: Mutex<HashMap<String, Uuid>>,
    id_to_paths: Mutex<HashMap<Uuid, Vec<String>>>,
    trie_root: Mutex<ReverseTrieNode>,
    cache_stats: Mutex<CacheStats>,
}

impl MemoryBackend {
    fn new() -> Self {
        Self {
            documents: Mutex::new(HashMap::new()),
            path_to_id: Mutex::new(HashMap::new()),
            id_to_paths: Mutex::new(HashMap::new()),
            trie_root: Mutex::new(ReverseTrieNode {
                edge: String::new(),
                parent_index: -1,
                self_index: 0,
                document_id: None,
                children: HashMap::new(),
            }),
            cache_stats: Mutex::new(CacheStats::default()),
        }
    }

    fn validate_path(&self, path: &str) -> io::Result<()> {
        if path.is_empty() || path.contains('\0') || path.contains("//") {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Invalid path"));
        }
        Ok(())
    }

    fn serialize_trie_node(&self, node: &ReverseTrieNode) -> io::Result<Vec<u8>> {
        let mut buffer = Vec::new();
        let edge_bytes = node.edge.as_bytes();
        write_varint(&mut buffer, edge_bytes.len() as u64)?;
        buffer.write_all(edge_bytes)?;
        write_varint(&mut buffer, zigzag_encode(node.parent_index))?;
        write_varint(&mut buffer, zigzag_encode(node.self_index))?;
        if let Some(id) = node.document_id {
            buffer.write_u8(1)?;
            buffer.write_all(id.as_bytes())?;
        } else {
            buffer.write_u8(0)?;
        }
        write_varint(&mut buffer, node.children.len() as u64)?;
        for (&c, &index) in &node.children {
            write_varint(&mut buffer, c as u64)?;
            write_varint(&mut buffer, zigzag_encode(index))?;
        }
        Ok(buffer)
    }

    fn deserialize_trie_node(&self, data: &[u8]) -> io::Result<ReverseTrieNode> {
        let mut reader = Cursor::new(data);
        let edge_len = read_varint(&mut reader)? as usize;
        let mut edge_bytes = vec![0u8; edge_len];
        reader.read_exact(&mut edge_bytes)?;
        let edge = String::from_utf8(edge_bytes)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        let parent_index = zigzag_decode(read_varint(&mut reader)?);
        let self_index = zigzag_decode(read_varint(&mut reader)?);
        let has_id = reader.read_u8()?;
        let document_id = if has_id == 1 {
            let mut bytes = [0u8; 16];
            reader.read_exact(&mut bytes)?;
            Some(Uuid::from_bytes(bytes))
        } else {
            None
        };
        let children_count = read_varint(&mut reader)? as usize;
        let mut children = HashMap::with_capacity(children_count);
        for _ in 0..children_count {
            let c = read_varint(&mut reader)?;
            let c_char = char::try_from(c as u32)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Invalid child char"))?;
            let index = zigzag_decode(read_varint(&mut reader)?);
            children.insert(c_char, index);
        }
        Ok(ReverseTrieNode {
            edge,
            parent_index,
            self_index,
            document_id,
            children,
        })
    }

    fn trie_insert(&self, path: &str, id: Uuid) -> io::Result<()> {
        self.validate_path(path)?;
        let reversed: String = path.chars().rev().collect();
        let mut current = self.trie_root.lock();
        let mut remaining = reversed.as_str();
        let mut path_stack = vec![];
        while !remaining.is_empty() {
            let first_char = remaining.chars().next().unwrap();
            if let Some(&child_index) = current.children.get(&first_char) {
                let child = self.trie_root.lock();
                if child.self_index == child_index {
                    let edge = child.edge.as_str();
                    let common_len = edge.chars().zip(remaining.chars()).take_while(|(a, b)| a == b).count();
                    if common_len == edge.len() {
                        path_stack.push(current.self_index);
                        current = child;
                        remaining = &remaining[common_len..];
                        continue;
                    } else if common_len > 0 {
                        let common = &edge[..common_len];
                        let suffix = &edge[common_len..];
                        let new_intermediate = ReverseTrieNode {
                            edge: common.to_string(),
                            parent_index: current.self_index,
                            self_index: child.self_index,
                            document_id: None,
                            children: HashMap::new(),
                        };
                        let new_child = ReverseTrieNode {
                            edge: suffix.to_string(),
                            parent_index: new_intermediate.self_index,
                            self_index: child.self_index + 1,
                            document_id: child.document_id,
                            children: child.children,
                        };
                        new_intermediate.children.insert(suffix.chars().next().unwrap(), new_child.self_index);
                        *child = new_intermediate;
                        path_stack.push(current.self_index);
                        current = new_child;
                        remaining = &remaining[common_len..];
                    } else {
                        let new_child = ReverseTrieNode {
                            edge: remaining.to_string(),
                            parent_index: current.self_index,
                            self_index: current.self_index + 1,
                            document_id: Some(id),
                            children: HashMap::new(),
                        };
                        current.children.insert(first_char, new_child.self_index);
                        *self.trie_root.lock() = new_child;
                        return Ok(());
                    }
                }
            } else {
                let new_child = ReverseTrieNode {
                    edge: remaining.to_string(),
                    parent_index: current.self_index,
                    self_index: current.self_index + 1,
                    document_id: Some(id),
                    children: HashMap::new(),
                };
                current.children.insert(first_char, new_child.self_index);
                *self.trie_root.lock() = new_child;
                return Ok(());
            }
        }
        current.document_id = Some(id);
        Ok(())
    }

    fn trie_delete(&self, path: &str) -> io::Result<()> {
        self.validate_path(path)?;
        let reversed: String = path.chars().rev().collect();
        let mut current = self.trie_root.lock();
        let mut remaining = reversed.as_str();
        let mut path_stack = vec![];
        while !remaining.is_empty() {
            let first_char = remaining.chars().next().unwrap();
            if let Some(&child_index) = current.children.get(&first_char) {
                let child = self.trie_root.lock();
                if child.self_index == child_index {
                    let edge = child.edge.as_str();
                    if remaining.starts_with(edge) {
                        path_stack.push((current.self_index, first_char));
                        current = child;
                        remaining = &remaining[edge.len()..];
                    } else {
                        return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                    }
                } else {
                    return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                }
            } else {
                return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
            }
        }
        if current.document_id.is_none() {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
        }
        current.document_id = None;
        while let Some((parent_index, c)) = path_stack.pop() {
            let mut parent = self.trie_root.lock();
            if parent.self_index == parent_index {
                if current.document_id.is_none() && current.children.len() == 1 {
                    let (child_char, child_index) = current.children.iter().next().unwrap().clone();
                    let child_node = self.trie_root.lock();
                    if child_node.self_index == child_index {
                        let merged_edge = format!("{}{}", current.edge, child_node.edge);
                        let merged_node = ReverseTrieNode {
                            edge: merged_edge,
                            parent_index: parent.self_index,
                            self_index: current.self_index,
                            document_id: child_node.document_id,
                            children: child_node.children,
                        };
                        *current = merged_node;
                        parent.children.insert(c, current.self_index);
                        current.children.remove(&child_char);
                    }
                } else if current.document_id.is_none() && current.children.is_empty() {
                    parent.children.remove(&c);
                } else {
                    break;
                }
            }
        }
        Ok(())
    }

    fn trie_collect_paths(&self, node: &ReverseTrieNode, prefix: &str, current_path: String, results: &mut Vec<String>) {
        let new_path = format!("{}{}", node.edge, current_path);
        if let Some(_) = node.document_id {
            let path = new_path.chars().rev().collect::<String>();
            if path.starts_with(prefix) {
                results.push(path);
            }
        }
        for (_, child_index) in &node.children {
            let child = self.trie_root.lock();
            if child.self_index == *child_index {
                self.trie_collect_paths(&child, prefix, new_path.clone(), results);
            }
        }
    }
}

impl DatabaseBackend for MemoryBackend {
    fn write_document(&mut self, data: &mut dyn Read) -> io::Result<Uuid> {
        let id = Uuid::new_v4();
        let mut buf = Vec::new();
        data.read_to_end(&mut buf)?;
        let compressed = snappy::compress(&buf);
        self.documents.lock().insert(id, compressed);
        self.id_to_paths.lock().insert(id, vec![]);
        Ok(id)
    }

    fn read_document(&self, id: Uuid) -> io::Result<Vec<u8>> {
        self.read_document_quick(id, false)
    }

    fn read_document_quick(&self, id: Uuid, _quick: bool) -> io::Result<Vec<u8>> {
        let mut stats = self.cache_stats.lock();
        if self.documents.lock().contains_key(&id) {
            stats.hits += 1;
        } else {
            stats.misses += 1;
            return Err(io::Error::new(io::ErrorKind::NotFound, "Document not found"));
        }
        let compressed = self.documents.lock().get(&id).unwrap().clone();
        snappy::uncompress(&compressed)
    }

    fn delete_document(&mut self, id: Uuid) -> io::Result<()> {
        self.documents.lock().remove(&id);
        if let Some(paths) = self.id_to_paths.lock().remove(&id) {
            let mut path_map = self.path_to_id.lock();
            for p in paths {
                path_map.remove(&p);
                self.trie_delete(&p)?;
            }
        }
        Ok(())
    }

    fn bind_path_to_document(&mut self, path: &str, id: Uuid) -> io::Result<Uuid> {
        self.validate_path(path)?;
        if !self.documents.lock().contains_key(&id) {
            return Err(io::Error::new(io::ErrorKind::NotFound, "ID not found"));
        }
        if self.path_to_id.lock().contains_key(path) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Path already bound"));
        }
        self.path_to_id.lock().insert(path.to_string(), id);
        self.id_to_paths.lock().entry(id).or_insert(vec![]).push(path.to_string());
        self.trie_insert(path, id)?;
        Ok(id)
    }

    fn get_document_id_by_path(&self, path: &str) -> io::Result<Uuid> {
        self.validate_path(path)?;
        let reversed: String = path.chars().rev().collect();
        let mut current = self.trie_root.lock();
        let mut remaining = reversed.as_str();
        while !remaining.is_empty() {
            let first_char = remaining.chars().next().unwrap();
            if let Some(&child_index) = current.children.get(&first_char) {
                let child = self.trie_root.lock();
                if child.self_index == child_index {
                    if remaining.starts_with(&child.edge) {
                        remaining = &remaining[child.edge.len()..];
                        if remaining.is_empty() {
                            return child.document_id.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                        }
                        current = child;
                    } else {
                        return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                    }
                } else {
                    return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                }
            } else {
                return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
            }
        }
        current.document_id.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Path not found"))
    }

    fn search_paths(&self, prefix: &str) -> io::Result<Vec<String>> {
        self.validate_path(prefix)?;
        let mut results = vec![];
        let node = self.trie_root.lock();
        self.trie_collect_paths(&node, prefix, String::new(), &mut results);
        Ok(results)
    }

    fn list_paths_for_document(&self, id: Uuid) -> io::Result<Vec<String>> {
        self.id_to_paths.lock().get(&id).cloned().ok_or(io::Error::new(io::ErrorKind::NotFound, "ID not found"))
    }

    fn count_free_pages(&self) -> io::Result<i64> {
        Ok(0) // No pages in memory
    }

    fn get_info(&self, id: Uuid) -> io::Result<String> {
        if let Some(data) = self.documents.lock().get(&id) {
            let paths = self.list_paths_for_document(id)?;
            Ok(format!("ID: {}, Version: 1, Size: {} bytes, Paths: {:?}", id, data.len(), paths))
        } else {
            Err(io::Error::new(io::ErrorKind::NotFound, "ID not found"))
        }
    }

    fn delete_paths_for_document(&mut self, id: Uuid) -> io::Result<()> {
        if let Some(paths) = self.id_to_paths.lock().get(&id) {
            let mut path_map = self.path_to_id.lock();
            for p in paths {
                path_map.remove(p);
                self.trie_delete(p)?;
            }
        }
        Ok(())
    }

    fn remove_from_index(&mut self, id: Uuid) -> io::Result<()> {
        self.delete_paths_for_document(id)?;
        Ok(())
    }

    fn get_cache_stats(&self) -> io::Result<CacheStats> {
        Ok(self.cache_stats.lock().clone())
    }

    fn get_stream(&self, id: Uuid) -> io::Result<impl Iterator<Item = io::Result<Vec<u8>>>> {
        let data = self.read_document(id)?;
        Ok(std::iter::once(Ok(data)))
    }
}

// File Backend - Main persistent implementation
struct FileBackend {
    config: Config,
    file: Arc<Mutex<File>>,
    mmap: Arc<PlRwLock<Option<MmapMut>>>,
    current_size: Arc<Mutex<u64>>,
    document_index_root: Mutex<VersionedLink>,
    trie_root: Mutex<VersionedLink>,
    free_list_root: Mutex<VersionedLink>,
    page_cache: Mutex<LruCache<i64, Arc<Vec<u8>>>>,
    quick_mode: AtomicBool,
    old_versions: Mutex<HashMap<Uuid, Vec<(i32, i64)>>>,
    cache_stats: Mutex<CacheStats>,
    empty_free_list_count: Mutex<u64>,
}

impl FileBackend {
    pub fn new<P: AsRef<Path>>(path: P, config: Config) -> io::Result<Self> {
        let mut file = OpenOptions::new().read(true).write(true).create(true).open(path.as_ref())?;
        let mut initial_size = file.seek(SeekFrom::End(0))?;
        let mut header = DatabaseHeader {
            magic: MAGIC,
            index_root: VersionedLink { page_id: -1, version: 0 },
            path_lookup_root: VersionedLink { page_id: -1, version: 0 },
            free_list_root: VersionedLink { page_id: -1, version: 0 },
        };

        if initial_size == 0 {
            initial_size = config.page_size;
            file.set_len(initial_size)?;
            Self::write_header(&mut file, &header, &config)?;
        } else {
            header = Self::read_header(&mut file, &config)?;
            if header.magic != MAGIC {
                // Reinitialize on corrupt header
                header = DatabaseHeader {
                    magic: MAGIC,
                    index_root: VersionedLink { page_id: -1, version: 0 },
                    path_lookup_root: VersionedLink { page_id: -1, version: 0 },
                    free_list_root: VersionedLink { page_id: -1, version: 0 },
                };
                file.seek(SeekFrom::Start(0))?;
                file.set_len(config.page_size)?;
                Self::write_header(&mut file, &header, &config)?;
                initial_size = config.page_size;
            }
        }

        let mmap = if config.use_mmap {
            let mmap = unsafe { MmapOptions::new().len(initial_size as usize).map_mut(&file)? };
            Some(mmap)
        } else {
            None
        };

        let backend = Self {
            config,
            file: Arc::new(Mutex::new(file)),
            mmap: Arc::new(PlRwLock::new(mmap)),
            current_size: Arc::new(Mutex::new(initial_size)),
            document_index_root: Mutex::new(header.index_root),
            trie_root: Mutex::new(header.path_lookup_root),
            free_list_root: Mutex::new(header.free_list_root),
            page_cache: Mutex::new(LruCache::new(std::num::NonZeroUsize::new(config.page_cache_size).unwrap())),
            quick_mode: AtomicBool::new(false),
            old_versions: Mutex::new(HashMap::new()),
            cache_stats: Mutex::new(CacheStats::default()),
            empty_free_list_count: Mutex::new(0),
        };

        backend.recover()?;
        Ok(backend)
    }

    fn validate_path(&self, path: &str) -> io::Result<()> {
        if path.is_empty() || path.contains('\0') || path.contains("//") {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Invalid path"));
        }
        Ok(())
    }

    fn write_header(file: &mut File, header: &DatabaseHeader, config: &Config) -> io::Result<()> {
        file.seek(SeekFrom::Start(0))?;
        let mut writer = BufWriter::new(file);
        writer.write_all(&header.magic)?;
        writer.write_i64::<LittleEndian>(header.index_root.page_id)?;
        writer.write_i32::<LittleEndian>(header.index_root.version)?;
        writer.write_i64::<LittleEndian>(header.path_lookup_root.page_id)?;
        writer.write_i32::<LittleEndian>(header.path_lookup_root.version)?;
        writer.write_i64::<LittleEndian>(header.free_list_root.page_id)?;
        writer.write_i32::<LittleEndian>(header.free_list_root.version)?;
        writer.flush()?;
        Ok(())
    }

    fn read_header(file: &mut File, config: &Config) -> io::Result<DatabaseHeader> {
        file.seek(SeekFrom::Start(0))?;
        let mut reader = BufReader::new(file);
        let mut magic = [0u8; 8];
        reader.read_exact(&mut magic)?;
        let index_root = VersionedLink {
            page_id: reader.read_i64::<LittleEndian>()?,
            version: reader.read_i32::<LittleEndian>()?,
        };
        let path_lookup_root = VersionedLink {
            page_id: reader.read_i64::<LittleEndian>()?,
            version: reader.read_i32::<LittleEndian>()?,
        };
        let free_list_root = VersionedLink {
            page_id: reader.read_i64::<LittleEndian>()?,
            version: reader.read_i32::<LittleEndian>()?,
        };
        Ok(DatabaseHeader {
            magic,
            index_root,
            path_lookup_root,
            free_list_root,
        })
    }

    fn compute_crc(&self, data: &[u8]) -> u32 {
        let crc = CrcLib::<u32>::new(&CRC_32_ISO_HDLC);
        crc.checksum(data)
    }

    fn read_raw_page(&self, page_id: i64) -> io::Result<Arc<Vec<u8>>> {
        if page_id < 0 || page_id >= self.config.max_pages {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Invalid page ID"));
        }
        let offset = page_id as u64 * self.config.page_size + self.config.page_header_size;
        let mut cache = self.page_cache.lock();
        {
            let mut stats = self.cache_stats.lock();
            if cache.contains(&page_id) {
                stats.hits += 1;
                return Ok(cache.get(&page_id).unwrap().clone());
            }
            stats.misses += 1;
        }
        let header = self.read_page_header(page_id)?;
        let data_length = header.data_length as usize;
        if data_length as u64 > self.config.page_size - self.config.page_header_size {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "Invalid data length"));
        }
        let mut data = vec![0u8; data_length];
        if let Some(mmap) = self.mmap.read().as_ref() {
            let start = offset as usize;
            data.copy_from_slice(&mmap[start..start + data_length]);
        } else {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start(offset))?;
            file.read_exact(&mut data)?;
        }
        if self.config.use_compression {
            data = snappy::uncompress(&data)?;
        }
        if !self.quick_mode.load(Ordering::Relaxed) {
            let computed_crc = self.compute_crc(&data);
            if computed_crc != header.crc {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "CRC mismatch"));
            }
        }
        let arc_data = Arc::new(data);
        cache.put(page_id, arc_data.clone());
        if header.next_page_id != -1 {
            let _ = self.read_raw_page(header.next_page_id);
        }
        Ok(arc_data)
    }

    fn write_raw_page(&self, page_id: i64, data: &[u8], version: i32) -> io::Result<()> {
        if page_id < 0 || page_id >= self.config.max_pages {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Invalid page ID"));
        }
        let mut compressed = data.to_vec();
        if self.config.use_compression {
            compressed = snappy::compress(data);
        }
        if compressed.len() as u64 > self.config.page_size - self.config.page_header_size {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Data too large for page"));
        }
        let offset = page_id as u64 * self.config.page_size;
        let crc = self.compute_crc(&compressed);
        let header = PageHeader {
            crc,
            version,
            prev_page_id: -1,
            next_page_id: -1,
            flags: FLAG_DATA_PAGE,
            data_length: compressed.len() as i32,
            padding: [0; 3],
        };
        self.write_page_header(page_id, &header)?;
        if let Some(mmap) = self.mmap.write().as_mut() {
            let start = offset as usize + self.config.page_header_size as usize;
            mmap[start..start + compressed.len()].copy_from_slice(&compressed);
            mmap.flush()?;
        } else {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start(offset + self.config.page_header_size))?;
            file.write_all(&compressed)?;
            file.flush()?;
        }
        self.page_cache.lock().pop(&page_id);
        Ok(())
    }

    fn write_page_header(&self, page_id: i64, header: &PageHeader) -> io::Result<()> {
        let offset = page_id as u64 * self.config.page_size;
        let mut buffer = Vec::new();
        let mut writer = BufWriter::new(&mut buffer);
        writer.write_u32::<LittleEndian>(header.crc)?;
        writer.write_i32::<LittleEndian>(header.version)?;
        writer.write_i64::<LittleEndian>(header.prev_page_id)?;
        writer.write_i64::<LittleEndian>(header.next_page_id)?;
        writer.write_u8(header.flags)?;
        writer.write_i32::<LittleEndian>(header.data_length)?;
        writer.write_all(&header.padding)?;
        let data = buffer;
        if let Some(mmap) = self.mmap.write().as_mut() {
            let start = offset as usize;
            mmap[start..start + self.config.page_header_size as usize].copy_from_slice(&data);
            mmap.flush()?;
        } else {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start(offset))?;
            file.write_all(&data)?;
            file.flush()?;
        }
        Ok(())
    }

    fn read_page_header(&self, page_id: i64) -> io::Result<PageHeader> {
        if page_id < 0 || page_id >= self.config.max_pages {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Invalid page ID"));
        }
        let offset = page_id as u64 * self.config.page_size;
        let mut buffer = vec![0u8; self.config.page_header_size as usize];
        if let Some(mmap) = self.mmap.read().as_ref() {
            let start = offset as usize;
            buffer.copy_from_slice(&mmap[start..start + self.config.page_header_size as usize]);
        } else {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start(offset))?;
            file.read_exact(&mut buffer)?;
        }
        let mut reader = Cursor::new(buffer);
        let crc = reader.read_u32::<LittleEndian>()?;
        let version = reader.read_i32::<LittleEndian>()?;
        let prev_page_id = reader.read_i64::<LittleEndian>()?;
        let next_page_id = reader.read_i64::<LittleEndian>()?;
        let flags = reader.read_u8()?;
        let data_length = reader.read_i32::<LittleEndian>()?;
        let mut padding = [0u8; 3];
        reader.read_exact(&mut padding)?;
        Ok(PageHeader {
            crc,
            version,
            prev_page_id,
            next_page_id,
            flags,
            data_length,
            padding,
        })
    }

    fn update_free_list_used(&self, page_id: i64, used_entries: i32) -> io::Result<()> {
        let offset = page_id as u64 * self.config.page_size + self.config.page_header_size;
        let mut buffer = Vec::new();
        let mut writer = BufWriter::new(&mut buffer);
        writer.write_i64::<LittleEndian>(-1)?;
        writer.write_i32::<LittleEndian>(used_entries)?;
        let data = buffer;
        if let Some(mmap) = self.mmap.write().as_mut() {
            let start = offset as usize;
            mmap[start..start + FREE_LIST_HEADER_SIZE as usize].copy_from_slice(&data);
            mmap.flush()?;
        } else {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start(offset))?;
            file.write_all(&data)?;
            file.flush()?;
        }
        Ok(())
    }

    fn allocate_page(&self) -> io::Result<i64> {
        let popped = self.pop_free_page();
        if popped.is_ok() {
            *self.empty_free_list_count.lock() = 0;
            return popped;
        }
        let mut empty_count = self.empty_free_list_count.lock();
        *empty_count += 1;
        if *empty_count >= MAX_CONSECUTIVE_EMPTY_FREE_LIST {
            let new_pages = self.grow_file(BATCH_GROW_PAGES)?;
            *empty_count = 0;
            return Ok(new_pages);
        }
        let page_id = {
            let mut current_size = self.current_size.lock();
            let page_id = (*current_size / self.config.page_size) as i64;
            if page_id >= self.config.max_pages {
                return Err(io::Error::new(io::ErrorKind::Other, "Max pages exceeded"));
            }
            *current_size += self.config.page_size;
            page_id
        };
        let mut file = self.file.lock();
        file.set_len(page_id as u64 * self.config.page_size + self.config.page_size)?;
        Ok(page_id)
    }

    fn pop_free_page(&self) -> io::Result<i64> {
        let mut free_root = self.free_list_root.lock();
        if free_root.page_id == -1 {
            return Err(io::Error::new(io::ErrorKind::NotFound, "No free pages"));
        }
        let offset = free_root.page_id as u64 * self.config.page_size + self.config.page_header_size;
        let mut buffer = vec![0u8; FREE_LIST_HEADER_SIZE as usize + 8];
        if let Some(mmap) = self.mmap.read().as_ref() {
            let start = offset as usize;
            buffer.copy_from_slice(&mmap[start..start + FREE_LIST_HEADER_SIZE as usize + 8]);
        } else {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start(offset))?;
            file.read_exact(&mut buffer)?;
        }
        let mut reader = Cursor::new(buffer);
        let next_free_list_page = reader.read_i64::<LittleEndian>()?;
        let used_entries = reader.read_i32::<LittleEndian>()?;
        if used_entries <= 0 {
            free_root.page_id = next_free_list_page;
            return Err(io::Error::new(io::ErrorKind::NotFound, "No free pages in list"));
        }
        let page_id = reader.read_i64::<LittleEndian>()?;
        self.update_free_list_used(free_root.page_id, used_entries - 1)?;
        if used_entries == 1 {
            free_root.page_id = next_free_list_page;
        }
        Ok(page_id)
    }

    fn free_page(&self, page_id: i64) -> io::Result<()> {
        let mut free_root = self.free_list_root.lock();
        let mut free_list_page = FreeListPage {
            next_free_list_page: free_root.page_id,
            used_entries: 1,
            free_page_ids: vec![page_id],
        };
        if free_root.page_id != -1 {
            let offset = free_root.page_id as u64 * self.config.page_size + self.config.page_header_size;
            let mut buffer = vec![0u8; FREE_LIST_HEADER_SIZE as usize + 8 * FREE_LIST_ENTRIES_PER_PAGE];
            if let Some(mmap) = self.mmap.read().as_ref() {
                let start = offset as usize;
                buffer[0..FREE_LIST_HEADER_SIZE as usize].copy_from_slice(&mmap[start..start + FREE_LIST_HEADER_SIZE as usize]);
            } else {
                let mut file = self.file.lock();
                file.seek(SeekFrom::Start(offset))?;
                file.read_exact(&mut buffer[0..FREE_LIST_HEADER_SIZE as usize])?;
            }
            let mut reader = Cursor::new(buffer);
            free_list_page.next_free_list_page = reader.read_i64::<LittleEndian>()?;
            free_list_page.used_entries = reader.read_i32::<LittleEndian>()?;
            if (free_list_page.used_entries as usize) < FREE_LIST_ENTRIES_PER_PAGE {
                free_list_page.free_page_ids = vec![page_id];
                for _ in 0..free_list_page.used_entries {
                    free_list_page.free_page_ids.push(reader.read_i64::<LittleEndian>()?);
                }
            } else {
                let new_page_id = self.allocate_page()?;
                free_list_page = FreeListPage {
                    next_free_list_page: free_root.page_id,
                    used_entries: 1,
                    free_page_ids: vec![page_id],
                };
                free_root.page_id = new_page_id;
            }
        } else {
            free_root.page_id = self.allocate_page()?;
        }
        let offset = free_root.page_id as u64 * self.config.page_size;
        let mut buffer = Vec::new();
        let mut writer = BufWriter::new(&mut buffer);
        writer.write_i64::<LittleEndian>(free_list_page.next_free_list_page)?;
        writer.write_i32::<LittleEndian>(free_list_page.used_entries)?;
        for &id in &free_list_page.free_page_ids {
            writer.write_i64::<LittleEndian>(id)?;
        }
        let data = buffer;
        let header = PageHeader {
            crc: self.compute_crc(&data),
            version: 1,
            prev_page_id: -1,
            next_page_id: -1,
            flags: FLAG_FREE_LIST_PAGE,
            data_length: data.len() as i32,
            padding: [0; 3],
        };
        self.write_page_header(free_root.page_id, &header)?;
        if let Some(mmap) = self.mmap.write().as_mut() {
            let start = offset as usize + self.config.page_header_size as usize;
            mmap[start..start + data.len()].copy_from_slice(&data);
            mmap.flush()?;
        } else {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start(offset + self.config.page_header_size))?;
            file.write_all(&data)?;
            file.flush()?;
        }
        Ok(())
    }

    fn grow_file(&self, pages: u64) -> io::Result<i64> {
        let mut current_size = self.current_size.lock();
        let page_id = (*current_size / self.config.page_size) as i64;
        if page_id >= self.config.max_pages {
            return Err(io::Error::new(io::ErrorKind::Other, "Max pages exceeded"));
        }
        *current_size += pages * self.config.page_size;
        let mut file = self.file.lock();
        file.set_len(*current_size)?;
        file.flush()?;
        Ok(page_id)
    }

    fn recover(&self) -> io::Result<()> {
        let total_pages = (*self.current_size.lock() / self.config.page_size) as i64;
        let mut used_pages = HashSet::new();
        let mut free_pages = HashSet::new();
        let index_root = *self.document_index_root.lock();
        let trie_root = *self.trie_root.lock();
        let free_root = *self.free_list_root.lock();
        if index_root.page_id != -1 {
            used_pages.insert(index_root.page_id);
        }
        if trie_root.page_id != -1 {
            used_pages.insert(trie_root.page_id);
        }
        if free_root.page_id != -1 {
            let mut current = free_root.page_id;
            while current != -1 {
                used_pages.insert(current);
                let offset = current as u64 * self.config.page_size + self.config.page_header_size;
                let mut buffer = vec![0u8; FREE_LIST_HEADER_SIZE as usize];
                if let Some(mmap) = self.mmap.read().as_ref() {
                    let start = offset as usize;
                    buffer.copy_from_slice(&mmap[start..start + FREE_LIST_HEADER_SIZE as usize]);
                } else {
                    let mut file = self.file.lock();
                    file.seek(SeekFrom::Start(offset))?;
                    file.read_exact(&mut buffer)?;
                }
                let mut reader = Cursor::new(buffer);
                current = reader.read_i64::<LittleEndian>()?;
                let used_entries = reader.read_i32::<LittleEndian>()?;
                for _ in 0..used_entries {
                    let page_id = reader.read_i64::<LittleEndian>()?;
                    free_pages.insert(page_id);
                }
            }
        }
        for page_id in 0..total_pages {
            if used_pages.contains(&page_id) || free_pages.contains(&page_id) {
                continue;
            }
            let header = self.read_page_header(page_id);
            if header.is_err() || !self.quick_mode.load(Ordering::Relaxed) {
                self.free_page(page_id)?;
                continue;
            }
            let header = header.unwrap();
            if header.flags & FLAG_DATA_PAGE != 0 {
                used_pages.insert(page_id);
                let mut current = header.next_page_id;
                while current != -1 {
                    used_pages.insert(current);
                    let next_header = self.read_page_header(current)?;
                    current = next_header.next_page_id;
                }
            }
        }
        Ok(())
    }

    fn gc_old_versions(&self) -> io::Result<()> {
        let mut old_versions = self.old_versions.lock();
        for (id, versions) in old_versions.iter_mut() {
            while versions.len() as i32 > self.config.versions_to_keep {
                if let Some((_, page_id)) = versions.pop() {
                    let mut current = page_id;
                    while current != -1 {
                        let header = self.read_page_header(current)?;
                        self.free_page(current)?;
                        current = header.next_page_id;
                    }
                }
            }
        }
        Ok(())
    }

    fn serialize_trie_node(&self, node: &ReverseTrieNode) -> io::Result<Vec<u8>> {
        let edge_bytes = node.edge.as_bytes();
        let estimated_size = 5 + edge_bytes.len() + 10 + 10 + 1 + 17 + 5 + node.children.len() * (5 + 10);
        if estimated_size as u64 > self.config.page_size - self.config.page_header_size {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Trie node too large"));
        }
        let mut buffer = Vec::new();
        let mut writer = BufWriter::new(&mut buffer);
        write_varint(&mut writer, edge_bytes.len() as u64)?;
        writer.write_all(edge_bytes)?;
        write_varint(&mut writer, zigzag_encode(node.parent_index))?;
        write_varint(&mut writer, zigzag_encode(node.self_index))?;
        match node.document_id {
            Some(id) => {
                writer.write_u8(1)?;
                writer.write_all(id.as_bytes())?;
            }
            None => writer.write_u8(0)?,
        }
        write_varint(&mut writer, node.children.len() as u64)?;
        for (&c, &index) in &node.children {
            write_varint(&mut writer, c as u64)?;
            write_varint(&mut writer, zigzag_encode(index))?;
        }
        Ok(buffer)
    }

    fn deserialize_trie_node(&self, data: &[u8]) -> io::Result<ReverseTrieNode> {
        let mut reader = Cursor::new(data);
        let edge_len = read_varint(&mut reader)? as usize;
        let mut edge_bytes = vec![0u8; edge_len];
        reader.read_exact(&mut edge_bytes)?;
        let edge = String::from_utf8(edge_bytes)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        let parent_index = zigzag_decode(read_varint(&mut reader)?);
        let self_index = zigzag_decode(read_varint(&mut reader)?);
        let has_id = reader.read_u8()?;
        let document_id = if has_id == 1 {
            let mut bytes = [0u8; 16];
            reader.read_exact(&mut bytes)?;
            Some(Uuid::from_bytes(bytes))
        } else {
            None
        };
        let children_count = read_varint(&mut reader)? as usize;
        let mut children = HashMap::with_capacity(children_count);
        for _ in 0..children_count {
            let c = read_varint(&mut reader)?;
            let c_char = char::try_from(c as u32)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Invalid child char"))?;
            let index = zigzag_decode(read_varint(&mut reader)?);
            children.insert(c_char, index);
        }
        Ok(ReverseTrieNode {
            edge,
            parent_index,
            self_index,
            document_id,
            children,
        })
    }

    fn trie_insert(&self, path: &str, id: Uuid) -> io::Result<i64> {
        let mut trie_root = self.trie_root.lock();
        let reversed: String = path.chars().rev().collect();
        let mut current_page_id = trie_root.page_id;
        if current_page_id == -1 {
            current_page_id = self.allocate_page()?;
            trie_root.page_id = current_page_id;
            trie_root.version = 1;
            let root_node = ReverseTrieNode {
                edge: String::new(),
                parent_index: -1,
                self_index: current_page_id,
                document_id: None,
                children: HashMap::new(),
            };
            let data = self.serialize_trie_node(&root_node)?;
            self.write_raw_page(current_page_id, &data, 1)?;
        }
        let mut remaining = reversed.as_str();
        let mut current_node = self.deserialize_trie_node(&self.read_raw_page(current_page_id)?)?;
        let mut path_stack = vec![];
        while !remaining.is_empty() {
            let first_char = remaining.chars().next().unwrap();
            if let Some(&child_page_id) = current_node.children.get(&first_char) {
                let child_node = self.deserialize_trie_node(&self.read_raw_page(child_page_id)?)?;
                let edge = child_node.edge.as_str();
                let common_len = edge.chars().zip(remaining.chars()).take_while(|(a, b)| a == b).count();
                if common_len == edge.len() {
                    path_stack.push((current_page_id, current_node));
                    current_page_id = child_page_id;
                    current_node = child_node;
                    remaining = &remaining[common_len..];
                    continue;
                } else if common_len > 0 {
                    let common = &edge[..common_len];
                    let suffix = &edge[common_len..];
                    let new_intermediate_id = self.allocate_page()?;
                    let new_intermediate = ReverseTrieNode {
                        edge: common.to_string(),
                        parent_index: current_node.self_index,
                        self_index: new_intermediate_id,
                        document_id: None,
                        children: HashMap::new(),
                    };
                    let new_child_id = self.allocate_page()?;
                    let new_child = ReverseTrieNode {
                        edge: suffix.to_string(),
                        parent_index: new_intermediate_id,
                        self_index: new_child_id,
                        document_id: child_node.document_id,
                        children: child_node.children,
                    };
                    new_intermediate.children.insert(suffix.chars().next().unwrap(), new_child_id);
                    self.write_raw_page(new_intermediate_id, &self.serialize_trie_node(&new_intermediate)?, 1)?;
                    self.write_raw_page(new_child_id, &self.serialize_trie_node(&new_child)?, 1)?;
                    current_node.children.insert(first_char, new_intermediate_id);
                    self.write_raw_page(current_page_id, &self.serialize_trie_node(&current_node)?, 1)?;
                    path_stack.push((current_page_id, current_node));
                    current_page_id = new_intermediate_id;
                    current_node = new_intermediate;
                    remaining = &remaining[common_len..];
                } else {
                    let new_child_id = self.allocate_page()?;
                    let new_child = ReverseTrieNode {
                        edge: remaining.to_string(),
                        parent_index: current_node.self_index,
                        self_index: new_child_id,
                        document_id: Some(id),
                        children: HashMap::new(),
                    };
                    self.write_raw_page(new_child_id, &self.serialize_trie_node(&new_child)?, 1)?;
                    current_node.children.insert(first_char, new_child_id);
                    self.write_raw_page(current_page_id, &self.serialize_trie_node(&current_node)?, 1)?;
                    return Ok(new_child_id);
                }
            } else {
                let new_child_id = self.allocate_page()?;
                let new_child = ReverseTrieNode {
                    edge: remaining.to_string(),
                    parent_index: current_node.self_index,
                    self_index: new_child_id,
                    document_id: Some(id),
                    children: HashMap::new(),
                };
                self.write_raw_page(new_child_id, &self.serialize_trie_node(&new_child)?, 1)?;
                current_node.children.insert(first_char, new_child_id);
                self.write_raw_page(current_page_id, &self.serialize_trie_node(&current_node)?, 1)?;
                return Ok(new_child_id);
            }
        }
        current_node.document_id = Some(id);
        self.write_raw_page(current_page_id, &self.serialize_trie_node(&current_node)?, 1)?;
        Ok(current_page_id)
    }

    fn trie_delete(&self, path: &str) -> io::Result<()> {
        let mut trie_root = self.trie_root.lock();
        if trie_root.page_id == -1 {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
        }
        let reversed: String = path.chars().rev().collect();
        let mut path_stack = vec![];
        let mut current_page_id = trie_root.page_id;
        let mut current_node = self.deserialize_trie_node(&self.read_raw_page(current_page_id)?)?;
        let mut remaining = reversed.as_str();
        while !remaining.is_empty() {
            let first_char = remaining.chars().next().unwrap();
            if let Some(&child_page_id) = current_node.children.get(&first_char) {
                let child_node = self.deserialize_trie_node(&self.read_raw_page(child_page_id)?)?;
                let edge = child_node.edge.as_str();
                if remaining.starts_with(edge) {
                    path_stack.push((current_page_id, current_node, first_char));
                    current_page_id = child_page_id;
                    current_node = child_node;
                    remaining = &remaining[edge.len()..];
                } else {
                    return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                }
            } else {
                return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
            }
        }
        if current_node.document_id.is_none() {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
        }
        current_node.document_id = None;
        self.write_raw_page(current_page_id, &self.serialize_trie_node(&current_node)?, 1)?;
        while let Some((parent_page_id, mut parent_node, c)) = path_stack.pop() {
            if current_node.document_id.is_none() && current_node.children.len() == 1 {
                let (child_char, child_page_id) = current_node.children.iter().next().unwrap();
                let child_node = self.deserialize_trie_node(&self.read_raw_page(*child_page_id)?)?;
                let merged_edge = format!("{}{}", current_node.edge, child_node.edge);
                let merged_node = ReverseTrieNode {
                    edge: merged_edge,
                    parent_index: parent_node.self_index,
                    self_index: current_node.self_index,
                    document_id: child_node.document_id,
                    children: child_node.children,
                };
                self.write_raw_page(current_page_id, &self.serialize_trie_node(&merged_node)?, 1)?;
                self.free_page(*child_page_id)?;
                parent_node.children.insert(c, current_page_id);
                self.write_raw_page(parent_page_id, &self.serialize_trie_node(&parent_node)?, 1)?;
                current_page_id = parent_page_id;
                current_node = parent_node;
            } else if current_node.document_id.is_none() && current_node.children.is_empty() {
                parent_node.children.remove(&c);
                self.write_raw_page(parent_page_id, &self.serialize_trie_node(&parent_node)?, 1)?;
                self.free_page(current_page_id)?;
                current_page_id = parent_page_id;
                current_node = parent_node;
            } else {
                break;
            }
        }
        Ok(())
    }

    fn trie_collect_paths(&self, node: &ReverseTrieNode, current_path: String, results: &mut Vec<String>) -> io::Result<()> {
        let new_path = format!("{}{}", node.edge, current_path);
        if let Some(_) = node.document_id {
            let path = new_path.chars().rev().collect::<String>();
            results.push(path);
        }
        for (_, &child_page_id) in &node.children {
            let child_data = self.read_raw_page(child_page_id)?;
            let child = self.deserialize_trie_node(&child_data)?;
            self.trie_collect_paths(&child, new_path.clone(), results)?;
        }
        Ok(())
    }

    fn serialize_document(&self, doc: &Document) -> io::Result<Vec<u8>> {
        let mut buffer = Vec::new();
        let mut writer = BufWriter::new(&mut buffer);
        writer.write_all(doc.id.as_bytes())?;
        writer.write_i64::<LittleEndian>(doc.first_page_id)?;
        writer.write_i32::<LittleEndian>(doc.current_version)?;
        writer.write_u32::<LittleEndian>(doc.paths.len() as u32)?;
        for path in &doc.paths {
            writer.write_u32::<LittleEndian>(path.len() as u32)?;
            writer.write_all(path.as_bytes())?;
        }
        Ok(buffer)
    }

    fn deserialize_document(&self, data: &[u8]) -> io::Result<Document> {
        let mut reader = Cursor::new(data);
        let mut id_bytes = [0u8; 16];
        reader.read_exact(&mut id_bytes)?;
        let id = Uuid::from_bytes(id_bytes);
        let first_page_id = reader.read_i64::<LittleEndian>()?;
        let current_version = reader.read_i32::<LittleEndian>()?;
        let paths_len = reader.read_u32::<LittleEndian>()? as usize;
        let mut paths = Vec::with_capacity(paths_len);
        for _ in 0..paths_len {
            let path_len = reader.read_u32::<LittleEndian>()? as usize;
            let mut path_bytes = vec![0u8; path_len];
            reader.read_exact(&mut path_bytes)?;
            let path = String::from_utf8(path_bytes)
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
            paths.push(path);
        }
        Ok(Document {
            id,
            first_page_id,
            current_version,
            paths,
        })
    }

    fn get_stream(&self, id: Uuid) -> io::Result<impl Iterator<Item = io::Result<Vec<u8>>>> {
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Document not found"));
        }
        let mut current = Some(document.first_page_id);
        Ok(std::iter::from_fn(move || {
            if let Some(page_id) = current {
                let data = self.read_raw_page(page_id);
                if let Ok(ref d) = data {
                    let header = self.read_page_header(page_id).ok()?;
                    current = if header.next_page_id != -1 { Some(header.next_page_id) } else { None };
                }
                data.map(Ok)
            } else {
                None
            }
        }))
    }
}

impl DatabaseBackend for FileBackend {
    fn write_document(&mut self, data: &mut dyn Read) -> io::Result<Uuid> {
        let id = Uuid::new_v4();
        let mut buffer = Vec::new();
        let bytes_read = data.read_to_end(&mut buffer)?;
        if bytes_read as u64 > self.config.max_document_size {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Document too large"));
        }
        let mut current_page_id = self.allocate_page()?;
        let mut first_page_id = current_page_id;
        let mut bytes_written = 0;
        let mut version = 1;
        let mut prev_page_id = -1;
        while bytes_written < bytes_read {
            let chunk_size = std::cmp::min(
                (self.config.page_size - self.config.page_header_size) as usize,
                bytes_read - bytes_written,
            );
            let chunk = &buffer[bytes_written..bytes_written + chunk_size];
            self.write_raw_page(current_page_id, chunk, version)?;
            let mut header = self.read_page_header(current_page_id)?;
            header.prev_page_id = prev_page_id;
            if bytes_written + chunk_size < bytes_read {
                let next_page_id = self.allocate_page()?;
                header.next_page_id = next_page_id;
                self.write_page_header(current_page_id, &header)?;
                prev_page_id = current_page_id;
                current_page_id = next_page_id;
            } else {
                self.write_page_header(current_page_id, &header)?;
            }
            bytes_written += chunk_size;
        }
        let mut index_root = self.document_index_root.lock();
        let document = Document {
            id,
            first_page_id,
            current_version: version,
            paths: vec![],
        };
        let data = self.serialize_document(&document)?;
        if index_root.page_id == -1 {
            index_root.page_id = self.allocate_page()?;
            index_root.version = 1;
        }
        self.write_raw_page(index_root.page_id, &data, index_root.version)?;
        if let Some(old_version) = self.old_versions.lock().get(&id).cloned() {
            self.old_versions.lock().get_mut(&id).unwrap().push((version - 1, old_version[0].1));
        } else {
            self.old_versions.lock().insert(id, vec![(version - 1, first_page_id)]);
        }
        Ok(id)
    }

    fn read_document(&self, id: Uuid) -> io::Result<Vec<u8>> {
        self.read_document_quick(id, self.quick_mode.load(Ordering::Relaxed))
    }

    fn read_document_quick(&self, id: Uuid, quick: bool) -> io::Result<Vec<u8>> {
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Document not found"));
        }
        let mut result = Vec::new();
        let mut current_page_id = document.first_page_id;
        while current_page_id != -1 {
            let quick_mode = self.quick_mode.load(Ordering::Relaxed);
            self.quick_mode.store(quick, Ordering::Relaxed);
            let data = self.read_raw_page(current_page_id)?;
            self.quick_mode.store(quick_mode, Ordering::Relaxed);
            result.extend_from_slice(&data);
            let header = self.read_page_header(current_page_id)?;
            current_page_id = header.next_page_id;
        }
        Ok(result)
    }

    fn delete_document(&mut self, id: Uuid) -> io::Result<()> {
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Document not found"));
        }
        let mut current_page_id = document.first_page_id;
        while current_page_id != -1 {
            let header = self.read_page_header(current_page_id)?;
            self.free_page(current_page_id)?;
            current_page_id = header.next_page_id;
        }
        self.remove_from_index(id)?;
        Ok(())
    }

    fn bind_path_to_document(&mut self, path: &str, id: Uuid) -> io::Result<Uuid> {
        self.validate_path(path)?;
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let mut document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "ID not found"));
        }
        if self.get_document_id_by_path(path).is_ok() {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Path already bound"));
        }
        document.paths.push(path.to_string());
        let data = self.serialize_document(&document)?;
        self.write_raw_page(index_root.page_id, &data, index_root.version)?;
        self.trie_insert(path, id)?;
        Ok(id)
    }

    fn get_document_id_by_path(&self, path: &str) -> io::Result<Uuid> {
        self.validate_path(path)?;
        let trie_root = self.trie_root.lock();
        if trie_root.page_id == -1 {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
        }
        let reversed: String = path.chars().rev().collect();
        let mut current_page_id = trie_root.page_id;
        let mut remaining = reversed.as_str();
        while !remaining.is_empty() {
            let node = self.deserialize_trie_node(&self.read_raw_page(current_page_id)?)?;
            let edge = node.edge.as_str();
            if remaining.starts_with(edge) {
                remaining = &remaining[edge.len()..];
                if remaining.is_empty() {
                    return node.document_id.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                }
                let first_char = remaining.chars().next().unwrap();
                if let Some(&child_id) = node.children.get(&first_char) {
                    current_page_id = child_id;
                } else {
                    return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
                }
            } else {
                return Err(io::Error::new(io::ErrorKind::NotFound, "Path not found"));
            }
        }
        let node = self.deserialize_trie_node(&self.read_raw_page(current_page_id)?)?;
        node.document_id.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Path not found"))
    }

    fn search_paths(&self, prefix: &str) -> io::Result<Vec<String>> {
        self.validate_path(prefix)?;
        let trie_root = self.trie_root.lock();
        if trie_root.page_id == -1 {
            return Ok(vec![]);
        }
        let reversed_prefix: String = prefix.chars().rev().collect();
        let mut current_page_id = trie_root.page_id;
        let mut remaining = reversed_prefix.as_str();
        while !remaining.is_empty() {
            let node = self.deserialize_trie_node(&self.read_raw_page(current_page_id)?)?;
            let edge = node.edge.as_str();
            if remaining.starts_with(edge) {
                remaining = &remaining[edge.len()..];
                if remaining.is_empty() {
                    let mut results = vec![];
                    self.trie_collect_paths(&node, String::new(), &mut results)?;
                    return Ok(results.into_iter().filter(|p| p.starts_with(prefix)).collect());
                }
                let first_char = remaining.chars().next().unwrap();
                if let Some(&child_id) = node.children.get(&first_char) {
                    current_page_id = child_id;
                } else {
                    return Ok(vec![]);
                }
            } else {
                return Ok(vec![]);
            }
        }
        let node = self.deserialize_trie_node(&self.read_raw_page(current_page_id)?)?;
        let mut results = vec![];
        self.trie_collect_paths(&node, String::new(), &mut results)?;
        Ok(results.into_iter().filter(|p| p.starts_with(prefix)).collect())
    }

    fn list_paths_for_document(&self, id: Uuid) -> io::Result<Vec<String>> {
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "ID not found"));
        }
        Ok(document.paths)
    }

    fn count_free_pages(&self) -> io::Result<i64> {
        let mut count = 0;
        let mut current = self.free_list_root.lock().page_id;
        while current != -1 {
            let offset = current as u64 * self.config.page_size + self.config.page_header_size;
            let mut buffer = vec![0u8; FREE_LIST_HEADER_SIZE as usize];
            if let Some(mmap) = self.mmap.read().as_ref() {
                let start = offset as usize;
                buffer.copy_from_slice(&mmap[start..start + FREE_LIST_HEADER_SIZE as usize]);
            } else {
                let mut file = self.file.lock();
                file.seek(SeekFrom::Start(offset))?;
                file.read_exact(&mut buffer)?;
            }
            let mut reader = Cursor::new(buffer);
            current = reader.read_i64::<LittleEndian>()?;
            let used_entries = reader.read_i32::<LittleEndian>()?;
            count += used_entries as i64;
        }
        Ok(count)
    }

    fn get_info(&self, id: Uuid) -> io::Result<String> {
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "ID not found"));
        }
        let mut size = 0;
        let mut current_page_id = document.first_page_id;
        while current_page_id != -1 {
            let header = self.read_page_header(current_page_id)?;
            size += header.data_length as u64;
            current_page_id = header.next_page_id;
        }
        Ok(format!(
            "ID: {}, Version: {}, Size: {} bytes, Paths: {:?}",
            id, document.current_version, size, document.paths
        ))
    }

    fn delete_paths_for_document(&mut self, id: Uuid) -> io::Result<()> {
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let mut document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "ID not found"));
        }
        for path in document.paths.iter() {
            self.trie_delete(path)?;
        }
        document.paths.clear();
        let data = self.serialize_document(&document)?;
        self.write_raw_page(index_root.page_id, &data, index_root.version)?;
        Ok(())
    }

    fn remove_from_index(&mut self, id: Uuid) -> io::Result<()> {
        self.delete_paths_for_document(id)?;
        Ok(())
    }

    fn get_cache_stats(&self) -> io::Result<CacheStats> {
        Ok(self.cache_stats.lock().clone())
    }

    fn get_stream(&self, id: Uuid) -> io::Result<impl Iterator<Item = io::Result<Vec<u8>>>> {
        let index_root = self.document_index_root.lock();
        let data = self.read_raw_page(index_root.page_id)?;
        let document = self.deserialize_document(&data)?;
        if document.id != id {
            return Err(io::Error::new(io::ErrorKind::NotFound, "Document not found"));
        }
        let mut current = Some(document.first_page_id);
        Ok(std::iter::from_fn(move || {
            if let Some(page_id) = current {
                let data = self.read_raw_page(page_id);
                if let Ok(ref d) = data {
                    let header = self.read_page_header(page_id).ok()?;
                    current = if header.next_page_id != -1 { Some(header.next_page_id) } else { None };
                }
                data.map(Ok)
            } else {
                None
            }
        }))
    }
}

pub struct StreamDb {
    backend: Box<dyn DatabaseBackend + Send + Sync>,
    path_cache: Mutex<LruCache<String, Uuid>>,
    quick_mode: AtomicBool,
}

impl StreamDb {
    pub fn open_with_config<P: AsRef<Path>>(path: P, config: Config) -> io::Result<Self> {
        let
