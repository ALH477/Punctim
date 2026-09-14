;;;; SPDX-License-Identifier: LGPL-3.0-only
;;;; lisp/src/wire.lisp — dependency-free DeModFrame wire codec + self-cert.
;;;;
;;;; Loadable under bare SBCL (no Quicklisp, no CFFI). The certify-lisp CI loads
;;;; this file and exits non-zero unless the cross-language anchors match — the
;;;; Lisp analogue of the dependency-free C cert (C_SDK/tests/test_wire_certify.c).
;;;; The full SDK codec lives in punctim.lisp (its crc16-ccitt is identical and
;;;; self-certifies on load); this file lets CI prove the wire algorithm without
;;;; pulling the SDK's Quicklisp dependency graph.

(defpackage :dcf-wire
  (:use :cl)
  (:export :crc16-ccitt :encode-frame :decode-frame :syndrome :certify
           :certify-golden :pack-super :unpack-super :superpackp))
(in-package :dcf-wire)

;;; ---------------------------------------------------------------------------
;;; Minimal dependency-free JSON reader — just enough to read the canonical
;;; Documentation/golden_vectors.json (objects, arrays, strings, integers). No
;;; Quicklisp, no CFFI: the Lisp cert reads the SAME file every other language
;;; reads, so there is no generated vector file to drift. Objects -> alist of
;;; (string . value), arrays -> list, numbers -> integer, strings -> string.
;;; ---------------------------------------------------------------------------

(defun %json-skip-ws (s i)
  (loop while (and (< i (length s))
                   (member (char s i) '(#\Space #\Tab #\Newline #\Return)))
        do (incf i))
  i)

(defun %json-parse-string (s i)
  ;; assumes s[i] = #\" ; golden_vectors.json strings are plain ASCII (hex +
  ;; description text), no \u escapes, so handle only simple backslash escapes.
  (incf i)
  (let ((out (make-string-output-stream)))
    (loop
      (when (>= i (length s)) (error "unterminated JSON string"))
      (let ((c (char s i)))
        (cond ((char= c #\") (return (values (get-output-stream-string out) (1+ i))))
              ((char= c #\\)
               (incf i)
               (let ((e (char s i)))
                 (write-char (case e (#\n #\Newline) (#\t #\Tab) (#\r #\Return)
                                     (t e))
                             out)
                 (incf i)))
              (t (write-char c out) (incf i)))))))

(defun %json-parse-number (s i)
  (let ((start i))
    (loop while (and (< i (length s))
                     (or (digit-char-p (char s i))
                         (member (char s i) '(#\- #\+ #\. #\e #\E))))
          do (incf i))
    (let ((tok (subseq s start i)))
      ;; golden vectors' numbers are integers; parse as integer.
      (values (values (parse-integer tok)) i))))

;; value <-> array/object are mutually recursive; forward-declare to keep the
;; compiler quiet without reordering the natural top-down definitions.
(declaim (ftype (function (string fixnum) (values t fixnum)) %json-parse-array %json-parse-object))

(defun %json-parse-value (s i)
  (setf i (%json-skip-ws s i))
  (let ((c (char s i)))
    (cond
      ((char= c #\{) (%json-parse-object s i))
      ((char= c #\[) (%json-parse-array s i))
      ((char= c #\") (%json-parse-string s i))
      ((or (digit-char-p c) (char= c #\-)) (%json-parse-number s i))
      ((and (<= (+ i 4) (length s)) (string= (subseq s i (+ i 4)) "true"))
       (values t (+ i 4)))
      ((and (<= (+ i 5) (length s)) (string= (subseq s i (+ i 5)) "false"))
       (values nil (+ i 5)))
      ((and (<= (+ i 4) (length s)) (string= (subseq s i (+ i 4)) "null"))
       (values nil (+ i 4)))
      (t (error "unexpected JSON char ~S at ~D" c i)))))

(defun %json-parse-array (s i)
  (incf i) ; past [
  (setf i (%json-skip-ws s i))
  (when (char= (char s i) #\]) (return-from %json-parse-array (values nil (1+ i))))
  (let ((items '()))
    (loop
      (multiple-value-bind (v ni) (%json-parse-value s i)
        (push v items)
        (setf i (%json-skip-ws s ni))
        (case (char s i)
          (#\, (incf i))
          (#\] (return (values (nreverse items) (1+ i))))
          (t (error "expected , or ] at ~D" i)))))))

(defun %json-parse-object (s i)
  (incf i) ; past {
  (setf i (%json-skip-ws s i))
  (when (char= (char s i) #\}) (return-from %json-parse-object (values nil (1+ i))))
  (let ((pairs '()))
    (loop
      (setf i (%json-skip-ws s i))
      (multiple-value-bind (key ni) (%json-parse-string s i)
        (setf i (%json-skip-ws s ni))
        (unless (char= (char s i) #\:) (error "expected : at ~D" i))
        (multiple-value-bind (val nj) (%json-parse-value s (1+ i))
          (push (cons key val) pairs)
          (setf i (%json-skip-ws s nj))
          (case (char s i)
            (#\, (incf i))
            (#\} (return (values (nreverse pairs) (1+ i))))
            (t (error "expected , or } at ~D" i))))))))

(defun json-parse-file (path)
  "Parse a JSON file into alists/lists/strings/integers."
  (with-open-file (in path :direction :input :external-format :utf-8)
    (let ((s (make-string (file-length in))))
      (read-sequence s in)
      (values (%json-parse-value s 0)))))

(defun jget (obj key)
  "Value for KEY in a parsed JSON object (alist), or NIL."
  (cdr (assoc key obj :test #'string=)))

(defun hex->bytes (s)
  "Hex string -> (unsigned-byte 8) vector."
  (let ((v (make-array (floor (length s) 2) :element-type '(unsigned-byte 8))))
    (dotimes (i (length v) v)
      (setf (aref v i) (parse-integer s :start (* 2 i) :end (+ 2 (* 2 i)) :radix 16)))))

(defconstant +sync+ #xD3)
(defconstant +version+ 1)
(defconstant +frame-size+ 17)
(defconstant +crc-cover+ 15)

;; DCF SuperPack: a 32-byte container carrying two 17-byte frames under one joint
;; CRC (34 -> 32 bytes). A frame pair ships as a single datagram instead of two —
;; one packet, one IP/UDP header, one syscall — so paired traffic has strictly
;; lower per-pair overhead and latency. The unpacked frames are ordinary valid
;; DeModFrames, so the 246-vector wire certificate is untouched.
(defconstant +super-type+ #x05)
(defconstant +super-len+ 32)
(defconstant +super-core-len+ 14)
(defconstant +super-sflags+ (logior (ash +version+ 4) +super-type+))

(defun crc16-ccitt (vec &optional (start 0) (end (length vec)))
  "CRC-16/CCITT-FALSE (poly #x1021, init #xFFFF) over VEC[START..END)."
  (let ((crc #xFFFF))
    (loop for i from start below end do
      (setf crc (logand #xFFFF (logxor crc (ash (aref vec i) 8))))
      (loop repeat 8 do
        (setf crc (if (logbitp 15 crc)
                      (logand (logxor (ash crc 1) #x1021) #xFFFF)
                      (logand (ash crc 1) #xFFFF)))))
    crc))

(defun u16 (hi lo) (logior (ash hi 8) lo))

(defun encode-frame (&key (version 1) (type 0) (seq 0) (src 0) (dst 0)
                          (payload #(0 0 0 0)) (ts-us 0))
  "Serialise into a 17-byte (unsigned-byte 8) vector with an appended CRC."
  (let ((b (make-array +frame-size+ :element-type '(unsigned-byte 8)
                                    :initial-element 0)))
    (setf (aref b 0) +sync+
          (aref b 1) (logior (ash (logand version #x0F) 4) (logand type #x0F))
          (aref b 2) (logand (ash seq -8) #xFF)  (aref b 3) (logand seq #xFF)
          (aref b 4) (logand (ash src -8) #xFF)  (aref b 5) (logand src #xFF)
          (aref b 6) (logand (ash dst -8) #xFF)  (aref b 7) (logand dst #xFF))
    (dotimes (i 4) (setf (aref b (+ 8 i)) (aref payload i)))
    (setf (aref b 12) (logand (ash ts-us -16) #xFF)
          (aref b 13) (logand (ash ts-us -8) #xFF)
          (aref b 14) (logand ts-us #xFF))
    (let ((crc (crc16-ccitt b 0 +crc-cover+)))
      (setf (aref b 15) (logand (ash crc -8) #xFF)
            (aref b 16) (logand crc #xFF)))
    b))

(defun syndrome (w)
  "Affine validity syndrome: W is CRC-valid iff this returns 0."
  (logxor (crc16-ccitt w 0 +crc-cover+) (u16 (aref w 15) (aref w 16))))

(defun decode-frame (w)
  "Return a plist of fields, or NIL if W is not a valid 17-byte frame."
  (when (and (= (length w) +frame-size+)
             (= (aref w 0) +sync+)
             (= (ash (aref w 1) -4) +version+)
             (zerop (syndrome w)))
    (list :version (ash (aref w 1) -4)
          :type (logand (aref w 1) #x0F)
          :seq (u16 (aref w 2) (aref w 3))
          :src (u16 (aref w 4) (aref w 5))
          :dst (u16 (aref w 6) (aref w 7))
          :payload (subseq w 8 12)
          :ts-us (logior (ash (aref w 12) 16) (ash (aref w 13) 8) (aref w 14)))))

(defun %frame-core (f)
  "The 14 reconstructable bytes of a 17-byte frame, or NIL if F is not valid."
  (when (and (= (length f) +frame-size+)
             (= (aref f 0) +sync+)
             (= (ash (aref f 1) -4) +version+)
             (= (crc16-ccitt f 0 +crc-cover+) (u16 (aref f 15) (aref f 16))))
    (subseq f 1 15)))

(defun %rebuild-frame (core)
  "Rebuild a full 17-byte frame from its 14-byte CORE (sync + recomputed crc)."
  (let ((f (make-array +frame-size+ :element-type '(unsigned-byte 8) :initial-element 0)))
    (setf (aref f 0) +sync+)
    (dotimes (i +super-core-len+) (setf (aref f (+ 1 i)) (aref core i)))
    (let ((crc (crc16-ccitt f 0 +crc-cover+)))
      (setf (aref f 15) (logand (ash crc -8) #xFF)
            (aref f 16) (logand crc #xFF)))
    f))

(defun pack-super (a b)
  "Combine two valid 17-byte frames into one 32-byte SuperPack, or NIL on failure."
  (let ((ca (%frame-core a)) (cb (%frame-core b)))
    (when (and ca cb)
      (let ((out (make-array +super-len+ :element-type '(unsigned-byte 8) :initial-element 0)))
        (setf (aref out 0) +sync+ (aref out 1) +super-sflags+)
        (dotimes (i +super-core-len+)
          (setf (aref out (+ 2 i)) (aref ca i)
                (aref out (+ 2 +super-core-len+ i)) (aref cb i)))
        (let ((crc (crc16-ccitt out 0 30)))
          (setf (aref out 30) (logand (ash crc -8) #xFF)
                (aref out 31) (logand crc #xFF)))
        out))))

(defun superpackp (buf)
  "True iff BUF looks like a SuperPack (length + sync + version/type tag)."
  (and (= (length buf) +super-len+)
       (= (aref buf 0) +sync+)
       (= (aref buf 1) +super-sflags+)))

(defun unpack-super (buf)
  "Split a 32-byte SuperPack into (values frame-a frame-b), or NIL on failure."
  (when (and (= (length buf) +super-len+)
             (= (aref buf 0) +sync+)
             (= (ash (aref buf 1) -4) +version+)
             (= (logand (aref buf 1) #x0F) +super-type+)
             (= (crc16-ccitt buf 0 30) (u16 (aref buf 30) (aref buf 31))))
    (let ((a (%rebuild-frame (subseq buf 2 16)))
          (b (%rebuild-frame (subseq buf 16 30))))
      (when (and (decode-frame a) (decode-frame b))
        (values a b)))))

(defun certify ()
  "Self-cert against the cross-language anchors; returns :CERTIFIED or signals."
  (let ((anchor (map '(vector (unsigned-byte 8)) #'char-code "123456789"))
        (zeros  (make-array 15 :element-type '(unsigned-byte 8) :initial-element 0))
        (want   (coerce #(#xD3 #x13 #x12 #x34 #x00 #x01 #xFF #xFF
                          #xDE #xAD #xBE #xEF #xAB #x12 #xCD #x24 #xC0)
                        '(vector (unsigned-byte 8)))))
    (assert (= (crc16-ccitt anchor) #x29B1) ()
            "crc16(\"123456789\")=#x~4,'0X, want #x29B1" (crc16-ccitt anchor))
    (assert (= (crc16-ccitt zeros) #x4EC3) ()
            "crc16(0^15)=#x~4,'0X, want #x4EC3" (crc16-ccitt zeros))
    ;; golden exampleFrame_full: Ctrl(3) seq #x1234 src 1 dst #xFFFF DEADBEEF ts #xAB12CD
    (let ((ex (encode-frame :version 1 :type 3 :seq #x1234 :src 1 :dst #xFFFF
                            :payload #(#xDE #xAD #xBE #xEF) :ts-us #xAB12CD)))
      (assert (equalp ex want) () "exampleFrame_full mismatch")
      (let ((d (decode-frame ex)))
        (assert d () "exampleFrame failed to decode")
        (assert (= (getf d :seq) #x1234) () "exampleFrame seq mismatch")
        (assert (equalp (getf d :payload) #(#xDE #xAD #xBE #xEF)) () "payload mismatch"))
      (let ((bad (copy-seq ex)))
        (setf (aref bad 9) (logxor (aref bad 9) 1))
        (assert (null (decode-frame bad)) () "corrupted frame was ACCEPTED")))
    ;; SuperPack container vectors (subset embedded from superpack_vectors.json).
    (flet ((hx (s)
             (let ((v (make-array (/ (length s) 2) :element-type '(unsigned-byte 8))))
               (dotimes (i (length v) v)
                 (setf (aref v i)
                       (parse-integer s :start (* 2 i) :end (+ 2 (* 2 i)) :radix 16))))))
      (dolist (c '(("d310000000000000000000000000005b80" "d31312340001ffffdeadbeefab12cd24c0"
                    "d31510000000000000000000000000001312340001ffffdeadbeefab12cd2435")
                   ("d310010203040506cafebabe010203f4af" "d3117fffa1a100b270696e670000ff93b3"
                    "d31510010203040506cafebabe010203117fffa1a100b270696e670000ff2ea0")
                   ("d31200a010002000ff00ff00ffffffb630" "d313ffffffffffffffffffffffffff00fc"
                    "d3151200a010002000ff00ff00ffffff13ffffffffffffffffffffffffff02d4")))
        (destructuring-bind (ah bh sh) c
          (let ((a (hx ah)) (b (hx bh)) (sp (hx sh)))
            (assert (equalp (pack-super a b) sp) () "superpack pack mismatch")
            (assert (superpackp sp) () "superpack not recognised")
            (multiple-value-bind (ra rb) (unpack-super sp)
              (assert (and (equalp ra a) (equalp rb b)) () "superpack unpack mismatch")))))
      ;; zero-core joint CRC anchor = #x5B75
      (let* ((zero (encode-frame :version 1 :type 0))
             (spz (pack-super zero zero)))
        (assert (= (u16 (aref spz 30) (aref spz 31)) #x5B75) ()
                "superpack zero-core joint CRC anchor")))
    :certified))

(defun %word-from-bit (bit)
  "The 17-byte word that is all zero except bit BIT (bit 0 = MSB of byte 0)."
  (let ((w (make-array +frame-size+ :element-type '(unsigned-byte 8) :initial-element 0)))
    (multiple-value-bind (byte off) (floor bit 8)
      (when (< byte +frame-size+)
        (setf (aref w byte) (ash 1 (- 7 off)))))
    w))

(defun certify-golden (path)
  "Certify the full golden certificate at PATH: all 109 encode_basis frames are
CRC-valid (and known types round-trip) and all 137 syndrome_basis words hash to
their recorded syndrome. Returns (values encode-count syndrome-count) or signals."
  (let* ((root (json-parse-file path))
         (anchors (jget root "anchors"))
         (encs (jget root "encode_basis"))
         (syns (jget root "syndrome_basis")))
    ;; anchors
    (assert (= (crc16-ccitt (map '(vector (unsigned-byte 8)) #'char-code "123456789"))
               (parse-integer (jget anchors "crc_123456789") :start 2 :radix 16))
            () "crc_123456789 anchor")
    (assert (equalp (encode-frame :version 1 :type 3 :seq #x1234 :src 1 :dst #xFFFF
                                  :payload #(#xDE #xAD #xBE #xEF) :ts-us #xAB12CD)
                    (hex->bytes (jget anchors "exampleFrame_full")))
            () "exampleFrame_full anchor")
    ;; encode_basis: every frame raw-CRC-valid; known types (<=3) round-trip.
    (let ((n 0))
      (dolist (o encs)
        (let ((bytes (hex->bytes (jget o "frame"))))
          (assert (= (length bytes) +frame-size+) () "encode_basis[~D] length" n)
          (assert (= (crc16-ccitt bytes 0 +crc-cover+) (u16 (aref bytes 15) (aref bytes 16)))
                  () "encode_basis[~D] raw CRC invalid" n)
          (when (<= (logand (aref bytes 1) #x0F) 3)
            (let ((d (decode-frame bytes)))
              (assert d () "encode_basis[~D] failed to decode" n)))
          (incf n)))
      ;; syndrome_basis: word is all-zero, or a single set bit if "bit" is given.
      (let ((m 0))
        (dolist (o syns)
          (let* ((bit (jget o "bit"))
                 (word (if bit (%word-from-bit bit)
                           (make-array +frame-size+ :element-type '(unsigned-byte 8)
                                                    :initial-element 0)))
                 (want (jget o "syndrome"))
                 (got (logxor (crc16-ccitt word 0 +crc-cover+)
                              (u16 (aref word 15) (aref word 16)))))
            (assert (= got want) () "syndrome_basis[~D]: got #x~4,'0X want #x~4,'0X" m got want)
            (incf m)))
        (values n m)))))

(defun %find-golden ()
  "Locate Documentation/golden_vectors.json from a few plausible CWDs."
  (dolist (p '("Documentation/golden_vectors.json"
               "../Documentation/golden_vectors.json"
               "../../Documentation/golden_vectors.json"))
    (when (probe-file p) (return p))))

;; Self-certify on load (the certify-lisp CI just loads this file). If the
;; golden certificate is reachable, certify all 246 vectors; otherwise fall
;; back to the embedded anchors so the file still loads in isolation.
(eval-when (:load-toplevel :execute)
  (handler-case
      (let ((golden (%find-golden)))
        (certify)
        (if golden
            (multiple-value-bind (e s) (certify-golden golden)
              (format t "~&;; dcf-wire (lisp): CERTIFIED — ~D encode + ~D syndrome vectors, exampleFrame + SuperPack OK~%" e s))
            (format t "~&;; dcf-wire (lisp): CERTIFIED (embedded anchors; golden_vectors.json not found) — crc16 #x29B1 / #x4EC3~%")))
    (error (e)
      (format *error-output* "~&;; dcf-wire (lisp): FAILED — ~A~%" e)
      #+sbcl (sb-ext:exit :code 1))))
