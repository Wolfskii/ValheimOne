#!/usr/bin/env bash
set -euo pipefail
# Install a bounded user service around separately provisioned native game files.
runtime=${1:?Supply the isolated game directory}
data=${2:?Supply the persistent map directory}
port=${3:-8797}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for path in "$runtime" "$data" "$source_dir"; do
  [[ "$path" =~ ^/[A-Za-z0-9_./-]+$ ]] || { echo 'Use an absolute path without spaces'; exit 1; }
done
[[ "$port" =~ ^[0-9]+$ && "$port" -gt 1024 && "$port" -lt 65536 ]] || exit 1
[[ -x "$runtime/valheim_server.x86_64" ]] || { echo 'Game runtime is missing'; exit 1; }
mkdir -p "$data" "$data/runtime-config" "$HOME/.config/systemd/user"
dotnet build "$source_dir/exporter/SeedPreviewExporter.csproj" -c Release --nologo \
  -p:ValheimManaged="$runtime/valheim_server_Data/Managed" \
  -p:BepInExCore="$runtime/BepInEx/core" -p:UseSharedCompilation=false
install -m 0644 "$source_dir/exporter/bin/Release/net472/SeedPreviewExporter.dll" "$runtime/BepInEx/plugins/SeedPreviewExporter.dll"
cat > "$HOME/.config/systemd/user/valheim-seed-map.service" <<UNIT
[Unit]
Description=Valheim seed map generator
After=network.target
[Service]
Type=simple
WorkingDirectory=$source_dir
Environment=VALHEIM_SEED_SERVER=$runtime
Environment=VALHEIM_SEED_DATA=$data
Environment=VALHEIM_SEED_PORT=$port
Environment=XDG_CONFIG_HOME=$data/runtime-config
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $source_dir/server.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group
MemoryMax=4G
CPUQuota=200%
TasksMax=128
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$data $runtime
UMask=0077
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now valheim-seed-map.service
systemctl --user is-active valheim-seed-map.service
