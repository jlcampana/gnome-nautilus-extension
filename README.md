# Pegar con rsync para Nautilus

Añade un botón **Pegar con rsync** a la barra superior de Nautilus y al menú contextual del
fondo de la vista. Copia en una ventana con Copiar, ve a otra y pulsa el botón. El progreso sale
abajo del panel lateral y, al terminar, llega una notificación.

## Instalación

Requiere Nautilus 43 o posterior, `nautilus-python` y `rsync`. Si falta alguno, el
instalador indica el comando para instalarlo en tu distribución.

    curl -fsSL https://raw.githubusercontent.com/jlcampana/gnome-nautilus-extension/main/install.sh | bash

Después, reinicia Nautilus:

    nautilus -q

`nautilus -q` cierra Nautilus y **cancela las copias en curso**: espera a que terminen.

Para actualizar, repite la instalación. Para desinstalar:

    curl -fsSL https://raw.githubusercontent.com/jlcampana/gnome-nautilus-extension/main/install.sh | bash -s -- --desinstalar

### Instalación manual

    install -Dm644 pegar-con-rsync.py ~/.local/share/nautilus-python/extensions/pegar-con-rsync.py
    nautilus -q

## Comportamiento

- **Conflictos:** si algo ya existe en el destino, pregunta si sobrescribir u omitir.
- **Cortar:** no mueve archivos; hay que usar el pegado normal.
- **Misma carpeta:** no duplica archivos; hay que usar el pegado normal.
- **Carpetas de red:** solo admite rutas locales o montadas como ruta local.
- **Discos sin permisos Unix (FAT, exFAT, NTFS):** usa `-rlt` en lugar de `-a`.

## Rendimiento

rsync no siempre es más rápido. No usa reflinks, así que dentro de un mismo volumen btrfs
la copia normal de Nautilus clona los archivos al instante sin ocupar espacio, y rsync hace una
copia completa. Su ventaja es poder repetir una copia interrumpida: salta los archivos ya copiados.

## Compatibilidad

Probado con Nautilus 50. El botón y el indicador se insertan en widgets internos: si una versión
futura los cambia, dejarán de aparecer, pero el menú contextual seguirá disponible.
