#!/usr/bin/env python3
"""
Panorama agregado de Costa Rica con la API oficial de Shodan.
IF-6000 Redes en los Negocios, UCR Sede de Occidente.

Solo usa shodan.count() con facets: devuelve totales y rankings agregados.
No obtiene IPs ni banners, no se conecta a ningun dispositivo.

Uso:
    python shodan_cr.py            # consulta Shodan y guarda datos + graficos
    python shodan_cr.py --offline  # regenera graficos desde datos_cr.json (respaldo)

La API key se lee de la variable SHODAN_API_KEY o, si no existe, de la misma
ubicacion que usa el CLI tras "shodan init". Nunca se escribe en el codigo.
"""
import argparse
import json
import os
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PAIS = "country:CR"
SALIDA = Path("salida")
DATOS = SALIDA / "datos_cr.json"

# Facets de la consulta base: (nombre, cuantos valores pedir)
FACETS = [("port", 15), ("product", 15), ("org", 15), ("city", 10), ("os", 10)]

# Servicios sensibles de los que solo contamos cuantos hay expuestos.
SERVICIOS = {
    "SSH (22)": "port:22",
    "Telnet (23)": "port:23",
    "FTP (21)": "port:21",
    "HTTP (80)": "port:80",
    "HTTPS (443)": "port:443",
    "SMB (445)": "port:445",
    "RDP (3389)": "port:3389",
    "VNC (5900)": "port:5900",
}

# Solo puertos estandar (asignados por IANA). Los demas se muestran como numero.
PUERTOS_ESTANDAR = {
    "22": "SSH", "23": "Telnet", "21": "FTP", "80": "HTTP", "443": "HTTPS",
    "123": "NTP", "161": "SNMP", "179": "BGP", "445": "SMB", "3389": "RDP",
    "5900": "VNC",
}

NOTA_GENERAL = "Se cuentan servicios indexados por Shodan, no equipos únicos ni equipos vulnerables."
NOTA_ORG = ("Valores del campo org de Shodan. Algunos corresponden a bloques de red con nombre "
            "geográfico y no se atribuyen a una empresa. " + NOTA_GENERAL)
NOTA_CIUDAD = ("La ubicación es una estimación basada en la IP y no equivale a la ubicación física "
               "exacta. " + NOTA_GENERAL)

PAUSA = 1.1  # la API permite 1 solicitud por segundo


def leer_key():
    key = os.environ.get("SHODAN_API_KEY")
    if key:
        return key.strip()
    # Misma funcion que usa el CLI de Shodan, asi siempre encuentra la key de "shodan init"
    try:
        from shodan.cli.helpers import get_api_key
        key = get_api_key()
        if key:
            return key.strip()
    except Exception:
        pass
    ruta = Path.home() / ".shodan" / "api_key"
    if ruta.exists():
        return ruta.read_text().strip()
    sys.exit("No hay API key. Ejecuta 'shodan init TU_KEY' o define SHODAN_API_KEY.")


def consultar():
    import shodan  # se importa aqui para que --offline funcione sin la libreria

    api = shodan.Shodan(leer_key())
    info = api.info()
    antes = info.get("query_credits")
    print(f"Creditos de consulta antes: {antes}")

    datos = {"fecha": datetime.now().isoformat(timespec="seconds"),
             "consulta_base": PAIS, "creditos_antes": antes,
             "total": None, "facets": {}, "servicios": {}}

    # 1) Consulta base con todos los facets
    try:
        r = api.count(PAIS, facets=FACETS)
        datos["total"] = r["total"]
        for nombre, valores in r.get("facets", {}).items():
            datos["facets"][nombre] = [(v["value"], v["count"]) for v in valores]
        print(f"Total {PAIS}: {datos['total']:,}")
    except shodan.APIError as e:
        sys.exit(f"Error en la consulta base: {e}")

    # 2) Conteo por servicio sensible
    for etiqueta, filtro in SERVICIOS.items():
        time.sleep(PAUSA)
        try:
            datos["servicios"][etiqueta] = api.count(f"{PAIS} {filtro}")["total"]
            print(f"  {etiqueta}: {datos['servicios'][etiqueta]:,}")
        except shodan.APIError as e:
            print(f"  {etiqueta}: error ({e})")

    datos["creditos_despues"] = api.info().get("query_credits")
    print(f"Creditos de consulta despues: {datos['creditos_despues']}")

    SALIDA.mkdir(exist_ok=True)
    DATOS.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Datos guardados en {DATOS}")
    return datos


def etiquetar_puertos(pares):
    """Agrega el nombre del servicio solo a puertos estandar."""
    salida = []
    for valor, cuenta in pares or []:
        nombre = PUERTOS_ESTANDAR.get(str(valor))
        salida.append((f"{valor} ({nombre})" if nombre else str(valor), cuenta))
    return salida


def pie(fig, fecha, nota=None):
    fig.text(0.99, 0.01, f"Fuente: Shodan API ({fecha}). Elaboración propia.",
             ha="right", fontsize=8, color="gray")
    if nota:
        fig.text(0.01, 0.045, "\n".join(textwrap.wrap(nota, 120)),
                 ha="left", va="bottom", fontsize=8, color="gray")


def barras(titulo, pares, archivo, color, fecha, nota=NOTA_GENERAL):
    if not pares:
        return
    etiquetas = [str(p[0]) for p in pares]
    valores = [p[1] for p in pares]
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.barh(etiquetas[::-1], valores[::-1], color=color)
    ax.set_xlabel("Cantidad de servicios indexados")
    ax.set_title(titulo)
    ax.grid(axis="x", alpha=0.3)
    pie(fig, fecha, nota)
    fig.tight_layout(rect=[0, 0.1, 1, 1])
    fig.savefig(SALIDA / archivo, dpi=150)
    plt.close(fig)
    print(f"Grafico: {SALIDA / archivo}")


def graficar(d):
    fecha = d["fecha"][:10]
    f = d["facets"]
    puertos = etiquetar_puertos(f.get("port"))
    servicios = sorted(d.get("servicios", {}).items(), key=lambda x: x[1], reverse=True)

    barras("Puertos más expuestos en Costa Rica", puertos, "01_puertos.png", "#2a6f97", fecha)
    barras("Productos más frecuentes en Costa Rica", f.get("product"), "02_productos.png", "#52796f", fecha)
    barras("Valores más frecuentes del campo org (Shodan)", f.get("org"), "03_organizaciones.png",
           "#bc6c25", fecha, NOTA_ORG)
    barras("Ubicaciones con más servicios indexados", f.get("city"), "04_ciudades.png",
           "#6a4c93", fecha, NOTA_CIUDAD)
    barras("Servicios comunes expuestos en Costa Rica", servicios, "05_servicios.png", "#9b2226", fecha)

    # Panel resumen para la demo
    fig, ejes = plt.subplots(2, 2, figsize=(14, 9))
    paneles = [("Puertos", puertos, "#2a6f97"),
               ("Productos", f.get("product"), "#52796f"),
               ("Campo org (Shodan)", f.get("org"), "#bc6c25"),
               ("Servicios comunes", servicios, "#9b2226")]
    for ax, (nombre, pares, color) in zip(ejes.flat, paneles):
        pares = (pares or [])[:8]
        ax.barh([str(p[0]) for p in pares][::-1], [p[1] for p in pares][::-1], color=color)
        ax.set_title(nombre)
        ax.set_xlabel("Servicios indexados")
        ax.grid(axis="x", alpha=0.3)
    total = d.get("total")
    fig.suptitle(f"Panorama Shodan de Costa Rica ({PAIS}), total indexado: {total:,}" if total
                 else "Panorama Shodan de Costa Rica", fontsize=14)
    pie(fig, fecha, NOTA_GENERAL)
    fig.tight_layout(rect=[0, 0.07, 1, 0.96])
    fig.savefig(SALIDA / "00_dashboard.png", dpi=150)
    plt.close(fig)
    print(f"Dashboard: {SALIDA / '00_dashboard.png'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="usar datos_cr.json en vez de consultar Shodan")
    args = ap.parse_args()

    if args.offline:
        if not DATOS.exists():
            sys.exit(f"No existe {DATOS}. Corre primero sin --offline.")
        datos = json.loads(DATOS.read_text(encoding="utf-8"))
    else:
        datos = consultar()
    graficar(datos)
