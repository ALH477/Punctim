# DeMoD Community Node

Contribute your bandwidth and compute power to the **DeMoD Distributed Computing Framework (DCF)** — a global, low-latency Punctim network for gaming, real-time applications, and distributed workloads.

This is the **official open-source community node** — lightweight, secure, and easy to run on any VPS, home server, or even free-tier cloud instances.

[![Docker Pulls](https://img.shields.io/docker/pulls/alh477/dcf-rs.svg?style=flat-square&logo=docker)](https://hub.docker.com/r/alh477/dcf-rs)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](./LICENSE)

## Why Run a Community Node?

- Help build a truly decentralized, low-latency mesh network
- Support gamers and developers worldwide with better ping and reliability
- No telemetry, no billing, no personal data collection — pure voluntary contribution
- Runs efficiently on cheap / free hardware (Oracle Free Tier, Vultr $2.50/mo, Raspberry Pi, etc.)

## Features

- **Production-hardened** Docker setup with security best practices
- Real-time 125 Hz loop support (SYS_NICE, memory locking, high ulimits)
- CPU pinning, resource limits, read-only filesystem, tmpfs
- Automatic healthchecks and restarts
- Minimal footprint — only the DCF-SDK service
- Easy one-command setup script

## Requirements

- Docker & Docker Compose (v2+)
- Public IPv4 address
- UDP port 7777 open (inbound) on your firewall / cloud security group
- At least 1 vCPU + 512 MB RAM recommended (runs fine on 256 MB too)

## Quick Start (Recommended)

1. Clone or download this repository
2. Make the setup script executable:

   ```bash
   chmod +x setup-demod-node.sh
   ```

3. Run it (use `sudo` if you want automatic firewall rules):

   ```bash
   ./setup-demod-node.sh
   # or with custom directory:
   ./setup-demod-node.sh --dir my-demod-node
   ```

4. Follow the on-screen instructions:
   - Register your node at https://dcf.demod.ltd/register (Not running yet)
   - Add your assigned node ID to `./config/dcf_config.toml`
   - Restart: `docker compose restart`

Done! Your node should connect within seconds.

## Manual Deployment (Advanced)

```bash
# 1. Create project folder
mkdir demod-community-node && cd demod-community-node

# 2. Create config directory and file
mkdir config
# (copy or create dcf_config.toml — see example below)

# 3. Start
docker compose up -d
```

Example minimal `config/dcf_config.toml`:

```toml
[network]
gateway_url = "https://dcf.demod.ltd"
discovery_mode = "central"
node_type = "community"

[server]
bind_udp = "0.0.0.0:7777"
bind_grpc = "0.0.0.0:50051"

[performance]
target_hz = 125
shim_mode = "universal"

[node]
id = "your-node-id-from-registration"
```

## Useful Commands

```bash
# View logs
docker compose logs -f

# Check node status
docker exec demod-community-node /usr/local/bin/dcf status

# Restart
docker compose restart

# Update to latest image
docker compose pull && docker compose up -d

# Stop & remove
docker compose down
```

## Firewall & Ports

You **must** allow inbound traffic:

- **UDP 7777** → Required (main game/mesh traffic)
- **TCP 50051** → Optional (gRPC metrics/control)

Cloud providers (Oracle, Vultr, Hetzner, etc.) usually require you to also open these ports in the security group / firewall rules panel.

## Troubleshooting

- **Node not connecting?**  
  Check logs: `docker compose logs`  
  Verify UDP 7777 is open externally: `nc -u -l 7777` on the node + test from another machine

- **Container crashes?**  
  Look for permission or capability errors — make sure you're not running in a restricted environment (some cheap shared hosts disable NET_RAW)

- **Low performance?**  
  Try changing `cpuset: "0"` to another core, or increase resource limits in `docker-compose.yml`

## Contributing

Want to improve the setup script, documentation, or add features?  
Pull requests are welcome!

1. Fork this repo
2. Create a feature branch (`git checkout -b feature/amazing-thing`)
3. Commit your changes
4. Open a PR

## License

- Setup script & documentation → **MIT License**
- DCF-SDK container image → See [Docker Hub page](https://hub.docker.com/r/alh477/dcf-rs) for licensing

## Community & Support

- Website: https://dcf.demod.ltd
- Register your node: https://dcf.demod.ltd/register

Thank you for helping decentralize the future of real-time computing!

Made with spite by Asher LeRoy!!!!
