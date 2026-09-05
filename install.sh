#!/bin/bash
set -Eeuo pipefail

REPOSITORY="sasocq-web/dex-remote-installer"
MODE="full"
RESTORE_FROM=""
LOCAL_DIR=""

usage() {
  cat <<'EOF'
Uso: install.sh [--mode full|projects] [--restore-from /caminho/do/snapshot] [--local-dir /pasta]

Baixa a release mais recente do Dex, verifica o SHA-256, instala a versão
fixada do Codex CLI e configura o serviço. --restore-from importa o estado
privado de um snapshot Restic previamente desbloqueado e restaurado.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; MODE="$2"; shift 2 ;;
    --restore-from) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; RESTORE_FROM="$2"; shift 2 ;;
    --local-dir) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; LOCAL_DIR="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Opção desconhecida: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ "$MODE" == "full" || "$MODE" == "projects" ]] || { echo "Modo inválido." >&2; exit 64; }
if [[ -n "$RESTORE_FROM" ]]; then
  [[ "$MODE" == "full" ]] || { echo "A recuperação privada exige o perfil full." >&2; exit 64; }
  [[ "$RESTORE_FROM" == /* && "$RESTORE_FROM" != "/" ]] || { echo "Use um caminho absoluto e específico para o snapshot." >&2; exit 64; }
fi
if [[ -n "$LOCAL_DIR" ]]; then
  [[ "$LOCAL_DIR" == /* && "$LOCAL_DIR" != "/" && -d "$LOCAL_DIR" ]] || { echo "Pasta local de instalação inválida." >&2; exit 64; }
fi

if [[ "$(id -u)" -ne 0 ]]; then
  command -v sudo >/dev/null || { echo "sudo não está disponível." >&2; exit 77; }
  elevated=(--mode "$MODE")
  [[ -z "$RESTORE_FROM" ]] || elevated+=(--restore-from "$RESTORE_FROM")
  [[ -z "$LOCAL_DIR" ]] || elevated+=(--local-dir "$LOCAL_DIR")
  exec sudo -- "$0" "${elevated[@]}"
fi

if systemctl is-active --quiet codex-linux-control.service 2>/dev/null; then
  echo "O Control Plane SASOCQ está ativo. Use este reinstalador somente numa instalação nova ou no ambiente de recuperação." >&2
  exit 78
fi

WORK="${LOCAL_DIR:-$(mktemp -d /tmp/dex-remote-install.XXXXXX)}"
cleanup() {
  [[ -z "$LOCAL_DIR" ]] || return 0
  case "$WORK" in /tmp/dex-remote-install.*) rm -rf -- "$WORK" ;; esac
}
trap cleanup EXIT

if [[ -z "$LOCAL_DIR" ]]; then
  python3 - "$REPOSITORY" "$WORK/assets" <<'PY'
import json, pathlib, sys, urllib.request
repository, output = sys.argv[1:]
request = urllib.request.Request(
    f"https://api.github.com/repos/{repository}/releases/latest",
    headers={"Accept": "application/vnd.github+json", "User-Agent": "dex-sasocq-reinstaller"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    release = json.load(response)
assets = release.get("assets") or []
deb = [item for item in assets if str(item.get("name", "")).endswith("_all.deb")]
sha = [item for item in assets if str(item.get("name", "")).endswith("_all.deb.sha256")]
if len(deb) != 1 or len(sha) != 1:
    raise SystemExit("A release mais recente não possui um par .deb + SHA-256 único.")
pathlib.Path(output).write_text(
    json.dumps({"deb": deb[0]["browser_download_url"], "sha": sha[0]["browser_download_url"]}),
    encoding="utf-8",
)
PY

  DEB_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deb"])' "$WORK/assets")"
  SHA_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' "$WORK/assets")"
  DEB="$WORK/$(basename "$DEB_URL")"
  SHA="$WORK/$(basename "$SHA_URL")"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$DEB_URL" --output "$DEB"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$SHA_URL" --output "$SHA"
else
  mapfile -t DEBS < <(find "$WORK" -maxdepth 1 -type f -name 'dex-remote-installer_*_all.deb' -print)
  mapfile -t SHAS < <(find "$WORK" -maxdepth 1 -type f -name 'dex-remote-installer_*_all.deb.sha256' -print)
  [[ ${#DEBS[@]} -eq 1 && ${#SHAS[@]} -eq 1 ]] || { echo "O bundle deve conter exatamente um .deb e um checksum." >&2; exit 66; }
  DEB="${DEBS[0]}"
  SHA="${SHAS[0]}"
fi
(cd "$WORK" && sha256sum --check "$(basename "$SHA")")

printf 'dex-remote-installer dex-remote-installer/mode select %s\n' "$MODE" | debconf-set-selections
apt-get update
apt-get install -y "$DEB"
dex-remote-setup --mode "$MODE" --install-codex

if [[ -n "$RESTORE_FROM" ]]; then
  dex-remote-restore --from "$RESTORE_FROM" --confirm
fi

curl --fail --silent --show-error http://127.0.0.1:8787/api/health >/dev/null
echo "Dex reinstalado e validado em http://127.0.0.1:8787."
