;;;; Punctim ASDF System Definition
;;;; DeMoD-LISP (D-LISP) / Punctim v2.2.0

(defsystem "punctim"
  :version "2.2.0"
  :author "DeMoD Framework"
  :license "LGPL-3.0-only"
  :description "DeMoD Communications Framework - Lisp Implementation"
  :long-description "Punctim provides UDP gaming, real-time audio, 
and binary Protocol Buffers for low-latency networked applications."
  :depends-on (:cffi
               :uuid
               :usocket
               :bordeaux-threads
               :log4cl
               :trivial-backtrace
               :flexi-streams
               :fiveam
               :ieee-floats
               :cl-json)
  :serial t
  :components ((:file "src/punctim")
               (:file "src/punctim.core")))
