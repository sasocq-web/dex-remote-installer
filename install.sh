#!/bin/bash
set -Eeuo pipefail

REPOSITORY="sasocq-web/dex-remote-installer"
MODE="sasocq"
RESTORE_FROM=""
LOCAL_DIR=""
VERIFY_ONLY="no"

usage() {
  cat <<'EOF'
Uso: install.sh [--mode sasocq|full|projects] [--restore-from /caminho/do/snapshot] [--local-dir /pasta] [--verify-only]

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
    --verify-only) VERIFY_ONLY="yes"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Opção desconhecida: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ "$MODE" == "sasocq" || "$MODE" == "full" || "$MODE" == "projects" ]] || { echo "Modo inválido." >&2; exit 64; }
if [[ -n "$RESTORE_FROM" ]]; then
  [[ "$MODE" != "projects" ]] || { echo "A recuperação privada exige o perfil full ou sasocq." >&2; exit 64; }
  [[ "$RESTORE_FROM" == /* && "$RESTORE_FROM" != "/" ]] || { echo "Use um caminho absoluto e específico para o snapshot." >&2; exit 64; }
fi
if [[ -n "$LOCAL_DIR" ]]; then
  [[ "$LOCAL_DIR" == /* && "$LOCAL_DIR" != "/" && -d "$LOCAL_DIR" ]] || { echo "Pasta local de instalação inválida." >&2; exit 64; }
fi

if [[ "$(id -u)" -ne 0 && "$VERIFY_ONLY" != "yes" ]]; then
  command -v sudo >/dev/null || { echo "sudo não está disponível." >&2; exit 77; }
  elevated=(--mode "$MODE")
  [[ -z "$RESTORE_FROM" ]] || elevated+=(--restore-from "$RESTORE_FROM")
  [[ -z "$LOCAL_DIR" ]] || elevated+=(--local-dir "$LOCAL_DIR")
  [[ "$VERIFY_ONLY" != "yes" ]] || elevated+=(--verify-only)
  exec sudo -- "$0" "${elevated[@]}"
fi

if [[ "$VERIFY_ONLY" != "yes" ]] && systemctl is-active --quiet codex-linux-control.service 2>/dev/null; then
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
  python3 - "$REPOSITORY" "$WORK/assets" "$MODE" <<'PY'
import json, pathlib, sys, urllib.request
repository, output, mode = sys.argv[1:]
request = urllib.request.Request(
    f"https://api.github.com/repos/{repository}/releases/latest",
    headers={"Accept": "application/vnd.github+json", "User-Agent": "dex-sasocq-reinstaller"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    release = json.load(response)
assets = release.get("assets") or []
deb = [item for item in assets if str(item.get("name", "")).startswith("dex-remote-installer_") and str(item.get("name", "")).endswith("_all.deb")]
sha = [item for item in assets if str(item.get("name", "")).startswith("dex-remote-installer_") and str(item.get("name", "")).endswith("_all.deb.sha256")]
if len(deb) != 1 or len(sha) != 1:
    raise SystemExit("A release mais recente não possui um par .deb + SHA-256 único.")
control_deb = [item for item in assets if str(item.get("name", "")).startswith("sasocq-control-plane_") and str(item.get("name", "")).endswith("_all.deb")]
control_sha = [item for item in assets if str(item.get("name", "")).startswith("sasocq-control-plane_") and str(item.get("name", "")).endswith("_all.deb.sha256")]
if mode == "sasocq" and (len(control_deb) != 1 or len(control_sha) != 1):
    raise SystemExit("A release mais recente não possui o Control Plane SASOCQ verificável.")
pathlib.Path(output).write_text(
    json.dumps({
        "deb": deb[0]["browser_download_url"],
        "sha": sha[0]["browser_download_url"],
        "control_deb": control_deb[0]["browser_download_url"] if control_deb else "",
        "control_sha": control_sha[0]["browser_download_url"] if control_sha else "",
    }),
    encoding="utf-8",
)
PY

  DEB_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deb"])' "$WORK/assets")"
  SHA_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' "$WORK/assets")"
  DEB="$WORK/$(basename "$DEB_URL")"
  SHA="$WORK/$(basename "$SHA_URL")"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$DEB_URL" --output "$DEB"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$SHA_URL" --output "$SHA"
  CONTROL_DEB=""
  CONTROL_SHA=""
  if [[ "$MODE" == "sasocq" ]]; then
    CONTROL_DEB_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["control_deb"])' "$WORK/assets")"
    CONTROL_SHA_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["control_sha"])' "$WORK/assets")"
    CONTROL_DEB="$WORK/$(basename "$CONTROL_DEB_URL")"
    CONTROL_SHA="$WORK/$(basename "$CONTROL_SHA_URL")"
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$CONTROL_DEB_URL" --output "$CONTROL_DEB"
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$CONTROL_SHA_URL" --output "$CONTROL_SHA"
  fi
else
  mapfile -t DEBS < <(find "$WORK" -maxdepth 1 -type f -name 'dex-remote-installer_*_all.deb' -print)
  mapfile -t SHAS < <(find "$WORK" -maxdepth 1 -type f -name 'dex-remote-installer_*_all.deb.sha256' -print)
  [[ ${#DEBS[@]} -eq 1 && ${#SHAS[@]} -eq 1 ]] || { echo "O bundle deve conter exatamente um .deb e um checksum." >&2; exit 66; }
  DEB="${DEBS[0]}"
  SHA="${SHAS[0]}"
  CONTROL_DEB=""
  CONTROL_SHA=""
  if [[ "$MODE" == "sasocq" ]]; then
    mapfile -t CONTROL_DEBS < <(find "$WORK" -maxdepth 1 -type f -name 'sasocq-control-plane_*_all.deb' -print)
    mapfile -t CONTROL_SHAS < <(find "$WORK" -maxdepth 1 -type f -name 'sasocq-control-plane_*_all.deb.sha256' -print)
    [[ ${#CONTROL_DEBS[@]} -eq 1 && ${#CONTROL_SHAS[@]} -eq 1 ]] || { echo "O bundle SASOCQ deve conter o Control Plane e seu checksum." >&2; exit 66; }
    CONTROL_DEB="${CONTROL_DEBS[0]}"
    CONTROL_SHA="${CONTROL_SHAS[0]}"
  fi
fi
(cd "$WORK" && sha256sum --check "$(basename "$SHA")")
[[ -z "$CONTROL_SHA" ]] || (cd "$WORK" && sha256sum --check "$(basename "$CONTROL_SHA")")
if [[ "$VERIFY_ONLY" == "yes" ]]; then
  echo "Artefatos do perfil $MODE baixados e verificados."
  exit 0
fi

DEBCONF_MODE="$MODE"
[[ "$DEBCONF_MODE" != "sasocq" ]] || DEBCONF_MODE=full
printf 'dex-remote-installer dex-remote-installer/mode select %s\n' "$DEBCONF_MODE" | debconf-set-selections
apt-get update
if [[ "$MODE" == "sasocq" ]]; then
  apt-get install -y "$DEB" "$CONTROL_DEB"
else
  apt-get install -y "$DEB"
fi
dex-remote-setup --mode "$MODE" --install-codex

if [[ -n "$RESTORE_FROM" ]]; then
  dex-remote-restore --from "$RESTORE_FROM" --confirm
fi

curl --fail --silent --show-error http://127.0.0.1:8787/api/health >/dev/null
echo "Dex reinstalado e validado em http://127.0.0.1:8787."
