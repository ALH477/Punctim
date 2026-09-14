# SPDX-License-Identifier: LGPL-3.0-only
#
# NixOS module for the DCF-SPA port authorizer (spec §10). Renders the
# nftables table and runs the authorizer as a hardened systemd service holding
# only CAP_NET_ADMIN. Point `services.dcf-spa.package` at the flake's
# `dcf-spa-authorizer` output.
#
# Authentication only — no confidentiality, no key exchange (EAR99, §3).
{ config, lib, pkgs, ... }:
let
  cfg = config.services.dcf-spa;
in {
  options.services.dcf-spa = {
    enable = lib.mkEnableOption "DCF-SPA single-packet port authorizer";
    package = lib.mkOption {
      type = lib.types.package;
      description = "The dcf-spa-authorizer package (e.g. inputs.punctim.packages.\${system}.dcf-spa-authorizer).";
    };
    knockPort = lib.mkOption { type = lib.types.port; default = 62201; };
    meshPort = lib.mkOption { type = lib.types.port; default = 7100; };
    windowMs = lib.mkOption { type = lib.types.int; default = 30000; };
    grantTtl = lib.mkOption { type = lib.types.int; default = 30; };
    credsDir = lib.mkOption {
      type = lib.types.path;
      description = "Directory of per-device public keys (NNNN.pub, ed25519) or PSKs (NNNN.key, hmac).";
    };
  };

  config = lib.mkIf cfg.enable {
    networking.nftables.enable = true;
    networking.nftables.tables.hydramesh_spa = {
      family = "inet";
      content = ''
        set allowed_peers { type ipv4_addr; flags timeout; }
        chain input {
          type filter hook input priority filter; policy drop;
          ct state established,related accept
          ct state invalid drop
          iif "lo" accept
          ip protocol icmp icmp type echo-request limit rate 5/second accept
          udp dport ${toString cfg.knockPort} limit rate 20/second accept
          ip saddr @allowed_peers udp dport ${toString cfg.meshPort} accept
          ip saddr @allowed_peers tcp dport ${toString cfg.meshPort} accept
        }
      '';
    };

    systemd.services.dcf-spa = {
      description = "DCF-SPA single-packet port authorizer";
      after = [ "network.target" "nftables.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        ExecStart = ''
          ${cfg.package}/bin/dcf-spa-authorizer \
            --knock-port ${toString cfg.knockPort} \
            --mesh-port ${toString cfg.meshPort} \
            --window-ms ${toString cfg.windowMs} \
            --grant-ttl ${toString cfg.grantTtl} \
            --creds-dir ${cfg.credsDir} \
            --nft-table hydramesh_spa --nft-set allowed_peers
        '';
        DynamicUser = true;
        # Narrowly scoped: the authorizer only needs to edit the nftables set.
        AmbientCapabilities = [ "CAP_NET_ADMIN" ];
        CapabilityBoundingSet = [ "CAP_NET_ADMIN" ];
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_NETLINK" ];
        SystemCallFilter = [ "@system-service" ];
        MemoryDenyWriteExecute = true;
        LockPersonality = true;
      };
    };
  };
}
