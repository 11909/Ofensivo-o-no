"""
diccionarios_web.py
Descarga páginas web de "diccionarios de groserías mexicanas", extrae de ellas
  1) términos (groserías / modismos) con su definición  -> candidatos para lexico_mx.csv
  2) frases de ejemplo de uso                           -> filas para el dataset
y propone una categoría / etiqueta para cada cosa.

Subcomandos:
  descargar  Baja los .html de una lista de URLs (respeta robots.txt, espera entre
             peticiones al mismo sitio y guarda una caché: no vuelve a bajar lo que ya tiene).
  extraer    Lee TODOS los .html de la carpeta (también los que guardes a mano desde el
             navegador con "Guardar como…") y genera los CSV de salida.
  todo       descargar + extraer.

Ejemplos:
  python diccionarios_web.py descargar --urls fuentes_groserias.txt
  python diccionarios_web.py extraer --lexico lexico_mx.csv
  python diccionarios_web.py extraer --lexico lexico_mx.csv --fusionar --min-fuentes 2
  python diccionarios_web.py todo --urls fuentes_groserias.txt --lexico lexico_mx.csv

Cómo se extrae (no hay un formato único, así que se combinan estrategias):
  - <dl><dt>término</dt><dd>definición</dd>
  - tablas: 1a columna término, 2a definición, 3a (opcional) ejemplo
  - encabezados (h2-h6 o párrafos todo en negritas) tipo "4. Pinche" o "¿Qué significa
    pendejo?", seguidos de párrafos / viñetas hasta el siguiente encabezado
  - viñetas o párrafos "**Término**: definición" / "Término – definición"
  Dentro de cada entrada, se toma como EJEMPLO: lo que va después de "Ejemplo:", "Ej.:",
  "Por ejemplo", "Uso:"; texto en cursivas o <blockquote>; y fragmentos entre comillas
  ("…", “…”, «…») que contienen el término.

Etiquetado:
  - Categoría del término (ofensivo | contexto | amistoso | neutro):
      a) si ya está en lexico_mx.csv, se usa la de ahí;
      b) si no, reglas con palabras clave sobre la definición ("insulto", "despectivo",
         "saludo", "depende del contexto", "vulgar"…);
      c) lo que las reglas no deciden lo predice una red neuronal pequeña (MLP sobre
         n-gramas de caracteres) entrenada con (a) y (b). Si la red duda -> "revisar".
    "neutro" = modismo mexicano que NO es grosería (p. ej. "¿bueno?" al contestar el
    teléfono). No entra al léxico, pero sus ejemplos sirven como clase 0.
  - Etiqueta de cada frase: término ofensivo -> 1; amistoso/neutro -> 0; de contexto ->
    se decide con el prefiltro del léxico y patrones de insulto directo ("eres un…",
    "vete a…"), y si no queda claro se deja vacía con sugerencia "revisar".
  Todo es una PROPUESTA: revisa las columnas `confianza` y `sugerencia` antes de entrenar.

Salidas (todas en .gitignore salvo el léxico):
  lexico_candidatos.csv   termino, categoria, confianza, metodo, n_fuentes, fuentes,
                          ya_en_lexico, definicion
  frases_diccionarios.csv id, texto, fuente, termino, categoria_termino, sugerencia,
                          terminos_lexico, etiqueta   (compatible con construir_dataset.py)
  --fusionar agrega a lexico_mx.csv (con respaldo .bak) los candidatos nuevos que pasen
  --min-fuentes y --min-confianza. La nota solo dice "web (n fuentes)": no se copian
  definiciones de terceros al repo.

Requisitos: pip install requests beautifulsoup4 scikit-learn
Usa la descarga con moderación: es para un proyecto escolar, no para replicar los sitios.
No publiques el HTML ni las definiciones; para entrenar solo necesitas texto + etiqueta.
"""
import argparse
import csv
import hashlib
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from urllib import robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, NavigableString

# Reutilizamos el normalizador y el prefiltro del recolector de YouTube para que
# ambos recolectores etiqueten con exactamente las mismas reglas.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from youtube_comentarios import cargar_lexico, normalizar, prefiltrar  # noqa: E402

AGENTE = "Ofensivo-o-no/1.0 (proyecto escolar ESCOM-IPN; descarga ocasional)"
CATEGORIAS_LEXICO = ("ofensivo", "contexto", "amistoso")


# ==========================================================================
# 1. DESCARGA
# ==========================================================================
class Descargador:
    def __init__(self, carpeta, espera, forzar=False, ignorar_robots=False):
        self.carpeta = carpeta
        self.espera = espera
        self.forzar = forzar
        self.ignorar_robots = ignorar_robots
        self.sesion = requests.Session()
        self.sesion.headers.update({"User-Agent": AGENTE, "Accept-Language": "es-MX,es;q=0.9"})
        self.robots = {}           # dominio -> RobotFileParser | "permitir" | "bloquear"
        self.ultima = {}           # dominio -> time.time() de la última petición
        os.makedirs(carpeta, exist_ok=True)
        self.indice_ruta = os.path.join(carpeta, "indice.csv")
        self.indice = leer_indice(self.indice_ruta)

    # -- robots.txt -------------------------------------------------------
    def _robots(self, url):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.robots:
            try:
                r = self.sesion.get(base + "/robots.txt", timeout=20)
                if r.status_code == 200:
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
                    self.robots[base] = rp
                elif 400 <= r.status_code < 500:
                    self.robots[base] = "permitir"     # sin robots.txt = sin restricciones
                else:
                    self.robots[base] = "bloquear"     # error del servidor: mejor no insistir
            except requests.RequestException:
                self.robots[base] = "bloquear"
        return self.robots[base]

    def permitido(self, url):
        if self.ignorar_robots:
            return True, 0
        rp = self._robots(url)
        if rp == "permitir":
            return True, 0
        if rp == "bloquear":
            return False, 0
        return rp.can_fetch(AGENTE, url), (rp.crawl_delay(AGENTE) or 0)

    # -- descarga ---------------------------------------------------------
    def bajar(self, url):
        archivo = nombre_archivo(url)
        ruta = os.path.join(self.carpeta, archivo)
        if os.path.exists(ruta) and not self.forzar:
            return "cache", archivo
        ok, retraso = self.permitido(url)
        if not ok:
            return "robots", archivo
        dominio = urlparse(url).netloc
        espera = max(self.espera, retraso) - (time.time() - self.ultima.get(dominio, 0))
        if espera > 0:
            time.sleep(espera)
        estado = "error"
        for intento in range(3):
            try:
                r = self.sesion.get(url, timeout=30)
                self.ultima[dominio] = time.time()
            except requests.RequestException as e:
                estado = f"error:{type(e).__name__}"
                time.sleep(2 ** intento)
                continue
            if r.status_code == 200 and "html" in r.headers.get("Content-Type", "html"):
                with open(ruta, "wb") as f:
                    f.write(r.content)
                estado = "ok"
                break
            estado = f"http_{r.status_code}"
            if r.status_code in (429, 500, 502, 503):
                time.sleep(5 * (intento + 1))
                continue
            break
        self.indice[archivo] = {"url": url, "archivo": archivo, "estado": estado,
                                "fecha": datetime.now().isoformat(timespec="seconds")}
        return estado, archivo

    def guardar_indice(self):
        with open(self.indice_ruta, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["url", "archivo", "estado", "fecha"])
            w.writeheader()
            w.writerows(self.indice.values())


def nombre_archivo(url):
    dominio = re.sub(r"[^\w.-]", "_", urlparse(url).netloc)
    return f"{dominio}__{hashlib.sha1(url.encode()).hexdigest()[:10]}.html"


def leer_indice(ruta):
    if not os.path.exists(ruta):
        return {}
    with open(ruta, encoding="utf-8") as f:
        return {fila["archivo"]: fila for fila in csv.DictReader(f)}


def leer_urls(ruta):
    with open(ruta, encoding="utf-8") as f:
        urls = [l.split("#")[0].strip() for l in f]
    return [u for u in urls if u.startswith("http")]


def cmd_descargar(a):
    d = Descargador(a.dir, a.espera, a.forzar, a.ignorar_robots)
    resumen = defaultdict(int)
    for url in leer_urls(a.urls):
        estado, archivo = d.bajar(url)
        resumen[estado.split(":")[0]] += 1
        print(f"[{estado:>10}] {url}")
    d.guardar_indice()
    print(f"\nDescarga: {dict(resumen)}  -> {a.dir}/")
    if resumen.get("robots"):
        print("  'robots' = el sitio pide no ser descargado por bots. Si aun así quieres usarlo,\n"
              "  ábrelo en tu navegador, 'Guardar como…' (solo HTML) dentro de la carpeta y\n"
              "  vuelve a correr 'extraer'.")


# ==========================================================================
# 2. EXTRACCIÓN
# ==========================================================================
ETIQUETAS_EJEMPLO = re.compile(
    r"^\s*(?:(?:ejemplos?(?: de uso)?|ej\.|frases?(?: de ejemplo)?|uso|modo de uso|"
    r"en una frase|se usa así)\s*[:\-–—]|por ejemplo\s*[:,]?)\s*", re.I)
ETIQUETAS_CAMPO = re.compile(
    r"^\s*(significado(?: actual| original)?|definici[oó]n|origen|etimolog[ií]a|evoluci[oó]n|"
    r"sin[oó]nimos?|variantes?|categor[ií]a|nivel|tipo|regi[oó]n|traducci[oó]n|"
    r"ejemplos?(?: de uso)?|ej\.?|uso|frases?)\s*$", re.I)

# Palabras que delatan que un encabezado NO es una entrada de diccionario
PALABRAS_BASURA = {
    "groserias", "groseria", "palabras", "palabra", "expresiones", "mexicanismos", "insultos",
    "lista", "indice", "contenido", "contenidos", "introduccion", "conclusion", "conclusiones",
    "referencias", "bibliografia", "fuentes", "fuente", "comentarios", "comentario", "compartir",
    "comparte", "publicidad", "suscribete", "newsletter", "relacionado", "relacionados",
    "relacionadas", "interesar", "tambien", "leer", "lee", "articulo", "articulos", "notas",
    "nota", "autor", "menu", "buscar", "inicio", "categorias", "etiquetas", "tags", "video",
    "videos", "galeria", "siguenos", "redes", "sociales", "cookies", "privacidad", "aviso",
    "contacto", "mexico", "mexicanas", "mexicanos", "mexicano", "mexicana", "ejemplos",
    "significado", "significados", "origen", "historia", "tabla", "resumen", "preguntas",
    "frecuentes", "faq", "glosario", "diccionario", "vocabulario", "mas", "populares",
    "comunes", "top", "mejores",
}

RE_NUMERACION = re.compile(r"^\s*(?:\d{1,3}|[ivxlc]{1,6})\s*[.)\-–:]\s*", re.I)
RE_QUE_SIGNIFICA = re.compile(
    r"^\s*¿?\s*(?:qu[eé]\s+(?:significa|quiere\s+decir|es)|significado\s+de)\s+"
    r"(?:la\s+palabra\s+|la\s+expresi[oó]n\s+|el\s+t[eé]rmino\s+|decir\s+)?(.+?)\s*\??\s*$", re.I)
RE_INLINE = re.compile(r"^\s*(?:\d{1,3}[.)\-]\s*)?(?P<t>[^:–—=]{1,45}?)\s*[:–—=]\s+(?P<d>.{8,})$",
                       re.S)
RE_GUION_INLINE = re.compile(r"^\s*(?:\d{1,3}[.)]\s*)?(?P<t>[^\-]{1,40}?)\s+-\s+(?P<d>.{8,})$", re.S)
RE_EJEMPLO_MEDIO = re.compile(r"[.;,]?\s+(?:ejemplos?|ej\.|por ejemplo)\s*:\s*", re.I)
RE_COMILLAS = re.compile(r"[\"“”«](?P<f>[^\"“”«»]{4,250})[\"“”»]")
COMILLAS_EXTREMOS = "\"'“”«»‘’ "

QUITAR_TAGS = ["script", "style", "noscript", "iframe", "svg", "form", "nav", "header",
               "footer", "aside", "button", "select", "input", "figure", "figcaption"]
RE_CLASE_BASURA = re.compile(
    r"(comment|coment|menu|sidebar|related|relacionad|share|compart|newsletter|cookie|"
    r"breadcrumb|footer|header|advert|publicidad|banner|social|promo|recomend|widget|"
    r"author-box|tags|paginat)", re.I)

BLOQUES = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre"]


def texto_de(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def limpiar_dom(soup):
    for t in soup(QUITAR_TAGS):
        t.decompose()
    for el in soup.find_all(True):
        if el.attrs is None:          # ya eliminado junto con un ancestro
            continue
        attrs = " ".join(el.get("class", []) or []) + " " + (el.get("id") or "")
        if attrs.strip() and RE_CLASE_BASURA.search(attrs) and el.name not in ("body", "html", "main", "article"):
            el.decompose()


def contenedor_principal(soup):
    for sel in ("article", "main", '[role="main"]'):
        el = soup.select_one(sel)
        if el and len(texto_de(el)) > 500:
            return el
    # el elemento con más texto en párrafos/viñetas directos
    mejor, puntos = soup.body or soup, 0
    for el in soup.find_all(["div", "section"]):
        p = sum(len(texto_de(x)) for x in el.find_all(["p", "li", "dd", "td"], recursive=False))
        if p > puntos:
            mejor, puntos = el, p
    return mejor if puntos > 500 else (soup.body or soup)


# -- limpieza de términos ---------------------------------------------------
def limpiar_termino(t):
    t = RE_NUMERACION.sub("", t)
    m = RE_QUE_SIGNIFICA.match(t)
    if m:
        t = m.group(1)
    t = t.strip(COMILLAS_EXTREMOS + "¿?¡!.,;:*•·-–—()[]")
    return re.sub(r"\s+", " ", t).lower()


def variantes(t):
    """'pendejo/a' -> pendejo, pendeja ; 'güey (wey)' -> güey, wey ; 'chido, chida' -> ambos."""
    t = RE_NUMERACION.sub("", t)
    m = RE_QUE_SIGNIFICA.match(t)
    t = re.sub(r"\s+", " ", (m.group(1) if m else t)).strip(COMILLAS_EXTREMOS + "¿?¡!.,;:*•·-–—").lower()
    salida = []
    m = re.match(r"^(.*\w)o/a\b(.*)$", t)
    if m:
        return [m.group(1) + "o" + m.group(2), m.group(1) + "a" + m.group(2)]
    alternativas = re.findall(r"\(([^)]+)\)", t)
    base = re.sub(r"\s*\([^)]*\)", "", t).strip()
    partes = re.split(r"\s*/\s*|\s*,\s*", base)
    if len(partes) == 1 and re.fullmatch(r"\w+ o \w+", base):
        partes = base.split(" o ")
    salida += partes
    for alt in alternativas:
        if not re.search(r"\b(tambi[eé]n|o|plural|m[eé]x|vulg|coloq)\b", alt):
            salida += re.split(r"\s*/\s*|\s*,\s*", alt)
    return [limpiar_termino(s) for s in salida if s.strip()]


def termino_valido(t):
    if not t or not (2 <= len(t) <= 40) or re.search(r"\d|https?:|www\.|@", t):
        return False
    palabras = normalizar(t).split()
    if not (1 <= len(palabras) <= 5) or not re.search(r"[a-zñ]", normalizar(t)):
        return False
    return not any(p in PALABRAS_BASURA for p in palabras)


# -- ejemplos ---------------------------------------------------------------
def limpiar_frase(f):
    f = ETIQUETAS_EJEMPLO.sub("", f).strip(COMILLAS_EXTREMOS + "-–—•*")
    f = re.sub(r"\s+", " ", f)
    if not (2 <= len(f.split()) <= 60) or len(f) > 350:
        return None
    return f


def frases_de(texto):
    """Un bloque marcado como ejemplo puede traer varias frases entre comillas."""
    entre = [m.group("f") for m in RE_COMILLAS.finditer(texto)]
    candidatas = entre if entre else re.split(r"\s+/\s+|\s*\|\s*", ETIQUETAS_EJEMPLO.sub("", texto))
    return [f for f in (limpiar_frase(c) for c in candidatas) if f]


def contiene(termino, texto):
    t = normalizar(termino)
    return bool(re.search(r"(?<!\w)" + re.escape(t) + r"\w{0,3}(?!\w)", normalizar(texto)))


# -- entradas ---------------------------------------------------------------
class Entrada:
    def __init__(self, termino, metodo):
        self.termino = termino
        self.metodo = metodo
        self.definicion = []
        self.ejemplos = []

    def agregar_texto(self, texto, es_ejemplo=False):
        if not texto:
            return
        if es_ejemplo or ETIQUETAS_EJEMPLO.match(texto) and len(texto) > 12:
            self.ejemplos += frases_de(texto)
            return
        partes = RE_EJEMPLO_MEDIO.split(texto, maxsplit=1)
        if len(partes) == 2 and partes[0].strip():
            texto = partes[0].strip()
            self.ejemplos += frases_de(partes[1])
        m = RE_INLINE.match(texto)
        if m and ETIQUETAS_CAMPO.match(m.group("t")):
            campo = m.group("t").lower()
            if campo.startswith(("ejemplo", "ej", "uso", "frase")):
                self.ejemplos += frases_de(m.group("d"))
                return
            texto = m.group("d")
        self.definicion.append(texto)
        # frases entre comillas que contienen el término dentro de la definición
        for mm in RE_COMILLAS.finditer(texto):
            f = limpiar_frase(mm.group("f"))
            if f and len(f.split()) >= 2 and any(contiene(v, f) for v in variantes(self.termino)):
                self.ejemplos.append(f)


def es_cursiva(el):
    if el.name == "blockquote" or el.find_parent("blockquote"):
        return True
    total = texto_de(el)
    cursiva = " ".join(texto_de(x) for x in el.find_all(["em", "i"]))
    return bool(total) and len(cursiva) >= 0.8 * len(total)


def negrita_inicial(el):
    """Si el bloque empieza con <strong>/<b>, devuelve ese texto."""
    for hijo in el.children:
        if isinstance(hijo, NavigableString):
            if hijo.strip():
                return None
            continue
        if hijo.name in ("strong", "b") or (hijo.name in ("span", "a") and hijo.find(["strong", "b"])):
            return texto_de(hijo)
        return None
    return None


def extraer_dl(main):
    entradas = []
    for dl in main.find_all("dl"):
        actual = None
        for el in dl.find_all(["dt", "dd"]):
            if el.name == "dt":
                actual = Entrada(texto_de(el), "dl")
                entradas.append(actual)
            elif actual:
                actual.agregar_texto(texto_de(el), es_cursiva(el))
        dl.decompose()
    return entradas


def extraer_tablas(main):
    entradas = []
    for tabla in main.find_all("table"):
        for tr in tabla.find_all("tr"):
            celdas = tr.find_all(["td", "th"])
            if len(celdas) < 2 or tr.find("th") and not tr.find("td"):
                continue
            e = Entrada(texto_de(celdas[0]), "tabla")
            e.agregar_texto(texto_de(celdas[1]))
            for c in celdas[2:]:
                e.agregar_texto(texto_de(c), es_ejemplo=True)
            entradas.append(e)
        tabla.decompose()
    return entradas


def extraer_secciones(main):
    """Recorre los bloques en orden y arma secciones encabezado -> contenido."""
    entradas, actual, nivel_actual, modo_ejemplo = [], None, 9, False
    elegidos = [el for el in main.find_all(BLOQUES)
                if not el.find_parent(["li", "blockquote", "p"])]
    for el in elegidos:
        txt = texto_de(el)
        if not txt:
            continue
        nivel = int(el.name[1]) if re.fullmatch(r"h[1-6]", el.name) else None
        negrita = negrita_inicial(el)
        solo_negrita = nivel is None and negrita and len(negrita) >= len(txt) - 2 and len(txt) <= 60
        if nivel == 1:
            continue
        if nivel or solo_negrita:
            nivel = nivel or 6
            limpio = RE_NUMERACION.sub("", txt).strip(" :.")
            if ETIQUETAS_EJEMPLO.fullmatch(limpio + ":") or re.match(r"^ejemplos?\b", limpio, re.I):
                modo_ejemplo = True
                continue
            if ETIQUETAS_CAMPO.match(limpio):
                modo_ejemplo = False
                continue
            terms = variantes(txt)
            if terms and all(termino_valido(t) for t in terms):
                actual = Entrada(txt, "encabezado")
                entradas.append(actual)
                nivel_actual, modo_ejemplo = nivel, False
            elif nivel <= nivel_actual:
                actual, nivel_actual = None, 9     # encabezado genérico: cierra sección
            continue
        # bloque de texto: ¿es una entrada en línea "Término: definición"?
        m = RE_INLINE.match(txt) or (RE_GUION_INLINE.match(txt) if negrita else None)
        if m and not ETIQUETAS_CAMPO.match(m.group("t")) and not ETIQUETAS_EJEMPLO.match(txt):
            cand = m.group("t")
            if (negrita and normalizar(negrita).strip(" :–-") == normalizar(cand).strip()) or \
               (el.name == "li" and termino_valido(limpiar_termino(cand))):
                if all(termino_valido(t) for t in variantes(cand)):
                    e = Entrada(cand, "en_linea")
                    e.agregar_texto(m.group("d"))
                    entradas.append(e)
                    continue
        if actual:
            actual.agregar_texto(txt, modo_ejemplo or es_cursiva(el))
        elif ETIQUETAS_EJEMPLO.match(txt):
            huerfana = Entrada("", "huerfano")      # se asigna después al término que contenga
            huerfana.ejemplos += frases_de(txt)
            entradas.append(huerfana)
    return entradas


def extraer_html(html):
    soup = BeautifulSoup(html, "lxml") if _tiene_lxml() else BeautifulSoup(html, "html.parser")
    canonica = soup.find("link", rel="canonical")
    og = soup.find("meta", property="og:url")
    url = (canonica and canonica.get("href")) or (og and og.get("content")) or ""
    limpiar_dom(soup)
    main = contenedor_principal(soup)
    entradas = extraer_dl(main) + extraer_tablas(main) + extraer_secciones(main)
    return url, entradas


def _tiene_lxml():
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


# ==========================================================================
# 3. ETIQUETADO: reglas + red neuronal pequeña
# ==========================================================================
REGLAS = {
    "ofensivo": [
        r"insult", r"ofens", r"ofend", r"despectiv", r"peyorativ", r"denigr", r"humill",
        r"menospreci", r"desprecio", r"agresiv", r"degrad", r"discrimin", r"homofob",
        r"racist", r"clasist", r"machist", r"misogin", r"mentada", r"mentar la madre",
        r"tont[oa]", r"estupid", r"idiota", r"imbecil", r"poco inteligente", r"cobarde",
        r"traicioner", r"mala persona", r"persona (?:despreciable|mala|vil|abusiva)",
        r"para (?:agredir|atacar|ofender|molestar a)", r"llamar a alguien",
        r"a la (?:madre|verga|chingada)", r"deshonr",
    ],
    "amistoso": [
        r"amistos", r"carin", r"afectuos", r"entre amigos", r"de confianza", r"saludo",
        r"sorpresa", r"asombro", r"incredulidad", r"admiracion", r"elogi", r"excelente",
        r"muy buen[oa]", r"genial", r"increible", r"padr[ei]simo", r"afirmacion", r"de acuerdo",
        r"muletilla", r"compa(?:ner|dre)", r"amigo", r"broma", r"positiv", r"alegria",
    ],
    "contexto": [
        r"depende del contexto", r"segun el (?:contexto|tono)", r"puede ser (?:un )?(?:insulto|ofensiv)",
        r"tanto .{0,40} como", r"tambien se (?:usa|utiliza) (?:para|como)", r"intensificador",
        r"(?:positiv|negativ)[oa] (?:o|y) (?:positiv|negativ)", r"dependiendo",
        r"ambos sentidos", r"doble sentido", r"varios (?:significados|usos)",
    ],
}
VULGAR = [
    r"vulgar", r"groser", r"malsonante", r"soez", r"obscen", r"altisonante", r"mala palabra",
    r"tabu", r"sexual", r"genital", r"pene", r"testicul", r"nalga", r"\bano\b", r"\bculo\b",
    r"prostitu", r"mierda", r"coit", r"fornic", r"homosexual", r"vagina",
]
RE_NEGACION = re.compile(r"\b(?:sin ser|no es|no se considera|nada|sin)\s+(?:una?\s+)?"
                         r"(?:groseri\w*|vulgar\w*|ofensiv\w*|insult\w*|despectiv\w*)")
# Raíces malsonantes: si el término las contiene, se trata como grosería aunque la
# definición no lo diga ("chingón: algo excelente").
RE_RAIZ_VULGAR = re.compile(
    r"ching|chingad|\bmam[aoe]|pend[eo]j|verg|\bcul[eo]|put[oa]|cabr[oó]n|madre|pedo|huev|"
    r"cag[aoe]|mierd|\bjoto|ojet|\bverga|\bcog[ei]|nalg|\bpito|panoch|chich")
_REGLAS = {c: [re.compile(p) for p in ps] for c, ps in REGLAS.items()}
_VULGAR = [re.compile(p) for p in VULGAR]


def categoria_por_reglas(termino, definicion):
    """Devuelve (categoria | None, confianza). None = las reglas no deciden."""
    t = RE_NEGACION.sub(" ", normalizar(f"{termino}. {definicion}"))
    raiz_vulgar = bool(RE_RAIZ_VULGAR.search(normalizar(termino)))
    votos = {c: sum(1 for p in ps if p.search(t)) for c, ps in _REGLAS.items()}
    vulgar = sum(1 for p in _VULGAR if p.search(t)) + raiz_vulgar
    if votos["contexto"] and (votos["ofensivo"] or votos["amistoso"]):
        return "contexto", 0.75
    if votos["ofensivo"] and votos["amistoso"]:
        return "contexto", 0.65         # se describe de las dos formas
    if votos["ofensivo"]:
        return "ofensivo", min(0.95, 0.6 + 0.1 * votos["ofensivo"])
    if votos["amistoso"]:
        if not vulgar:                   # modismo sin grosería (padre, neta, cuate…)
            return "neutro", min(0.85, 0.55 + 0.1 * votos["amistoso"])
        return "amistoso", min(0.9, 0.55 + 0.1 * votos["amistoso"])
    if votos["contexto"]:
        return "contexto", 0.6
    if vulgar:
        return "contexto", 0.55         # vulgar pero sin intención de insulto descrita
    if len(t) > 25:
        return "neutro", 0.55        # definición larga sin rastro de grosería
    return None, 0.0


def red_neuronal(textos, etiquetas, a_predecir, semilla=42):
    """
    MLP pequeño (1 capa oculta de 64) sobre TF-IDF de n-gramas de caracteres.
    Se entrena con lo que ya está etiquetado (léxico + reglas) y predice el resto.
    Devuelve (predicciones, probabilidades) o None si no hay datos suficientes.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.neural_network import MLPClassifier
    except ImportError:
        print("  (scikit-learn no está instalado: se omite la red neuronal)", file=sys.stderr)
        return None
    clases = set(etiquetas)
    if len(textos) < 20 or len(clases) < 2 or not a_predecir:
        return None
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=1,
                          sublinear_tf=True, preprocessor=normalizar, max_features=30000)
    X = vec.fit_transform(textos)
    red = MLPClassifier(hidden_layer_sizes=(64,), alpha=1e-3, max_iter=400,
                        early_stopping=len(textos) >= 60, random_state=semilla)
    red.fit(X, etiquetas)
    P = red.predict_proba(vec.transform(a_predecir))
    return [red.classes_[i] for i in P.argmax(1)], P.max(1)


# -- etiqueta de frases -----------------------------------------------------
RE_INSULTO_DIRECTO = re.compile(
    r"\b(eres (?:un|una|bien|muy)|son (?:unos|unas|bien)|no seas|pinche \w+ de|vete a|"
    r"chinga(?:te| tu)|tu (?:puta )?madre|callate|lárgate|largate|que asco de)\b")


def etiquetar_frase(frase, cat_termino, lexico):
    sug, terms = prefiltrar(frase, lexico)
    directo = bool(RE_INSULTO_DIRECTO.search(normalizar(frase)))
    if cat_termino == "ofensivo":
        return "candidato_ofensivo", terms, "1"
    if cat_termino in ("amistoso", "neutro"):
        if sug == "candidato_ofensivo" or directo:
            return "revisar", terms, ""              # trae otra grosería ofensiva
        return "candidato_no_ofensivo", terms, "0"
    # contexto o sin decidir
    if sug == "candidato_ofensivo" or directo:
        return "candidato_ofensivo", terms, "1" if directo and sug == "candidato_ofensivo" else ""
    return "revisar", terms, ""


# ==========================================================================
# 4. COMANDO extraer
# ==========================================================================
def cmd_extraer(a):
    lexico = cargar_lexico(a.lexico) if a.lexico else []
    en_lexico = {term: cat for term, cat, _ in lexico}
    indice = leer_indice(os.path.join(a.dir, "indice.csv"))

    archivos = sorted(f for f in os.listdir(a.dir) if f.lower().endswith((".html", ".htm")))
    if not archivos:
        sys.exit(f"No hay .html en {a.dir}/ (corre primero 'descargar' o guarda páginas ahí)")

    # termino_normalizado -> datos agregados de todas las páginas
    agregado = defaultdict(lambda: {"forma": None, "defs": [], "ejemplos": {}, "fuentes": set()})
    huerfanos = []
    for archivo in archivos:
        with open(os.path.join(a.dir, archivo), "rb") as f:
            url_html, entradas = extraer_html(f.read())
        url = indice.get(archivo, {}).get("url") or url_html or archivo
        fuente = urlparse(url).netloc or archivo
        n_ej = 0
        for e in entradas:
            if e.metodo == "huerfano":
                huerfanos += [(ej, fuente) for ej in e.ejemplos]
                n_ej += len(e.ejemplos)
                continue
            for t in variantes(e.termino):
                if not termino_valido(t):
                    continue
                d = agregado[normalizar(t)]
                d["forma"] = d["forma"] or t
                d["defs"] += e.definicion
                d["fuentes"].add(fuente)
                for ej in e.ejemplos:
                    d["ejemplos"].setdefault(normalizar(ej), (ej, fuente))
                    n_ej += 1
        print(f"[extraer] {archivo}: {len(entradas)} entradas, {n_ej} ejemplos  ({fuente})")

    # ejemplos sueltos -> al término más largo que aparezca en la frase
    por_largo = sorted(agregado, key=len, reverse=True)
    for ej, fuente in huerfanos:
        dueño = next((k for k in por_largo if contiene(k, ej)), None)
        if dueño:
            agregado[dueño]["ejemplos"].setdefault(normalizar(ej), (ej, fuente))

    # quita entradas sin definición ni ejemplos (suelen ser encabezados sueltos)
    agregado = {k: v for k, v in agregado.items() if v["defs"] or v["ejemplos"]}

    # --- categoría: léxico > reglas > red ---------------------------------
    filas = []
    for clave, d in agregado.items():
        definicion = " ".join(d["defs"])
        texto_red = f"{d['forma']} | {definicion} | " + " ".join(e for e, _ in d["ejemplos"].values())
        if clave in en_lexico:
            cat, conf, metodo = en_lexico[clave], 1.0, "lexico"
        else:
            cat, conf = categoria_por_reglas(d["forma"], definicion)
            metodo = "reglas" if cat else "pendiente"
        filas.append({"clave": clave, "termino": d["forma"], "categoria": cat, "confianza": round(conf, 3),
                      "metodo": metodo, "n_fuentes": len(d["fuentes"]),
                      "fuentes": "|".join(sorted(d["fuentes"])),
                      "ya_en_lexico": "si" if clave in en_lexico else "no",
                      "definicion": definicion[:300], "_texto": texto_red})

    if not a.sin_red:
        entren = [f for f in filas if f["categoria"]]
        pend = [f for f in filas if not f["categoria"]]
        res = red_neuronal([f["_texto"] for f in entren], [f["categoria"] for f in entren],
                           [f["_texto"] for f in pend])
        if res:
            for f, cat, p in zip(pend, *res):
                f["categoria"], f["confianza"], f["metodo"] = str(cat), round(float(p), 3), "red"
            print(f"[red] entrenada con {len(entren)} términos; predijo {len(pend)}")
    for f in filas:
        if not f["categoria"] or f["confianza"] < a.umbral_red and f["metodo"] == "red":
            f["metodo"] = f"revisar({f['categoria'] or '?'})"
            f["categoria"] = "revisar"

    # --- candidatos al léxico ---------------------------------------------
    filas.sort(key=lambda f: (f["ya_en_lexico"] == "si", f["categoria"], -f["n_fuentes"], f["termino"]))
    campos = ["termino", "categoria", "confianza", "metodo", "n_fuentes", "fuentes",
              "ya_en_lexico", "definicion"]
    with open(a.candidatos, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)

    # --- frases para el dataset -------------------------------------------
    cat_de = {f["clave"]: f["categoria"] for f in filas}
    vistas, frases = set(), []
    for clave, d in agregado.items():
        for norm, (ej, fuente) in d["ejemplos"].items():
            if norm in vistas:
                continue
            vistas.add(norm)
            sug, terms, etiqueta = etiquetar_frase(ej, cat_de[clave], lexico)
            frases.append({"id": hashlib.sha1(norm.encode()).hexdigest()[:12], "texto": ej,
                           "fuente": fuente, "termino": d["forma"],
                           "categoria_termino": cat_de[clave], "sugerencia": sug,
                           "terminos_lexico": terms, "etiqueta": etiqueta})
    with open(a.frases, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "texto", "fuente", "termino", "categoria_termino",
                                          "sugerencia", "terminos_lexico", "etiqueta"])
        w.writeheader()
        w.writerows(frases)

    # --- resumen ----------------------------------------------------------
    por_cat = defaultdict(int)
    for f in filas:
        if f["ya_en_lexico"] == "no":
            por_cat[f["categoria"]] += 1
    por_et = defaultdict(int)
    for fr in frases:
        por_et[fr["etiqueta"] or "sin_etiqueta"] += 1
    print(f"\n{len(filas)} términos ({sum(por_cat.values())} nuevos: {dict(por_cat)}) -> {a.candidatos}")
    print(f"{len(frases)} frases {dict(por_et)} -> {a.frases}")

    if a.fusionar:
        fusionar(a, filas)


def fusionar(a, filas):
    if not a.lexico:
        sys.exit("--fusionar necesita --lexico")
    nuevos = [f for f in filas if f["ya_en_lexico"] == "no" and f["categoria"] in CATEGORIAS_LEXICO
              and f["n_fuentes"] >= a.min_fuentes and f["confianza"] >= a.min_confianza]
    if not nuevos:
        print("[fusionar] ningún candidato pasa los filtros; léxico sin cambios")
        return
    shutil.copyfile(a.lexico, a.lexico + ".bak")
    with open(a.lexico, "rb+") as f:            # asegura salto de línea final
        f.seek(0, 2)
        if f.tell():
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                f.write(b"\n")
    with open(a.lexico, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for n in nuevos:
            w.writerow([n["termino"], n["categoria"],
                        f"web ({n['n_fuentes']} fuente{'s' if n['n_fuentes'] > 1 else ''}, {n['metodo']})"])
    print(f"[fusionar] +{len(nuevos)} términos en {a.lexico} (respaldo: {a.lexico}.bak)")


# ==========================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def comunes_descarga(p):
        p.add_argument("--urls", required=True, help=".txt con una URL por línea (# = comentario)")
        p.add_argument("--espera", type=float, default=3.0, help="segundos entre peticiones al mismo sitio")
        p.add_argument("--forzar", action="store_true", help="vuelve a bajar aunque ya esté en caché")
        p.add_argument("--ignorar-robots", action="store_true",
                       help="NO recomendado; solo para sitios tuyos o con permiso")

    def comunes_extraer(p):
        p.add_argument("--lexico", help="lexico_mx.csv (termino,categoria,nota)")
        p.add_argument("--candidatos", default="lexico_candidatos.csv")
        p.add_argument("--frases", default="frases_diccionarios.csv")
        p.add_argument("--sin-red", action="store_true", help="solo léxico + reglas")
        p.add_argument("--umbral-red", type=float, default=0.6,
                       help="probabilidad mínima de la red; debajo -> 'revisar'")
        p.add_argument("--fusionar", action="store_true", help="agrega candidatos nuevos a --lexico")
        p.add_argument("--min-fuentes", type=int, default=2,
                       help="sitios distintos que deben mencionar el término para fusionarlo")
        p.add_argument("--min-confianza", type=float, default=0.6)

    for nombre, ayuda in (("descargar", "baja los HTML"), ("extraer", "procesa los HTML"),
                          ("todo", "descargar + extraer")):
        p = sub.add_parser(nombre, help=ayuda)
        p.add_argument("--dir", default="html_crudo", help="carpeta de los .html")
        if nombre in ("descargar", "todo"):
            comunes_descarga(p)
        if nombre in ("extraer", "todo"):
            comunes_extraer(p)

    a = ap.parse_args()
    if a.cmd in ("descargar", "todo"):
        cmd_descargar(a)
    if a.cmd in ("extraer", "todo"):
        cmd_extraer(a)


if __name__ == "__main__":
    main()
