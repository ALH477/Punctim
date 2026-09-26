# HydraModem -- acoustic modem for the 17-byte Punctim/DCF wire frame.
# =============================================================================
# Default build uses the portable C reference DSP (zero external dependencies):
#     make                 # static + shared lib, examples, test binaries
#     make check           # build + run the FULL test suite (unit/fuzz/stream/loopback)
#     make asan            # rebuild the suite under AddressSanitizer + UBSan and run
#     sudo make install    # install lib + headers + pkg-config (PREFIX=/usr/local)
#
# Faust-backed build (runs the actual compiled Faust DSP). Requires the `faust`
# compiler and its C glue header (faust/gui/CInterface.h):
#     make faust-check     # regenerate Faust C backends, build + run loopback on them
#     make validate        # compile-check the .dsp files only
#
# Cross-compile for StarFive JH7110 (scalar RV64GC, no vector ext):
#     make CC=riscv64-linux-gnu-gcc ARCHFLAGS="-march=rv64gc -mtune=sifive-u74"
# =============================================================================

CC        ?= cc
AR        ?= ar
CSTD      ?= -std=gnu11
OPT       ?= -O3
WARN      ?= -Wall -Wextra
ARCHFLAGS ?=
CFLAGS    ?= $(CSTD) $(OPT) $(WARN) $(ARCHFLAGS) -Isrc
LDLIBS    ?= -lm
# No FMA contraction, whatever CFLAGS says: the musical transmitter's output is
# a normative function of its sine table and a fixed order of IEEE-754 double
# operations (docs/MUSIC.md), and a fused multiply-add on RISC-V/AArch64 would
# change the last bit. x86-64 baseline has no FMA, so there nothing changes.
override CFLAGS += -ffp-contract=off

# install layout
PREFIX     ?= /usr/local
DESTDIR    ?=
LIBDIR     ?= $(PREFIX)/lib
INCLUDEDIR ?= $(PREFIX)/include
PKGDIR     ?= $(LIBDIR)/pkgconfig

FAUST      ?= faust
# NB: no `-ftz 2` — Faust >= 2.83 emits malformed C for the denormal-flush
# bit-cast under `-os`; the reference DSP doesn't flush denormals (it's the
# numeric reference), so dropping it keeps TX/RX byte-equivalent and lets the
# adapters build across Faust 2.72..2.85. See docs/FAUST_MODERNIZATION.md.
FAUSTFLAGS ?= -lang c -os -double
FAUST_INC  ?= /usr/include          # location of faust/gui/CInterface.h

# version is single-sourced from src/hydramodem.h (HYDRAMODEM_VERSION) so the
# library, soname and pkg-config can never drift from the header macro.
VERSION    := $(shell sed -n 's/^#define HYDRAMODEM_VERSION[ \t][ \t]*"\([0-9.]*\)".*/\1/p' src/hydramodem.h)
SOVERSION  := $(shell echo $(VERSION) | cut -d. -f1)

BUILD := build
OBJ   := $(BUILD)/obj

# backend-agnostic core (protocol + FEC + framing + modem + wav + version)
CORE := hydra_profile hydra_crc hydra_fec hydra_conv hydra_interleave \
        hydra_frame hydra_modem wav hydramodem_version
CORE_OBJ := $(addprefix $(OBJ)/,$(addsuffix .o,$(CORE)))

REF_OBJ   := $(OBJ)/hydra_dsp_ref.o
FAUST_OBJ := $(OBJ)/hydra_dsp_faust_tx.o $(OBJ)/hydra_dsp_faust_rx.o

# public headers (umbrella + the component headers it includes)
PUBHDR := src/hydramodem.h src/hydra_profile.h src/hydra_modem.h src/wav.h

TESTS    := test_unit test_fuzz test_stream test_channel test_loopback test_music
TESTBIN  := $(addprefix $(BUILD)/,$(TESTS))
EXAMPLES := $(BUILD)/tx_demo $(BUILD)/rx_demo

LIB_A  := $(BUILD)/libhydramodem.a
LIB_SO := $(BUILD)/libhydramodem.so.$(VERSION)

.PHONY: all check test asan examples faust faust-check validate install uninstall \
        pkgconfig dist help clean $(BUILD)/hydramodem.pc
.DEFAULT_GOAL := all

all: $(LIB_A) $(LIB_SO) examples $(TESTBIN) pkgconfig

# ---------- objects (PIC so the same objects serve static and shared) ----------
$(OBJ)/%.o: src/%.c | $(OBJ)
	$(CC) $(CFLAGS) -fPIC -c $< -o $@

# ---------- reference (default) libraries ----------
$(LIB_A): $(CORE_OBJ) $(REF_OBJ)
	$(AR) rcs $@ $^

$(LIB_SO): $(CORE_OBJ) $(REF_OBJ)
	$(CC) -shared -Wl,-soname,libhydramodem.so.$(SOVERSION) $^ $(LDLIBS) -o $@
	ln -sf libhydramodem.so.$(VERSION) $(BUILD)/libhydramodem.so.$(SOVERSION)
	ln -sf libhydramodem.so.$(SOVERSION) $(BUILD)/libhydramodem.so

# ---------- examples ----------
examples: $(EXAMPLES)
$(BUILD)/tx_demo: examples/tx_demo.c $(LIB_A) | $(BUILD)
	$(CC) $(CFLAGS) $< $(LIB_A) $(LDLIBS) -o $@
$(BUILD)/rx_demo: examples/rx_demo.c $(LIB_A) | $(BUILD)
	$(CC) $(CFLAGS) $< $(LIB_A) $(LDLIBS) -o $@

# ---------- tests ----------
$(BUILD)/%: tests/%.c $(LIB_A) | $(BUILD)
	$(CC) $(CFLAGS) $< $(LIB_A) $(LDLIBS) -o $@

check: $(TESTBIN)
	@rc=0; for t in $(TESTBIN); do \
	  echo "==== $$t ===="; ./$$t || rc=1; echo; done; \
	  [ $$rc -eq 0 ] && echo "==== ALL TESTS PASSED ====" || { echo "==== TESTS FAILED ===="; exit 1; }

test: check          # alias

# ---------- sanitizer suite (memory safety) ----------
SAN_SRC := $(addprefix src/,$(addsuffix .c,$(CORE))) src/hydra_dsp_ref.c
asan:
	@rc=0; for t in $(TESTS); do \
	  echo "==== ASan/UBSan: $$t ===="; \
	  $(CC) -std=gnu11 -Isrc -fsanitize=address,undefined -fno-omit-frame-pointer -g -O1 \
	    tests/$$t.c $(SAN_SRC) $(LDLIBS) -o $(BUILD)/san_$$t || { rc=1; continue; }; \
	  ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 ./$(BUILD)/san_$$t >/dev/null && echo "  clean" || rc=1; \
	done; \
	[ $$rc -eq 0 ] && echo "==== SANITIZERS CLEAN ====" || { echo "==== SANITIZER ISSUES ===="; exit 1; }

# ---------- Faust-generated backends ----------
$(BUILD)/hydratx.c: faust/hydramodem_tx.dsp faust/demod_modem.lib | $(BUILD)
	$(FAUST) $(FAUSTFLAGS) -cn hydratx -I faust $< -o $@
$(BUILD)/hydrarx.c: faust/hydramodem_rx.dsp faust/demod_modem.lib | $(BUILD)
	$(FAUST) $(FAUSTFLAGS) -cn hydrarx -I faust $< -o $@

# adapters #include the generated .c; silence its unused-parameter noise
$(OBJ)/hydra_dsp_faust_tx.o: src/hydra_dsp_faust_tx.c $(BUILD)/hydratx.c | $(OBJ)
	$(CC) $(CFLAGS) -fPIC -Wno-unused-parameter -I$(FAUST_INC) -c $< -o $@
$(OBJ)/hydra_dsp_faust_rx.o: src/hydra_dsp_faust_rx.c $(BUILD)/hydrarx.c | $(OBJ)
	$(CC) $(CFLAGS) -fPIC -Wno-unused-parameter -I$(FAUST_INC) -c $< -o $@

$(BUILD)/libhydramodem_faust.a: $(CORE_OBJ) $(FAUST_OBJ)
	$(AR) rcs $@ $^

faust: $(BUILD)/libhydramodem_faust.a
faust-check: $(BUILD)/libhydramodem_faust.a
	$(CC) $(CFLAGS) tests/test_loopback.c $(BUILD)/libhydramodem_faust.a $(LDLIBS) -o $(BUILD)/test_loopback_faust
	./$(BUILD)/test_loopback_faust

# ---------- compile-check the Faust DSP only ----------
validate:
	@$(FAUST) $(FAUSTFLAGS) -cn hydratx -I faust faust/hydramodem_tx.dsp -o /dev/null && echo "TX dsp OK"
	@$(FAUST) $(FAUSTFLAGS) -cn hydrarx -I faust faust/hydramodem_rx.dsp -o /dev/null && echo "RX dsp OK"
	@$(FAUST) -I faust faust/hydramodem_test.dsp -o /dev/null && echo "test dsp OK"

# ---------- pkg-config (regenerated each run so PREFIX= on the command line wins) ----------
pkgconfig: $(BUILD)/hydramodem.pc
$(BUILD)/hydramodem.pc: | $(BUILD)
	@printf 'prefix=%s\n' "$(PREFIX)"                              >  $@
	@printf 'exec_prefix=$${prefix}\n'                             >> $@
	@printf 'libdir=%s\n' "$(LIBDIR)"                              >> $@
	@printf 'includedir=%s\n\n' "$(INCLUDEDIR)"                    >> $@
	@printf 'Name: hydramodem\n'                                   >> $@
	@printf 'Description: Acoustic modem for the 17-byte Punctim/DCF frame\n' >> $@
	@printf 'Version: %s\n' "$(VERSION)"                           >> $@
	@printf 'Libs: -L$${libdir} -lhydramodem\n'                    >> $@
	@printf 'Libs.private: -lm\n'                                  >> $@
	@printf 'Cflags: -I$${includedir}\n'                           >> $@

# ---------- install / uninstall ----------
install: $(LIB_A) $(LIB_SO) $(BUILD)/hydramodem.pc
	install -d $(DESTDIR)$(LIBDIR) $(DESTDIR)$(INCLUDEDIR)/hydramodem $(DESTDIR)$(PKGDIR)
	install -m644 $(LIB_A) $(DESTDIR)$(LIBDIR)/
	install -m755 $(LIB_SO) $(DESTDIR)$(LIBDIR)/
	ln -sf libhydramodem.so.$(VERSION)   $(DESTDIR)$(LIBDIR)/libhydramodem.so.$(SOVERSION)
	ln -sf libhydramodem.so.$(SOVERSION) $(DESTDIR)$(LIBDIR)/libhydramodem.so
	install -m644 $(PUBHDR) $(DESTDIR)$(INCLUDEDIR)/hydramodem/
	install -m644 $(BUILD)/hydramodem.pc $(DESTDIR)$(PKGDIR)/

uninstall:
	rm -f $(DESTDIR)$(LIBDIR)/libhydramodem.a \
	      $(DESTDIR)$(LIBDIR)/libhydramodem.so* \
	      $(DESTDIR)$(PKGDIR)/hydramodem.pc
	rm -rf $(DESTDIR)$(INCLUDEDIR)/hydramodem

# ---------- source distribution tarball ----------
DISTNAME  := hydramodem-$(VERSION)
DISTFILES := README.md BUILD.md CHANGELOG.md LICENSE NOTICE Makefile .gitignore \
             src faust examples tests docs

dist: $(DISTNAME).tar.gz
$(DISTNAME).tar.gz:
	rm -rf $(DISTNAME)
	mkdir -p $(DISTNAME)
	cp -r $(DISTFILES) $(DISTNAME)/
	tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='2025-01-01 00:00:00' \
	    -czf $@ $(DISTNAME) 2>/dev/null || tar -czf $@ $(DISTNAME)
	rm -rf $(DISTNAME)
	@echo "created $@ ($$(du -h $@ | cut -f1))"

# ---------- self-documenting ----------
help:
	@echo "HydraModem $(VERSION) -- make targets:"
	@echo "  make            build static+shared lib, examples, test binaries (default)"
	@echo "  make check      build and run the full test suite (== make test)"
	@echo "  make asan       run the suite under AddressSanitizer + UBSan"
	@echo "  make faust-check build+run loopback against the real compiled Faust DSP"
	@echo "  make validate   compile-check the Faust .dsp files only"
	@echo "  make install    install lib + headers + pkg-config (PREFIX=$(PREFIX))"
	@echo "  make dist       create $(DISTNAME).tar.gz source release"
	@echo "  make clean      remove build output and tarballs"
	@echo "  cross-compile:  make CC=riscv64-linux-gnu-gcc ARCHFLAGS=\"-march=rv64gc -mtune=sifive-u74\""

$(BUILD) $(OBJ):
	mkdir -p $@

clean:
	rm -rf $(BUILD) $(DISTNAME) $(DISTNAME).tar.gz
