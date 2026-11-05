# Extensión de Nautilus (requiere los paquetes nautilus-python y rsync):
# añade un botón "Pegar con rsync" a la barra superior (y al menú del fondo de la vista)
# que copia con rsync lo copiado en cualquier ventana de Nautilus. El progreso sale abajo
# del panel lateral, junto a las operaciones normales, y al terminar avisa con una notificación.
#
# La API de extensiones no permite añadir botones ni operaciones al indicador nativo, así que
# el botón y el indicador se insertan en los widgets de la ventana. Si un Nautilus futuro cambia
# esa estructura, ambos dejan de aparecer pero el menú contextual sigue funcionando.
import os
import re
import signal
import subprocess

import gi

gi.require_foreign("cairo")
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Nautilus, Pango

TIPOS_PORTAPAPELES = ["x-special/gnome-copied-files", "text/uri-list"]
TIPO_NAUTILUS = "NautilusClipboard"  # GType del contenido cuando copia el propio Nautilus
# Sistemas de archivos sin permisos ni propietarios Unix: con -a, rsync acabaría con error
FS_SIN_PERMISOS = {"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "fuse", "cifs", "smb3"}
# 1,234,567  45%  12.34MB/s  0:00:12
PROGRESO = re.compile(r"([\d,.]+)\s+(\d+)%\s+(\S+/s)\s+(\d+:\d\d:\d\d)")
SEGUNDOS_FILA_TERMINADA = 5


def plural(n, singular, varios):
    return f"{n} {singular if n == 1 else varios}"


class Trabajo(GObject.Object):
    """Una ejecución de rsync."""

    titulo = GObject.Property(type=str, default="")
    detalle = GObject.Property(type=str, default="Preparando…")
    resumen = GObject.Property(type=str, default="Preparando…")
    fraccion = GObject.Property(type=float, default=0.0)
    terminado = GObject.Property(type=bool, default=False)

    def __init__(self, origenes, destino, omitir_existentes):
        super().__init__()
        self.origenes = origenes
        self.destino = destino
        self.omitir_existentes = omitir_existentes
        self.cancelado = False
        self.proceso = None
        self.tuberias_abiertas = 0
        self.resto_salida = ""
        self.errores = ""
        nombre = os.path.basename(origenes[0]) if len(origenes) == 1 else plural(
            len(origenes), "elemento", "elementos")
        self.nombre = nombre
        self.titulo = f"Copiando {nombre} a {os.path.basename(destino) or destino}"

    def comando(self):
        tipo = Gio.File.new_for_path(self.destino).query_filesystem_info(
            Gio.FILE_ATTRIBUTE_FILESYSTEM_TYPE, None).get_attribute_string(
            Gio.FILE_ATTRIBUTE_FILESYSTEM_TYPE)
        modo = ["-rlt", "--modify-window=1"] if tipo in FS_SIN_PERMISOS else ["-a"]
        omitir = ["--ignore-existing"] if self.omitir_existentes else []
        # --no-inc-recursive: escanea antes de copiar para que el porcentaje sea real
        return ["rsync", *modo, *omitir, "--info=progress2", "--no-inc-recursive", "--outbuf=L",
                *self.origenes, os.path.join(self.destino, "")]

    def lanzar(self):
        self.proceso = subprocess.Popen(
            self.comando(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"}, start_new_session=True)
        for tuberia, funcion in ((self.proceso.stdout, self._leer_salida),
                                 (self.proceso.stderr, self._leer_errores)):
            os.set_blocking(tuberia.fileno(), False)
            self.tuberias_abiertas += 1
            GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, tuberia.fileno(),
                                  GLib.IOCondition.IN | GLib.IOCondition.HUP,
                                  self._al_leer, tuberia, funcion)

    def _al_leer(self, _fd, _condicion, tuberia, funcion):
        try:
            datos = os.read(tuberia.fileno(), 65536)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        except OSError:
            datos = b""
        if datos:
            funcion(datos.decode(errors="replace"))
            return GLib.SOURCE_CONTINUE
        tuberia.close()
        self.tuberias_abiertas -= 1
        if self.tuberias_abiertas == 0:
            # Sin esperas bloqueantes: rsync cierra sus tuberías justo antes de salir
            GLib.timeout_add(50, self._esperar_salida)
        return GLib.SOURCE_REMOVE

    def _leer_salida(self, texto):
        # rsync escribe \r al principio de cada actualización: el último trozo ya suele estar completo
        lineas = re.split(r"[\r\n]", self.resto_salida + texto)
        self.resto_salida = lineas[-1]
        for linea in reversed(lineas):
            encontrado = PROGRESO.search(linea)
            if encontrado:
                self._mostrar_progreso(*encontrado.groups())
                break

    def _mostrar_progreso(self, bytes_copiados, porcentaje, velocidad, restante):
        copiado = GLib.format_size(int(re.sub(r"\D", "", bytes_copiados)))
        self.fraccion = int(porcentaje) / 100
        self.resumen = f"Copiando {porcentaje} %"
        self.detalle = f"{copiado} · {velocidad} · quedan {restante}"

    def _leer_errores(self, texto):
        self.errores = (self.errores + texto)[-4096:]

    def _esperar_salida(self):
        codigo = self.proceso.poll()
        if codigo is None:
            return GLib.SOURCE_CONTINUE
        self._finalizar(codigo)
        return GLib.SOURCE_REMOVE

    def _finalizar(self, codigo):
        self.fallido = False
        if self.cancelado:
            self.resumen = self.detalle = "Copia cancelada"
        elif codigo == 0:
            self.fraccion = 1.0
            self.resumen = self.detalle = "Copia terminada"
        else:
            self.fallido = True
            lineas = [l for l in self.errores.splitlines() if l.strip()]
            self.resumen = "Copia con errores"
            self.detalle = "\n".join(lineas[-3:]) or f"rsync terminó con el código {codigo}"
        self.terminado = True

    def cancelar(self):
        if self.proceso and self.proceso.poll() is None:
            self.cancelado = True
            self.detalle = "Cancelando…"
            # rsync se divide en varios procesos: la señal va a todo el grupo
            os.killpg(self.proceso.pid, signal.SIGTERM)


class Gestor(GObject.Object):
    """Estado común a todas las ventanas: trabajos en curso y contenido del portapapeles."""

    activo = GObject.Property(type=bool, default=False)
    fraccion = GObject.Property(type=float, default=0.0)
    resumen = GObject.Property(type=str, default="")
    hay_archivos = GObject.Property(type=bool, default=False)

    def __init__(self):
        super().__init__()
        self.trabajos = Gio.ListStore(item_type=Trabajo)
        self.portapapeles = None

    def vigilar_portapapeles(self):
        if self.portapapeles is None:
            self.portapapeles = Gdk.Display.get_default().get_clipboard()
            self.portapapeles.connect("changed", self._al_cambiar_portapapeles)
            self._al_cambiar_portapapeles()

    def _al_cambiar_portapapeles(self, *_):
        formatos = self.portapapeles.get_formats()
        self.hay_archivos = any(formatos.contain_mime_type(t) for t in TIPOS_PORTAPAPELES)

    def anadir(self, trabajo):
        app = Gtk.Application.get_default()
        trabajo.lanzar()
        app.hold()  # Que Nautilus no se cierre si se cierran todas las ventanas durante la copia
        for propiedad in ("fraccion", "resumen"):
            trabajo.connect(f"notify::{propiedad}", lambda *_: self._recalcular())
        trabajo.connect("notify::terminado", self._al_terminar)
        self.trabajos.append(trabajo)
        self._recalcular()

    def quitar(self, trabajo):
        encontrado, posicion = self.trabajos.find(trabajo)
        if encontrado:
            self.trabajos.remove(posicion)
            self._recalcular()
        return GLib.SOURCE_REMOVE

    def _al_terminar(self, trabajo, _propiedad):
        app = Gtk.Application.get_default()
        app.release()
        if not trabajo.cancelado:
            self._notificar(app, trabajo)
        # Los errores se quedan a la vista hasta que el usuario los descarte
        if not trabajo.fallido:
            GLib.timeout_add_seconds(SEGUNDOS_FILA_TERMINADA, self.quitar, trabajo)
        self._recalcular()

    def _notificar(self, app, trabajo):
        if trabajo.fallido:
            notificacion = Gio.Notification.new("Copia con errores")
            notificacion.set_body(f"{trabajo.nombre}\n{trabajo.detalle}")
            notificacion.set_priority(Gio.NotificationPriority.HIGH)
        else:
            notificacion = Gio.Notification.new("Copia terminada")
            notificacion.set_body(f"{trabajo.nombre} → {trabajo.destino}")
        notificacion.set_icon(Gio.ThemedIcon.new("org.gnome.Nautilus"))
        app.send_notification(f"pegar-rsync-{id(trabajo)}", notificacion)

    def _recalcular(self):
        trabajos = list(self.trabajos)
        en_curso = [t for t in trabajos if not t.terminado]
        self.activo = bool(trabajos)
        if len(trabajos) == 1:
            self.resumen = trabajos[0].resumen
            self.fraccion = trabajos[0].fraccion
        elif en_curso:
            self.fraccion = sum(t.fraccion for t in en_curso) / len(en_curso)
            self.resumen = f"{plural(len(en_curso), 'copia', 'copias')} · {round(self.fraccion * 100)} %"
        elif trabajos:
            self.fraccion = 1.0
            self.resumen = plural(len(trabajos), "copia terminada", "copias terminadas")


gestor = Gestor()


# --- Interfaz ---

class Tarta(Gtk.DrawingArea):
    """Icono circular de progreso, como el del indicador nativo."""

    fraccion = GObject.Property(type=float, default=0.0)

    def __init__(self):
        super().__init__(content_width=14, content_height=14, valign=Gtk.Align.CENTER, margin_start=3)
        self.set_draw_func(self._dibujar)
        self.connect("notify::fraccion", lambda *_: self.queue_draw())

    def _dibujar(self, _area, cr, ancho, alto):
        color = self.get_color()
        centro_x, centro_y, radio = ancho / 2, alto / 2, min(ancho, alto) / 2
        cr.set_source_rgba(color.red, color.green, color.blue, 0.25)
        cr.arc(centro_x, centro_y, radio, 0, 2 * 3.14159265)
        cr.fill()
        cr.set_source_rgba(color.red, color.green, color.blue, 1)
        cr.move_to(centro_x, centro_y)
        cr.arc(centro_x, centro_y, radio, -3.14159265 / 2, -3.14159265 / 2 + self.fraccion * 2 * 3.14159265)
        cr.close_path()
        cr.fill()


def crear_fila(trabajo):
    enlazar = GObject.BindingFlags.SYNC_CREATE
    titulo = Gtk.Label(width_request=300, hexpand=True, xalign=0, margin_bottom=6,
                       ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=40)
    barra = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True, margin_start=2, margin_bottom=4)
    detalle = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["dim-label", "numeric"])
    boton = Gtk.Button(icon_name="cancel-operation-symbolic", tooltip_text="Cancelar", valign=Gtk.Align.CENTER,
                       margin_start=20, css_classes=["circular"])
    trabajo.bind_property("titulo", titulo, "label", enlazar)
    trabajo.bind_property("fraccion", barra, "fraction", enlazar)
    trabajo.bind_property("detalle", detalle, "label", enlazar)

    def al_pulsar(_boton):
        if trabajo.terminado:
            gestor.quitar(trabajo)
        else:
            trabajo.cancelar()

    def al_terminar(*_):
        boton.set_tooltip_text("Quitar de la lista")
        boton.set_icon_name("window-close-symbolic")

    boton.connect("clicked", al_pulsar)
    trabajo.connect("notify::terminado", al_terminar)

    fila = Gtk.Grid(margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
    fila.attach(titulo, 0, 0, 1, 1)
    fila.attach(barra, 0, 1, 1, 1)
    fila.attach(detalle, 0, 2, 1, 1)
    fila.attach(boton, 1, 0, 1, 3)
    return fila


class Indicador(Adw.Bin):
    """Fila de progreso situada bajo la del indicador nativo, abajo del panel lateral."""

    activo = GObject.Property(type=bool, default=False)

    def __init__(self, nativo, panel):
        super().__init__(css_classes=["nautilus-progress-indicator"], hexpand=True, visible=False)
        self.nativo = nativo
        self.panel = panel

        tarta = Tarta()
        etiqueta = Gtk.Label(ellipsize=Pango.EllipsizeMode.END)
        contenido = Gtk.Box(spacing=9)
        contenido.append(tarta)
        contenido.append(etiqueta)

        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=False,
                            margin_start=6, margin_end=6, margin_top=6, margin_bottom=6,
                            css_classes=["operations-list"])
        lista.bind_model(gestor.trabajos, crear_fila)
        desplazable = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, max_content_height=270,
                                         propagate_natural_height=True, child=lista)
        ventana_emergente = Gtk.Popover(child=desplazable)
        boton = Gtk.MenuButton(child=contenido, popover=ventana_emergente, direction=Gtk.ArrowType.RIGHT,
                               css_classes=["flat"], tooltip_text="Mostrar copias con rsync")
        self.set_child(boton)

        enlazar = GObject.BindingFlags.SYNC_CREATE
        nativo.bind_property("popover-position", ventana_emergente, "position", enlazar)
        gestor.bind_property("fraccion", tarta, "fraccion", enlazar)
        gestor.bind_property("resumen", etiqueta, "label", enlazar)
        gestor.bind_property("activo", self, "activo", enlazar)

        self.connect("notify::activo", self._sincronizar)
        nativo.connect("notify::reveal", self._sincronizar)
        # El panel enlaza esta propiedad a la del indicador nativo y la apagaría durante la copia
        panel.connect("notify::reveal-bottom-bars", self._sincronizar)
        self._sincronizar()

    def _sincronizar(self, *_):
        nativo_activo = self.nativo.get_property("reveal")
        self.nativo.set_visible(nativo_activo)  # Sin copias nativas, su fila no debe ocupar sitio
        self.set_visible(self.activo)
        self.panel.set_reveal_bottom_bars(nativo_activo or self.activo)


def buscar(widget, condicion):
    if condicion(widget):
        return widget
    hijo = widget.get_first_child()
    while hijo is not None:
        encontrado = buscar(hijo, condicion)
        if encontrado is not None:
            return encontrado
        hijo = hijo.get_next_sibling()
    return None


def por_tipo(nombre):
    return lambda widget: widget.__gtype__.name == nombre


def instalar_en_ventanas(*_):
    ventanas = Gtk.Window.get_toplevels()
    for i in range(ventanas.get_n_items()):
        ventana = ventanas.get_item(i)
        if ventana.__gtype__.name == "NautilusWindow" and not ventana.has_css_class("pegar-rsync"):
            ventana.add_css_class("pegar-rsync")
            gestor.vigilar_portapapeles()
            instalar_boton(ventana)
            instalar_indicador(ventana)
    return GLib.SOURCE_REMOVE


def instalar_boton(ventana):
    barra = buscar(ventana, por_tipo("NautilusToolbar"))
    cabecera = barra and buscar(barra, lambda w: isinstance(w, Adw.HeaderBar))
    if not cabecera:
        print("pegar-con-rsync: no se encuentra la barra superior; solo queda el menú contextual")
        return
    boton = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Pegar con rsync", focus_on_click=False)
    boton.connect("clicked", lambda *_: pegar(ventana, ventana.get_property("active-slot").get_property("location")))
    gestor.bind_property("hay_archivos", boton, "sensitive", GObject.BindingFlags.SYNC_CREATE)
    cabecera.pack_end(boton)


def instalar_indicador(ventana):
    nativo = buscar(ventana, por_tipo("NautilusProgressIndicator"))
    panel = nativo and nativo.get_ancestor(Adw.ToolbarView)
    if not panel:
        print("pegar-con-rsync: no se encuentra el indicador de progreso nativo")
        return
    caja = nativo.get_parent()
    caja.set_orientation(Gtk.Orientation.VERTICAL)
    caja.append(Indicador(nativo, panel))


def avisar(ventana, texto):
    capa = buscar(ventana, lambda w: isinstance(w, Adw.ToastOverlay))
    if capa:
        capa.add_toast(Adw.Toast.new(texto))
    else:
        print(f"pegar-con-rsync: {texto}")


# --- Pegado ---

def pegar(ventana, carpeta):
    destino = carpeta.get_path() if carpeta else None
    if not destino:
        avisar(ventana, "Con rsync solo se puede pegar en carpetas locales")
        return
    if not os.access(destino, os.W_OK):
        avisar(ventana, "No tienes permiso para escribir en esta carpeta")
        return

    leer_portapapeles(lambda contenido: continuar(ventana, destino, contenido))


def leer_portapapeles(al_leer):
    """Llama a al_leer con el contenido del portapapeles como texto ("" si no hay archivos)."""
    portapapeles = Gdk.Display.get_default().get_clipboard()
    salida = Gio.MemoryOutputStream.new_resizable()

    def entregar():
        al_leer(salida.steal_as_bytes().get_data().decode(errors="replace"))

    def al_serializar(_salida, resultado):
        try:
            Gdk.content_serialize_finish(resultado)
            salida.close(None)
        except GLib.Error:
            al_leer("")
            return
        entregar()

    def al_obtener_valor(_portapapeles, resultado, tipo):
        try:
            valor = GObject.Value(tipo, portapapeles.read_value_finish(resultado))
        except GLib.Error:
            al_leer("")
            return
        Gdk.content_serialize_async(salida, TIPOS_PORTAPAPELES[0], valor, GLib.PRIORITY_DEFAULT, None, al_serializar)

    def al_volcar(_salida, resultado):
        salida.splice_finish(resultado)
        entregar()

    def al_abrir(_portapapeles, resultado):
        try:
            flujo, _tipo = portapapeles.read_finish(resultado)
        except GLib.Error:
            al_leer("")
            return
        salida.splice_async(flujo, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET,
                            GLib.PRIORITY_DEFAULT, None, al_volcar)

    proveedor = portapapeles.get_content()
    if proveedor is not None and TIPO_NAUTILUS in proveedor.ref_formats().to_string().split():
        # Lo copió este mismo proceso. NO usar read_async aquí: Nautilus serializa este formato con
        # escrituras síncronas a la tubería que lee este mismo hilo, y se cuelga toda la aplicación.
        # Serializado a un flujo en memoria, que nunca bloquea, el resultado es el mismo.
        tipo = GObject.type_from_name(TIPO_NAUTILUS)
        portapapeles.read_value_async(tipo, GLib.PRIORITY_DEFAULT, None, al_obtener_valor, tipo)
    else:
        portapapeles.read_async(TIPOS_PORTAPAPELES, GLib.PRIORITY_DEFAULT, None, al_abrir)


def continuar(ventana, destino, contenido):
    lineas = [l.strip() for l in contenido.splitlines() if l.strip() and not l.startswith("#")]
    if lineas and lineas[0] == "cut":
        avisar(ventana, "Eso está cortado: rsync solo copia. Para moverlo, usa Pegar")
        return
    uris = [l for l in lineas if l not in ("copy", "cut")]
    origenes = [Gio.File.new_for_uri(uri).get_path() for uri in uris]
    if not origenes:
        avisar(ventana, "No hay archivos copiados")
        return
    if None in origenes:
        avisar(ventana, "Con rsync solo se pueden copiar archivos locales")
        return
    origenes = [o.rstrip("/") or "/" for o in origenes]

    destino_real = os.path.realpath(destino)
    for origen in origenes:
        origen_real = os.path.realpath(origen)
        if os.path.dirname(origen_real) == destino_real:
            avisar(ventana, "Ya está en esta carpeta: para duplicarlo, usa Pegar")
            return
        if destino_real == origen_real or destino_real.startswith(os.path.join(origen_real, "")):
            avisar(ventana, "No se puede copiar una carpeta dentro de sí misma")
            return

    existentes = [o for o in origenes if os.path.lexists(os.path.join(destino, os.path.basename(o)))]
    if existentes:
        preguntar_conflicto(ventana, origenes, destino, existentes)
    else:
        iniciar(ventana, origenes, destino, False)


def preguntar_conflicto(ventana, origenes, destino, existentes):
    if len(existentes) == 1:
        titulo = f"«{os.path.basename(existentes[0])}» ya existe"
    else:
        titulo = f"{len(existentes)} elementos ya existen"
    dialogo = Adw.AlertDialog.new(
        titulo,
        f"Están en «{os.path.basename(destino) or destino}». Sobrescribir reemplaza los archivos que "
        "sean distintos y fusiona las carpetas. Omitir conserva los archivos que ya existen.")
    dialogo.add_response("cancelar", "Cancelar")
    dialogo.add_response("omitir", "Omitir existentes")
    dialogo.add_response("sobrescribir", "Sobrescribir")
    dialogo.set_response_appearance("sobrescribir", Adw.ResponseAppearance.DESTRUCTIVE)
    dialogo.set_default_response("omitir")
    dialogo.set_close_response("cancelar")

    def al_responder(_dialogo, respuesta):
        if respuesta != "cancelar":
            iniciar(ventana, origenes, destino, respuesta == "omitir")

    dialogo.connect("response", al_responder)
    dialogo.present(ventana)


def iniciar(ventana, origenes, destino, omitir_existentes):
    try:
        gestor.anadir(Trabajo(origenes, destino, omitir_existentes))
    except FileNotFoundError:
        avisar(ventana, "rsync no está instalado")
    except (OSError, GLib.Error) as error:
        avisar(ventana, f"No se pudo iniciar rsync: {error}")


class PegarConRsync(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self):
        super().__init__()
        # Cada vez que se abre una ventana; idle para que ya tenga sus widgets
        Gtk.Window.get_toplevels().connect("items-changed", lambda *_: GLib.idle_add(instalar_en_ventanas))
        GLib.idle_add(instalar_en_ventanas)

    def get_background_items(self, carpeta):
        item = Nautilus.MenuItem(name="PegarConRsync::fondo", label="Pegar con rsync")
        item.connect("activate", lambda *_: pegar(
            Gtk.Application.get_default().get_active_window(), carpeta.get_location()))
        return [item]
