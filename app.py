# -*- coding: utf-8 -*-
"""
GRAN RIFA SOLIDARIA — app.py  (v2)
====================================

Nuevas funciones respecto a la v1:
  - Admin puede cambiar usuario y contraseña desde la app.
  - Diseño responsive (CSS clamp + media queries).
  - Estado de pago: pendiente / efectivo / nequi (selectbox en tabla admin).
  - Admin descarga el ticket de cualquier comprador como imagen PNG.
  - El ícono sobre los números vendidos es configurable por el admin (❤️, 🔒, ✅…).
  - Admin descarga una imagen del tablero lista para compartir por WhatsApp.

Ejecución
---------
    streamlit run app.py

Dependencias (requirements.txt):
    streamlit>=1.33
    pandas
    matplotlib
    streamlit-autorefresh          (opcional — refresco en vivo)

Credenciales iniciales
-----------------------
Por defecto: admin / rifa2026.
Cámbialas dentro de la app (pestaña "Mi cuenta") o con variables de entorno:
    RIFA_ADMIN_USER, RIFA_ADMIN_PASS
"""

import io
import os
import sqlite3
import datetime as dt
from contextlib import closing

import pandas as pd
import streamlit as st

# ------------------------------------------------------------------ #
# Constantes                                                           #
# ------------------------------------------------------------------ #
DB_PATH   = os.environ.get("RIFA_DB_PATH", "rifa.db")
NUMEROS   = [f"{i:02d}" for i in range(1, 100)] + ["00"]   # 01..99 y 00
TOTAL_NUM = len(NUMEROS)                                    # 100

# Valores de configuración que se guardan en la BD por primera vez.
CONFIG_DEFAULTS = {
    "titulo":        "GRAN RIFA SOLIDARIA",
    "subtitulo":     "Apoya el sueño deportivo de Daniel Henao",
    "descripcion":   ("Correr es su pasión, pero competir requiere equipo. "
                      "¡Súmate a su equipo comprando un número y comparte su sueño!"),
    "premio":        "800000",
    "valor_unitario":"20000",
    "fecha_juego":   "3 de julio",
    "modalidad":     "Juega con las dos últimas cifras del Chontico Día",
    "pago_metodo":   "Nequi",
    "pago_numero":   "3233631724",
    "pago_titular":  "Leidy Mosquera",
    "pago_banco":    "",
    "pago_cuenta":   "",
    "pago_notas":    "Envía el comprobante de pago por WhatsApp al mismo número.",
    "icono_vendido": "❤️",
    # Credenciales del admin — se inicializan en init_db a partir del entorno.
    "admin_user": "",
    "admin_pass": "",
}

OPCIONES_PAGO = ["pendiente", "efectivo", "nequi"]


def _env_or_default(env_key: str, fallback: str) -> str:
    """Lee variable de entorno → st.secrets → valor por defecto."""
    if env_key in os.environ:
        return os.environ[env_key]
    try:
        if env_key in st.secrets:
            return str(st.secrets[env_key])
    except Exception:
        pass
    return fallback


# ------------------------------------------------------------------ #
# Capa de base de datos                                                #
# ------------------------------------------------------------------ #

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Crea tablas, migra columnas antiguas y siembra datos por defecto."""
    with closing(get_conn()) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre   TEXT NOT NULL,
                apellido TEXT NOT NULL,
                celular  TEXT NOT NULL,
                total    REAL NOT NULL,
                pagado   TEXT NOT NULL DEFAULT 'pendiente',
                fecha    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS numeros (
                numero    TEXT PRIMARY KEY,
                ticket_id INTEGER REFERENCES tickets(id) ON DELETE SET NULL
            );
        """)

        # Migración: pagado como INTEGER (0/1) → texto (pendiente/efectivo).
        conn.execute(
            "UPDATE tickets SET pagado='pendiente' "
            "WHERE typeof(pagado)='integer' AND CAST(pagado AS INTEGER)=0"
        )
        conn.execute(
            "UPDATE tickets SET pagado='efectivo' "
            "WHERE typeof(pagado)='integer' AND CAST(pagado AS INTEGER)=1"
        )

        # Credenciales admin: environment > default, INSERT OR IGNORE.
        admin_user = _env_or_default("RIFA_ADMIN_USER", "admin")
        admin_pass = _env_or_default("RIFA_ADMIN_PASS", "rifa2026")

        all_defaults = {**CONFIG_DEFAULTS, "admin_user": admin_user, "admin_pass": admin_pass}
        for clave, valor in all_defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (clave, valor) VALUES (?, ?)",
                (clave, valor),
            )

        for n in NUMEROS:
            conn.execute("INSERT OR IGNORE INTO numeros (numero) VALUES (?)", (n,))

        conn.commit()


def get_config() -> dict:
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT clave, valor FROM config").fetchall()
    cfg = dict(CONFIG_DEFAULTS)
    cfg.update({k: v for k, v in rows})
    return cfg


def set_config(nuevos: dict) -> None:
    with closing(get_conn()) as conn:
        for clave, valor in nuevos.items():
            conn.execute(
                "INSERT INTO config (clave, valor) VALUES (?, ?) "
                "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
                (clave, str(valor)),
            )
        conn.commit()


def estado_numeros() -> dict:
    """Devuelve {numero: ticket_id_o_None}. Siempre lee directo de la BD."""
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT numero, ticket_id FROM numeros").fetchall()
    return {n: t for n, t in rows}


def comprar_numeros(numeros, nombre, apellido, celular):
    """
    Registra la compra con BEGIN IMMEDIATE para evitar doble-venta.
    Retorna (ok, ocupados, ticket_id).
    """
    numeros = sorted(set(numeros))
    if not numeros:
        return False, [], None

    conn = get_conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")

        marcas = ",".join("?" * len(numeros))
        ocupados = [
            r[0] for r in conn.execute(
                f"SELECT numero FROM numeros "
                f"WHERE numero IN ({marcas}) AND ticket_id IS NOT NULL",
                numeros,
            ).fetchall()
        ]
        if ocupados:
            conn.execute("ROLLBACK")
            return False, ocupados, None

        valor = float(
            conn.execute(
                "SELECT valor FROM config WHERE clave='valor_unitario'"
            ).fetchone()[0]
        )
        fecha = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO tickets (nombre, apellido, celular, total, pagado, fecha) "
            "VALUES (?, ?, ?, ?, 'pendiente', ?)",
            (nombre.strip(), apellido.strip(), celular.strip(), valor * len(numeros), fecha),
        )
        tid = cur.lastrowid
        for n in numeros:
            conn.execute("UPDATE numeros SET ticket_id=? WHERE numero=?", (tid, n))

        conn.execute("COMMIT")
        return True, [], tid
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_tickets_df() -> pd.DataFrame:
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.nombre, t.apellido, t.celular, t.total, t.pagado,
                   t.fecha, GROUP_CONCAT(n.numero) AS numeros
            FROM tickets t
            LEFT JOIN numeros n ON n.ticket_id = t.id
            GROUP BY t.id ORDER BY t.id DESC
            """
        ).fetchall()

    return pd.DataFrame(
        [
            {
                "ID": tid, "Números": (", ".join(sorted((nums or "").split(",")))
                                       if nums else "—"),
                "Nombre": nombre, "Apellido": apellido, "Celular": celular,
                "Total": total, "Pago": pagado, "Fecha": fecha,
            }
            for tid, nombre, apellido, celular, total, pagado, fecha, nums in rows
        ]
    )


def get_ticket(ticket_id):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT id, nombre, apellido, celular, total, pagado, fecha "
            "FROM tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()
        if not row:
            return None
        nums = [
            r[0] for r in conn.execute(
                "SELECT numero FROM numeros WHERE ticket_id=? ORDER BY numero",
                (ticket_id,),
            ).fetchall()
        ]
    return dict(zip(
        ["id", "nombre", "apellido", "celular", "total", "pagado", "fecha", "numeros"],
        list(row) + [nums],
    ))


def actualizar_pagos(cambios: dict) -> None:
    """cambios = {ticket_id: 'pendiente'|'efectivo'|'nequi'}"""
    with closing(get_conn()) as conn:
        for tid, metodo in cambios.items():
            conn.execute(
                "UPDATE tickets SET pagado=? WHERE id=?",
                (str(metodo), int(tid)),
            )
        conn.commit()


def anular_ticket(ticket_id) -> None:
    with closing(get_conn()) as conn:
        conn.execute("UPDATE numeros SET ticket_id=NULL WHERE ticket_id=?", (ticket_id,))
        conn.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
        conn.commit()


# ------------------------------------------------------------------ #
# Utilidades                                                           #
# ------------------------------------------------------------------ #

def cop(valor) -> str:
    """Formatea como pesos colombianos: 20000 → $20.000."""
    try:
        return "$" + f"{float(valor):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return str(valor)


def _auth_ok(cfg: dict, usuario: str, clave: str) -> bool:
    return (usuario == cfg.get("admin_user", "admin") and
            clave   == cfg.get("admin_pass", "rifa2026"))


# ------------------------------------------------------------------ #
# Generación de imágenes (matplotlib)                                  #
# ------------------------------------------------------------------ #

def _mpl():
    """Importa matplotlib de forma segura; retorna (plt, mpatches) o (None, None)."""
    try:
        import matplotlib
        matplotlib.use("Agg")          # backend sin pantalla
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        return plt, mpatches
    except ImportError:
        return None, None


def generar_imagen_tablero(cfg: dict, estado: dict):
    """PNG del tablero completo, optimizado para compartir por WhatsApp (~1080 px)."""
    plt, mpatches = _mpl()
    if plt is None:
        return None

    ROJO  = "#F4231F"
    CORAL = "#F26C6C"
    CREMA = "#FAFAF5"

    fig, ax = plt.subplots(figsize=(10.0, 14.5))
    fig.patch.set_facecolor(CREMA)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 14.5)
    ax.axis("off")

    # Cabecera — dos líneas como el flyer original
    palabras = cfg["titulo"].split(" ", 1)
    ax.text(5, 14.3, palabras[0], ha="center", va="top",
            fontsize=70, fontweight="black", color=ROJO)
    ax.text(5, 13.3, palabras[1] if len(palabras) > 1 else "",
            ha="center", va="top", fontsize=56, fontweight="black", color=ROJO)
    ax.text(5, 12.25, cfg["subtitulo"], ha="center", va="top",
            fontsize=10.5, fontweight="bold", color="#1c1c1c")

    # Banner premio / valor
    ax.add_patch(mpatches.FancyBboxPatch(
        (0.3, 11.5), 9.4, 0.62,
        boxstyle="round,pad=0.1", facecolor=CORAL, edgecolor="none"
    ))
    ax.text(5, 11.81,
            f"PREMIO: {cop(cfg['premio'])}   ·   {cop(cfg['valor_unitario'])} EL NÚMERO",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="white")

    # Cuadrícula 10×10
    GRID_TOP = 11.3
    CELL = 1.0         # cada celda mide 1 unidad en ambos ejes
    vendidos = 0
    for idx, numero in enumerate(NUMEROS):
        r, c = divmod(idx, 10)
        x = c * CELL
        y = GRID_TOP - (r + 1) * CELL
        sold = estado.get(numero) is not None
        if sold:
            vendidos += 1
        M = 0.03
        ax.add_patch(mpatches.Rectangle(
            (x + M, y + M), CELL - 2*M, CELL - 2*M,
            facecolor=(CORAL if sold else "white"),
            edgecolor="#aaaaaa", linewidth=0.35,
        ))
        ax.text(x + CELL/2, y + CELL/2, numero,
                ha="center", va="center", fontsize=9.5, fontweight="bold",
                color=("white" if sold else "#1c1c1c"))

    # Pie
    ax.text(5, 1.05, f"Vendidos: {vendidos} / {TOTAL_NUM}",
            ha="center", fontsize=9.5, color="#666")
    ax.text(5, 0.72, f"Juega el {cfg['fecha_juego']}  ·  {cfg['modalidad']}",
            ha="center", fontsize=9.5, fontweight="bold", color="#1c1c1c")
    ax.text(5, 0.38,
            f"{cfg['pago_metodo']}: {cfg['pago_numero']}  ·  {cfg['pago_titular']}",
            ha="center", fontsize=9, color="#444")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=108, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def generar_imagen_ticket(ticket_id, cfg: dict):
    """PNG tipo ticket/recibo para un comprador concreto."""
    plt, mpatches = _mpl()
    if plt is None:
        return None

    t = get_ticket(ticket_id)
    if not t:
        return None

    ROJO  = "#F4231F"
    CORAL = "#F26C6C"
    CREMA = "#FAFAF5"

    fig, ax = plt.subplots(figsize=(6, 8.2))
    fig.patch.set_facecolor(CREMA)
    ax.set_xlim(0, 6)
    ax.set_ylim(0, 8.2)
    ax.axis("off")

    # Marco punteado
    ax.add_patch(mpatches.FancyBboxPatch(
        (0.15, 0.15), 5.7, 7.9,
        boxstyle="round,pad=0.08",
        facecolor="white", edgecolor=ROJO,
        linewidth=1.8, linestyle="--"
    ))

    # Cabecera
    ax.text(3, 7.9, cfg["titulo"], ha="center", va="top",
            fontsize=13, fontweight="black", color=ROJO)
    ax.text(3, 7.45, f"Ticket N.° {t['id']}  ·  {t['fecha'][:10]}",
            ha="center", fontsize=8, color="#777")
    ax.plot([0.4, 5.6], [7.1, 7.1], color=ROJO, lw=0.7, ls="--", alpha=0.5)

    # Números
    ax.text(3, 6.9, "Número(s)", ha="center", fontsize=9, color="#999")
    ax.text(3, 6.35, "  ".join(t["numeros"]),
            ha="center", fontsize=28, fontweight="black", color="#1c1c1c")
    ax.plot([0.4, 5.6], [5.85, 5.85], color="#e0e0e0", lw=0.5)

    # Comprador
    ax.text(0.45, 5.65, f"Comprador:  {t['nombre']} {t['apellido']}",
            fontsize=10, color="#1c1c1c")
    ax.text(0.45, 5.25, f"Celular:         {t['celular']}",
            fontsize=10, color="#1c1c1c")
    ax.plot([0.4, 5.6], [4.9, 4.9], color="#e0e0e0", lw=0.5)

    # ---- Banner total: coral si pendiente, verde si ya pagó ----
    pagado = t.get("pagado", "pendiente")
    es_pagado = pagado in ("nequi", "efectivo")
    VERDE = "#27AE60"

    banner_color = VERDE if es_pagado else CORAL
    if es_pagado:
        metodo_label = "Nequi" if pagado == "nequi" else "Efectivo"
        banner_texto = f"✓  Pagado con {metodo_label}  —  {cop(t['total'])}"
    else:
        banner_texto = f"Total a pagar: {cop(t['total'])}"

    ax.add_patch(mpatches.FancyBboxPatch(
        (0.45, 4.3), 5.1, 0.52,
        boxstyle="round,pad=0.05", facecolor=banner_color, edgecolor="none"
    ))
    ax.text(3, 4.56, banner_texto,
            ha="center", va="center", fontsize=12, fontweight="bold", color="white")

    # ---- Sección inferior: "Datos de pago" (pendiente) / "Datos de responsable" (pagado) ----
    ax.plot([0.4, 5.6], [4.1, 4.1], color=(VERDE if es_pagado else ROJO),
            lw=0.7, ls="--", alpha=0.5)

    if es_pagado:
        # Pagado → muestra responsable del cobro, sin nota de WhatsApp
        ax.text(3, 3.9, "Datos de responsable", ha="center", fontsize=9,
                fontweight="bold", color="#1c1c1c")
        lineas_pago = []
        if cfg.get("pago_titular"): lineas_pago.append(cfg["pago_titular"])
        if cfg.get("pago_numero"):  lineas_pago.append(f"Celular: {cfg['pago_numero']}")
    else:
        # Pendiente → muestra instrucciones de pago completas
        ax.text(3, 3.9, "Datos de pago", ha="center", fontsize=9,
                fontweight="bold", color="#1c1c1c")
        lineas_pago = [f"{cfg['pago_metodo']}: {cfg['pago_numero']}"]
        if cfg.get("pago_titular"): lineas_pago.append(f"Titular: {cfg['pago_titular']}")
        if cfg.get("pago_banco"):   lineas_pago.append(f"Banco: {cfg['pago_banco']}")
        if cfg.get("pago_notas"):   lineas_pago.append(cfg["pago_notas"])

    for i, l in enumerate(lineas_pago):
        ax.text(3, 3.55 - i * 0.38, l, ha="center", fontsize=9, color="#444")

    # Pie
    ax.plot([0.4, 5.6], [1.35, 1.35], color="#e0e0e0", lw=0.5)
    ax.text(3, 1.15, cfg["modalidad"], ha="center", fontsize=7.5, color="#888")
    ax.text(3, 0.82, f"Juega el {cfg['fecha_juego']}",
            ha="center", fontsize=9, fontweight="bold", color="#1c1c1c")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ------------------------------------------------------------------ #
# CSS global (responsive)                                              #
# ------------------------------------------------------------------ #

def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --rojo:  #F4231F;
            --coral: #F26C6C;
            --tinta: #1c1c1c;
        }
        /* ---- Cabecera tipo flyer ---- */
        .rifa-titulo {
            font-family: 'Arial Black', Impact, sans-serif;
            font-weight: 900;
            color: var(--rojo);
            font-size: clamp(2rem, 9vw, 3.4rem);
            line-height: .95;
            letter-spacing: -1px;
            text-align: center;
            margin: 0;
        }
        .rifa-sub {
            text-align: center;
            font-weight: 800;
            font-size: clamp(.9rem, 3vw, 1.15rem);
            margin: .4rem 0 0;
        }
        .rifa-desc {
            text-align: center;
            color: #444;
            font-size: clamp(.82rem, 2.5vw, 1rem);
            margin: .4rem auto .2rem;
            max-width: 640px;
        }
        .rifa-premio {
            text-align: center;
            font-weight: 900;
            font-size: clamp(1rem, 3.5vw, 1.3rem);
            margin-top: .3rem;
        }
        .rifa-banner {
            background: var(--coral);
            color: white;
            text-align: center;
            font-weight: 800;
            font-size: clamp(.95rem, 3.5vw, 1.3rem);
            padding: .7rem;
            border-radius: 8px;
            margin: .9rem auto;
            max-width: 640px;
        }
        .rifa-footer {
            text-align: center;
            font-weight: 800;
            font-size: .9rem;
            margin-top: 1rem;
            text-transform: uppercase;
        }
        /* ---- Cuadrícula de números ---- */
        div[data-testid="column"] .stButton > button {
            width: 100%;
            border-radius: 5px;
            font-weight: 700;
            padding: .3rem 0;
            font-size: clamp(.55rem, 1.8vw, .82rem);
            border: 1px solid #cfcfcf;
            min-height: 2rem;
        }
        /* Seleccionado → coral */
        div[data-testid="column"] .stButton > button[kind="primary"] {
            background: var(--coral) !important;
            border-color: var(--coral) !important;
            color: white !important;
        }
        /* ---- Ticket HTML (vista comprador) ---- */
        .ticket {
            border: 2px dashed var(--rojo);
            border-radius: 12px;
            padding: 1.2rem 1.4rem;
            max-width: 520px;
            margin: 1rem auto;
            background: #fffdfa;
        }
        .ticket h3  { color: var(--rojo); margin: 0 0 .4rem; }
        .ticket .nums  { font-size: clamp(1.6rem, 6vw, 2rem); font-weight: 900;
                          letter-spacing: 3px; color: var(--tinta); }
        .ticket .total { font-size: clamp(1.1rem, 4vw, 1.4rem); font-weight: 900;
                          color: var(--rojo); }
        /* ---- Móvil (<= 480 px) ---- */
        @media (max-width: 480px) {
            div[data-testid="column"] .stButton > button {
                font-size: .5rem;
                padding: .15rem 0;
                min-height: 1.6rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------ #
# Vista pública — compradores                                          #
# ------------------------------------------------------------------ #

def vista_publica(cfg: dict) -> None:
    valor = float(cfg["valor_unitario"])
    icono = cfg.get("icono_vendido", "❤️")

    # Refresco automático opcional
    auto = st.sidebar.toggle("🔄 Sincronizar en vivo",
                             help="Refresca el tablero cada 6 s.")
    if auto:
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=6000, key="auto_pub")
        except ImportError:
            st.sidebar.caption("`pip install streamlit-autorefresh` para refresco automático.")

    # Cabecera
    lineas = cfg["titulo"].split(" ", 1)
    st.markdown(
        f'<p class="rifa-titulo">{"<br>".join(lineas)}</p>',
        unsafe_allow_html=True,
    )
    st.markdown(f'<p class="rifa-sub">{cfg["subtitulo"]}.</p>', unsafe_allow_html=True)
    st.markdown(f'<p class="rifa-desc">{cfg["descripcion"]}</p>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="rifa-premio">Y GANA {cop(cfg["premio"])}!</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="rifa-banner">{cop(valor)} EL NÚMERO</div>',
        unsafe_allow_html=True,
    )

    # Barra de progreso
    estado = estado_numeros()
    vendidos = sum(1 for v in estado.values() if v is not None)
    cp, cb = st.columns([4, 1])
    cp.progress(vendidos / TOTAL_NUM, text=f"{vendidos} de {TOTAL_NUM} vendidos")
    if cb.button("🔄 Actualizar", use_container_width=True):
        st.rerun()

    # Limpiar selección obsoleta
    if "seleccion" not in st.session_state:
        st.session_state.seleccion = set()
    st.session_state.seleccion = {
        n for n in st.session_state.seleccion if estado.get(n) is None
    }

    # Cuadrícula 10×10
    st.markdown("##### Elige tu(s) número(s)")
    for fila in range(0, TOTAL_NUM, 10):
        cols = st.columns(10, gap="small")
        for i, n in enumerate(NUMEROS[fila:fila + 10]):
            with cols[i]:
                if estado.get(n) is not None:
                    st.button(f"{icono}{n}", key=f"btn_{n}",
                              disabled=True, use_container_width=True)
                else:
                    sel = n in st.session_state.seleccion
                    if st.button(n, key=f"btn_{n}",
                                 type="primary" if sel else "secondary",
                                 use_container_width=True):
                        if sel:
                            st.session_state.seleccion.discard(n)
                        else:
                            st.session_state.seleccion.add(n)
                        st.rerun()

    st.caption(f"{icono} = vendido  ·  coral = seleccionado  ·  blanco = disponible")

    # Formulario de compra
    seleccion = sorted(st.session_state.seleccion)
    st.divider()
    if not seleccion:
        st.info("Selecciona uno o más números disponibles para continuar.")
        return

    total = valor * len(seleccion)
    st.markdown(f"**Seleccionados:** {', '.join(seleccion)}")
    st.markdown(f"**Total a pagar:** {cop(total)}  ({len(seleccion)} × {cop(valor)})")

    with st.form("form_compra", clear_on_submit=False):
        c1, c2 = st.columns(2)
        nombre   = c1.text_input("Nombre completo *")
        apellido = c2.text_input("Apellido *")
        celular  = st.text_input("Número de celular *")
        confirmar = st.form_submit_button(
            "✅ Confirmar y reservar mis números",
            type="primary", use_container_width=True,
        )

    if confirmar:
        if not nombre.strip() or not apellido.strip() or not celular.strip():
            st.error("Completa todos los campos requeridos.")
            return
        cel = celular.strip().lstrip("+")
        if not cel.isdigit() or len(cel) < 7:
            st.error("Ingresa un número de celular válido (solo dígitos).")
            return

        ok, ocupados, ticket_id = comprar_numeros(seleccion, nombre, apellido, celular)
        if not ok:
            st.error(
                f"⚠️ Los números **{', '.join(ocupados)}** acaban de ser tomados. "
                "Se quitaron de tu selección, elige otros."
            )
            st.session_state.seleccion -= set(ocupados)
            st.rerun()
        else:
            st.session_state.seleccion     = set()
            st.session_state.ultimo_ticket = ticket_id
            st.rerun()


def render_ticket(cfg: dict) -> None:
    """Muestra el ticket HTML y ofrece descarga como imagen PNG."""
    ticket_id = st.session_state.get("ultimo_ticket")
    if not ticket_id:
        return
    t = get_ticket(ticket_id)
    if not t:
        return

    st.success("✅ ¡Compra registrada! Conserva tu ticket y realiza el pago.")

    lineas_pago = [f"<b>{cfg['pago_metodo']}:</b> {cfg['pago_numero']}"]
    if cfg.get("pago_titular"): lineas_pago.append(f"<b>Titular:</b> {cfg['pago_titular']}")
    if cfg.get("pago_banco"):   lineas_pago.append(f"<b>Banco:</b> {cfg['pago_banco']}")
    if cfg.get("pago_cuenta"):  lineas_pago.append(f"<b>Cuenta:</b> {cfg['pago_cuenta']}")
    if cfg.get("pago_notas"):   lineas_pago.append(f"<i>{cfg['pago_notas']}</i>")

    st.markdown(
        f"""<div class="ticket">
            <h3>🎟️ {cfg['titulo']}</h3>
            <div style="color:#888;font-size:.85rem">
                Ticket N.° {t['id']} · {t['fecha']}
            </div>
            <hr>
            <div style="color:#888">Número(s):</div>
            <div class="nums">{' '.join(t['numeros'])}</div>
            <p><b>Comprador:</b> {t['nombre']} {t['apellido']}<br>
               <b>Celular:</b> {t['celular']}</p>
            <div class="total">Total a pagar: {cop(t['total'])}</div>
            <hr>
            <div><b>Datos de pago</b><br>{'<br>'.join(lineas_pago)}</div>
            <hr>
            <div style="font-size:.82rem;color:#666">
                {cfg['modalidad']} · Juega el {cfg['fecha_juego']}.
            </div>
        </div>""",
        unsafe_allow_html=True,
    )

    img = generar_imagen_ticket(ticket_id, cfg)
    if img:
        st.download_button(
            "⬇️ Guardar ticket como imagen",
            data=img, file_name=f"ticket_{ticket_id}.png", mime="image/png",
        )

    if st.button("Hacer otra compra"):
        st.session_state.ultimo_ticket = None
        st.rerun()


# ------------------------------------------------------------------ #
# Vista de administración                                              #
# ------------------------------------------------------------------ #

def login_admin(cfg: dict) -> bool:
    if st.session_state.get("admin_ok"):
        return True
    st.subheader("🔐 Acceso administrador")
    with st.form("login"):
        usuario = st.text_input("Usuario")
        clave   = st.text_input("Contraseña", type="password")
        entrar  = st.form_submit_button("Ingresar", type="primary")
    if entrar:
        if _auth_ok(cfg, usuario, clave):
            st.session_state.admin_ok = True
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")
    return False


def panel_admin(cfg: dict) -> None:
    st.subheader("📋 Panel de administración")
    if st.sidebar.button("Cerrar sesión"):
        st.session_state.admin_ok = False
        st.rerun()

    tab_reg, tab_img, tab_cfg, tab_cta = st.tabs(
        ["Registros y pagos", "Tablero / WhatsApp", "Configuración", "Mi cuenta"]
    )

    # ============================================================== #
    # TAB 1 — Registros y pagos                                       #
    # ============================================================== #
    with tab_reg:
        df = get_tickets_df()

        if not df.empty:
            cnt_vend = df["Números"].apply(
                lambda s: 0 if s == "—" else len(s.split(","))
            ).sum()
            recaudado = df.loc[df["Pago"] != "pendiente", "Total"].sum()
        else:
            cnt_vend, recaudado = 0, 0.0

        m1, m2, m3 = st.columns(3)
        m1.metric("Números vendidos",  f"{cnt_vend}/{TOTAL_NUM}")
        m2.metric("Recaudado (pagado)", cop(recaudado))
        m3.metric("Valor unitario",     cop(cfg["valor_unitario"]))

        if df.empty:
            st.info("Aún no hay ventas registradas.")
        else:
            df_ed = df.copy()
            df_ed["Total"] = df_ed["Total"].apply(cop)
            editado = st.data_editor(
                df_ed,
                hide_index=True,
                use_container_width=True,
                disabled=["ID","Números","Nombre","Apellido","Celular","Total","Fecha"],
                column_config={
                    "Pago": st.column_config.SelectboxColumn(
                        "Estado de pago",
                        options=OPCIONES_PAGO,
                        required=True,
                        help="pendiente · efectivo · nequi",
                    )
                },
                key="ed_pagos",
            )

            ca, cb = st.columns(2)
            if ca.button("💾 Guardar pagos", type="primary"):
                actualizar_pagos({int(r["ID"]): r["Pago"] for _, r in editado.iterrows()})
                st.success("Estados de pago guardados.")
                st.rerun()

            # Anular ticket
            opts = ["—"] + [
                f"T{r['ID']} — {r['Números']} ({r['Nombre']} {r['Apellido']})"
                for _, r in df.iterrows()
            ]
            id_map = {
                f"T{r['ID']} — {r['Números']} ({r['Nombre']} {r['Apellido']})": r["ID"]
                for _, r in df.iterrows()
            }
            anular_sel = cb.selectbox("Anular ticket", opts, key="sel_anular")
            if cb.button("Anular y liberar") and anular_sel != "—":
                anular_ticket(int(id_map[anular_sel]))
                st.warning("Ticket anulado y números liberados.")
                st.rerun()

            # Descargar ticket individual como imagen
            st.divider()
            st.markdown("**Descargar ticket de un comprador como imagen**")
            t_opts = ["—"] + [
                f"T{r['ID']} — {r['Números']} ({r['Nombre']} {r['Apellido']})"
                for _, r in df.iterrows()
            ]
            t_id_map = {
                f"T{r['ID']} — {r['Números']} ({r['Nombre']} {r['Apellido']})": r["ID"]
                for _, r in df.iterrows()
            }
            sel_t = st.selectbox("Selecciona un ticket", t_opts, key="sel_dl_ticket")
            if sel_t != "—":
                img_t = generar_imagen_ticket(int(t_id_map[sel_t]), cfg)
                if img_t:
                    st.download_button(
                        "⬇️ Descargar ticket PNG",
                        data=img_t,
                        file_name=f"ticket_{t_id_map[sel_t]}.png",
                        mime="image/png",
                        type="primary",
                    )
                else:
                    st.warning("Instala `matplotlib` para generar imágenes.")

            # CSV de respaldo
            st.download_button(
                "⬇️ Exportar registros (CSV)",
                data=df.to_csv(index=False).encode("utf-8-sig"),
                file_name="rifa_registros.csv",
                mime="text/csv",
            )

    # ============================================================== #
    # TAB 2 — Tablero / imagen para WhatsApp                          #
    # ============================================================== #
    with tab_img:
        st.markdown(
            "Genera una imagen PNG del tablero actualizado, lista para "
            "compartir por WhatsApp o redes sociales (~1080 px de ancho)."
        )
        estado = estado_numeros()
        vend = sum(1 for v in estado.values() if v)
        st.metric("Números vendidos en este momento", f"{vend}/{TOTAL_NUM}")

        if st.button("🖼️ Generar imagen del tablero", type="primary"):
            with st.spinner("Generando imagen…"):
                img_tab = generar_imagen_tablero(cfg, estado)
            if img_tab:
                st.image(img_tab, caption="Vista previa del tablero", use_container_width=True)
                st.download_button(
                    "⬇️ Descargar imagen (PNG)",
                    data=img_tab,
                    file_name="tablero_rifa.png",
                    mime="image/png",
                )
            else:
                st.error("Instala `matplotlib` (`pip install matplotlib`) para generar imágenes.")

    # ============================================================== #
    # TAB 3 — Configuración                                           #
    # ============================================================== #
    with tab_cfg:
        with st.form("form_cfg"):
            st.markdown("**Datos generales de la rifa**")
            c1, c2 = st.columns(2)
            val_unit = c1.number_input("Valor unitario (COP)", min_value=0, step=1000,
                                       value=int(float(cfg["valor_unitario"])))
            premio   = c2.number_input("Premio (COP)", min_value=0, step=10000,
                                       value=int(float(cfg["premio"])))
            titulo      = st.text_input("Título",     value=cfg["titulo"])
            subtitulo   = st.text_input("Subtítulo",  value=cfg["subtitulo"])
            descripcion = st.text_area("Descripción", value=cfg["descripcion"])
            c3, c4 = st.columns(2)
            fecha_juego = c3.text_input("Fecha de juego", value=cfg["fecha_juego"])
            modalidad   = c4.text_input("Modalidad",       value=cfg["modalidad"])

            st.markdown("**Ícono para números vendidos** (tablero web)")
            icono_vendido = st.text_input(
                "Emoji o carácter (máx. 4, p. ej. ❤️ 🔒 ✅ 🎟️ ★)",
                value=cfg.get("icono_vendido", "❤️"), max_chars=4,
            )

            st.markdown("**Datos de consignación / pago** (aparecen en el ticket)")
            c5, c6 = st.columns(2)
            pago_metodo  = c5.text_input("Método (Nequi, Bancolombia…)", value=cfg["pago_metodo"])
            pago_numero  = c6.text_input("Número / referencia",           value=cfg["pago_numero"])
            c7, c8 = st.columns(2)
            pago_titular = c7.text_input("Titular",               value=cfg["pago_titular"])
            pago_banco   = c8.text_input("Banco (opcional)",      value=cfg.get("pago_banco",""))
            pago_cuenta  = st.text_input("Número de cuenta (opcional)", value=cfg.get("pago_cuenta",""))
            pago_notas   = st.text_area("Notas de pago",          value=cfg["pago_notas"])

            guardar = st.form_submit_button("💾 Guardar configuración", type="primary")

        if guardar:
            set_config({
                "valor_unitario": val_unit, "premio": premio,
                "titulo": titulo, "subtitulo": subtitulo,
                "descripcion": descripcion, "fecha_juego": fecha_juego,
                "modalidad": modalidad, "icono_vendido": icono_vendido,
                "pago_metodo": pago_metodo, "pago_numero": pago_numero,
                "pago_titular": pago_titular, "pago_banco": pago_banco,
                "pago_cuenta": pago_cuenta, "pago_notas": pago_notas,
            })
            st.success("Configuración guardada correctamente.")
            st.rerun()

    # ============================================================== #
    # TAB 4 — Mi cuenta (cambiar credenciales)                        #
    # ============================================================== #
    with tab_cta:
        st.markdown(
            "Cambia el usuario y la contraseña del administrador.\n\n"
            "> ⚠️ **Anota las nuevas credenciales antes de guardar.** "
            "Si las olvidas tendrás que modificar la BD directamente."
        )
        with st.form("form_cuenta"):
            st.markdown("**Verificación de identidad**")
            usr_actual = st.text_input("Usuario actual")
            pwd_actual = st.text_input("Contraseña actual", type="password")
            st.divider()
            st.markdown("**Nuevas credenciales**")
            nuevo_usr = st.text_input("Nuevo usuario",
                                      value=cfg.get("admin_user","admin"))
            nueva_pwd  = st.text_input("Nueva contraseña",         type="password")
            nueva_pwd2 = st.text_input("Confirmar nueva contraseña", type="password")
            guardar_cta = st.form_submit_button("🔑 Actualizar credenciales", type="primary")

        if guardar_cta:
            if not _auth_ok(cfg, usr_actual, pwd_actual):
                st.error("El usuario o la contraseña actual son incorrectos.")
            elif not nuevo_usr.strip():
                st.error("El nuevo usuario no puede estar vacío.")
            elif len(nueva_pwd) < 6:
                st.error("La nueva contraseña debe tener al menos 6 caracteres.")
            elif nueva_pwd != nueva_pwd2:
                st.error("Las nuevas contraseñas no coinciden.")
            else:
                set_config({"admin_user": nuevo_usr.strip(), "admin_pass": nueva_pwd})
                st.session_state.admin_ok = False
                st.success("Credenciales actualizadas. Vuelve a iniciar sesión.")
                st.rerun()


# ------------------------------------------------------------------ #
# Punto de entrada                                                     #
# ------------------------------------------------------------------ #

def main() -> None:
    st.set_page_config(
        page_title="Gran Rifa Solidaria",
        page_icon="🎟️",
        layout="centered",
    )
    init_db()
    inject_css()
    cfg = get_config()

    vista = st.sidebar.radio(
        "", ["🎟️ Comprar números", "🔐 Administración"],
        label_visibility="collapsed",
    )

    if "Comprar" in vista:
        if st.session_state.get("ultimo_ticket"):
            render_ticket(cfg)
        else:
            vista_publica(cfg)
        st.markdown(
            f'<div class="rifa-footer">'
            f'Juega el {cfg["fecha_juego"]} · {cfg["modalidad"]}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        if login_admin(cfg):
            panel_admin(cfg)


if __name__ == "__main__":
    main()
