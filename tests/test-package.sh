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
bash -n "$ROOT/build.sh" "$ROOT/install.sh" "$ROOT/scripts/dex-remote-configure" \
  "$ROOT/scripts/dex-remote-restore" "$ROOT/scripts/create-recovery-bundle" \
  "$ROOT/scripts/reinstall-from-bundle" \
  "$ROOT/scripts/publish-after-backup"
"$ROOT/install.sh" --help >/dev/null
[[ "$(cat "$ROOT/CONTROL_PLANE_VERSION")" =~ ^[0-9][0-9A-Za-z.+:~-]*$ ]]
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
[[ -f "$EXTRACT/opt/dex-remote/app/clc/automations.py" ]]
[[ -f "$EXTRACT/opt/dex-remote/app/clc/workbench.py" ]]
[[ -f "$EXTRACT/opt/dex-remote/app/web/automations.js" ]]
[[ -f "$EXTRACT/opt/dex-remote/app/web/workbench.js" ]]
[[ "$(cat "$EXTRACT/opt/dex-remote/CODEX_CLI_VERSION")" == "$(cat "$ROOT/CODEX_CLI_VERSION")" ]]
[[ "$(cat "$EXTRACT/opt/dex-remote/CONTROL_PLANE_VERSION")" == "$(cat "$ROOT/CONTROL_PLANE_VERSION")" ]]
[[ -x "$EXTRACT/usr/sbin/dex-remote-setup" ]]
[[ -x "$EXTRACT/usr/sbin/dex-remote-restore" ]]

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

SASOCQ_ROOT="$TEST_ROOT/sasocq"
DEX_REMOTE_STATE_ROOT="$SASOCQ_ROOT" DEX_REMOTE_PACKAGE_ROOT="$EXTRACT" \
  "$EXTRACT/usr/sbin/dex-remote-setup" --mode sasocq --skip-codex --no-start
python3 - "$SASOCQ_ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
config = json.loads((root / "home/codex/.config/codex-linux-control/config.json").read_text())
assert config["install_mode"] == "sasocq"
assert config["control_plane_enabled"] is True
assert config["full_experience_installed"] is True
assert config["browser_control_enabled"] is True
assert config["external_url"] == "https://dex.sasocq.com"
assert config["allowed_project_roots"] == str(root / "srv/sasocq/projects")
assert (root / "etc/sudoers.d/dex-remote-system").is_file()
PY

RESTORE_SOURCE="$TEST_ROOT/restore-source"
mkdir -p "$RESTORE_SOURCE/home/codex/.codex" \
  "$RESTORE_SOURCE/home/codex/.config/codex-linux-control" \
  "$RESTORE_SOURCE/home/codex/SystemWorkspace" \
  "$RESTORE_SOURCE/home/codex-worker/.codex" \
  "$RESTORE_SOURCE/srv/sasocq/projects/example"
printf 'private-auth-fixture\n' >"$RESTORE_SOURCE/home/codex/.codex/auth.json"
printf 'preserve-package-config\n' >"$RESTORE_SOURCE/home/codex/.config/codex-linux-control/config.json"
printf 'conversation-fixture\n' >"$RESTORE_SOURCE/home/codex/.config/codex-linux-control/conversations.json"
printf 'project-fixture\n' >"$RESTORE_SOURCE/srv/sasocq/projects/example/README.md"
DEX_REMOTE_STATE_ROOT="$FULL_ROOT" "$EXTRACT/usr/sbin/dex-remote-restore" \
  --from "$RESTORE_SOURCE" --confirm
[[ "$(cat "$FULL_ROOT/home/codex/.codex/auth.json")" == "private-auth-fixture" ]]
[[ "$(cat "$FULL_ROOT/home/codex/.config/codex-linux-control/conversations.json")" == "conversation-fixture" ]]
[[ "$(cat "$FULL_ROOT/srv/sasocq/projects/example/README.md")" == "project-fixture" ]]
python3 - "$FULL_ROOT/home/codex/.config/codex-linux-control/config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
assert config["install_mode"] == "full"
PY

runtime_check() {
  local mode="$1" state_root="$2" home="$3" port="$4" expect_system="$5"
  local cookies="$TEST_ROOT/$mode.cookies" session="$TEST_ROOT/$mode.session.json"
  local health="$TEST_ROOT/$mode.health.json" projects="$TEST_ROOT/$mode.projects.json"
  local log="$TEST_ROOT/$mode.backend.log" ready="no" playwright_cookie=""
  HOME="$home" CLC_CONFIG_FILE="$home/.config/codex-linux-control/config.json" \
    CLC_PORT="$port" PYTHONPATH="$EXTRACT/opt/dex-remote/app" \
    python3 -m clc >"$log" 2>&1 &
  RUNTIME_PID=$!
  for _ in $(seq 1 120); do
    if [[ "$mode" == "sasocq" && -z "$playwright_cookie" && -f "$home/.local/share/codex-linux-control/browser-storage-state.json" ]]; then
      playwright_cookie="$(python3 - "$home/.local/share/codex-linux-control/browser-storage-state.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
print(next(item["value"] for item in state["cookies"] if item["name"] == "clc_playwright_access"))
PY
)"
    fi
    request_cookie=()
    [[ -z "$playwright_cookie" ]] || request_cookie=(-b "clc_playwright_access=$playwright_cookie")
    if curl -fsS "${request_cookie[@]}" -c "$cookies" "http://127.0.0.1:$port/api/session" >"$session" 2>/dev/null; then
      ready="yes"
      break
    fi
    sleep 0.1
  done
  if [[ "$ready" != "yes" ]]; then
    sed -n '1,240p' "$log" >&2
    return 1
  fi
  if [[ -n "$playwright_cookie" ]]; then
    printf '127.0.0.1\tFALSE\t/\tFALSE\t0\tclc_playwright_access\t%s\n' "$playwright_cookie" >>"$cookies"
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
assert health["ok"] is (mode != "sasocq")
assert health["functional_canary"]["ok"] is False
assert health["install_mode"] == mode
expected_identity = "internal:playwright-read-only" if mode == "sasocq" else "localhost"
assert session["identity"] == expected_identity
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
runtime_check sasocq "$SASOCQ_ROOT" "$SASOCQ_ROOT/home/codex" 18789 yes

CONTROL_VERSION="$(tr -d '[:space:]' <"$ROOT/CONTROL_PLANE_VERSION")"
CONTROL_DEB="$ROOT/dist/sasocq-control-plane_${CONTROL_VERSION}_all.deb"
CONTROL_SHA="$CONTROL_DEB.sha256"
if [[ -f "$CONTROL_DEB" && -f "$CONTROL_SHA" ]]; then
  BUNDLE_ROOT="$TEST_ROOT/recovery-bundle"
  mkdir -p "$BUNDLE_ROOT"
  touch "$BUNDLE_ROOT/dex-remote-installer_0.0.0_all.deb" \
    "$BUNDLE_ROOT/dex-remote-installer_0.0.0_all.deb.sha256"
  "$ROOT/scripts/create-recovery-bundle" "$BUNDLE_ROOT" >/dev/null
  [[ ! -e "$BUNDLE_ROOT/dex-remote-installer_0.0.0_all.deb" ]]
  MANIFEST_FIRST="$(sha256sum "$BUNDLE_ROOT/manifest.json" | awk '{print $1}')"
  "$ROOT/scripts/create-recovery-bundle" "$BUNDLE_ROOT" >/dev/null
  [[ "$(sha256sum "$BUNDLE_ROOT/manifest.json" | awk '{print $1}')" == "$MANIFEST_FIRST" ]]
  "$BUNDLE_ROOT/reinstall" --mode sasocq --verify-only >/dev/null
  python3 - "$BUNDLE_ROOT/manifest.json" "$DEB" "$CONTROL_DEB" <<'PY'
import hashlib, json, pathlib, sys
manifest_path, dex_path, control_path = map(pathlib.Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
assert manifest["sha256"] == hashlib.sha256(dex_path.read_bytes()).hexdigest()
assert manifest["control_plane_sha256"] == hashlib.sha256(control_path.read_bytes()).hexdigest()
assert manifest["contains_secrets"] is False
PY
fi

if rg -n --hidden --glob '!*.png' --glob '!test-package.sh' \
  'BEGIN (RSA|OPENSSH|EC|PGP) PRIVATE KEY|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|[A-Za-z0-9._%+-]+@(gmail|hotmail|outlook)\.' \
  "$EXTRACT"; then
  echo "Possível credencial ou identidade pessoal encontrada no pacote" >&2
  exit 1
fi

echo "Testes estruturais e funcionais do pacote concluídos."
