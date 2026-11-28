"""
youtube_comentarios.py
Descarga comentarios públicos de YouTube con la API OFICIAL (YouTube Data API v3),
los anonimiza y los prefiltra con un léxico mexicano para facilitar el etiquetado manual.

Fuentes que acepta:
  --videos    IDs de videos (o un .txt con un ID por línea)
  --canales   handles (@Canal) o IDs de canal (UC...) -> toma sus N videos más recientes.
              Útil con canales de noticieros mexicanos: es el equivalente "legal" a los
              comentarios de portales de noticias que usó el dataset argentino.

Requisitos:
  pip install requests
  export YOUTUBE_API_KEY="tu_api_key"     (Google Cloud Console -> YouTube Data API v3)

Ejemplos:
  python youtube_comentarios.py --videos dQw4w9WgXcQ --salida crudos.csv
  python youtube_comentarios.py --canales canales_mx.txt --videos-por-canal 10 \
         --max-por-video 300 --lexico lexico_mx.csv --salida crudos.csv

Cuota: la API da 10,000 unidades diarias por proyecto. Cada página de comentarios
(hasta 100), cada consulta de canal y cada página de videos cuesta 1 unidad. Este script
NO usa search.list (cuesta 100). Usa --presupuesto para no pasarte.

Privacidad: no se guarda el nombre ni el ID del autor; las menciones (@usuario) y los
enlaces se reemplazan. No publiques los comentarios crudos; comparte solo texto + etiqueta.
"""
import argparse
import csv
import hashlib
import os
import re
import sys
import time
import unicodedata

import requests

API = "https://www.googleapis.com/youtube/v3"


# --------------------------------------------------------------------------
# Cliente mínimo de la API con control de cuota
# --------------------------------------------------------------------------
class YouTube:
    def __init__(self, api_key, presupuesto):
        self.key = api_key
        self.presupuesto = presupuesto
        self.gastado = 0

    def get(self, recurso, **params):
        if self.gastado >= self.presupuesto:
            raise RuntimeError("presupuesto de cuota agotado")
        params["key"] = self.key
        for intento in range(3):
            r = requests.get(f"{API}/{recurso}", params=params, timeout=30)
            self.gastado += 1
            if r.status_code == 200:
                return r.json()
            if r.status_code in (500, 503):
                time.sleep(2 ** intento)
                continue
            # 403 con commentsDisabled, quotaExceeded, etc.
            try:
                razon = r.json()["error"]["errors"][0]["reason"]
            except Exception:
                razon = r.text[:200]
            raise ApiError(r.status_code, razon)
        raise ApiError(r.status_code, "reintentos agotados")


class ApiError(Exception):
    def __init__(self, status, razon):
        super().__init__(f"{status}: {razon}")
        self.status, self.razon = status, razon


# --------------------------------------------------------------------------
# Videos a partir de canales
# --------------------------------------------------------------------------
def playlist_de_subidas(yt, canal):
    canal = canal.strip()
    if canal.startswith("UC"):
        datos = yt.get("channels", part="contentDetails,snippet", id=canal)
    else:
        datos = yt.get("channels", part="contentDetails,snippet",
                       forHandle=canal if canal.startswith("@") else "@" + canal)
    items = datos.get("items", [])
    if not items:
        return None, None
    it = items[0]
    return it["contentDetails"]["relatedPlaylists"]["uploads"], it["snippet"]["title"]


def videos_recientes(yt, playlist, n):
    ids, token = [], None
    while len(ids) < n:
        datos = yt.get("playlistItems", part="contentDetails", playlistId=playlist,
                       maxResults=min(50, n - len(ids)), pageToken=token or "")
        ids += [i["contentDetails"]["videoId"] for i in datos.get("items", [])]
        token = datos.get("nextPageToken")
        if not token:
            break
    return ids


# --------------------------------------------------------------------------
# Comentarios
# --------------------------------------------------------------------------
def comentarios_de_video(yt, video_id, maximo, con_respuestas, orden="relevance"):
    token, n = None, 0
    while n < maximo:
        datos = yt.get("commentThreads",
                       part="snippet,replies" if con_respuestas else "snippet",
                       videoId=video_id, maxResults=100, textFormat="plainText",
                       order=orden, pageToken=token or "")
        for hilo in datos.get("items", []):
            top = hilo["snippet"]["topLevelComment"]
            yield top["id"], top["snippet"]["textDisplay"]
            n += 1
            if con_respuestas:
                for resp in hilo.get("replies", {}).get("comments", []):
                    yield resp["id"], resp["snippet"]["textDisplay"]
                    n += 1
            if n >= maximo:
                return
        token = datos.get("nextPageToken")
        if not token:
            return


# --------------------------------------------------------------------------
# Limpieza / anonimización
# --------------------------------------------------------------------------
RE_URL = re.compile(r"https?://\S+|www\.\S+")
RE_MENCION = re.compile(r"@[\w.\-]+")
RE_ESPACIOS = re.compile(r"\s+")


def anonimizar(texto):
    texto = RE_URL.sub("<URL>", texto)
    texto = RE_MENCION.sub("@usuario", texto)
    return RE_ESPACIOS.sub(" ", texto).strip()


def id_anonimo(comment_id):
    return hashlib.sha256(comment_id.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Prefiltro con léxico (para priorizar el etiquetado, NO es la etiqueta final)
# --------------------------------------------------------------------------
LEET = str.maketrans({"4": "a", "3": "e", "1": "i", "0": "o", "$": "s", "7": "t", "@": "a"})


def normalizar(t):
    t = t.lower().translate(LEET)
    t = "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")
    t = re.sub(r"(?<=\w)[.\-*_](?=\w)", "", t)   # p.e.n.d.e.j.o -> pendejo
    t = re.sub(r"(.)\1{2,}", r"\1", t)           # pendejoooo -> pendejo
    return t


def cargar_lexico(ruta):
    """CSV con columnas: termino, categoria (ofensivo | contexto | amistoso)."""
    if not ruta:
        return []
    lex = []
    with open(ruta, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            term = normalizar(fila["termino"].strip())
            if term:
                patron = re.compile(r"(?<!\w)" + re.escape(term) + r"\w{0,3}(?!\w)")
                lex.append((term, fila.get("categoria", "ofensivo").strip(), patron))
    # términos largos primero (frases antes que palabras sueltas)
    return sorted(lex, key=lambda x: -len(x[0]))


def prefiltrar(texto, lexico):
    norm = normalizar(texto)
    hallados = {}
    for term, cat, patron in lexico:
        if patron.search(norm):
            hallados.setdefault(cat, []).append(term)
    if "ofensivo" in hallados:
        sugerencia = "candidato_ofensivo"
    elif hallados:
        sugerencia = "revisar"           # groserías de contexto o amistosas
    else:
        sugerencia = "candidato_no_ofensivo"
    terminos = sorted({t for ts in hallados.values() for t in ts})
    return sugerencia, "|".join(terminos)


# --------------------------------------------------------------------------
def leer_lista(valores):
    out = []
    for v in valores or []:
        if os.path.isfile(v):
            with open(v, encoding="utf-8") as f:
                out += [l.split("#")[0].strip() for l in f]
        else:
            out.append(v)
    return [x for x in out if x]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos", nargs="*", help="IDs de video o .txt con uno por línea")
    ap.add_argument("--canales", nargs="*", help="@handles / IDs UC... o .txt")
    ap.add_argument("--videos-por-canal", type=int, default=10)
    ap.add_argument("--max-por-video", type=int, default=300)
    ap.add_argument("--con-respuestas", action="store_true",
                    help="incluye respuestas (solo las que vienen en el hilo)")
    ap.add_argument("--orden", choices=["relevance", "time"], default="relevance",
                    help="relevance = los más votados; time = los más recientes (incluye más "
                         "comentarios ofensivos, que suelen tener pocos likes)")
    ap.add_argument("--min-caracteres", type=int, default=3)
    ap.add_argument("--lexico", help="CSV termino,categoria para el prefiltro")
    ap.add_argument("--presupuesto", type=int, default=9000, help="unidades máximas a gastar")
    ap.add_argument("--salida", default="comentarios_crudos.csv")
    a = ap.parse_args()

    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("Falta la variable de entorno YOUTUBE_API_KEY")

    yt = YouTube(key, a.presupuesto)
    lexico = cargar_lexico(a.lexico)

    # (video_id, nombre_fuente)
    objetivos = [(v, "video") for v in leer_lista(a.videos)]
    for canal in leer_lista(a.canales):
        try:
            playlist, titulo = playlist_de_subidas(yt, canal)
        except ApiError as e:
            print(f"[canal] {canal}: {e}", file=sys.stderr)
            continue
        if not playlist:
            print(f"[canal] {canal}: no encontrado", file=sys.stderr)
            continue
        vids = videos_recientes(yt, playlist, a.videos_por_canal)
        print(f"[canal] {titulo}: {len(vids)} videos")
        objetivos += [(v, titulo) for v in vids]

    vistos, filas = set(), []
    for video_id, fuente in objetivos:
        n_antes = len(filas)
        try:
            for cid, texto in comentarios_de_video(yt, video_id, a.max_por_video,
                                                   a.con_respuestas, a.orden):
                texto = anonimizar(texto)
                clave = normalizar(texto)
                if len(texto) < a.min_caracteres or clave in vistos:
                    continue
                vistos.add(clave)
                sug, terms = prefiltrar(texto, lexico)
                filas.append({
                    "id": id_anonimo(cid), "texto": texto, "video_id": video_id,
                    "fuente": fuente, "sugerencia": sug, "terminos_lexico": terms,
                    "etiqueta": "",  # <- la llenas tú: 1 ofensivo, 0 no ofensivo
                })
        except ApiError as e:
            if e.razon == "quotaExceeded":
                print("Cuota diaria agotada; se guarda lo obtenido.", file=sys.stderr)
                break
            print(f"[video] {video_id}: {e.razon}", file=sys.stderr)
        except RuntimeError as e:
            print(f"{e}; se guarda lo obtenido.", file=sys.stderr)
            break
        print(f"[video] {video_id}: +{len(filas) - n_antes} comentarios "
              f"(cuota usada: {yt.gastado})")

    campos = ["id", "texto", "video_id", "fuente", "sugerencia", "terminos_lexico", "etiqueta"]
    with open(a.salida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(filas)

    resumen = {}
    for fila in filas:
        resumen[fila["sugerencia"]] = resumen.get(fila["sugerencia"], 0) + 1
    print(f"\n{len(filas)} comentarios -> {a.salida}  {resumen}  (cuota usada: {yt.gastado})")


if __name__ == "__main__":
    main()
