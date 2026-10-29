#!/usr/bin/env python3
"""
Interfaz grafica local para el panorama de Costa Rica con Shodan.
IF-6000 Redes en los Negocios, UCR Sede de Occidente.

Levanta un servidor SOLO en este equipo (127.0.0.1) y abre el navegador con un
dashboard. No necesita escribir comandos durante la demo:

  - Boton "Consultar Shodan en vivo": ejecuta las mismas consultas de shodan_cr.py
    (api.count con facets, sin IPs ni banners) y va llenando las graficas.
  - Selector de datos guardados: funciona sin red ni creditos (respaldo).

Uso (desde la carpeta scripts, con el entorno virtual activo):
    python shodan_gui.py
    python shodan_gui.py --puerto 9000 --sin-navegador

La API key se lee igual que en shodan_cr.py (variable SHODAN_API_KEY o la de
"shodan init"). Nunca se envia al navegador. Solo libreria estandar + shodan_cr.py.
"""
import argparse
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import shodan_cr as cr  # noqa: E402  (reutiliza constantes y la lectura de la key)

VIVO = AQUI / "salida_vivo"       # la consulta en vivo NO pisa las carpetas salida*
LOCK = threading.Lock()           # una sola consulta en vivo a la vez
HOSTS_OK = ("127.0.0.1", "localhost")

CONFIG = {
    "pais": cr.PAIS,
    "puertos": cr.PUERTOS_ESTANDAR,
    "notaGeneral": cr.NOTA_GENERAL,
    "notaOrg": cr.NOTA_ORG,
    "notaCiudad": cr.NOTA_CIUDAD,
    "totalServicios": len(cr.SERVICIOS),
}


def listar_datasets():
    salida = []
    for f in sorted(AQUI.glob("salida*/datos_cr.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        salida.append({"id": f.parent.name, "fecha": d.get("fecha"), "total": d.get("total")})
    return salida


def pagina():
    cfg = json.dumps(CONFIG, ensure_ascii=False).replace("</", "<\\/")
    return HTML.replace("__CONFIG__", cfg).encode("utf-8")


class Manejador(BaseHTTPRequestHandler):
    server_version = "ShodanGUI"

    def log_message(self, *a):  # silencio en la terminal
        pass

    # ---------- utilidades ----------
    def _enviar(self, codigo, tipo, cuerpo):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, obj, codigo=200):
        self._enviar(codigo, "application/json; charset=utf-8",
                     json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _local(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        origen = self.headers.get("Origin")
        if host not in HOSTS_OK:
            return False
        if origen and urlparse(origen).hostname not in HOSTS_OK:
            return False
        return True

    # ---------- rutas ----------
    def do_GET(self):
        if not self._local():
            return self._enviar(403, "text/plain; charset=utf-8", b"Solo acceso local")
        ruta = urlparse(self.path)
        if ruta.path == "/":
            return self._enviar(200, "text/html; charset=utf-8", pagina())
        if ruta.path == "/api/datasets":
            return self._json(listar_datasets())
        if ruta.path == "/api/datos":
            ident = parse_qs(ruta.query).get("id", [""])[0]
            if ident not in {x["id"] for x in listar_datasets()}:  # evita rutas arbitrarias
                return self._json({"error": "conjunto de datos no encontrado"}, 404)
            texto = (AQUI / ident / "datos_cr.json").read_text(encoding="utf-8")
            return self._enviar(200, "application/json; charset=utf-8", texto.encode("utf-8"))
        if ruta.path == "/api/vivo":
            return self._vivo()
        self._enviar(404, "text/plain; charset=utf-8", b"No encontrado")

    def _vivo(self):
        if not LOCK.acquire(blocking=False):
            return self._json({"error": "ya hay una consulta en curso"}, 409)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            def emitir(tipo, **kw):
                linea = json.dumps({"tipo": tipo, **kw}, ensure_ascii=False)
                self.wfile.write(f"data: {linea}\n\n".encode("utf-8"))
                self.wfile.flush()

            try:
                self._consulta(emitir)
            except (BrokenPipeError, ConnectionResetError):
                pass  # el navegador cerro la pestana
            except SystemExit as e:  # leer_key() usa sys.exit(mensaje)
                self._error_seguro(emitir, str(e.code))
            except Exception as e:
                self._error_seguro(emitir, f"{type(e).__name__}: {e}")
        finally:
            LOCK.release()

    @staticmethod
    def _error_seguro(emitir, mensaje):
        try:
            emitir("error", mensaje=mensaje)
        except Exception:
            pass

    def _consulta(self, emitir):
        """Misma logica que shodan_cr.consultar(), pero informando cada paso."""
        import shodan  # import tardio: la GUI abre aunque falte la libreria

        emitir("paso", texto="Leyendo la API key local (nunca sale de este equipo)")
        api = shodan.Shodan(cr.leer_key())

        emitir("paso", texto="api.info()  ->  creditos antes")
        antes = api.info().get("query_credits")
        emitir("creditos_antes", valor=antes)

        datos = {"fecha": datetime.now().isoformat(timespec="seconds"),
                 "consulta_base": cr.PAIS, "creditos_antes": antes,
                 "total": None, "facets": {}, "servicios": {}}

        emitir("paso", texto=f"api.count('{cr.PAIS}', facets=port, product, org, city, os)")
        try:
            r = api.count(cr.PAIS, facets=cr.FACETS)
        except shodan.APIError as e:
            return emitir("error", mensaje=f"Error en la consulta base: {e}")
        datos["total"] = r["total"]
        for nombre, valores in r.get("facets", {}).items():
            datos["facets"][nombre] = [(v["value"], v["count"]) for v in valores]
        emitir("base", total=datos["total"], facets=datos["facets"], fecha=datos["fecha"])

        n = len(cr.SERVICIOS)
        for i, (etiqueta, filtro) in enumerate(cr.SERVICIOS.items(), start=1):
            time.sleep(cr.PAUSA)  # la API permite 1 solicitud por segundo
            consulta = f"{cr.PAIS} {filtro}"
            emitir("paso", texto=f"api.count('{consulta}')")
            try:
                valor = api.count(consulta)["total"]
                datos["servicios"][etiqueta] = valor
                emitir("servicio", etiqueta=etiqueta, total=valor, n=i, de=n)
            except shodan.APIError as e:
                emitir("servicio_error", etiqueta=etiqueta, mensaje=str(e), n=i, de=n)

        emitir("paso", texto="api.info()  ->  creditos despues")
        datos["creditos_despues"] = api.info().get("query_credits")

        VIVO.mkdir(exist_ok=True)
        (VIVO / "datos_cr.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        emitir("fin", datos=datos, guardado="salida_vivo/datos_cr.json")


def main():
    ap = argparse.ArgumentParser(description="Dashboard local de Shodan para Costa Rica")
    ap.add_argument("--puerto", type=int, default=8765)
    ap.add_argument("--sin-navegador", action="store_true")
    args = ap.parse_args()

    servidor = None
    for puerto in range(args.puerto, args.puerto + 20):
        try:
            servidor = ThreadingHTTPServer(("127.0.0.1", puerto), Manejador)
            break
        except OSError:
            continue
    if servidor is None:
        sys.exit("No encontre un puerto libre. Usa --puerto OTRO_NUMERO.")
    servidor.daemon_threads = True

    url = f"http://127.0.0.1:{servidor.server_address[1]}/"
    print(f"Dashboard en {url}   (Ctrl+C para cerrar)")
    if not args.sin_navegador:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado.")


HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shodan · Panorama de Costa Rica</title>
<style>
  :root{
    --bg:#0E1726; --bg2:#0A111D; --panel:#16233A; --line:#26385A;
    --txt:#E8EDF4; --mut:#9FB0C6; --acc:#F0654D; --teal:#4CC3B0; --ok:#5FD08A; --warn:#F0B34D;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans)}
  body{min-height:100vh;background:
        radial-gradient(1200px 500px at 85% -10%, rgba(240,101,77,.14), transparent 60%),
        radial-gradient(900px 500px at -10% 110%, rgba(76,195,176,.10), transparent 60%), var(--bg)}
  .wrap{max-width:1500px;margin:0 auto;padding:28px 32px 20px}
  header{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap}
  .eyebrow{font-family:var(--mono);font-size:14px;letter-spacing:.12em;color:var(--acc);margin:0 0 6px}
  h1{margin:0;font-size:clamp(28px,3.4vw,46px);font-weight:650;line-height:1.1}
  h1 span{color:var(--teal);font-family:var(--mono);font-weight:500;font-size:.7em}
  .pill{display:inline-flex;align-items:center;gap:10px;padding:9px 16px;border-radius:999px;
        border:1px solid var(--line);background:var(--bg2);font-family:var(--mono);font-size:14px;color:var(--mut)}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--warn)}
  .pill.vivo{color:var(--txt);border-color:#2f7d56}
  .pill.vivo .dot{background:var(--ok);animation:pulso 1.2s infinite}
  @keyframes pulso{0%{box-shadow:0 0 0 0 rgba(95,208,138,.7)}100%{box-shadow:0 0 0 12px rgba(95,208,138,0)}}

  .controles{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:22px 0 18px}
  .btn-vivo{appearance:none;border:0;cursor:pointer;background:var(--acc);color:#fff;font:600 18px var(--sans);
        padding:14px 26px;border-radius:12px;box-shadow:0 6px 24px rgba(240,101,77,.35);transition:transform .12s, filter .12s}
  .btn-vivo:hover{transform:translateY(-1px);filter:brightness(1.08)}
  .btn-vivo:disabled{cursor:progress;filter:grayscale(.5) brightness(.8);transform:none;box-shadow:none}
  .fuentes{display:flex;gap:8px;flex-wrap:wrap}
  .fuente{appearance:none;cursor:pointer;text-align:left;color:var(--txt);background:var(--panel);border:1px solid var(--line);
        border-radius:10px;padding:8px 14px;font:500 15px var(--sans);transition:border-color .12s}
  .fuente small{display:block;font:12px var(--mono);color:var(--mut);margin-top:2px}
  .fuente:hover{border-color:var(--mut)}
  .fuente.on{border-color:var(--teal);box-shadow:inset 0 0 0 1px var(--teal)}
  .rotulo{font:13px var(--mono);color:var(--mut);margin-right:2px}

  .kpis{display:grid;grid-template-columns:1.5fr 1.2fr 1fr 1fr;gap:14px;margin-bottom:18px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px 22px;min-width:0}
  .kpi .t{font:13px var(--mono);color:var(--mut);letter-spacing:.06em;text-transform:uppercase}
  .kpi .v{font-size:clamp(28px,3.6vw,52px);font-weight:650;line-height:1.15;margin-top:6px;font-variant-numeric:tabular-nums}
  .kpi .v.chico{font-size:clamp(22px,2.4vw,34px)}
  .kpi .s{font-size:14px;color:var(--mut);margin-top:4px;min-height:20px}
  .kpi.destacado{border-color:var(--acc)}
  .tag-ok{display:inline-block;background:rgba(95,208,138,.14);color:var(--ok);border:1px solid rgba(95,208,138,.4);
        padding:2px 10px;border-radius:999px;font:600 13px var(--sans)}
  .tag-warn{display:inline-block;background:rgba(240,179,77,.14);color:var(--warn);border:1px solid rgba(240,179,77,.4);
        padding:2px 10px;border-radius:999px;font:600 13px var(--sans)}

  .cuerpo{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:18px;align-items:start}
  .cuerpo.sinlog{grid-template-columns:minmax(0,1fr)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px 22px}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
  .tab{appearance:none;cursor:pointer;border:1px solid var(--line);background:var(--bg2);color:var(--mut);
        border-radius:10px;padding:9px 16px;font:600 15px var(--sans);transition:all .12s}
  .tab kbd{font:12px var(--mono);opacity:.7;margin-right:8px;border:1px solid currentColor;border-radius:4px;padding:0 5px}
  .tab:hover{color:var(--txt)}
  .tab.on{color:#0E1726;background:var(--c);border-color:var(--c)}
  .tab.on kbd{opacity:.8}
  .titulo-vista{font-size:22px;font-weight:600;margin:0 0 14px}

  .row{display:grid;grid-template-columns:minmax(150px,32%) minmax(0,1fr) 84px 64px;gap:12px;align-items:center;padding:5px 0}
  .lab{font-size:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .track{height:22px;background:var(--bg2);border-radius:6px;overflow:hidden}
  .fill{height:100%;width:0;border-radius:6px;transition:width .8s cubic-bezier(.2,.8,.2,1)}
  .val{font:600 17px var(--mono);text-align:right;font-variant-numeric:tabular-nums}
  .pct{font:14px var(--mono);color:var(--mut);text-align:right}
  .vacio{color:var(--mut);padding:30px 0;text-align:center}
  .nota{font-size:13.5px;color:var(--mut);margin:14px 0 0;line-height:1.45;border-top:1px solid var(--line);padding-top:12px}

  .log-card{padding:16px 18px}
  .log-card h3{margin:0 0 10px;font:13px var(--mono);letter-spacing:.08em;color:var(--mut);text-transform:uppercase}
  .barra{height:8px;background:var(--bg2);border-radius:6px;overflow:hidden;margin-bottom:12px}
  .barra i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--teal));transition:width .4s}
  #log{font:13px/1.55 var(--mono);height:390px;overflow:auto;background:var(--bg2);border-radius:10px;padding:10px 12px;margin:0;
        list-style:none;border:1px solid var(--line)}
  #log li{margin-bottom:3px;word-break:break-word}
  #log .h{color:#6D7F99} #log .ok{color:var(--ok)} #log .err{color:#FF8A78} #log .info{color:var(--teal)}

  .aviso{display:none;margin:0 0 16px;padding:12px 16px;border-radius:12px;background:rgba(255,138,120,.12);
        border:1px solid rgba(255,138,120,.45);color:#FFC2B8;font-size:15px}
  .aviso.on{display:block}
  footer{margin-top:20px;padding-top:14px;border-top:1px solid var(--line);color:var(--mut);font-size:13.5px;line-height:1.5;
        display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}
  footer b{color:var(--txt);font-weight:600}
  .atajos{font-family:var(--mono)}

  @media (max-width:1100px){
    .cuerpo{grid-template-columns:minmax(0,1fr)}
    .kpis{grid-template-columns:1fr 1fr}
  }
  @media (max-width:640px){
    .wrap{padding:18px 16px}
    .row{grid-template-columns:minmax(110px,40%) minmax(0,1fr) 70px}
    .pct{display:none}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <p class="eyebrow">SHODAN · LABORATORIO PRÁCTICO IF-6000</p>
      <h1>Panorama agregado de Costa Rica <span id="consulta"></span></h1>
    </div>
    <div class="pill" id="modo"><i class="dot"></i><span id="modoTxt">Cargando…</span></div>
  </header>

  <div class="controles">
    <button class="btn-vivo" id="btnVivo" title="Atajo: L">▶ Consultar Shodan en vivo</button>
    <span class="rotulo">o usar datos guardados:</span>
    <div class="fuentes" id="fuentes"></div>
  </div>

  <div class="aviso" id="aviso"></div>

  <section class="kpis">
    <div class="kpi destacado"><div class="t">Servicios indexados</div><div class="v" id="kTotal">–</div>
      <div class="s" id="kTotalS">country:CR</div></div>
    <div class="kpi"><div class="t">Créditos de consulta</div><div class="v chico" id="kCred">–</div>
      <div class="s" id="kCredS"></div></div>
    <div class="kpi"><div class="t">Consultas de servicios</div><div class="v chico" id="kServ">–</div>
      <div class="s">puertos sensibles contados</div></div>
    <div class="kpi"><div class="t">Fecha de los datos</div><div class="v chico" id="kFecha">–</div>
      <div class="s" id="kFechaS"></div></div>
  </section>

  <div class="cuerpo sinlog" id="cuerpo">
    <section class="card">
      <div class="tabs" id="tabs" role="tablist"></div>
      <h2 class="titulo-vista" id="tituloVista"></h2>
      <div id="bars"></div>
      <p class="nota" id="nota"></p>
    </section>
    <aside class="card log-card" id="logCard" style="display:none">
      <h3>Consulta en vivo</h3>
      <div class="barra"><i id="prog"></i></div>
      <ul id="log"></ul>
    </aside>
  </div>

  <footer>
    <div><b>Consulta pasiva.</b> Solo <code>api.count()</code> con facets: totales y rankings agregados. No se obtienen IPs ni banners
      y no hay conexión con ningún dispositivo. Se cuentan servicios indexados, no equipos únicos ni vulnerables.</div>
    <div class="atajos">1–6 vistas · ←/→ cambiar · L consulta en vivo</div>
  </footer>
</div>

<script>
const CFG = __CONFIG__;
const $ = s => document.querySelector(s);
const fmt = n => (n == null ? '–' : Number(n).toLocaleString('en-US'));
const MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
const fechaBonita = iso => { if(!iso) return '–'; const [a,m,d] = iso.slice(0,10).split('-'); return `${+d} ${MESES[+m-1]} ${a}`; };
const hora = iso => (iso && iso.length >= 16) ? iso.slice(11,16) : '';
function h(tag, cls, texto){ const e = document.createElement(tag); if(cls) e.className = cls; if(texto != null) e.textContent = texto; return e; }

const etiquetarPuertos = pares => (pares || []).map(([v,c]) => { const n = CFG.puertos[String(v)]; return [n ? `${v} (${n})` : String(v), c]; });
const TABS = [
  {id:'port',     nombre:'Puertos',            titulo:'Puertos más expuestos',                 color:'#5AA9E6', nota:CFG.notaGeneral, datos:d => etiquetarPuertos(d.facets.port)},
  {id:'product',  nombre:'Productos',          titulo:'Productos más frecuentes',              color:'#4CC3B0', nota:CFG.notaGeneral, datos:d => d.facets.product || []},
  {id:'org',      nombre:'Campo org',          titulo:'Valores más frecuentes del campo org',  color:'#F0A04B', nota:CFG.notaOrg,     datos:d => d.facets.org || []},
  {id:'city',     nombre:'Ciudades',           titulo:'Ubicaciones con más servicios indexados',color:'#A98BE0', nota:CFG.notaCiudad,  datos:d => d.facets.city || []},
  {id:'os',       nombre:'SO',titulo:'Sistemas operativos detectados',        color:'#7FB77E', nota:CFG.notaGeneral, datos:d => d.facets.os || []},
  {id:'servicios',nombre:'Servicios',        titulo:'Servicios comunes expuestos',           color:'#F0654D', nota:CFG.notaGeneral, datos:d => Object.entries(d.servicios || {}).sort((a,b) => b[1]-a[1])},
];

const estado = {datos:null, fuente:null, vivo:false, corriendo:false, tab:0};

/* ---------- vistas ---------- */
function renderTabs(){
  const cont = $('#tabs'); cont.replaceChildren();
  TABS.forEach((t,i) => {
    const b = h('button', 'tab' + (i === estado.tab ? ' on' : ''));
    b.style.setProperty('--c', t.color); b.setAttribute('role','tab');
    b.append(h('kbd', null, String(i+1)), document.createTextNode(t.nombre));
    b.onclick = () => { estado.tab = i; renderTabs(); renderBars(true); };
    cont.append(b);
  });
}
function renderBars(animar){
  const tab = TABS[estado.tab], cont = $('#bars');
  $('#tituloVista').textContent = tab.titulo;
  $('#nota').textContent = tab.nota;
  cont.replaceChildren();
  const pares = estado.datos ? tab.datos(estado.datos) : [];
  if(!pares.length){
    cont.append(h('p','vacio', estado.corriendo ? 'Esperando datos de Shodan…' : 'Sin datos para esta vista.'));
    return;
  }
  const max = Math.max(...pares.map(p => p[1])), total = estado.datos.total, fills = [];
  pares.forEach(([lab, v], i) => {
    const row = h('div','row'), l = h('span','lab',lab), track = h('div','track'), fill = h('div','fill');
    l.title = lab;
    fill.style.background = tab.color;
    fill.style.width = animar ? '0%' : (v/max*100) + '%';
    fill.style.transitionDelay = animar ? (i*45) + 'ms' : '0ms';
    track.append(fill);
    row.append(l, track, h('span','val',fmt(v)), h('span','pct', total ? (v/total*100).toFixed(1) + ' %' : ''));
    cont.append(row); fills.push([fill, v/max*100]);
  });
  if(animar) requestAnimationFrame(() => requestAnimationFrame(() => fills.forEach(([f,w]) => f.style.width = w + '%')));
}
let animId = 0;
function animarNumero(el, hasta){
  const id = ++animId, t0 = performance.now(), dur = 900;
  if(hasta == null){ el.textContent = '–'; return; }
  (function paso(t){
    if(id !== animId) return;
    const p = Math.min(1, (t - t0)/dur), e = 1 - Math.pow(1-p, 3);
    el.textContent = fmt(Math.round(hasta*e));
    if(p < 1) requestAnimationFrame(paso);
  })(t0);
}
function renderModo(){
  const d = estado.datos, pill = $('#modo');
  pill.className = 'pill' + (estado.vivo ? ' vivo' : '');
  if(!d){ $('#modoTxt').textContent = 'Sin datos'; return; }
  $('#modoTxt').textContent = estado.vivo
    ? `EN VIVO · Shodan API · ${hora(d.fecha)}`
    : `OFFLINE · datos guardados · ${fechaBonita(d.fecha)}`;
}
function renderKpis(animarTotal){
  const d = estado.datos;
  if(!d){ return; }
  $('#consulta').textContent = d.consulta_base || CFG.pais;
  if(animarTotal) animarNumero($('#kTotal'), d.total); else $('#kTotal').textContent = fmt(d.total);
  $('#kTotalS').textContent = d.consulta_base || CFG.pais;
  const a = d.creditos_antes, b = d.creditos_despues;
  $('#kCred').textContent = (a == null ? '–' : a) + (b != null ? '  →  ' + b : '');
  const s = $('#kCredS'); s.replaceChildren();
  if(a != null && b != null){
    s.append(a === b ? h('span','tag-ok','0 créditos gastados') : h('span','tag-warn', `${a-b} crédito(s) gastado(s)`));
  } else if(a != null){ s.textContent = 'antes de la consulta'; }
  $('#kServ').textContent = `${Object.keys(d.servicios || {}).length} / ${CFG.totalServicios}`;
  $('#kFecha').textContent = fechaBonita(d.fecha);
  $('#kFechaS').textContent = hora(d.fecha) ? 'hora local ' + hora(d.fecha) : '';
}
function aviso(texto){ const a = $('#aviso'); a.textContent = texto || ''; a.classList.toggle('on', !!texto); }
function mostrar(datos, {vivo=false, animar=true} = {}){
  estado.datos = datos; estado.vivo = vivo;
  renderModo(); renderKpis(animar); renderTabs(); renderBars(animar);
}

/* ---------- datos guardados ---------- */
async function cargarLista(seleccionar){
  const r = await fetch('/api/datasets'); const lista = await r.json();
  const cont = $('#fuentes'); cont.replaceChildren();
  lista.forEach(x => {
    const b = h('button', 'fuente' + (x.id === estado.fuente ? ' on' : ''));
    b.append(document.createTextNode(`${fechaBonita(x.fecha)} · ${fmt(x.total)}`), h('small', null, x.id + '/'));
    b.onclick = () => cargarFuente(x.id);
    cont.append(b);
  });
  if(!lista.length) cont.append(h('span','rotulo','(no hay datos guardados todavía)'));
  if(seleccionar && lista.length){
    const ids = lista.map(x => x.id);
    await cargarFuente(ids.includes('salida') ? 'salida' : ids[0]);
  }
  return lista;
}
async function cargarFuente(id){
  if(estado.corriendo) return;
  try{
    aviso('');
    const r = await fetch('/api/datos?id=' + encodeURIComponent(id));
    if(!r.ok) throw new Error('no se pudo leer ' + id);
    estado.fuente = id;
    $('#logCard').style.display = 'none'; $('#cuerpo').classList.add('sinlog');
    mostrar(await r.json(), {vivo:false});
    document.querySelectorAll('.fuente').forEach(b => b.classList.toggle('on', b.querySelector('small').textContent === id + '/'));
  }catch(e){ aviso('Error al cargar datos guardados: ' + e.message); }
}

/* ---------- consulta en vivo ---------- */
function log(texto, cls){
  const ul = $('#log'), li = h('li', cls || 'h');
  li.textContent = `${new Date().toTimeString().slice(0,8)}  ${texto}`;
  ul.append(li); ul.scrollTop = ul.scrollHeight;
}
function vivo(){
  if(estado.corriendo) return;
  estado.corriendo = true; aviso('');
  const btn = $('#btnVivo'); btn.disabled = true; btn.textContent = '⏳ Consultando Shodan…';
  $('#log').replaceChildren(); $('#prog').style.width = '0%';
  $('#logCard').style.display = ''; $('#cuerpo').classList.remove('sinlog');

  const total = 2 + CFG.totalServicios + 1;      // info, base, servicios, info final
  let hechos = 0, terminado = false;
  const prog = () => $('#prog').style.width = Math.min(100, ++hechos/total*100) + '%';
  const nuevo = {fecha:new Date().toISOString().slice(0,19), consulta_base:CFG.pais,
                 total:null, facets:{}, servicios:{}, creditos_antes:null, creditos_despues:null};
  const es = new EventSource('/api/vivo');
  const cerrar = () => { terminado = true; es.close(); estado.corriendo = false;
                         btn.disabled = false; btn.textContent = '▶ Consultar Shodan en vivo'; };
  const fallo = msg => {
    cerrar(); log('ERROR: ' + msg, 'err');
    aviso('No se pudo consultar Shodan en vivo (' + msg + '). Usa los datos guardados: siguen disponibles sin red ni créditos.');
    estado.vivo = false; renderModo();
  };

  es.onmessage = ev => {
    const m = JSON.parse(ev.data);
    switch(m.tipo){
      case 'paso': log(m.texto, 'h'); prog(); break;
      case 'creditos_antes': nuevo.creditos_antes = m.valor; log(`créditos de consulta: ${m.valor}`, 'info'); break;
      case 'base':
        nuevo.total = m.total; nuevo.facets = m.facets; nuevo.fecha = m.fecha;
        log(`total ${CFG.pais}: ${fmt(m.total)}`, 'ok');
        mostrar(nuevo, {vivo:true, animar:true});
        break;
      case 'servicio':
        nuevo.servicios[m.etiqueta] = m.total; log(`${m.etiqueta}: ${fmt(m.total)}`, 'ok');
        if(estado.tab !== 5){ estado.tab = 5; renderTabs(); renderBars(true); } else { renderBars(false); }
        renderKpis(false);
        break;
      case 'servicio_error': log(`${m.etiqueta}: error (${m.mensaje})`, 'err'); break;
      case 'fin':
        terminado = true; prog();
        Object.assign(nuevo, m.datos);
        log(`créditos después: ${nuevo.creditos_despues}`, 'info');
        log(`guardado en ${m.guardado}`, 'ok');
        mostrar(nuevo, {vivo:true, animar:false});
        cerrar();
        cargarLista(false).then(() => { estado.fuente = 'salida_vivo';
          document.querySelectorAll('.fuente').forEach(b => b.classList.toggle('on', b.querySelector('small').textContent === 'salida_vivo/')); });
        break;
      case 'error': fallo(m.mensaje); break;
    }
  };
  es.onerror = () => { if(!terminado) fallo('se interrumpió la conexión con el servidor local'); };
}

/* ---------- eventos ---------- */
$('#btnVivo').onclick = vivo;
document.addEventListener('keydown', e => {
  if(e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if(k === 'l'){ vivo(); return; }
  let i = estado.tab;
  if(/^[1-6]$/.test(k)) i = +k - 1;
  else if(k === 'arrowright') i = (estado.tab + 1) % TABS.length;
  else if(k === 'arrowleft') i = (estado.tab + TABS.length - 1) % TABS.length;
  else return;
  estado.tab = i; renderTabs(); renderBars(true);
});

renderTabs(); renderBars(false);
cargarLista(true).catch(e => aviso('No se pudo contactar el servidor local: ' + e.message));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
