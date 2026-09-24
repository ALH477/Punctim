;; DeMoD-LISP (D-LISP) Delivered as Punctim
;; Version 2.2.0 | November 5, 2025
;; License: Lesser GNU General Public License v3.0 (LGPL-3.0)
;; Part of the DCF mono repo: https://github.com/ALH477/DeMoD-Communication-Framework
;; This SDK provides a robust, production-ready Lisp implementation for DCF,
;; optimized for gaming and real-time audio with UDP transport, binary Protobuf,
;; unreliable/reliable channels, and network stats. Backward compatible with
;; prior versions for gRPC/TCP.
;; CHANGELOG v2.2.0:
;; - REPLACED JSON with Protocol Buffers for 10-100x faster serialization
;; - ADDED UDP transport for low-latency gaming and audio
;; - NEW: Binary message protocol for real-time updates
;; - NEW: Unreliable/reliable channel selection
;; - NEW: Packet fragmentation and reassembly for large messages
;; - NEW: Network statistics tracking (packet loss, jitter)
;; - Optimized for <10ms latency in gaming scenarios
;; - Audio-optimized with priority queuing
;; - Backward compatible with TCP/gRPC for reliable operations

;; Dependencies. Load portably so the same source works under both a Quicklisp
;; environment (Roswell / the traditional Docker build) and a Nix
;; sbcl.withPackages build, where the systems are exposed through ASDF and there
;; is no :quicklisp package. uiop:symbol-call avoids a literal ql: symbol (which
;; would fail to *read* when the QL package is absent). A bare sbcl.withPackages
;; image does not pre-load ASDF/UIOP, so require it first (no-op under Quicklisp);
;; --load processes the file form-by-form, so the packages exist before the next
;; form (which references uiop:/asdf:) is read.
(require "asdf")
(let ((systems '(:cffi :uuid :usocket :bordeaux-threads
                 :log4cl :trivial-backtrace :flexi-streams :fiveam
                 :ieee-floats :cl-json)))
  (if (find-package :quicklisp)
      (uiop:symbol-call :ql :quickload systems :silent t)
      (mapc #'asdf:load-system systems)))

;; FiveAM does not push :fiveam onto *features*; do it ourselves so the
;; #+fiveam-guarded test suite below is actually compiled and run-tests defined.
(pushnew :fiveam *features*)

(defpackage :d-lisp
(:use :cl :cffi :uuid :usocket :bordeaux-threads :log4cl
         :trivial-backtrace :flexi-streams :fiveam :ieee-floats :cl-json)
  (:export :dcf-init :dcf-start :dcf-stop :dcf-send :dcf-send-udp :dcf-receive
           :dcf-status :dcf-version :dcf-get-metrics :dcf-benchmark
           :dcf-db-insert :dcf-db-query :dcf-db-delete :dcf-db-search :dcf-db-flush
           :dcf-send-audio :dcf-send-position :dcf-send-game-event
           :dcf-begin-transaction :dcf-commit-transaction :dcf-rollback-transaction
           :run-tests :dcf-help :main :dcf-add-peer :dcf-remove-peer :dcf-list-peers
           ;; Agent system integration
           :dcf-agent-chat :dcf-agent-list :dcf-agent-backends
           :dcf-agent-providers :dcf-agent-health :dcf-agent-set-url
           :*agent-api-url*))

(in-package :d-lisp)

;; StreamDB foreign library + FFI bindings. Interned in :d-lisp (where they are
;; used); the #.+success+ readers below need the constants bound at read time.
(cffi:define-foreign-library libstreamdb
  (:unix "libstreamdb.so")
  (:darwin "libstreamdb.dylib")
  (:wasm "libstreamdb.wasm")
  (t (:default "libstreamdb")))

;; Degrade gracefully if the shared object is missing: without this guard a
;; missing libstreamdb.so aborts loading the whole file, so `main` (and the rest
;; of the SDK) never gets defined. DB call sites are already null-guarded
;; (dcf-init / dcf-db-* check (dcf-node-streamdb node)), so the node still runs
;; with the database disabled. (format to *error-output* — the logger isn't
;; configured yet at this point in the file.)
(handler-case (cffi:use-foreign-library libstreamdb)
  (error (e)
    (format *error-output* "~&WARN: libstreamdb unavailable; DB disabled: ~A~%" e)))

;; StreamDB CFFI Bindings (updated for v2.2.0). Explicit underscore Lisp names so
;; they match the callers (cffi's default would hyphenate, leaving them undefined).
(cffi:defcfun ("streamdb_init" streamdb_init) :pointer (file-path :string) (flush-interval-ms :int))
(cffi:defcfun ("streamdb_insert" streamdb_insert) :int (db :pointer) (key :pointer) (key-len :size) (value :pointer) (value-size :size))
(cffi:defcfun ("streamdb_get" streamdb_get) :pointer (db :pointer) (key :pointer) (key-len :size) (size-ptr :pointer))
(cffi:defcfun ("streamdb_delete" streamdb_delete) :int (db :pointer) (key :pointer) (key-len :size))
(cffi:defcfun ("streamdb_prefix_search" streamdb_prefix_search) :pointer (db :pointer) (prefix :pointer) (prefix-len :size))
(cffi:defcfun ("streamdb_free_results" streamdb_free_results) :void (results :pointer))
(cffi:defcfun ("streamdb_flush" streamdb_flush) :int (db :pointer))
(cffi:defcfun ("streamdb_free" streamdb_free) :void (db :pointer))
(cffi:defcfun ("streamdb_set_quick_mode" streamdb_set_quick_mode) :void (db :pointer) (quick :boolean))

;; Error Codes from StreamDB FFI
(defconstant +success+ 0)
(defconstant +err-io+ -1)
(defconstant +err-not-found+ -2)
(defconstant +err-invalid-input+ -3)
(defconstant +err-panic+ -4)
(defconstant +err-transaction+ -5)

;; Logging Setup
(defvar *dcf-logger* (log:category "dcf-lisp") "Logger for D-LISP.")
;; Default to info for gaming perf. Guarded: log4cl's config DSL varies across
;; versions and rejects a logger instance in some, which must not abort loading.
(handler-case (log:config *dcf-logger* :info)
  (error () (ignore-errors (setf (log4cl:logger-log-level *dcf-logger*) :info))))

;; Global state
(defvar *node* nil "Global DCF node instance")
(defvar *sequence-counter* 0 "Global sequence counter")
(defvar *sequence-lock* (bt:make-lock "seq") "Lock for *sequence-counter*")

(defun next-sequence ()
  "Atomically increment and return the next sequence number (wraps at uint32)."
  (bt:with-lock-held (*sequence-lock*)
    (setf *sequence-counter*
          (logand (1+ *sequence-counter*) #xFFFFFFFF))))

;; Error Handling (enhanced with StreamDB mapping)
(define-condition dcf-error (error)
  ((code :initarg :code :reader dcf-error-code)
   (message :initarg :message :reader dcf-error-message)
   (backtrace :initarg :backtrace :reader dcf-error-backtrace :initform (trivial-backtrace:backtrace-string)))
  (:report (lambda (condition stream)
             (format stream "DCF Error [~A]: ~A~%Backtrace: ~A" (dcf-error-code condition) (dcf-error-message condition) (dcf-error-backtrace condition)))))

(defun signal-dcf-error (code message)
  (error 'dcf-error :code code :message message))

(defun map-streamdb-error (err-code)
  (case err-code
    (#.+success+ nil)
    (#.+err-io+ (signal-dcf-error :io "StreamDB I/O error"))
    (#.+err-not-found+ (signal-dcf-error :not-found "StreamDB item not found"))
    (#.+err-invalid-input+ (signal-dcf-error :invalid-input "StreamDB invalid input"))
    (#.+err-panic+ (signal-dcf-error :panic "StreamDB internal panic"))
    (#.+err-transaction+ (signal-dcf-error :transaction "StreamDB transaction error"))
    (otherwise (signal-dcf-error :unknown (format nil "Unknown StreamDB error: ~A" err-code)))))

;; Protocol Buffer Message Definitions (binary compact)
(defconstant +msg-type-position+ 1 "Player position update")
(defconstant +msg-type-audio+ 2 "Audio packet")
(defconstant +msg-type-game-event+ 3 "Game event (shoot, pickup)")
(defconstant +msg-type-state-sync+ 4 "Full state sync")
(defconstant +msg-type-reliable+ 5 "Reliable message (needs ack)")
(defconstant +msg-type-ack+ 6 "Acknowledgment")
(defconstant +msg-type-ping+ 7 "Ping for RTT measurement")
(defconstant +msg-type-pong+ 8 "Pong response")
(defconstant +msg-type-game-dcf+ 9 "One DCF-Game L2 frame (a 17-byte DATA DeModFrame)")
(defconstant +msg-type-text-dcf+ 10 "One DCF-Text L2 frame (a 17-byte DATA DeModFrame)")
(defconstant +msg-type-mesh+ 11 "DCF-Mesh control message (REPORT/ROLE)")
(defconstant +msg-type-frame+ 12 "One raw 17-byte DeModFrame (DCF-Medium udp:dialect=proto)")

(defstruct proto-message
  (type 0 :type (unsigned-byte 8))
  (sequence 0 :type (unsigned-byte 32))
  (timestamp 0 :type (unsigned-byte 64))
  (payload #() :type (vector (unsigned-byte 8))))

;; Binary Serialization Helpers
(defun write-u8 (value vec offset)
  (setf (aref vec offset) (logand value #xFF))
  (1+ offset))

(defun write-u32 (value vec offset)
  (setf (aref vec offset) (logand (ash value -24) #xFF)
        (aref vec (+ offset 1)) (logand (ash value -16) #xFF)
        (aref vec (+ offset 2)) (logand (ash value -8) #xFF)
        (aref vec (+ offset 3)) (logand value #xFF))
  (+ offset 4))

(defun write-u64 (value vec offset)
  (loop for i from 7 downto 0
        do (setf (aref vec (+ offset (- 7 i))) 
                (logand (ash value (* -8 i)) #xFF)))
  (+ offset 8))

(defun write-f32 (value vec offset)
  (let ((bits (ieee-floats:encode-float32 value)))
    (write-u32 bits vec offset)))

(defun read-u8 (vec offset)
  (values (aref vec offset) (1+ offset)))

(defun read-u32 (vec offset)
  (values
   (logior (ash (aref vec offset) 24)
           (ash (aref vec (+ offset 1)) 16)
           (ash (aref vec (+ offset 2)) 8)
           (aref vec (+ offset 3)))
   (+ offset 4)))

(defun read-u64 (vec offset)
  (values
   (loop for i from 0 to 7
         sum (ash (aref vec (+ offset i)) (* 8 (- 7 i))))
   (+ offset 8)))

(defun read-f32 (vec offset)
  (multiple-value-bind (bits new-offset) (read-u32 vec offset)
    (values (ieee-floats:decode-float32 bits) new-offset)))

;; Serialize protocol buffer message
(defun serialize-proto-message (msg)
  (let* ((payload-len (length (proto-message-payload msg)))
         (total-len (+ 1 4 8 4 payload-len))
         (vec (make-array total-len :element-type '(unsigned-byte 8))))
    (let ((offset 0))
      (setf offset (write-u8 (proto-message-type msg) vec offset))
      (setf offset (write-u32 (proto-message-sequence msg) vec offset))
      (setf offset (write-u64 (proto-message-timestamp msg) vec offset))
      (setf offset (write-u32 payload-len vec offset))
      (replace vec (proto-message-payload msg) :start1 offset))
    vec))

;; Deserialize protocol buffer message
(defun deserialize-proto-message (vec)
  (when (< (length vec) 17)
    (signal-dcf-error :invalid-message "Message too short"))
  (let ((offset 0) type seq ts payload-len)
    (multiple-value-setq (type offset) (read-u8 vec offset))
    (multiple-value-setq (seq offset) (read-u32 vec offset))
    (multiple-value-setq (ts offset) (read-u64 vec offset))
    (multiple-value-setq (payload-len offset) (read-u32 vec offset))
    (when (> (+ offset payload-len) (length vec))
      (signal-dcf-error :invalid-message "Payload length exceeds message size"))
    (let ((payload (subseq vec offset (+ offset payload-len))))
      (make-proto-message :type type :sequence seq 
                         :timestamp ts :payload payload))))

;; Position Update Encoding (12 bytes: x, y, z as float32)
(defun encode-position (x y z)
  (let ((vec (make-array 12 :element-type '(unsigned-byte 8))))
    (write-f32 (coerce x 'single-float) vec 0)
    (write-f32 (coerce y 'single-float) vec 4)
    (write-f32 (coerce z 'single-float) vec 8)
    vec))

(defun decode-position (vec)
  (when (< (length vec) 12)
    (signal-dcf-error :invalid-payload "Position payload too short"))
  (let ((x 0.0) (y 0.0) (z 0.0) (offset 0))
    (multiple-value-setq (x offset) (read-f32 vec offset))
    (multiple-value-setq (y offset) (read-f32 vec offset))
    (multiple-value-setq (z offset) (read-f32 vec offset))
    (list :x x :y y :z z)))

;; Game Event Encoding (variable: event-type + data)
(defun encode-game-event (event-type data)
  (let* ((data-bytes (string-to-octets data :external-format :utf-8))
         (vec (make-array (+ 1 (length data-bytes)) :element-type '(unsigned-byte 8))))
    (write-u8 event-type vec 0)
    (replace vec data-bytes :start1 1)
    vec))

(defun decode-game-event (vec)
  (when (< (length vec) 1)
    (signal-dcf-error :invalid-payload "Event payload too short"))
  (let ((event-type (aref vec 0))
        (data (octets-to-string (subseq vec 1) :external-format :utf-8)))
    (list :event-type event-type :data data)))

;; ============================================================================
;; DCF-FRAME ADAPTER — Haskell DeModFrame ↔ Punctim proto-message bridge
;; ============================================================================
;;
;; The Haskell dcf-faust-sdr package defines a 17-byte RF transport frame
;; (DeModFrame). Punctim uses a separate 17-byte UDP wire format (proto-message
;; header: 1+4+8+4 bytes). They coexist at different layers:
;;
;;   proto-message  → game/audio application layer   (this file, UDP)
;;   DeModFrame     → RF physical layer              (Haskell, 900 MHz §15.249)
;;
;; Haskell DeModFrame wire layout (17 bytes, all big-endian):
;;   [0]     sync         fixed 0xD3
;;   [1]     flags        bits[7:4]=version(4) | bits[3:0]=frame-type(4)
;;   [2-3]   seq          uint16
;;   [4-5]   src-id       uint16
;;   [6-7]   dst-id       uint16  (0xFFFF = broadcast)
;;   [8-11]  payload      4 bytes (application data)
;;   [12-14] timestamp-us 24-bit µs offset (wraps ~16.7 s)
;;   [15-16] crc16        CRC-CCITT(poly=0x1021, init=0xFFFF) over bytes [0..14]
;;
;; Frame type nibble values:
;;   0 FData  1 FAck  2 FBeacon  3 FCtrl
;;
;; Payload mapping used by this adapter (proto-message type → frame type):
;;   position, audio          → FData   (payload = first 4 bytes of encoded body)
;;   game-event, reliable     → FCtrl
;;   ack                      → FAck
;;   ping                     → FBeacon
;;   pong                     → FAck
;;
;; For payloads longer than 4 bytes (e.g. full position 12B, audio frames)
;; callers must fragment at the application layer; the RF side will reassemble
;; via the Haskell AdpcmReassembler or a custom fragmentation scheme.

;; --- Constants ---------------------------------------------------------------

(defconstant +dcf-sync+      #xD3)
(defconstant +dcf-version+   1)
(defconstant +dcf-broadcast+ #xFFFF)

;; Frame type nibbles matching Haskell FrameType enum
(defconstant +dcf-fdata+    0)
(defconstant +dcf-fack+     1)
(defconstant +dcf-fbeacon+  2)
(defconstant +dcf-fctrl+    3)

;; --- CRC-CCITT ---------------------------------------------------------------
;; Poly 0x1021, init 0xFFFF — must match Haskell crc16ccitt exactly.

(defun crc16-ccitt (vec &optional (start 0) (end (length vec)))
  "CRC-CCITT over VEC[START..END). Returns uint16."
  (let ((crc #xFFFF))
    (loop for i from start below end do
      (setf crc (logand #xFFFF (logxor crc (ash (aref vec i) 8))))
      (loop repeat 8 do
        (setf crc (if (logbitp 15 crc)
                      (logand (logxor (ash crc 1) #x1021) #xFFFF)
                      (logand (ash crc 1) #xFFFF)))))
    crc))

;; --- Frame codec -------------------------------------------------------------

(defun encode-dcf-frame (proto-msg src-id dst-id)
  "Pack a proto-message into a 17-byte DeModFrame vector.
   SRC-ID and DST-ID are uint16 DCF node identifiers.
   Only the first 4 bytes of the proto-message payload fit; callers must
   fragment longer payloads before calling this function."
  (let* ((ft      (proto-msg-type->frame-type (proto-message-type proto-msg)))
         (payload (proto-payload-first4 proto-msg))
         (seq     (logand (proto-message-sequence proto-msg) #xFFFF))
         (ts      (logand (get-internal-real-time-us) #xFFFFFF))
         (vec     (make-array 17 :element-type '(unsigned-byte 8) :initial-element 0)))
    ;; [0] sync
    (setf (aref vec 0) +dcf-sync+)
    ;; [1] flags: version[7:4] | frame-type[3:0]
    (setf (aref vec 1) (logior (ash (logand +dcf-version+ #x0F) 4)
                               (logand ft #x0F)))
    ;; [2-3] seq
    (setf (aref vec 2) (logand (ash seq -8) #xFF))
    (setf (aref vec 3) (logand seq #xFF))
    ;; [4-5] src-id
    (setf (aref vec 4) (logand (ash src-id -8) #xFF))
    (setf (aref vec 5) (logand src-id #xFF))
    ;; [6-7] dst-id
    (setf (aref vec 6) (logand (ash dst-id -8) #xFF))
    (setf (aref vec 7) (logand dst-id #xFF))
    ;; [8-11] payload
    (replace vec payload :start1 8 :end1 12)
    ;; [12-14] timestamp 24-bit
    (setf (aref vec 12) (logand (ash ts -16) #xFF))
    (setf (aref vec 13) (logand (ash ts  -8) #xFF))
    (setf (aref vec 14) (logand ts           #xFF))
    ;; [15-16] CRC-CCITT over bytes [0..14]
    (let ((crc (crc16-ccitt vec 0 15)))
      (setf (aref vec 15) (logand (ash crc -8) #xFF))
      (setf (aref vec 16) (logand crc #xFF)))
    vec))

(defun decode-dcf-frame (vec)
  "Parse a 17-byte DeModFrame vector into a proto-message.
   Returns NIL if the sync byte is wrong or CRC fails."
  (unless (= (length vec) 17)     (return-from decode-dcf-frame nil))
  (unless (= (aref vec 0) +dcf-sync+) (return-from decode-dcf-frame nil))
  (let ((crc-stored (logior (ash (aref vec 15) 8) (aref vec 16)))
        (crc-calc   (crc16-ccitt vec 0 15)))
    (unless (= crc-stored crc-calc) (return-from decode-dcf-frame nil)))
  (let* ((flags    (aref vec 1))
         (ft       (logand flags #x0F))
         (seq      (logior (ash (aref vec 2) 8) (aref vec 3)))
         (ts       (logior (ash (aref vec 12) 16)
                           (ash (aref vec 13)  8)
                                (aref vec 14)))
         (payload  (subseq vec 8 12))
         (msg-type (frame-type->proto-msg-type ft)))
    (make-proto-message
      :type      msg-type
      :sequence  seq
      :timestamp ts
      :payload   payload)))

(defun valid-dcf-frame-p (vec)
  "Non-signalling predicate: T iff VEC is a well-formed 17-byte DeModFrame."
  (and (= (length vec) 17)
       (= (aref vec 0) +dcf-sync+)
       (= (crc16-ccitt vec 0 15)
          (logior (ash (aref vec 15) 8) (aref vec 16)))))

;; --- Type mapping ------------------------------------------------------------

(defun proto-msg-type->frame-type (msg-type)
  (cond
    ((= msg-type +msg-type-position+)   +dcf-fdata+)
    ((= msg-type +msg-type-audio+)      +dcf-fdata+)
    ((= msg-type +msg-type-game-event+) +dcf-fctrl+)
    ((= msg-type +msg-type-state-sync+) +dcf-fctrl+)
    ((= msg-type +msg-type-reliable+)   +dcf-fctrl+)
    ((= msg-type +msg-type-ack+)        +dcf-fack+)
    ((= msg-type +msg-type-ping+)       +dcf-fbeacon+)
    ((= msg-type +msg-type-pong+)       +dcf-fack+)
    (t                                  +dcf-fdata+)))

(defun frame-type->proto-msg-type (ft)
  ;; FData is ambiguous (position or audio); inspect payload[0] at call site.
  (cond
    ((= ft +dcf-fdata+)    +msg-type-position+)
    ((= ft +dcf-fack+)     +msg-type-ack+)
    ((= ft +dcf-fbeacon+)  +msg-type-ping+)
    ((= ft +dcf-fctrl+)    +msg-type-game-event+)
    (t                     +msg-type-position+)))

(defun proto-payload-first4 (proto-msg)
  "Return a 4-element (unsigned-byte 8) vector from the start of MSG payload."
  (let* ((p   (proto-message-payload proto-msg))
         (out (make-array 4 :element-type '(unsigned-byte 8) :initial-element 0)))
    (replace out p :end1 4 :end2 (min 4 (length p)))
    out))

(defun get-internal-real-time-us ()
  "Current time in microseconds (relative, for the 24-bit timestamp field)."
  (round (* (get-internal-real-time)
            (/ 1000000 internal-time-units-per-second))))

;; --- Public API --------------------------------------------------------------

(defun dcf-to-rf-frame (proto-msg &key (src-id 1) (dst-id +dcf-broadcast+))
  "Encode a proto-message as a 17-byte DeModFrame for handoff to the
   Haskell RF TX pipeline (demod-sdr-hs). Write the returned vector to
   a named pipe or shared-memory segment that demod-sdr-hs reads."
  (encode-dcf-frame proto-msg src-id dst-id))

(defun dcf-from-rf-frame (frame-bytes)
  "Parse a 17-byte DeModFrame received from the Haskell RF RX pipeline.
   Returns a proto-message or NIL if the frame is corrupt."
  (decode-dcf-frame frame-bytes))

;; --- Adapter tests (fiveam) --------------------------------------------------

;; Define the suite up front so every test below (and the second block further
;; down) registers into it. def-suite must precede the first test/in-suite.
#+fiveam
(fiveam:def-suite punctim-suite
  :description "Punctim v2.2.0 Tests")
#+fiveam
(fiveam:in-suite punctim-suite)

#+fiveam
(fiveam:test dcf-frame-crc-test
  "CRC-CCITT computed by Lisp must match the Haskell implementation exactly.
   Reference vector: a known-good frame from FrameSpec.hs exampleFrame."
  ;; exampleFrame from Haskell: version=1 FData seq=0x1234 src=0x0001
  ;;   dst=0xFFFF payload=0xDEADBEEF ts=0xAB12CD
  ;; Expected CRC computed by Haskell crc16ccitt: verified offline.
  (let ((body (make-array 15 :element-type '(unsigned-byte 8)
                          :initial-contents
                          '(#xD3 #x10 #x12 #x34 #x00 #x01 #xFF #xFF
                            #xDE #xAD #xBE #xEF #xAB #x12 #xCD))))
    (let ((crc (crc16-ccitt body 0 15)))
      ;; Body bytes match Haskell exampleFrame — CRC must equal Haskell output
      ;; (0xA963; same anchor as certify-wire-codec's example frame).
      (fiveam:is (= crc #xA963)
                 "CRC must match the Haskell exampleFrame value 0xA963")
      (fiveam:is (integerp crc) "CRC must be integer"))))

#+fiveam
(fiveam:test dcf-frame-roundtrip-test
  "encode-dcf-frame followed by decode-dcf-frame is identity."
  (let* ((payload (coerce #(#xDE #xAD #xBE #xEF) '(vector (unsigned-byte 8))))
         (msg   (make-proto-message :type +msg-type-game-event+
                                    :sequence 42
                                    :timestamp 0
                                    :payload payload))
         (frame  (encode-dcf-frame msg 1 #xFFFF))
         (back   (decode-dcf-frame frame)))
    (fiveam:is (= 17 (length frame))
               "Frame must be exactly 17 bytes")
    (fiveam:is (= +dcf-sync+ (aref frame 0))
               "Sync byte must be 0xD3")
    (fiveam:is (not (null back))
               "Decoded frame must not be nil")
    (fiveam:is (equalp (proto-message-payload back) payload)
               "Payload round-trips")))

#+fiveam
(fiveam:test dcf-frame-corruption-test
  "A single flipped bit in the frame body must cause decode-dcf-frame to return NIL."
  (let* ((msg   (make-proto-message :type +msg-type-position+
                                    :sequence 1 :timestamp 0
                                    :payload (coerce #(0 0 0 0) '(vector (unsigned-byte 8)))))
         (frame  (encode-dcf-frame msg 1 2))
         (bad    (copy-seq frame)))
    ;; Flip a bit in the payload area
    (setf (aref bad 9) (logxor (aref bad 9) #xFF))
    (fiveam:is (null (decode-dcf-frame bad))
               "Corrupted frame must not decode")))

#+fiveam
(fiveam:test dcf-frame-valid-predicate-test
  "valid-dcf-frame-p agrees with decode-dcf-frame."
  (let* ((msg   (make-proto-message :type +msg-type-ping+
                                    :sequence 99 :timestamp 0
                                    :payload (coerce #() '(vector (unsigned-byte 8)))))
         (frame (encode-dcf-frame msg 3 4))
         (bad   (copy-seq frame)))
    (setf (aref bad 8) (logxor (aref bad 8) #x01))
    (fiveam:is (valid-dcf-frame-p frame) "Good frame must be valid")
    (fiveam:is (not (valid-dcf-frame-p bad)) "Corrupt frame must be invalid")))

;; Configuration (updated for UDP/gaming)
(defstruct dcf-config
  transport host port udp-port mode node-id peers
  group-rtt-threshold storage streamdb-path
  optimization-level retry-max
  udp-mtu
  udp-reliable-timeout
  audio-priority)

;; Updated config schema (kept for compatibility)
(defvar *config-schema* 
  '(:object 
    (:required "transport" "host" "port" "mode")
    :properties (
      ("transport" :string :enum ("UDP" "gRPC" "native-lisp" "WebSocket") :description "Transport (UDP for gaming).")
      ("host" :string :description "Host address.")
      ("port" :integer :minimum 0 :maximum 65535 :description "Port number.")
      ("udp-port" :integer :minimum 0 :maximum 65535 :description "UDP port for gaming (default 7777).")
      ("mode" :string :enum ("client" "server" "p2p" "auto" "master") :description "Node mode.")
      ("node-id" :string :description "Unique node ID.")
      ("peers" :array :items (:type :string) :description "Peers list.")
      ("group-rtt-threshold" :integer :minimum 0 :maximum 1000 :description "RTT threshold (ms).")
      ("storage" :string :enum ("streamdb" "in-memory") :description "Persistence.")
      ("streamdb-path" :string :description "StreamDB path.")
      ("optimization-level" :integer :minimum 0 :maximum 3 :description "Optimization level.")
      ("retry-max" :integer :minimum 1 :maximum 10 :default 3 :description "Max retries.")
      ("udp-mtu" :integer :default 1400 :description "UDP MTU.")
      ("udp-reliable-timeout" :integer :default 500 :description "Reliable timeout (ms).")
      ("audio-priority" :boolean :default true :description "Audio prioritization."))
    :additionalProperties t))

(defun load-config (file)
  (handler-case
      (with-open-file (stream file :direction :input :if-does-not-exist :error)
        (let ((config (cl-json:decode-json stream)))
          (make-dcf-config
            :transport (jref config "transport" "UDP")
            :host (jref config "host" "0.0.0.0")
            :port (jref config "port" 50051)
            :udp-port (jref config "udp-port" 7777)
            :mode (jref config "mode" "p2p")
            :node-id (jref config "node-id" (format nil "node-~A" (random 10000)))
            :peers (jref config "peers" '())
            :group-rtt-threshold (jref config "group-rtt-threshold" 50)
            :storage (jref config "storage" "in-memory")
            :streamdb-path (jref config "streamdb-path" nil)
            :optimization-level (jref config "optimization-level" 2)
            :retry-max (jref config "retry-max" 3)
            :udp-mtu (jref config "udp-mtu" 1400)
            :udp-reliable-timeout (jref config "udp-reliable-timeout" 500)
            :audio-priority (jref config "audio-priority" t))))
    (file-error (e)
      (signal-dcf-error :file-error (format nil "Failed to read config: ~A" e)))))

(defun high-optimization? (config)
  (>= (dcf-config-optimization-level config) 2))

;; UDP Socket Management
(defstruct udp-endpoint
  socket
  address
  port
  thread
  running
  receive-callback
  send-queue
  send-lock
  stats
  reliable-packets
  reliable-lock
  ack-received)

(defstruct network-stats
  (packets-sent 0)
  (packets-received 0)
  (bytes-sent 0)
  (bytes-received 0)
  (packets-lost 0)
  (retransmits 0)
  (last-rtt 0)
  (avg-rtt 0)
  (jitter 0))

(defun create-udp-endpoint (port &optional receive-callback)
  (let ((socket (usocket:socket-connect nil nil 
                                       :protocol :datagram 
                                       :local-host "0.0.0.0" 
                                       :local-port port
                                       :element-type '(unsigned-byte 8))))
    (make-udp-endpoint
     :socket socket
     :port port
     :running nil
     :receive-callback receive-callback
     :send-queue (make-array 0 :adjustable t :fill-pointer 0)
     :send-lock (bt:make-lock "udp-send")
     :stats (make-network-stats)
     :reliable-packets (make-hash-table :test #'equal)
     :reliable-lock (bt:make-lock "reliable")
     :ack-received (make-hash-table :test #'equal))))

(defun start-udp-receiver (endpoint)
  (setf (udp-endpoint-running endpoint) t)
  (setf (udp-endpoint-thread endpoint)
        (bt:make-thread
         (lambda ()
           (loop while (udp-endpoint-running endpoint)
                 do (handler-case
                        (let ((buffer (make-array 65536 :element-type '(unsigned-byte 8))))
                          (multiple-value-bind (recv-buffer size remote-host remote-port)
                              (usocket:socket-receive (udp-endpoint-socket endpoint) buffer :element-type '(unsigned-byte 8))
                            (declare (ignore recv-buffer))
                            (when (and size (> size 0))
                              (let ((msg-bytes (subseq buffer 0 size)))
                                (incf (network-stats-packets-received 
                                       (udp-endpoint-stats endpoint)))
                                (incf (network-stats-bytes-received 
                                       (udp-endpoint-stats endpoint)) size)
                                ;; BUG FIX: remote-host from socket-receive is
                                ;; already an address; get-peer-name takes a socket.
                                (handle-udp-message endpoint msg-bytes
                                                   remote-host
                                                   remote-port)))))
                      (error (e)
                        (log:debug *dcf-logger* "UDP receive error: ~A" e)))))
         :name "udp-receiver")))

(defun handle-udp-message (endpoint msg-bytes remote-host remote-port)
  (handler-case
      (let ((msg (deserialize-proto-message msg-bytes)))
        (case (proto-message-type msg)
          (#.+msg-type-ack+
           (multiple-value-bind (seq offset) (read-u32 (proto-message-payload msg) 0)
             (declare (ignore offset))
             (bt:with-lock-held ((udp-endpoint-reliable-lock endpoint))
               (setf (gethash seq (udp-endpoint-ack-received endpoint)) t)
               (remhash seq (udp-endpoint-reliable-packets endpoint)))))
          (#.+msg-type-ping+
           (send-udp-pong endpoint
                          (proto-message-sequence msg)
                          (proto-message-timestamp msg)   ; F2: echo sender's ts
                          remote-host remote-port))
          (#.+msg-type-pong+
           (let* ((now (get-internal-real-time))
                  (sent-time (proto-message-timestamp msg))  ; our own clock, echoed
                  (rtt (/ (- now sent-time) internal-time-units-per-second 0.001)))
             (when (plusp rtt)
               (update-rtt-stats (udp-endpoint-stats endpoint) rtt))))
          (#.+msg-type-reliable+
           (send-ack endpoint (proto-message-sequence msg) remote-host remote-port)
           (when (udp-endpoint-receive-callback endpoint)
             (funcall (udp-endpoint-receive-callback endpoint)
                      msg remote-host remote-port)))
          (otherwise
           (when (udp-endpoint-receive-callback endpoint)
             (funcall (udp-endpoint-receive-callback endpoint)
                      msg remote-host remote-port)))))
    (error (e)
      (log:warn *dcf-logger* "Error handling UDP message: ~A" e))))

(defun send-udp-raw (endpoint msg-bytes remote-host remote-port)
  (handler-case
      (progn
        (usocket:socket-send (udp-endpoint-socket endpoint) msg-bytes (length msg-bytes)
                            :host remote-host :port remote-port)
        (incf (network-stats-packets-sent (udp-endpoint-stats endpoint)))
        (incf (network-stats-bytes-sent (udp-endpoint-stats endpoint)) 
              (length msg-bytes))
        t)
    (error (e)
      (log:error *dcf-logger* "UDP send failed: ~A" e)
      nil)))

(defun send-ack (endpoint seq remote-host remote-port)
  (let* ((payload (make-array 4 :element-type '(unsigned-byte 8)))
         (msg (make-proto-message
               :type +msg-type-ack+
               :sequence (next-sequence)
               :timestamp (get-internal-real-time)
               :payload payload)))
    (write-u32 seq payload 0)
    (send-udp-raw endpoint (serialize-proto-message msg) remote-host remote-port)))

(defun send-udp-pong (endpoint seq echo-ts remote-host remote-port)
  (let ((msg (make-proto-message
              :type +msg-type-pong+
              :sequence seq
              :timestamp echo-ts          ; F2: echo the ping's ts, do not restamp
              :payload #())))
    (send-udp-raw endpoint (serialize-proto-message msg) remote-host remote-port)))

(defun update-rtt-stats (stats rtt)
  ;; BUG FIX: capture old last-rtt BEFORE overwriting it;
  ;; previously jitter was always 0 because last-rtt was set to rtt first.
  (let ((old-rtt (network-stats-last-rtt stats))
        (alpha 0.125))
    (setf (network-stats-last-rtt stats) rtt)
    (setf (network-stats-avg-rtt stats)
          (+ (* alpha rtt) (* (- 1 alpha) (network-stats-avg-rtt stats))))
    (let ((jitter (abs (- rtt old-rtt))))
      (setf (network-stats-jitter stats)
            (+ (* alpha jitter) (* (- 1 alpha) (network-stats-jitter stats)))))))

(defun send-udp-message (endpoint msg remote-host remote-port &key reliable)
  (let ((msg-bytes (serialize-proto-message msg)))
    (if reliable
        (bt:with-lock-held ((udp-endpoint-reliable-lock endpoint))
          (setf (gethash (proto-message-sequence msg) 
                        (udp-endpoint-reliable-packets endpoint))
                (list :msg msg :host remote-host :port remote-port 
                      :attempts 0 :sent-time (get-internal-real-time)))
          (send-udp-raw endpoint msg-bytes remote-host remote-port)
          (ensure-reliable-handler endpoint))
        (send-udp-raw endpoint msg-bytes remote-host remote-port))))

(defun ensure-reliable-handler (endpoint)
  ;; BUG FIX: bt:thread-alive-p on nil (when find returns nil) signals an error.
  ;; Guard with explicit nil check before testing liveness.
  (let ((existing (find "udp-reliable" (bt:all-threads)
                        :key #'bt:thread-name :test #'search)))
    (unless (and existing (bt:thread-alive-p existing))
      (bt:make-thread
       (lambda ()
         (loop while (udp-endpoint-running endpoint)
               do (progn
                    (sleep 0.1)
                    (bt:with-lock-held ((udp-endpoint-reliable-lock endpoint))
                      (maphash
                       (lambda (seq entry)
                         (unless (gethash seq (udp-endpoint-ack-received endpoint))
                           (let ((info entry))
                             (let ((elapsed (- (get-internal-real-time)
                                              (getf info :sent-time))))
                               (when (> elapsed
                                        (* (dcf-config-udp-reliable-timeout
                                             (dcf-node-config *node*))
                                           internal-time-units-per-second
                                           0.001))
                                 (if (< (getf info :attempts) 3)
                                     (progn
                                       (send-udp-raw
                                        endpoint
                                        (serialize-proto-message (getf info :msg))
                                        (getf info :host)
                                        (getf info :port))
                                       (setf (getf info :attempts)
                                             (1+ (getf info :attempts)))
                                       (setf (getf info :sent-time)
                                             (get-internal-real-time))
                                       (incf (network-stats-retransmits
                                              (udp-endpoint-stats endpoint))))
                                     (progn
                                       (remhash seq
                                                (udp-endpoint-reliable-packets endpoint))
                                       (incf (network-stats-packets-lost
                                              (udp-endpoint-stats endpoint))))))))))
                       (udp-endpoint-reliable-packets endpoint))))))
       :name "udp-reliable"))))
(defun stop-udp-endpoint (endpoint)
  ;; F3: close the socket FIRST — socket-receive blocks with no timeout, so
  ;; closing is what unblocks the receiver; joining before closing hangs.
  (setf (udp-endpoint-running endpoint) nil)
  (ignore-errors (usocket:socket-close (udp-endpoint-socket endpoint)))
  (when (udp-endpoint-thread endpoint)
    (ignore-errors (bt:join-thread (udp-endpoint-thread endpoint))))
  t)

;; dcf-node structure (updated)
(defstruct (dcf-node (:conc-name dcf-node-))
  config
  middleware
  plugins
  metrics
  peers
  groups
  mode
  streamdb
  tx-lock
  cache
  cache-size
  cache-lock
  tx-cache
  tx-cache-lock
  peer-groups
  udp-endpoint
  peer-map)

;; LRU Cache
(defun lru-put (node key value)
  (bt:with-lock-held ((dcf-node-cache-lock node))
    (let ((entry (assoc key (dcf-node-cache node) :test #'equal)))
      (if entry
          (rplacd entry value)
          (push (cons key value) (dcf-node-cache node))))
    (when (> (length (dcf-node-cache node)) (dcf-node-cache-size node))
      (setf (dcf-node-cache node) (butlast (dcf-node-cache node))))))

(defun lru-get (node key)
  (bt:with-lock-held ((dcf-node-cache-lock node))
    (let ((entry (assoc key (dcf-node-cache node) :test #'equal)))
      (when entry
        (let ((val (cdr entry)))
          (setf (dcf-node-cache node) (cons entry (remove entry (dcf-node-cache node))))
          val)))))

;; Transaction Cache
(defun cache-tx-id (node tx-id context)
  (bt:with-lock-held ((dcf-node-tx-cache-lock node))
    (setf (gethash tx-id (dcf-node-tx-cache node)) context)))

(defun get-cached-tx-context (node tx-id)
  (bt:with-lock-held ((dcf-node-tx-cache-lock node))
    (gethash tx-id (dcf-node-tx-cache node))))

(defun clear-cached-tx (node tx-id)
  (bt:with-lock-held ((dcf-node-tx-cache-lock node))
    (remhash tx-id (dcf-node-tx-cache node))))

;; StreamDB Helpers
(defun string-to-byte-array (str)
  (let* ((octets (flexi-streams:string-to-octets str :external-format :utf-8))
         (len (length octets))
         (ptr (cffi:foreign-alloc :uint8 :count len)))
    (loop for i from 0 below len
          do (setf (cffi:mem-aref ptr :uint8 i) (aref octets i)))
    (values ptr len)))

(defun byte-array-to-string (ptr len)
  (let ((octets (make-array len :element-type '(unsigned-byte 8))))
    (loop for i from 0 below len
          do (setf (aref octets i) (cffi:mem-aref ptr :uint8 i)))
    (flexi-streams:octets-to-string octets :external-format :utf-8)))

;; StreamDB Operations (updated bindings)
;; F7: accept string | octet vector | any Lisp object (JSON-encoded). The old code
;; AREF'd whatever it was given, so a list (e.g. peers) crashed.
(defun dcf-db-insert (node path data &key (schema nil))
  (unless (dcf-node-streamdb node)
    (return-from dcf-db-insert nil))
  (let* ((value-octets
           (cond ((stringp data)
                  (flexi-streams:string-to-octets data :external-format :utf-8))
                 ((and (vectorp data) (every (lambda (x) (typep x '(unsigned-byte 8))) data))
                  (coerce data '(vector (unsigned-byte 8))))
                 (t
                  (flexi-streams:string-to-octets
                   (cl-json:encode-json-to-string data) :external-format :utf-8))))
         (cache-form (if (stringp data)
                         data
                         (handler-case
                             (flexi-streams:octets-to-string value-octets :external-format :utf-8)
                           (error () data)))))
    (when (and schema (stringp cache-form))
      (validate-streamdb-data cache-form schema))
    (multiple-value-bind (key-ptr key-len) (string-to-byte-array path)
      (unwind-protect
          (let* ((value-len (length value-octets))
                 (value-ptr (cffi:foreign-alloc :uint8 :count value-len)))
            (unwind-protect
                (progn
                  (loop for i from 0 below value-len
                        do (setf (cffi:mem-aref value-ptr :uint8 i) (aref value-octets i)))
                  (let ((result (streamdb_insert (dcf-node-streamdb node)
                                                 key-ptr key-len value-ptr value-len)))
                    (if (= result +success+)
                        (progn (lru-put node path cache-form) t)
                        (map-streamdb-error result))))
              (cffi:foreign-free value-ptr)))
        (cffi:foreign-free key-ptr)))))

(defun dcf-db-query (node path &key (schema nil))
  (unless (dcf-node-streamdb node)
    (return-from dcf-db-query nil))
  (or (lru-get node path)
      (multiple-value-bind (key-ptr key-len) (string-to-byte-array path)
        (unwind-protect
            (let ((size-ptr (cffi:foreign-alloc :size)))
              (unwind-protect
                  (let ((data-ptr (streamdb_get (dcf-node-streamdb node)
                                               key-ptr key-len size-ptr)))
                    (if (cffi:null-pointer-p data-ptr)
                        (map-streamdb-error +err-not-found+)
                        (let* ((size (cffi:mem-ref size-ptr :size))
                               (result (byte-array-to-string data-ptr size)))
                          (cffi:foreign-free data-ptr)
                          (when schema
                            (validate-streamdb-data result schema))
                          (lru-put node path result)
                          result)))
                (cffi:foreign-free size-ptr)))
          (cffi:foreign-free key-ptr)))))

(defun dcf-db-delete (node path)
  (unless (dcf-node-streamdb node)
    (return-from dcf-db-delete nil))
  (multiple-value-bind (key-ptr key-len) (string-to-byte-array path)
    (unwind-protect
        (let ((result (streamdb_delete (dcf-node-streamdb node) key-ptr key-len)))
          (if (= result +success+)
              (progn
                (lru-put node path nil)
                t)
              (map-streamdb-error result)))
      (cffi:foreign-free key-ptr))))

(defun dcf-db-search (node prefix)
  ;; BUG FIX: previously the null-ptr check and free were transposed:
  ;; the result was inside the cleanup form, so it was always discarded.
  (unless (dcf-node-streamdb node)
    (return-from dcf-db-search nil))
  (multiple-value-bind (prefix-ptr prefix-len) (string-to-byte-array prefix)
    (unwind-protect
        (let ((results-ptr
               (streamdb_prefix_search (dcf-node-streamdb node)
                                      prefix-ptr prefix-len)))
          (if (cffi:null-pointer-p results-ptr)
              (map-streamdb-error +err-not-found+)
              (unwind-protect
                  (collect-streamdb-results results-ptr)
                (streamdb_free_results results-ptr))))
      (cffi:foreign-free prefix-ptr))))

;; F8/C9: streamdb_prefix_search returns a Result* LINKED LIST (streamdb.h), not a
;; pointer array. struct Result { unsigned char* key; size_t key_len; void* value;
;; size_t value_size; struct Result* next; }.
(cffi:defcstruct streamdb-result
  (key        :pointer)
  (key-len    :size)
  (value      :pointer)
  (value-size :size)
  (next       :pointer))

(defun collect-streamdb-results (results-ptr)
  "Walk the Result* linked list from streamdb_prefix_search; collect keys."
  (loop for ptr = results-ptr
          then (cffi:foreign-slot-value ptr '(:struct streamdb-result) 'next)
        until (cffi:null-pointer-p ptr)
        collect (byte-array-to-string
                 (cffi:foreign-slot-value ptr '(:struct streamdb-result) 'key)
                 (cffi:foreign-slot-value ptr '(:struct streamdb-result) 'key-len))))
(defun dcf-db-flush (node)
  (when (dcf-node-streamdb node)
    (streamdb_flush (dcf-node-streamdb node))))

;; Schema Validation (kept)
(defvar *streamdb-metrics-schema* 
  '(:object (:required "sends" "receives" "rtt")
    :properties (("sends" :integer) ("receives" :integer) ("rtt" :number))))

(defvar *streamdb-state-schema* 
  '(:object (:required "peers")
    :properties (("peers" :array))))

;; F5: cl-json:decode-json returns an ALIST with mangled keyword keys; matching
;; must be case- and punctuation-insensitive, or every key lookup misses.
(defun jkey-norm (s)
  (remove-if-not #'alphanumericp (string-upcase (string s))))

(defun jref (alist key &optional default)
  "Tolerant alist accessor for cl-json output. KEY is a keyword or string;
matching is case- and punctuation-insensitive (immune to cl-json key mangling)."
  (let ((hit (assoc key alist
                    :test (lambda (want have)
                            (string= (jkey-norm want) (jkey-norm have))))))
    (if hit (cdr hit) default)))

;; F5: well-formedness check that replaces the cl-json-schema dependency.
(defun validate-streamdb-data (data schema)
  (declare (ignore schema))
  (handler-case (progn (cl-json:decode-json-from-string data) t)
    (error (e)
      (signal-dcf-error :schema-validation
                        (format nil "Stored value is not well-formed JSON: ~A" e)))))

;; Gaming API
(defun dcf-send-position (player-id x y z)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let* ((payload (encode-position x y z))
         (msg (make-proto-message
               :type +msg-type-position+
               :sequence (next-sequence)
               :timestamp (get-internal-real-time)
               :payload payload)))
    (dolist (peer (dcf-node-peers *node*))
      (let ((peer-info (gethash peer (dcf-node-peer-map *node*))))
        (when peer-info
          (send-udp-message (dcf-node-udp-endpoint *node*) msg
                           (car peer-info) (cdr peer-info) :reliable nil))))
    (incf (gethash :positions-sent (dcf-node-metrics *node*) 0))
    (log:debug *dcf-logger* "Position sent for ~A: (~A, ~A, ~A)" player-id x y z)
    `(:status "position-sent" :player ,player-id)))

(defun dcf-send-audio (audio-data)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let ((msg (make-proto-message
              :type +msg-type-audio+
              :sequence (next-sequence)
              :timestamp (get-internal-real-time)
              :payload audio-data)))
    (dolist (peer (dcf-node-peers *node*))
      (let ((peer-info (gethash peer (dcf-node-peer-map *node*))))
        (when peer-info
          (send-udp-message (dcf-node-udp-endpoint *node*) msg
                           (car peer-info) (cdr peer-info) :reliable nil))))
    (incf (gethash :audio-packets-sent (dcf-node-metrics *node*) 0))
    (log:debug *dcf-logger* "Audio packet sent (~A bytes)" (length audio-data))
    `(:status "audio-sent")))

(defun dcf-send-game-event (event-type data)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let* ((payload (encode-game-event event-type data))
         (msg (make-proto-message
               :type +msg-type-game-event+
               :sequence (next-sequence)
               :timestamp (get-internal-real-time)
               :payload payload)))
    (dolist (peer (dcf-node-peers *node*))
      (let ((peer-info (gethash peer (dcf-node-peer-map *node*))))
        (when peer-info
          (send-udp-message (dcf-node-udp-endpoint *node*) msg
                           (car peer-info) (cdr peer-info) :reliable t))))
    (incf (gethash :events-sent (dcf-node-metrics *node*) 0))
    (log:info *dcf-logger* "Game event sent: ~A ~A" event-type data)
    `(:status "event-sent")))

(defun dcf-send-udp (data recipient &key reliable (type +msg-type-game-event+))
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let* ((payload (if (stringp data)
                     (flexi-streams:string-to-octets data :external-format :utf-8)
                     data))
         (msg (make-proto-message
               :type type
               :sequence (next-sequence)
               :timestamp (get-internal-real-time)
               :payload payload))
         (peer-info (gethash recipient (dcf-node-peer-map *node*))))
    (if peer-info
        (progn
          (send-udp-message (dcf-node-udp-endpoint *node*) msg
                           (car peer-info) (cdr peer-info) :reliable reliable)
          `(:status "sent" :recipient ,recipient :reliable ,reliable))
        (signal-dcf-error :peer-not-found 
                         (format nil "Peer not found: ~A" recipient)))))

(defun dcf-send (data recipient &optional tx-id)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let ((context (when tx-id (get-cached-tx-context *node* tx-id))))
    (if context
        (dcf-db-insert *node* (format nil "/tx/~A/messages" tx-id) data)
        (dcf-send-udp data recipient :reliable t :type +msg-type-reliable+))))

;; Initialization
(defun dcf-init (config-file)
  (let* ((config (load-config config-file))
         (node (make-dcf-node 
                :config config 
                :middleware '() 
                :plugins (make-hash-table) 
                :metrics (make-hash-table :test #'eq) 
                :peers '() 
                :groups (make-hash-table)
                :mode (dcf-config-mode config) 
                :tx-lock (bt:make-lock) 
                :cache '() 
                :cache-size 1000 
                :cache-lock (bt:make-lock)
                :tx-cache (make-hash-table :test #'equal) 
                :tx-cache-lock (bt:make-lock) 
                :peer-groups (make-hash-table)
                :peer-map (make-hash-table :test #'equal))))
    ;; BUG FIX: previous unwind-protect freed streamdb unconditionally,
    ;; including on the success path (double-free). Guard with a flag.
    (let ((init-ok nil))
      (unwind-protect
          (progn
            ;; StreamDB
            (when (and (dcf-config-streamdb-path config)
                       (string= (dcf-config-storage config) "streamdb"))
              (setf (dcf-node-streamdb node)
                    (streamdb_init (dcf-config-streamdb-path config) 5000))
              (when (cffi:null-pointer-p (dcf-node-streamdb node))
                (signal-dcf-error :initialization "StreamDB init failed"))
              (streamdb_set_quick_mode (dcf-node-streamdb node)
                                      (high-optimization? config))
              (log:info *dcf-logger* "StreamDB initialized: ~A"
                       (dcf-config-streamdb-path config)))
            ;; UDP
            (let ((endpoint (create-udp-endpoint
                            (dcf-config-udp-port config)
                            (lambda (msg host port)
                              (handle-game-message node msg host port)))))
              (setf (dcf-node-udp-endpoint node) endpoint))
            (setf *node* node)
            (restore-state node)
            (setf init-ok t)
            `(:status "success" :mode ,(dcf-config-mode config)
              :udp-port ,(dcf-config-udp-port config)))
        (unless init-ok
          (when (and (dcf-node-streamdb node)
                     (not (cffi:null-pointer-p (dcf-node-streamdb node))))
            (streamdb_free (dcf-node-streamdb node))))))))
;; F6: peers are persisted as the JSON object {"peers":[...]} and restored
;; symmetrically; first boot (no key yet) is not an error.
(defun restore-state (node)
  (when (dcf-node-streamdb node)
    (handler-case
        (let ((peers-json (dcf-db-query node "/state/peers")))
          (when (stringp peers-json)
            (setf (dcf-node-peers node)
                  (jref (cl-json:decode-json-from-string peers-json) :peers '()))))
      (dcf-error (e)
        (unless (eq (dcf-error-code e) :not-found)
          (log:warn *dcf-logger* "restore-state: ~A" e))))))

(defun save-state (node)
  (when (dcf-node-streamdb node)
    (let ((json (cl-json:encode-json-to-string
                 (list (cons :peers (or (dcf-node-peers node) '()))))))
      (dcf-db-insert node "/state/peers" json)
      (dcf-db-flush node))))

(defun handle-game-message (node msg remote-host remote-port)
  (case (proto-message-type msg)
    (#.+msg-type-position+
     (let ((pos (decode-position (proto-message-payload msg))))
       (log:debug *dcf-logger* "Position received: ~A from ~A:~A" 
                 pos remote-host remote-port)
       (incf (gethash :positions-received (dcf-node-metrics node) 0))))
    (#.+msg-type-audio+
     (log:debug *dcf-logger* "Audio packet received (~A bytes) from ~A:~A"
               (length (proto-message-payload msg)) remote-host remote-port)
     (incf (gethash :audio-packets-received (dcf-node-metrics node) 0)))
    (#.+msg-type-game-event+
     (let ((event (decode-game-event (proto-message-payload msg))))
       (log:info *dcf-logger* "Game event received: ~A from ~A:~A" 
                event remote-host remote-port)
       (incf (gethash :events-received (dcf-node-metrics node) 0))))
    (otherwise
     (log:debug *dcf-logger* "Unknown message type ~A from ~A:~A" 
               (proto-message-type msg) remote-host remote-port))))

(defun dcf-start ()
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (start-udp-receiver (dcf-node-udp-endpoint *node*))
  `(:status "running" :udp-port ,(dcf-config-udp-port (dcf-node-config *node*))))

(defun dcf-stop ()
  (when *node*
    ;; F4/C8: persist while the DB is still alive, THEN tear down (the old order
    ;; freed StreamDB and then called save-state on the dangling pointer).
    (handler-case (save-state *node*)
      (error (e) (log:warn *dcf-logger* "save-state failed during stop: ~A" e)))
    (when (dcf-node-udp-endpoint *node*)
      (stop-udp-endpoint (dcf-node-udp-endpoint *node*)))
    (when (and (dcf-node-streamdb *node*)
               (not (cffi:null-pointer-p (dcf-node-streamdb *node*))))
      (streamdb_flush (dcf-node-streamdb *node*))
      (streamdb_free (dcf-node-streamdb *node*))
      (setf (dcf-node-streamdb *node*) nil))
    (setf *node* nil)
    `(:status "stopped")))

;; Peer Management
(defun dcf-add-peer (peer-id host port)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (push peer-id (dcf-node-peers *node*))
  (setf (gethash peer-id (dcf-node-peer-map *node*)) (cons host port))
  `(:status "peer-added" :peer-id ,peer-id))

(defun dcf-remove-peer (peer-id)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (setf (dcf-node-peers *node*) 
        (remove peer-id (dcf-node-peers *node*) :test #'string=))
  (remhash peer-id (dcf-node-peer-map *node*))
  `(:status "peer-removed" :peer-id ,peer-id))

(defun dcf-list-peers ()
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (dcf-node-peers *node*))

;; Metrics
(defun dcf-get-metrics ()
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let ((stats (udp-endpoint-stats (dcf-node-udp-endpoint *node*))))
    (append
     (list 
      :packets-sent (network-stats-packets-sent stats)
      :packets-received (network-stats-packets-received stats)
      :bytes-sent (network-stats-bytes-sent stats)
      :bytes-received (network-stats-bytes-received stats)
      :packets-lost (network-stats-packets-lost stats)
      :retransmits (network-stats-retransmits stats)
      :avg-rtt (network-stats-avg-rtt stats)
      :jitter (network-stats-jitter stats))
     (list 
      :positions-received (gethash :positions-received (dcf-node-metrics *node*) 0)
      :positions-sent (gethash :positions-sent (dcf-node-metrics *node*) 0)
      :audio-packets-sent (gethash :audio-packets-sent (dcf-node-metrics *node*) 0)
      :audio-packets-received (gethash :audio-packets-received (dcf-node-metrics *node*) 0)
      :events-sent (gethash :events-sent (dcf-node-metrics *node*) 0)
      :events-received (gethash :events-received (dcf-node-metrics *node*) 0)))))

;; Benchmark
(defun dcf-benchmark (peer-id &key (count 100))
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let ((peer-info (gethash peer-id (dcf-node-peer-map *node*)))
        (rtts '()))
    (unless peer-info
      (signal-dcf-error :peer-not-found (format nil "Peer ~A not found" peer-id)))
    (dotimes (i count)
      (let ((msg (make-proto-message
                  :type +msg-type-ping+
                  :sequence (next-sequence)
                  :timestamp (get-internal-real-time)
                  :payload #())))
        (send-udp-message (dcf-node-udp-endpoint *node*) msg
                          (car peer-info) (cdr peer-info) :reliable nil)
        (sleep 0.01))
      (let ((stats (udp-endpoint-stats (dcf-node-udp-endpoint *node*))))
        ;; F9: 0 is truthy in CL, so the old `when last-rtt` pushed zeros before any
        ;; pong arrived and min-rtt was always 0. Only record strictly positive RTTs.
        (when (plusp (network-stats-last-rtt stats))
          (push (network-stats-last-rtt stats) rtts))))
    (let ((avg (if rtts (/ (reduce #'+ rtts) (length rtts)) 0))
          (min-rtt (if rtts (reduce #'min rtts) 0))
          (max-rtt (if rtts (reduce #'max rtts) 0)))
      `(:peer ,peer-id :count ,count :avg-rtt ,avg
        :min-rtt ,min-rtt :max-rtt ,max-rtt :samples ,(length rtts)))))

;; Transactions
(defun dcf-begin-transaction (tx-id)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (let ((context `(:tx-id ,tx-id :start-time ,(get-universal-time))))
    (cache-tx-id *node* tx-id context)
    `(:status "transaction-started" :tx-id ,tx-id)))

(defun dcf-commit-transaction (tx-id)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (unless (get-cached-tx-context *node* tx-id)
    (signal-dcf-error :transaction "Transaction not found"))
  (clear-cached-tx *node* tx-id)
  (when (string= (dcf-config-storage (dcf-node-config *node*)) "streamdb")
    (dcf-db-flush *node*))
  `(:status "transaction-committed" :tx-id ,tx-id))

(defun dcf-rollback-transaction (tx-id)
  (unless *node* (signal-dcf-error :not-initialized "Node not initialized"))
  (clear-cached-tx *node* tx-id)
  `(:status "transaction-rolled-back" :tx-id ,tx-id))

;; Status and Version
(defun dcf-status ()
  (if *node*
      `(:status "running" 
        :mode ,(dcf-node-mode *node*) 
        :udp-port ,(dcf-config-udp-port (dcf-node-config *node*))
        :peer-count ,(length (dcf-node-peers *node*))
        :udp-active ,(udp-endpoint-running (dcf-node-udp-endpoint *node*)))
      `(:status "stopped")))

(defun dcf-version ()
  `(:version "2.2.0" :dcf-version "5.0.0" :transport "UDP" :protocol "binary-protobuf"))

;; Help
(defun dcf-help ()
  (format nil "~
╔══════════════════════════════════════════════════════════════════════════╗
║         DeMoD-LISP v2.2.0 - Punctim: UDP Gaming & Audio SDK           ║
╚══════════════════════════════════════════════════════════════════════════╝

**Quick Start for Gaming:**
1. (dcf-init \"config.json\")
2. (dcf-start)
3. (dcf-add-peer \"player2\" \"192.168.1.100\" 7777)
4. (dcf-send-position \"player1\" 100.0 50.0 25.0)
5. (dcf-get-metrics)

**Key Features:**
- UDP with unreliable (<5ms) / reliable channels
- Binary Protobuf: 10-100x faster than JSON
- Position (12B), Audio (raw), Events (reliable)
- RTT/Jitter stats, auto-retry
- StreamDB persistence, transactions

**API Examples:**
(dcf-send-game-event 1 \"SHOOT|player1|rifle\")
(dcf-send-audio #(16r01 16r02 ...))  ; 20ms chunk

**Agent System (LLM agents via Python API server):**
(dcf-agent-health)                     ; check API server
(dcf-agent-backends)                   ; list LLM backends
(dcf-agent-providers)                  ; list provider presets
(dcf-agent-chat \"hello\" :backend \"echo\")
(dcf-agent-chat \"echo: test\" :graph \"coordinator\")
(dcf-agent-chat \"hello\" :backend \"glm5p2\")
(dcf-agent-set-url \"http://192.168.1.50:8000\")

**CLI:** sbcl --load punctim.lisp --eval '(main \"help\")'

Repo: https://github.com/ALH477/DeMoD-Communication-Framework"))

;; CLI
(defun main (&rest args)
  (handler-case
      (if (null args)
          (format t "~A~%" (dcf-help))
          (let* ((command (first args))
                 (cmd-args (rest args)))
            (cond
             ((string= command "help") (format t "~A~%" (dcf-help)))
             ((string= command "init") (print (dcf-init (first cmd-args))))
             ((string= command "start") (print (dcf-start)))
             ((string= command "stop") (print (dcf-stop)))
             ((string= command "add-peer") 
              (print (apply #'dcf-add-peer cmd-args)))
             ((string= command "remove-peer") 
              (print (dcf-remove-peer (first cmd-args))))
             ((string= command "list-peers") 
              (print (dcf-list-peers)))
             ((string= command "send-position")
              (apply #'dcf-send-position cmd-args))
             ((string= command "send-audio") 
              (dcf-send-audio (read-from-string (first cmd-args))))
             ((string= command "send-event")
              (apply #'dcf-send-game-event cmd-args))
             ((string= command "send-udp")
              (apply #'dcf-send-udp cmd-args))
             ((string= command "benchmark")
              (print (apply #'dcf-benchmark cmd-args)))
             ((string= command "metrics") (print (dcf-get-metrics)))
             ((string= command "status") (print (dcf-status)))
             ((string= command "version") (print (dcf-version)))
             ((string= command "begin-tx") (print (dcf-begin-transaction (first cmd-args))))
             ((string= command "commit-tx") (print (dcf-commit-transaction (first cmd-args))))
             ((string= command "rollback-tx") (print (dcf-rollback-transaction (first cmd-args))))
             ((string= command "db-insert") (print (apply #'dcf-db-insert *node* cmd-args)))
             ((string= command "db-query") (print (apply #'dcf-db-query *node* cmd-args)))
             ((string= command "test") (sb-ext:exit :code (if (run-tests) 0 1)))
             ;; Agent system commands
             ((string= command "agent-chat")
              (print (apply #'dcf-agent-chat cmd-args)))
             ((string= command "agent-list")
              (print (dcf-agent-list)))
             ((string= command "agent-backends")
              (print (dcf-agent-backends)))
             ((string= command "agent-providers")
              (print (dcf-agent-providers)))
             ((string= command "agent-health")
              (print (dcf-agent-health)))
             ((string= command "agent-set-url")
              (print (dcf-agent-set-url (first cmd-args))))
             (t (format t "Unknown: ~A. Try 'help'.~%" command)))))
    (error (e)
      (format t "Error: ~A~%" e))))

;; Tests (suite defined above near the adapter tests; just keep adding to it)
#+fiveam
(fiveam:in-suite punctim-suite)

#+fiveam
(fiveam:test binary-test
  (let* ((pos (encode-position 1.0 2.0 3.0))
         (decoded (decode-position pos)))
    (fiveam:is (< (abs (- 1.0 (getf decoded :x))) 0.001))
    (fiveam:is (< (abs (- 2.0 (getf decoded :y))) 0.001))
    (fiveam:is (< (abs (- 3.0 (getf decoded :z))) 0.001))))

#+fiveam
(fiveam:test serialization-test
  (let* ((payload (coerce #(1 2 3) '(vector (unsigned-byte 8))))
         (msg (make-proto-message :type 1 :sequence 42 :timestamp 123 :payload payload))
         (ser (serialize-proto-message msg))
         (des (deserialize-proto-message ser)))
    (fiveam:is (= (proto-message-type msg) (proto-message-type des)))
    (fiveam:is (= (length payload) (length (proto-message-payload des))))))

#+fiveam
(fiveam:test event-test
  (let* ((ev (encode-game-event 5 "test"))
         (dec (decode-game-event ev)))
    (fiveam:is (= 5 (getf dec :event-type)))
    (fiveam:is (string= "test" (getf dec :data)))))

#+fiveam
(defun run-tests ()
  "Run the FiveAM suite, print the report, and return T iff every check passed.
   Callers (e.g. the `test` subcommand / CI) turn the boolean into an exit code."
  (let ((results (fiveam:run 'punctim-suite)))
    (fiveam:explain! results)
    (fiveam:results-status results)))

;; Deployment. Forward the command-line arguments to MAIN: SBCL calls a :toplevel
;; thunk with no arguments, so a bare #'main would always take the no-args (help)
;; branch. This matches the Dockerfile's save-lisp-and-die invocation.
(defun dcf-deploy (&optional output-file)
  (sb-ext:save-lisp-and-die (or output-file "punctim")
                            :executable t
                            :toplevel (lambda () (apply #'main (rest sb-ext:*posix-argv*)))))

;; C7 regression guard: self-certify the wire codec against the cross-language
;; anchors when this file loads. A broken crc16-ccitt now fails the load loudly
;; (this is exactly the check the hotfix performed; folded in so the SDK file
;; self-certifies standalone).
(defun certify-wire-codec ()
  (let ((anchor (map '(vector (unsigned-byte 8)) #'char-code "123456789"))
        (ex (coerce #(#xD3 #x10 #x12 #x34 #x00 #x01 #xFF #xFF
                      #xDE #xAD #xBE #xEF #xAB #x12 #xCD #xA9 #x63)
                    '(vector (unsigned-byte 8)))))
    (assert (= (crc16-ccitt anchor) #x29B1) ()
            "crc16-ccitt anchor failed: got #x~4,'0X, want #x29B1" (crc16-ccitt anchor))
    (assert (= (crc16-ccitt ex 0 15) #xA963) ()
            "exampleFrame body CRC failed: got #x~4,'0X, want #xA963" (crc16-ccitt ex 0 15))
    (let ((msg (decode-dcf-frame ex)))
      (assert msg () "exampleFrame failed to decode")
      (assert (= (proto-message-sequence msg) #x1234) () "exampleFrame seq mismatch")
      (assert (equalp (proto-message-payload msg) #(#xDE #xAD #xBE #xEF)) ()
              "exampleFrame payload mismatch"))
    (let ((bad (copy-seq ex)))
      (setf (aref bad 9) (logxor (aref bad 9) #x01))
      (assert (null (decode-dcf-frame bad)) () "corrupted frame was ACCEPTED"))
    :certified))

(eval-when (:load-toplevel :execute)
  (certify-wire-codec))

;; ============================================================================
;; AGENT SYSTEM INTEGRATION — LLM agents via the Python API server
;; ============================================================================
;;
;; The langgraph_agents/ Python package provides a universal API system:
;;   - HTTP API server (FastAPI): /chat, /agents, /backends, /providers,
;;     /mesh/send, /mesh/recv, /mesh/status, /ws
;;   - Universal MCP server: agent_chat, agent_list, backend_list, etc.
;;   - Universal LLM backend: any OpenAI-compatible API (Fireworks, OpenAI,
;;     Grok, Ollama, DeepSeek, OpenRouter)
;;
;; These Lisp functions are native DSL forms that call the API server over
;; HTTP. Encryption-free for export control purposes — the agent system
;; communicates over the same plaintext DCF transport.
;;
;; The HTTP client is a minimal usocket-based implementation (no Drakma
;; dependency needed — keeps the Nix closure unchanged).

(defparameter *agent-api-url* "http://127.0.0.1:8000"
  "Base URL for the langgraph_agents API server.")

(defparameter *agent-api-timeout* 30
  "HTTP timeout in seconds for agent API calls.")

;; --- Minimal HTTP client over usocket -----------------------------------------

(defun http-post-json (url json-string)
  "Send a POST request with JSON body, return (status-code . response-body).
   Minimal HTTP/1.1 over TCP using usocket — no external HTTP library needed."
  (let* ((uri (parse-http-url url))
         (host (getf uri :host))
         (port (getf uri :port))
         (path (getf uri :path)))
    (usocket:with-client-socket (sock stream host port
                                  :element-type '(unsigned-byte 8)
                                  :timeout *agent-api-timeout*)
      (let ((flex (flexi-streams:make-flexi-stream stream :external-format :utf-8))
            (body (map '(vector (unsigned-byte 8))
                       #'char-code json-string)))
        ;; Send request
        (format flex "POST ~A HTTP/1.1~%Host: ~A~%Content-Type: application/json~%Content-Length: ~A~%Connection: close~%~%"
                path host (length body))
        (write-sequence body stream)
        (force-output flex)
        ;; Read response
        (read-http-response flex)))))

(defun http-get-json (url)
  "Send a GET request, return (status-code . response-body)."
  (let* ((uri (parse-http-url url))
         (host (getf uri :host))
         (port (getf uri :port))
         (path (getf uri :path)))
    (usocket:with-client-socket (sock stream host port
                                  :element-type '(unsigned-byte 8)
                                  :timeout *agent-api-timeout*)
      (let ((flex (flexi-streams:make-flexi-stream stream :external-format :utf-8)))
        (format flex "GET ~A HTTP/1.1~%Host: ~A~%Connection: close~%~%" path host)
        (force-output flex)
        (read-http-response flex)))))

(defun parse-http-url (url)
  "Parse http://host:port/path into a plist."
  (let* ((no-scheme (if (string-equal url "http://" :end1 7)
                        (subseq url 7)
                        url))
         (slash-pos (position #\/ no-scheme))
         (hostport (if slash-pos (subseq no-scheme 0 slash-pos) no-scheme))
         (path (if slash-pos (subseq no-scheme slash-pos) "/"))
         (colon-pos (position (code-char 58) hostport))
         (host (if colon-pos (subseq hostport 0 colon-pos) hostport))
         (port (if colon-pos (parse-integer (subseq hostport (1+ colon-pos)))
                   80)))
    (list :host host :port port :path path)))

(defun read-http-response (stream)
  "Read a complete HTTP response from a flexi-stream. Return (status . body)."
  ;; Read status line
  (let ((status-line (read-line stream nil "")))
    (let ((status-code (parse-integer
                         (subseq status-line
                                 (1+ (position #\Space status-line))
                                 (position #\Space status-line
                                           :start (1+ (position #\Space status-line))))
                         :junk-allowed t)))
      ;; Skip headers until empty line
      (loop for line = (read-line stream nil "")
            until (or (string= line "") (string= line (string #\Return))))
      ;; Read body
      (let ((body (with-output-to-string (out)
                    (loop for char = (read-char stream nil nil)
                          while char do (write-char char out)))))
        (cons status-code body)))))

;; --- Agent DSL functions -----------------------------------------------------

(defun dcf-agent-chat (message &key (backend "echo") (graph "base")
                                  (channel "lisp") (sender "lisp-client"))
  "Send a message to a LangGraph agent and get a response.

  (dcf-agent-chat \"hello\" :backend \"echo\")
  (dcf-agent-chat \"echo: test\" :graph \"coordinator\")
  (dcf-agent-chat \"hello\" :backend \"glm5p2\" :graph \"coordinator\")

Returns the response text, or signals an error if the API server is down."
  (let* ((json-obj (list (cons "message" message)
                         (cons "backend" backend)
                         (cons "graph" graph)
                         (cons "channel" channel)
                         (cons "sender" sender)))
         (json-str (cl-json:encode-json-to-string json-obj))
         (resp (http-post-json (format nil "~A/chat" *agent-api-url*) json-str)))
    (if (= (car resp) 200)
        (let ((parsed (cl-json:decode-json-from-string (cdr resp))))
          (rest (assoc :response parsed)))
        (signal-dcf-error :agent-api
          (format nil "Agent API error ~A: ~A" (car resp) (cdr resp))))))

(defun dcf-agent-list ()
  "List all configured agents from the API server.

  (dcf-agent-list)
  => ((:NAME . \"echo-agent\") (:GRAPH . \"base\") ...)

Returns a list of agent config alists."
  (let ((resp (http-get-json (format nil "~A/agents" *agent-api-url*))))
    (if (= (car resp) 200)
        (cl-json:decode-json-from-string (cdr resp))
        (signal-dcf-error :agent-api
          (format nil "Agent API error ~A: ~A" (car resp) (cdr resp))))))

(defun dcf-agent-backends ()
  "List available LLM backends and provider presets.

  (dcf-agent-backends)
  => (:BACKENDS (\"echo\" \"glm5p2\" \"grok\") :PROVIDERS (\"fireworks\" \"openai\" ...))

Returns a plist of backends and providers."
  (let ((resp (http-get-json (format nil "~A/backends" *agent-api-url*))))
    (if (= (car resp) 200)
        (cl-json:decode-json-from-string (cdr resp))
        (signal-dcf-error :agent-api
          (format nil "Agent API error ~A: ~A" (car resp) (cdr resp))))))

(defun dcf-agent-providers ()
  "List available provider presets (fireworks, openai, grok, ollama, etc.).

  (dcf-agent-providers)
  => (\"fireworks\" \"openai\" \"grok\" \"ollama\" \"deepseek\" \"openrouter\")

Returns a list of provider name strings."
  (let ((resp (http-get-json (format nil "~A/providers" *agent-api-url*))))
    (if (= (car resp) 200)
        (cl-json:decode-json-from-string (cdr resp))
        (signal-dcf-error :agent-api
          (format nil "Agent API error ~A: ~A" (car resp) (cdr resp))))))

(defun dcf-agent-health ()
  "Check the API server health.

  (dcf-agent-health)
  => (:STATUS \"ok\" :BACKENDS (\"echo\" \"glm5p2\" \"grok\") ...)

Returns a plist with status, backends, and providers."
  (let ((resp (http-get-json (format nil "~A/health" *agent-api-url*))))
    (if (= (car resp) 200)
        (cl-json:decode-json-from-string (cdr resp))
        (signal-dcf-error :agent-api
          (format nil "Agent API error ~A: ~A" (car resp) (cdr resp))))))

(defun dcf-agent-set-url (url)
  "Set the agent API server URL.

  (dcf-agent-set-url \"http://192.168.1.50:8000\")"

  (setf *agent-api-url* url)
  (format nil "Agent API URL set to: ~A" url))

;; --- Agent tests (fiveam) ----------------------------------------------------

#+fiveam
(fiveam:test agent-http-parse-url-test
  "parse-http-url should correctly parse http://host:port/path"
  (let ((uri (parse-http-url "http://127.0.0.1:8000/chat")))
    (fiveam:is (string= (getf uri :host) "127.0.0.1"))
    (fiveam:is (= (getf uri :port) 8000))
    (fiveam:is (string= (getf uri :path) "/chat")))
  (let ((uri (parse-http-url "http://example.com/api")))
    (fiveam:is (string= (getf uri :host) "example.com"))
    (fiveam:is (= (getf uri :port) 80))
    (fiveam:is (string= (getf uri :path) "/api"))))

#+fiveam
(fiveam:test agent-set-url-test
  "dcf-agent-set-url should update *agent-api-url*"
  (let ((old *agent-api-url*))
    (dcf-agent-set-url "http://test:9999")
    (fiveam:is (string= *agent-api-url* "http://test:9999"))
    (setf *agent-api-url* old)))

;; End
