#!/usr/bin/env bash
# Instalador de "Pegar con rsync" para Nautilus.
#   curl -fsSL https://raw.githubusercontent.com/jlcampana/gnome-nautilus-extension/main/install.sh | bash
# Desinstalar:
#   curl -fsSL https://raw.githubusercontent.com/jlcampana/gnome-nautilus-extension/main/install.sh | bash -s -- --desinstalar
set -euo pipefail

ORIGEN="${PEGAR_RSYNC_ORIGEN:-https://raw.githubusercontent.com/jlcampana/gnome-nautilus-extension/main}"
ARCHIVO="pegar-con-rsync.py"
DESTINO="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus-python/extensions"

error() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }
aviso() { printf '\033[1;33m%s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m%s\033[0m\n' "$*"; }

reiniciar() {
    if pgrep -x nautilus >/dev/null; then
        aviso "Nautilus está abierto: el cambio se aplicará cuando se reinicie."
        echo "Cuando no haya copias en curso (se cancelarían), ejecuta:"
        echo "    nautilus -q"
    fi
}

if [ "${1:-}" = "--desinstalar" ] || [ "${1:-}" = "--uninstall" ]; then
    rm -f "$DESTINO/$ARCHIVO"
    rm -f "$DESTINO/__pycache__/${ARCHIVO%.py}".*.pyc
    ok "Pegar con rsync desinstalado."
    reiniciar
    exit 0
fi

# Dependencias: no se instalan solas (harían falta permisos de administrador)
paquete_nautilus_python() {
    if command -v dnf >/dev/null; then echo "sudo dnf install nautilus-python"
    elif command -v apt >/dev/null; then echo "sudo apt install python3-nautilus"
    elif command -v pacman >/dev/null; then echo "sudo pacman -S python-nautilus"
    elif command -v zypper >/dev/null; then echo "sudo zypper install python3-nautilus"
    else echo "instala el paquete nautilus-python de tu distribución"
    fi
}
paquete_rsync() {
    if command -v dnf >/dev/null; then echo "sudo dnf install rsync"
    elif command -v apt >/dev/null; then echo "sudo apt install rsync"
    elif command -v pacman >/dev/null; then echo "sudo pacman -S rsync"
    elif command -v zypper >/dev/null; then echo "sudo zypper install rsync"
    else echo "instala rsync"
    fi
}

command -v nautilus >/dev/null || error "Nautilus no está instalado."
version=$(nautilus --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)
if [ -n "$version" ] && [ "$version" -lt 43 ]; then
    error "Hace falta Nautilus 43 o posterior (GTK 4); tienes la versión $version."
fi
command -v rsync >/dev/null || error "Falta rsync. Instálalo con: $(paquete_rsync)"
if ! compgen -G "/usr/lib*/nautilus/extensions-4/libnautilus-python.so" >/dev/null &&
   ! compgen -G "/usr/lib/*/nautilus/extensions-4/libnautilus-python.so" >/dev/null; then
    error "Falta nautilus-python. Instálalo con: $(paquete_nautilus_python)"
fi

mkdir -p "$DESTINO"
temporal=$(mktemp)
trap 'rm -f "$temporal"' EXIT
curl -fsSL "$ORIGEN/$ARCHIVO" -o "$temporal" || error "No se pudo descargar $ORIGEN/$ARCHIVO"
grep -q "class PegarConRsync" "$temporal" || error "El archivo descargado no es la extensión."
install -m644 "$temporal" "$DESTINO/$ARCHIVO"

ok "Pegar con rsync instalado en $DESTINO/$ARCHIVO"
reiniciar
