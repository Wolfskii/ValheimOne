#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
modding=${VALHEIM_MODDING_DIR:-"$HOME/valheim-modding"}
server="$modding/testserver"
[[ -x "$server/valheim_server.x86_64" ]] || { echo 'Native Linux testserver is required.' >&2; exit 1; }
exec 9>"$server/.runtime-regression.lock"
flock -n 9 || { echo 'Runtime regression already running.' >&2; exit 1; }
dotnet build "$repo/tools/RuntimeRegression/RuntimeRegression.csproj" -c Release
backup=$(mktemp -d)
plugins="$server/BepInEx/plugins"
config="$server/BepInEx/config"
output="$repo/artifacts/runtime-regression"
console_result="$backup/console-result.txt"
export CONSOLE_PROBE_TOKEN
CONSOLE_PROBE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
mkdir -p "$plugins" "$config" "$output" "$backup/worlds/worlds_local"
pid=
cleanup() {
    if [[ -n $pid ]]; then
        kill -INT -- "-$pid" 2>/dev/null || true
        for _ in {1..30}; do kill -0 -- "-$pid" 2>/dev/null || break; sleep 1; done
        kill -TERM -- "-$pid" 2>/dev/null || true
        for _ in {1..10}; do kill -0 -- "-$pid" 2>/dev/null || break; sleep 1; done
        kill -KILL -- "-$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        pid=
    fi
    for name in ValheimOne.dll ValheimOne.RuntimeRegression.dll; do
        rm -f "$plugins/$name"
        [[ ! -f "$backup/$name" ]] || mv "$backup/$name" "$plugins/$name"
    done
    rm -f "$config/valheimone.cfg"
    [[ ! -f "$backup/valheimone.cfg" ]] || mv "$backup/valheimone.cfg" "$config/valheimone.cfg"
    [[ ! -d "$config/runtime-regression" ]] || cp -a "$config/runtime-regression" "$output/data-$(date -u +%Y%m%dT%H%M%S)"
    rm -rf "$config/runtime-regression"
    [[ ! -d "$backup/runtime-regression" ]] || mv "$backup/runtime-regression" "$config/runtime-regression"
    rm -rf "$backup"
}
for name in ValheimOne.dll ValheimOne.RuntimeRegression.dll; do
    [[ ! -f "$plugins/$name" ]] || cp -p "$plugins/$name" "$backup/$name"
done
[[ ! -f "$config/valheimone.cfg" ]] || cp -p "$config/valheimone.cfg" "$backup/valheimone.cfg"
[[ ! -d "$config/runtime-regression" ]] || mv "$config/runtime-regression" "$backup/runtime-regression"
trap cleanup EXIT
cp "$repo/src/ValheimOne/bin/Release/net472/ValheimOne.dll" "$plugins/"
if [[ -n ${VALHEIMONE_PLUGIN_ZIP:-} ]]; then
    unzip -p "$VALHEIMONE_PLUGIN_ZIP" BepInEx/plugins/ValheimOne.dll > "$plugins/ValheimOne.dll"
    cmp "$plugins/ValheimOne.dll" "$repo/src/ValheimOne/bin/Release/net472/ValheimOne.dll"
fi
cp "$repo/tools/RuntimeRegression/bin/Release/net472/ValheimOne.RuntimeRegression.dll" "$plugins/"
python3 - "$repo/tools/release/valheimone.cfg" "$config/valheimone.cfg" <<'PY'
import configparser, os, sys
cfg=configparser.ConfigParser(interpolation=None, strict=False)
cfg.optionxform=str
cfg.read(sys.argv[1])
cfg['CraftFromChest']['Enabled']='true'
cfg['MapSharing']['Enabled']='true'
cfg['MapSharing']['SharedExploration']='true'
cfg['LiveMap'].update({'Enabled':'true', 'ConsoleEnabled':'true', 'BindIp':'127.0.0.1',
    'Port':'24583', 'TextureSize':'512', 'AccessToken':os.environ['CONSOLE_PROBE_TOKEN'],
    'PublicView':'false', 'StatusPublic':'false'})
with open(sys.argv[2], 'w') as out: cfg.write(out)
PY
cp "$repo/tools/fixtures/SmokeWorld.fwl" "$repo/tools/fixtures/SmokeWorld.db" "$backup/worlds/worlds_local/"
cd "$server"
rm -f "$server/BepInEx/LogOutput.log"
VALHEIMONE_RUNTIME_REGRESSION=1 VALHEIMONE_CONSOLE_PROBE=1 CONSOLE_PROBE_SETTLE_SECONDS=0 \
CONSOLE_PROBE_URL=http://127.0.0.1:24583 CONSOLE_PROBE_RESULT="$console_result" SteamAppId=892970 setsid bash -c '
    export DOORSTOP_ENABLED=1
    export DOORSTOP_TARGET_ASSEMBLY=./BepInEx/core/BepInEx.Preloader.dll
    export LD_LIBRARY_PATH="./doorstop_libs:./linux64:${LD_LIBRARY_PATH:-}"
    export LD_PRELOAD=libdoorstop_x64.so
    exec "$@"
' regression-doorstop "$server/valheim_server.x86_64" -name regression -port 24580 \
    -world SmokeWorld -password regression1 -savedir "$backup/worlds" -nographics -batchmode -public 0 \
    > "$output/server.log" 2>&1 &
pid=$!
deadline=$((SECONDS + 240))
while (( SECONDS < deadline )); do
    if [[ -f "$config/runtime-regression/result.txt" && -f "$console_result" ]]; then break; fi
    if [[ -f "$server/BepInEx/LogOutput.log" ]] &&
        grep -qE 'Failed to patch|Feature patch application failed' "$server/BepInEx/LogOutput.log"; then break; fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
status=1
if [[ -f "$config/runtime-regression/result.txt" ]]; then
    cp "$config/runtime-regression/result.txt" "$output/result.txt"
    cat "$output/result.txt"
    if grep -q '^RUNTIME REGRESSION PASS ' "$output/result.txt"; then status=0; fi
else
    echo 'Runtime regression did not complete.' >&2
    tail -n 50 "$output/server.log" >&2
fi
if [[ -f "$console_result" ]]; then
    cp "$console_result" "$output/console-result.txt"
    cat "$output/console-result.txt"
    grep -q '^CONSOLE PROBE PASS$' "$output/console-result.txt" || status=1
else
    echo 'HTTP console save probe did not complete.' >&2
    status=1
fi
cleanup
trap - EXIT
cp "$server/BepInEx/LogOutput.log" "$output/BepInEx.log"
if grep -qE 'Failed to patch|Feature patch application failed|RUNTIME REGRESSION FAIL|FieldAccessException|MissingFieldException|NullReferenceException' "$output/BepInEx.log" "$output/server.log"; then
    echo 'Runtime log contains an unhandled failure.' >&2
    grep -nE -A8 'Failed to patch|Feature patch application failed|RUNTIME REGRESSION FAIL|FieldAccessException|MissingFieldException|NullReferenceException' "$output/BepInEx.log" "$output/server.log" | head -n 100 >&2 || true
    status=1
fi
exit "$status"
