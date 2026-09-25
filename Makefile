# Punctim / DCF — top-level task runner.
# Run `make` or `make help` to list targets. This is the single discoverable entry
# point for setup, certification, tests, docs, and the client (see README.md).

.DEFAULT_GOAL := help
.PHONY: help setup certify io-matrix ci-local test docs client clean

help: ## List the available tasks
	@echo "Punctim / DCF — make targets:"
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'
	@echo ""
	@echo "Toolchain: 'nix develop' (all langs) or './install_deps.sh' (native) — see README.md."

setup: ## Install native dependencies (distro-aware; prompts before installing)
	./install_deps.sh

certify: ## Regenerate golden vectors + run the wire, audio, SuperPack, mesh & Medium certs
	@echo "== wire: verify laws + reference codec selftest =="
	python3 python/MCP/verify_laws.py /tmp/dcf_gv.json
	python3 python/MCP/certify_sdk.py --selftest
	@echo "== audio: regenerate vectors + diff against committed =="
	python3 python/MCP/gen_audio_vectors.py /tmp/dcf_av.json
	diff /tmp/dcf_av.json Documentation/audio_vectors.json
	@echo "== Rust certs =="
	cd codec && cargo test --test certify --test certify_audio
	@echo "== C certs (L2 + wire) =="
	gcc -std=c11 -I codec C_SDK/tests/test_wire_certify.c -lm -o /tmp/dcf_wc && /tmp/dcf_wc
	gcc -std=c11 -I codec C_SDK/tests/test_audio_certify.c -lm -o /tmp/dcf_ac && /tmp/dcf_ac
	@echo "== SuperPack: regenerate vectors + diff, then Rust + C certs =="
	python3 python/MCP/superpack.py
	python3 python/MCP/gen_superpack_vectors.py /tmp/dcf_sp.json
	diff /tmp/dcf_sp.json Documentation/superpack_vectors.json
	diff /tmp/dcf_sp.json python/MCP/superpack_vectors.json
	cd codec && cargo test --test certify_superpack
	gcc -std=c11 -I codec C_SDK/tests/test_superpack_certify.c -lm -o /tmp/dcf_sc && /tmp/dcf_sc
	@echo "== Modulation (Faust modem mapping): regenerate vectors + diff, Rust + C certs =="
	python3 python/MCP/modulationlab_core.py
	python3 python/MCP/gen_modulation_vectors.py /tmp/dcf_mod.json
	diff /tmp/dcf_mod.json Documentation/modulation_vectors.json
	diff /tmp/dcf_mod.json python/MCP/modulation_vectors.json
	cd codec && cargo test --test certify_modulation
	gcc -std=c11 -I codec C_SDK/tests/test_modulation_certify.c -o /tmp/dcf_mc && /tmp/dcf_mc
	@echo "== Mesh (self-healing algorithms): regenerate vectors + diff, Rust + C + Go certs =="
	python3 python/MCP/meshlab_core.py
	python3 python/MCP/gen_mesh_vectors.py /tmp/dcf_mesh.json
	diff /tmp/dcf_mesh.json Documentation/mesh_vectors.json
	diff /tmp/dcf_mesh.json python/MCP/mesh_vectors.json
	cd codec && cargo test --test certify_mesh
	gcc -std=c11 -I codec C_SDK/tests/test_mesh_certify.c -o /tmp/dcf_meshc && /tmp/dcf_meshc
	cd go && go test ./mesh/
	@echo "== Medium (DCF-Medium codecs): regenerate vectors + diff, Rust + C + Go + Node certs =="
	python3 python/MCP/mediumlab_core.py
	python3 python/MCP/gen_medium_vectors.py /tmp/dcf_med.json
	diff /tmp/dcf_med.json Documentation/medium_vectors.json
	diff /tmp/dcf_med.json python/MCP/medium_vectors.json
	diff /tmp/medium_vectors.gen.h codec/medium_vectors.gen.h
	cd codec && cargo test --test certify_medium
	gcc -std=c11 -I codec C_SDK/tests/test_medium_certify.c -lm -o /tmp/dcf_medc && /tmp/dcf_medc
	cd go && go test ./medium/
	node JS/nodejs/test/certify_medium.js
	@echo "== Minecraft (DCF-Minecraft register/events): regenerate vectors + diff, Python + Java certs =="
	python3 python/MCP/mclab_core.py
	python3 python/MCP/gen_minecraft_vectors.py /tmp/dcf_mc.json
	diff /tmp/dcf_mc.json Documentation/minecraft_vectors.json
	diff /tmp/dcf_mc.json python/MCP/minecraft_vectors.json
	cd python && python3 -m unittest tests.test_minecraft_vectors tests.test_minecraft_datapack
	mkdir -p /tmp/dcf_jmc && javac -d /tmp/dcf_jmc java/com/demod/dcf/Frame.java java/com/demod/dcf/JsonLite.java java/com/demod/dcf/Game.java java/com/demod/dcf/Text.java java/com/demod/dcf/McEvent.java java/com/demod/dcf/GameCertify.java java/com/demod/dcf/TextCertify.java java/com/demod/dcf/MinecraftCertify.java
	java -cp /tmp/dcf_jmc com.demod.dcf.GameCertify Documentation/game_vectors.json
	java -cp /tmp/dcf_jmc com.demod.dcf.TextCertify Documentation/text_vectors.json
	java -cp /tmp/dcf_jmc com.demod.dcf.MinecraftCertify Documentation/minecraft_vectors.json
	@echo "== Exsecutor: in-tree DeModFrame codec vs the live certificate =="
	@echo "   (skips without exsc/fasmg; see exsecutor/README.md)"
	./exsecutor/certify.sh
	@echo "ALL CERTS PASS"

io-matrix: ## Build the punctim CLIs and run the cross-language medium I/O matrix
	cmake -S C_SDK -B C_SDK/build -DDCF_BUILD_NODE=ON -DDCF_BUILD_EXAMPLES=OFF
	cmake --build C_SDK/build --target punctim
	cd codec && cargo build --bin punctim
	cd go && go build -o bin/punctim ./cmd/punctim
	@if [ -f hydramodem/dcf-tools/build.sh ]; then \
		bash hydramodem/dcf-tools/build.sh || \
			echo "(HydraModem tools not built: the hydra leg will be skipped)"; \
	fi
	python3 tests/io_matrix.py

ci-local: ## Run the wire-certify CI workflow locally (host toolchains + nix for the rest)
	bash .github/ci-local.sh

test: ## Run per-language tests (codec + rust + python; best-effort)
	cd codec && cargo test
	cd rust && cargo test
	pytest python/tests/ || echo "(python tests skipped/failed — see output)"

docs: ## Build the Sphinx HTML docs into Documentation/_build/html
	cd Documentation && pip install -r requirements.txt && make docs-html

client: ## Print how to run the Tauri comms client (needs the .#comms devshell)
	@echo "nix develop .#comms"
	@echo "cd client && npm install && cargo tauri dev"

clean: ## Remove Rust build artifacts
	cd codec && cargo clean
	cd rust && cargo clean
