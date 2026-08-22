#!/bin/bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(tr -d '[:space:]' <"$ROOT/PACKAGE_VERSION")"
[[ "$VERSION" =~ ^[0-9][0-9A-Za-z.+:~-]*$ ]] || {
  echo "Versão Debian inválida em PACKAGE_VERSION" >&2
  exit 64
}
PACKAGE="dex-remote-installer"
OUTPUT_DIR="$ROOT/dist"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/dex-remote-deb.XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT

install -d "$STAGE/DEBIAN" \
  "$STAGE/opt/dex-remote" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/sbin" \
  "$STAGE/usr/lib/dex-remote" \
  "$STAGE/usr/lib/systemd/system" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/doc/$PACKAGE" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps"

cp -a "$ROOT/vendor/app" "$STAGE/opt/dex-remote/app"
find "$STAGE/opt/dex-remote/app" -type f -name '*.pyc' -delete
find "$STAGE/opt/dex-remote/app" -type d -name '__pycache__' -empty -delete
cp -a "$ROOT/vendor/helpers/." "$STAGE/usr/lib/dex-remote/"
install -m 0755 "$ROOT/scripts/dex-remote-launcher" "$STAGE/usr/bin/dex-remote"
install -m 0755 "$ROOT/scripts/dex-remote-configure" "$STAGE/usr/sbin/dex-remote-setup"
install -m 0644 "$ROOT/packaging/dex-remote@.service" "$STAGE/usr/lib/systemd/system/dex-remote@.service"
install -m 0644 "$ROOT/packaging/dex-remote.desktop" "$STAGE/usr/share/applications/dex-remote.desktop"
install -m 0644 "$ROOT/vendor/app/web/icons/icon.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/dex-remote.svg"
install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/$PACKAGE/README.md"
install -m 0644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/$PACKAGE/copyright"

install -m 0755 "$ROOT/packaging/config" "$STAGE/DEBIAN/config"
install -m 0755 "$ROOT/packaging/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$ROOT/packaging/prerm" "$STAGE/DEBIAN/prerm"
install -m 0755 "$ROOT/packaging/postrm" "$STAGE/DEBIAN/postrm"
install -m 0644 "$ROOT/packaging/templates" "$STAGE/DEBIAN/templates"

cat >"$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Section: devel
Priority: optional
Architecture: all
Maintainer: SASOCQ <packages@sasocq.com>
Depends: debconf (>= 1.5.0), python3 (>= 3.10), python3-fastapi, python3-uvicorn, python3-wsproto, python3-cryptography, python3-qrcode, curl, ca-certificates, sudo, systemd, xdg-utils
Conflicts: codex-linux-control
Recommends: zenity, policykit-1, novnc, tigervnc-standalone-server, openbox, xterm, xauth, x11-xserver-utils, dbus-x11
Description: Dex remoto para Codex de Projetos e Codex do Sistema
 Instala uma interface web local e reutilizável para operar o Codex em Linux.
 Oferece o perfil somente Projetos, sem sudo, ou Sistema + Projetos, no qual
 uma identidade administrativa separada recebe sudo integral sem senha.
 Nenhuma credencial, conversa ou configuração pessoal acompanha o pacote.
EOF

find "$STAGE/opt/dex-remote/app" -type d -exec chmod 0755 {} +
find "$STAGE/opt/dex-remote/app" -type f -exec chmod 0644 {} +
find "$STAGE/usr/lib/dex-remote" -type f -exec chmod 0755 {} +

install -d "$OUTPUT_DIR"
OUTPUT="$OUTPUT_DIR/${PACKAGE}_${VERSION}_all.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUTPUT"
(cd "$OUTPUT_DIR" && sha256sum "$(basename "$OUTPUT")" >"$(basename "$OUTPUT").sha256")
chmod 0644 "$OUTPUT.sha256"
echo "$OUTPUT"
