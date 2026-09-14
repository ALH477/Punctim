;;;; SPDX-License-Identifier: LGPL-3.0-only
;;;; ============================================================================
;;;; punctim-hotfix.lisp — COMPATIBILITY SHIM
;;;;
;;;; As of v0.3.x, every fix this file used to carry at runtime (F1–F9) is folded
;;;; directly into lisp/src/punctim.lisp, which self-certifies the wire codec on
;;;; load (crc16("123456789") = #x29B1; exampleFrame round-trips). There is nothing
;;;; left to patch.
;;;;
;;;; This shim is retained only so existing instructions that load it after the SDK
;;;; ("(load \"src/punctim.lisp\") then (load \"punctim-hotfix.lisp\")") keep
;;;; working. Loading the SDK alone is now sufficient.
;;;; ============================================================================

(in-package :d-lisp)

(format t "~&;; punctim-hotfix: all fixes (F1-F9) are folded into src/punctim.lisp,~
           ~%;;   which self-certifies the wire codec on load — nothing to patch.~%")
