-- SPDX-License-Identifier: LGPL-3.0-only
-- Copyright (c) 2026 DeMoD LLC. A commercial license is available on request — see LICENSING.md.
-- ============================================================================
--  dcf_history.lua — persistent chat / call history for DCF clients.
--
--  Storage is DeMoD StreamDB (https://github.com/ALH477/DeMoD-StreamDB): an
--  embedded Rust key-value store over string paths, with a compressed reverse
--  trie index and paged storage. The same engine the Lisp SDK persists node state
--  with, reused here so a DCF client has one storage story across the ecosystem.
--
--  *** THE KEY SCHEMA IS BUILT BACKWARDS ON PURPOSE. ***
--  StreamDB's index is a REVERSE trie: search(x) matches every key that ENDS WITH
--  x — a suffix match, not a prefix match. So the attribute you want to scan by
--  goes at the END of the key. Default layout:
--
--      <seq>@<src>@<channel>          e.g. "000000000042@00a1@duet"
--
--  search("@duet")        -> the whole channel
--  search("@00a1@duet")   -> one peer's traffic in that channel
--
--  Get this backwards and every query silently returns nothing. The schema is
--  configurable (see cfg.schema) but any replacement must keep the scan key last.
--
--  BACKENDS ARE PLUGGABLE. Pure Lua 5.4 has no FFI, so StreamDB is reached through
--  a host binding (demod-ui's `dm.streamdb`, or any module exposing the same six
--  calls). A dependency-free in-memory backend ships here so the module is fully
--  usable and testable with no native library present, and you can register your
--  own backend against the same interface.
--
--  Backend interface (all six required):
--    open(path, opts) -> handle        insert(h, key, value) -> ok
--    get(h, key) -> value|nil          delete(h, key) -> ok
--    search(h, suffix) -> {{key,value},...}
--    flush(h) -> ok                    close(h)
-- ============================================================================

local M = {}

-- ── Configuration ───────────────────────────────────────────────────────────
M.defaults = {
  backend   = "auto",        -- "auto" | "streamdb" | "memory" | <name you registered>
  path      = "dcf-history.streamdb",
  flush_ms  = 5000,          -- StreamDB background flush cadence
  autoflush = 50,            -- flush after N writes (0 disables)

  schema = {
    -- Field order is the KEY ORDER. Last field is what search() scans by.
    -- Change this freely; `fields` drives both writing and parsing.
    fields    = { "seq", "src", "channel" },
    separator = "@",
    seq_width = 12,          -- zero-padded so lexical order == numeric order
    src_fmt   = "%04x",      -- u16 node id
  },

  retention = {
    max_per_channel = 0,     -- 0 = unlimited; else prune oldest beyond N
    check_every     = 1,     -- prune every N appends. 1 = exact cap (default). A
                             -- prune is a full channel scan, so raising this
                             -- amortises O(n) per append -- at the cost of holding
                             -- up to check_every-1 rows above max_per_channel
                             -- between sweeps. Raise it on hot channels only.
    -- StreamDB has a known bug deleting the LAST remaining key (see
    -- DCF_CODE_REVIEW.md C5/C8/C9), so pruning always leaves one row behind.
    keep_minimum    = 1,
  },

  -- Serialisation is swappable: store JSON, msgpack, or raw — your call.
  encode = nil,              -- fn(record) -> string   (default: builtin TSV-ish)
  decode = nil,              -- fn(string) -> record

  hooks = {
    on_write  = nil,         -- fn(key, record)
    on_prune  = nil,         -- fn(key)
    on_resume = nil,         -- fn(seq)  -- existing history found on open
    on_error  = nil,         -- fn(op, err)  -- called instead of raising
  },
}

local function deep_copy(t)
  if type(t) ~= "table" then return t end
  local o = {}
  for k, v in pairs(t) do o[k] = deep_copy(v) end
  return o
end

local function deep_merge(dst, src)
  for k, v in pairs(src) do
    if type(v) == "table" and type(dst[k]) == "table" then deep_merge(dst[k], v)
    else dst[k] = v end
  end
  return dst
end

function M.configure(t)
  local cfg = deep_copy(M.defaults)
  if t then deep_merge(cfg, deep_copy(t)) end
  local s = cfg.schema
  assert(#s.fields > 0, "schema.fields must be non-empty")
  assert(s.fields[#s.fields] ~= "seq",
         "schema: the LAST field is what search() scans by; putting `seq` last " ..
         "makes channel/peer queries impossible (StreamDB is a reverse trie)")
  assert(s.separator ~= "" and not s.separator:find("%w"),
         "schema.separator must be a non-alphanumeric delimiter")
  assert(s.fields[1] == "seq",
         "schema: `seq` must be the FIRST field -- it is the zero-padded ordering " ..
         "key, and resuming an existing store parses it from the head of the key")
  assert(cfg.retention.check_every >= 1, "retention.check_every must be >= 1")
  cfg.hooks = cfg.hooks or {}
  return cfg
end

-- ── Record serialisation (override via cfg.encode/decode) ───────────────────
-- Default is a length-prefixed field list: no escaping ambiguity, no JSON dep.
local function default_encode(rec)
  local order = { "kind", "ts_us", "src", "channel", "flags", "text" }
  local out = {}
  for _, k in ipairs(order) do
    local v = rec[k]
    if v ~= nil then
      v = tostring(v)
      out[#out + 1] = ("%s:%d:%s"):format(k, #v, v)
    end
  end
  return table.concat(out, ";")
end

local function default_decode(s)
  local rec, i = {}, 1
  while i <= #s do
    local k, len, rest = s:match("^([%w_]+):(%d+):()", i)   -- %w excludes "_" in Lua
    if not k then break end
    len = tonumber(len)
    rec[k] = s:sub(rest, rest + len - 1)
    i = rest + len + 1
  end
  for _, n in ipairs({ "ts_us", "src", "channel", "flags" }) do
    if rec[n] then rec[n] = tonumber(rec[n]) or rec[n] end
  end
  return rec
end

-- ── Backends ────────────────────────────────────────────────────────────────
M.backends = {}

function M.register_backend(name, be)
  for _, fn in ipairs({ "open", "insert", "get", "delete", "search", "flush", "close" }) do
    assert(type(be[fn]) == "function", ("backend %q missing %s()"):format(name, fn))
  end
  M.backends[name] = be
  return be
end

-- In-memory reference backend. Implements the reverse-trie SUFFIX semantics
-- faithfully, so code written against it behaves identically on StreamDB.
M.register_backend("memory", {
  open   = function(_, _) return { data = {}, order = {} } end,
  insert = function(h, k, v)
    if h.data[k] == nil then h.order[#h.order + 1] = k end
    h.data[k] = v; return true
  end,
  get    = function(h, k) return h.data[k] end,
  delete = function(h, k)
    if h.data[k] == nil then return false end
    h.data[k] = nil
    for i, key in ipairs(h.order) do
      if key == k then table.remove(h.order, i) break end
    end
    return true
  end,
  search = function(h, suffix)
    local out = {}
    for _, k in ipairs(h.order) do
      if suffix == "" or k:sub(-#suffix) == suffix then      -- SUFFIX, not prefix
        out[#out + 1] = { k, h.data[k] }
      end
    end
    return out
  end,
  flush  = function() return true end,
  close  = function(h) h.data, h.order = {}, {} end,
})

-- StreamDB backend. Binds to whatever the host exposes; checked in order:
--   1. an explicit cfg.streamdb module
--   2. dm.streamdb  (the demod-ui C binding over libstreamdb.so)
--   3. require("streamdb")
-- The C ABI it wraps is the one python/MCP/streamdb.py already uses:
--   streamdb_init/insert/get/delete/prefix_search/free_results/flush/free
local function find_streamdb(cfg)
  if cfg and type(cfg.streamdb) == "table" then return cfg.streamdb end
  local g = _G.dm
  if type(g) == "table" and type(g.streamdb) == "table" then return g.streamdb end
  local ok, mod = pcall(require, "streamdb")
  if ok and type(mod) == "table" then return mod end
  return nil
end

M.register_backend("streamdb", {
  open = function(path, opts)
    local sdb = find_streamdb(opts)
    if not sdb then return nil, "no StreamDB binding (dm.streamdb / require'streamdb')" end
    local h = sdb.open(path, opts and opts.flush_ms or 5000)
    if not h then return nil, "streamdb_init failed" end
    return { sdb = sdb, h = h }
  end,
  insert = function(s, k, v) return s.sdb.insert(s.h, k, v) end,
  get    = function(s, k) return s.sdb.get(s.h, k) end,
  delete = function(s, k) return s.sdb.delete(s.h, k) end,
  search = function(s, suffix) return s.sdb.search(s.h, suffix) end,  -- suffix match
  flush  = function(s) return s.sdb.flush(s.h) end,
  close  = function(s) return s.sdb.close(s.h) end,
})

-- ── History store ───────────────────────────────────────────────────────────
local History = {}
History.__index = History

--- Open a history store. Backend "auto" prefers StreamDB and transparently falls
--- back to memory when no binding is present (`h.backend_name` tells you which).
function M.open(cfg)
  cfg = (type(cfg) == "table" and cfg.schema and cfg) or M.configure(cfg)
  local names = (cfg.backend == "auto") and { "streamdb", "memory" } or { cfg.backend }

  local handle, chosen, last_err
  for _, name in ipairs(names) do
    local be = M.backends[name] or error("unknown backend: " .. tostring(name))
    local h, err = be.open(cfg.path, cfg)
    if h then handle, chosen = h, name break end
    last_err = err
  end
  if not handle then
    error("could not open history: " .. tostring(last_err))
  end

  local self = setmetatable({
    cfg = cfg, be = M.backends[chosen], h = handle, backend_name = chosen,
    seq = 0, writes = 0, since_prune = 0,
    enc = cfg.encode or default_encode,
    dec = cfg.decode or default_decode,
  }, History)

  -- Resume the sequence counter from what is already stored. Without this a
  -- reopened store restarts at 1 and every append OVERWRITES an existing row --
  -- silent history loss, and the corruption is invisible until you query.
  local ok, rows = pcall(self.be.search, self.h, "")
  if ok and rows then
    local w, sep = cfg.schema.seq_width, cfg.schema.separator
    local pat = "^(%d+)" .. (sep:gsub("%W", "%%%0"))
    local maxseq = 0
    for _, kv in ipairs(rows) do
      local n = tonumber((kv[1] or ""):match(pat) or (kv[1] or ""):sub(1, w))
      if n and n > maxseq then maxseq = n end
    end
    self.seq = maxseq
    if maxseq > 0 and cfg.hooks.on_resume then cfg.hooks.on_resume(maxseq) end
  end
  return self
end

--- Build a key from a record using cfg.schema. Scan field goes LAST.
function History:key(rec, seq)
  local s, parts = self.cfg.schema, {}
  for _, f in ipairs(s.fields) do
    if f == "seq" then
      parts[#parts + 1] = ("%0" .. s.seq_width .. "d"):format(seq)
    elseif f == "src" then
      parts[#parts + 1] = (s.src_fmt):format(rec.src or 0)
    else
      parts[#parts + 1] = tostring(rec[f] or "")
    end
  end
  return table.concat(parts, s.separator)
end

--- Suffix selector for a query, e.g. select{channel="duet"} -> "@duet"
--- and select{channel="duet", src=0x00a1} -> "@00a1@duet".
function History:selector(q)
  local s, tail, stopped = self.cfg.schema, {}, nil
  for i = #s.fields, 1, -1 do
    local f = s.fields[i]
    local v = q[f]
    if v == nil then stopped = i break end
    if f == "src" then v = (s.src_fmt):format(v) end
    table.insert(tail, 1, tostring(v))
  end
  -- A reverse trie can only answer SUFFIX queries, so the constrained fields must
  -- form an unbroken run from the end of the key. Asking for {src=...} with no
  -- channel is not a narrower query -- it degrades to "match everything", which
  -- looks like a working query returning wrong rows. Refuse it instead.
  if stopped then
    for i = 1, stopped do
      if q[s.fields[i]] ~= nil then
        error(("cannot query by %q without also constraining %q: StreamDB matches " ..
               "by key SUFFIX, so query fields must form a contiguous run from the " ..
               "end of the schema (%s)"):format(s.fields[i], s.fields[stopped],
               table.concat(s.fields, s.separator)))
      end
    end
  end
  if #tail == 0 then return "" end
  return s.separator .. table.concat(tail, s.separator)
end

--- Append a record. rec needs at least {channel=...}; src/ts_us/text/kind optional.
function History:append(rec)
  self.seq = self.seq + 1
  local key = self:key(rec, self.seq)
  local ok, err = pcall(function()
    assert(self.be.insert(self.h, key, self.enc(rec)))
  end)
  if not ok then
    if self.cfg.hooks.on_error then self.cfg.hooks.on_error("append", err) return nil end
    error(err)
  end
  self.writes = self.writes + 1
  if self.cfg.hooks.on_write then self.cfg.hooks.on_write(key, rec) end
  if self.cfg.autoflush > 0 and self.writes % self.cfg.autoflush == 0 then self:flush() end
  if self.cfg.retention.max_per_channel > 0 then
    self.since_prune = self.since_prune + 1
    if self.since_prune >= self.cfg.retention.check_every then
      self.since_prune = 0
      self:prune(rec.channel)
    end
  end
  return key
end

--- Query by any suffix of the schema, newest-last. opts.limit takes the newest N.
---   h:query{ channel = "duet" }
---   h:query({ channel = "duet", src = 0x00a1 }, { limit = 50 })
function History:query(q, opts)
  opts = opts or {}
  local rows = self.be.search(self.h, self:selector(q or {})) or {}
  table.sort(rows, function(a, b) return a[1] < b[1] end)   -- zero-padded seq sorts
  local out = {}
  for _, kv in ipairs(rows) do
    local rec = self.dec(kv[2])
    rec._key = kv[1]
    out[#out + 1] = rec
  end
  if opts.limit and #out > opts.limit then
    local trimmed = {}
    for i = #out - opts.limit + 1, #out do trimmed[#trimmed + 1] = out[i] end
    out = trimmed
  end
  return out
end

--- Drop oldest rows beyond retention.max_per_channel, never below keep_minimum
--- (StreamDB mishandles deleting the last remaining key — C5/C8/C9).
function History:prune(channel)
  local r = self.cfg.retention
  if r.max_per_channel <= 0 then return 0 end
  local rows = self.be.search(self.h, self:selector({ channel = channel })) or {}
  table.sort(rows, function(a, b) return a[1] < b[1] end)
  local excess = #rows - r.max_per_channel
  local floor_n = #rows - r.keep_minimum
  if excess > floor_n then excess = floor_n end
  local n = 0
  for i = 1, excess do
    if self.be.delete(self.h, rows[i][1]) then
      n = n + 1
      if self.cfg.hooks.on_prune then self.cfg.hooks.on_prune(rows[i][1]) end
    end
  end
  return n
end

--- Delete every row for a channel (or the whole store when channel is nil).
--- Honours retention.keep_minimum: StreamDB mishandles removing the last key.
function History:clear(channel)
  local sel = channel and self:selector({ channel = channel }) or ""
  local rows = self.be.search(self.h, sel) or {}
  table.sort(rows, function(a, b) return a[1] < b[1] end)
  local keep = self.cfg.retention.keep_minimum
  local n = 0
  for i = 1, math.max(0, #rows - (channel and 0 or keep)) do
    if self.be.delete(self.h, rows[i][1]) then n = n + 1 end
  end
  return n
end

function History:flush() return self.be.flush(self.h) end
function History:close() self:flush() return self.be.close(self.h) end
function History:count(q) return #self:query(q) end

M.History = History

return M
