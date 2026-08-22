#!/bin/bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' <"$ROOT/PACKAGE_VERSION")"
DEB="$ROOT/dist/dex-remote-installer_${VERSION}_all.deb"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dex-remote-test.XXXXXX")"
RUNTIME_PID=""
cleanup() {
  if [[ -n "$RUNTIME_PID" ]]; then
    kill -KILL "$RUNTIME_PID" >/dev/null 2>&1 || true
    wait "$RUNTIME_PID" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

PYTHONPYCACHEPREFIX="$TEST_ROOT/pycache" python3 -m compileall -q "$ROOT/vendor/app/clc"
node --check "$ROOT/vendor/app/web/app.js"
node --check "$ROOT/vendor/app/web/operations.js"
node --check "$ROOT/vendor/app/web/remote-viewer.js"
node --check "$ROOT/vendor/app/web/sw.js"
bash -n "$ROOT/build.sh" "$ROOT/scripts/dex-remote-configure" \
  "$ROOT/scripts/publish-after-backup"
for script in "$ROOT/scripts/dex-remote-launcher" "$ROOT/packaging/config" \
  "$ROOT/packaging/postinst" "$ROOT/packaging/prerm" "$ROOT/packaging/postrm"; do
  dash -n "$script"
done

[[ -f "$DEB" ]]
[[ "$(dpkg-deb -f "$DEB" Package)" == "dex-remote-installer" ]]
[[ "$(dpkg-deb -f "$DEB" Architecture)" == "all" ]]
[[ "$(dpkg-deb -f "$DEB" Version)" == "$VERSION" ]]

EXTRACT="$TEST_ROOT/extract"
mkdir -p "$EXTRACT"
dpkg-deb -x "$DEB" "$EXTRACT"
[[ -f "$EXTRACT/opt/dex-remote/app/clc/main.py" ]]
[[ -x "$EXTRACT/usr/sbin/dex-remote-setup" ]]

DPKG_ROOT="$TEST_ROOT/dpkg-root"
mkdir -p "$DPKG_ROOT/var/lib/dpkg/updates" "$DPKG_ROOT/var/log"
touch "$DPKG_ROOT/var/lib/dpkg/status" "$DPKG_ROOT/var/lib/dpkg/available"
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  dpkg --root="$DPKG_ROOT" --admindir="$DPKG_ROOT/var/lib/dpkg" \
  --log="$DPKG_ROOT/var/log/dpkg.log" --force-not-root --unpack "$DEB" >/dev/null
[[ -f "$DPKG_ROOT/opt/dex-remote/app/clc/main.py" ]]
[[ "$(dpkg-query --admindir="$DPKG_ROOT/var/lib/dpkg" -W -f='${db:Status-Status}' dex-remote-installer)" == "unpacked" ]]

PROJECTS_ROOT="$TEST_ROOT/projects"
DEX_REMOTE_STATE_ROOT="$PROJECTS_ROOT" DEX_REMOTE_PACKAGE_ROOT="$EXTRACT" \
  "$EXTRACT/usr/sbin/dex-remote-setup" --mode projects --skip-codex --no-start
python3 - "$PROJECTS_ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
config = json.loads((root / "home/codex-worker/.config/codex-linux-control/config.json").read_text())
assert config["install_mode"] == "projects"
assert config["control_plane_enabled"] is False
assert config["project_codex_command"] == "/usr/lib/dex-remote/run-project-app-server"
assert not (root / "etc/sudoers.d/dex-remote-system").exists()
assert (root / "home/codex-worker/CodexProjects/AGENTS.md").is_file()
PY

FULL_ROOT="$TEST_ROOT/full"
DEX_REMOTE_STATE_ROOT="$FULL_ROOT" DEX_REMOTE_PACKAGE_ROOT="$EXTRACT" \
  "$EXTRACT/usr/sbin/dex-remote-setup" --mode full --skip-codex --no-start
python3 - "$FULL_ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
config = json.loads((root / "home/codex/.config/codex-linux-control/config.json").read_text())
assert config["install_mode"] == "full"
assert config["project_worker_home"].endswith("/home/codex-worker")
sudoers = (root / "etc/sudoers.d/dex-remote-system").read_text()
assert "codex ALL=(ALL:ALL) NOPASSWD: ALL" in sudoers
assert (root / "home/codex/SystemWorkspace/AGENTS.md").is_file()
assert (root / "home/codex-worker/CodexProjects/AGENTS.md").is_file()
PY

runtime_check() {
  local mode="$1" state_root="$2" home="$3" port="$4" expect_system="$5"
  local cookies="$TEST_ROOT/$mode.cookies" session="$TEST_ROOT/$mode.session.json"
  local health="$TEST_ROOT/$mode.health.json" projects="$TEST_ROOT/$mode.projects.json"
  local log="$TEST_ROOT/$mode.backend.log" ready="no"
  HOME="$home" CLC_CONFIG_FILE="$home/.config/codex-linux-control/config.json" \
    CLC_PORT="$port" PYTHONPATH="$EXTRACT/opt/dex-remote/app" \
    python3 -m clc >"$log" 2>&1 &
  RUNTIME_PID=$!
  for _ in $(seq 1 120); do
    if curl -fsS -c "$cookies" "http://127.0.0.1:$port/api/session" >"$session" 2>/dev/null; then
      ready="yes"
      break
    fi
    sleep 0.1
  done
  if [[ "$ready" != "yes" ]]; then
    sed -n '1,240p' "$log" >&2
    return 1
  fi
  curl -fsS -b "$cookies" "http://127.0.0.1:$port/api/health" >"$health"
  curl -fsS -b "$cookies" "http://127.0.0.1:$port/api/projects" >"$projects"
  curl -fsS "http://127.0.0.1:$port/" | grep -q '<title>Codex Linux Control</title>'
  mkdir -p "$TEST_ROOT/mock-bin"
  cat >"$TEST_ROOT/mock-bin/xdg-open" <<'EOF'
#!/bin/sh
printf '%s\n' "$1" >"$DEX_REMOTE_OPEN_RESULT"
EOF
  chmod 0755 "$TEST_ROOT/mock-bin/xdg-open"
  DEX_REMOTE_URL="http://127.0.0.1:$port" \
    DEX_REMOTE_OPEN_RESULT="$TEST_ROOT/$mode.opened" \
    PATH="$TEST_ROOT/mock-bin:$PATH" "$EXTRACT/usr/bin/dex-remote"
  [[ "$(cat "$TEST_ROOT/$mode.opened")" == "http://127.0.0.1:$port" ]]
  python3 - "$mode" "$expect_system" "$health" "$session" "$projects" <<'PY'
import json, sys
mode, expect_system, health_path, session_path, projects_path = sys.argv[1:]
health = json.load(open(health_path))
session = json.load(open(session_path))
projects = json.load(open(projects_path))["projects"]
assert health["ok"] is True
assert health["install_mode"] == mode
assert session["identity"] == "localhost"
assert projects
assert any(item["kind"] == "system" for item in projects) is (expect_system == "yes")
PY
  kill "$RUNTIME_PID" >/dev/null 2>&1 || true
  for _ in $(seq 1 50); do
    kill -0 "$RUNTIME_PID" >/dev/null 2>&1 || break
    sleep 0.1
  done
  if kill -0 "$RUNTIME_PID" >/dev/null 2>&1; then
    kill -KILL "$RUNTIME_PID" >/dev/null 2>&1 || true
  fi
  wait "$RUNTIME_PID" >/dev/null 2>&1 || true
  RUNTIME_PID=""
}

runtime_check projects "$PROJECTS_ROOT" "$PROJECTS_ROOT/home/codex-worker" 18787 no
runtime_check full "$FULL_ROOT" "$FULL_ROOT/home/codex" 18788 yes

if rg -n --hidden --glob '!*.png' --glob '!test-package.sh' \
  'BEGIN (RSA|OPENSSH|EC|PGP) PRIVATE KEY|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|[A-Za-z0-9._%+-]+@(gmail|hotmail|outlook)\.' \
  "$EXTRACT"; then
  echo "Possível credencial ou identidade pessoal encontrada no pacote" >&2
  exit 1
fi

echo "Testes estruturais e funcionais do pacote concluídos."
