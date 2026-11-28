"""
construir_dataset.py
Arma un dataset binario (1 = ofensivo, 0 = no ofensivo) en español a partir de:
  1) Corpus reales ya etiquetados (MEX-A3T, OffendES) -> opcional, si los tienes en CSV
  2) Léxicos de insultos (SHARE, HurtLex-ES, lista propia) -> para datos sintéticos
  3) Plantillas de oraciones (ofensivas y no ofensivas)
  4) Aumentación "ruidosa" (puntuación, emoticonos, leetspeak, letras repetidas,
     sin acentos, separadores) aplicada A AMBAS CLASES por igual.

Uso:
    python construir_dataset.py --lexico lexico.txt --salida dataset.csv
    python construir_dataset.py --lexico lexico.txt --corpus mexa3t.csv --salida dataset.csv

El archivo de léxico es un .txt con una palabra/expresión ofensiva por línea.
"""
import argparse
import csv
import random
import re
import unicodedata

random.seed(42)

# --------------------------------------------------------------------------
# 1. Semillas mínimas (amplíalas con SHARE / HurtLex / tu propia lista)
# --------------------------------------------------------------------------
INSULTOS_BASE = [
    "idiota", "imbécil", "estúpido", "pendejo", "tarado", "baboso",
    "inútil", "naco", "mamón", "culero", "pinche", "menso",
]

# Groserías usadas SIN intención de ofender (clase 0): son los "casos trampa"
EXPRESIONES_NO_OFENSIVAS = [
    "está de poca madre", "qué chido, güey", "no manches, qué buena noticia",
    "me cae bien ese güey", "estuvo bien perro el examen", "a huevo, pasamos",
    "qué padre está tu casa", "está cabrón de difícil pero lo logré",
]

# Palabras "inocentes" que se parecen o contienen subcadenas de insultos
FALSOS_AMIGOS = [
    "computadora", "pendiente", "disputa", "reputación", "cola de la tortilla",
    "huevo estrellado", "el perro de mi vecino", "tarea de cálculo", "mamá",
]

PLANTILLAS_OFENSIVAS = [
    "eres un {x}", "qué {x} eres", "{x}", "cállate, {x}", "pinche {x}",
    "nadie te quiere, {x}", "ese profe es un {x}", "no seas {x}",
    "tu equipo está lleno de puro {x}", "vete a la verga, {x}",
]

PLANTILLAS_NEUTRAS = [
    "hoy entregué la práctica", "nos vemos mañana en la ESCOM",
    "¿ya viste el partido?", "gracias por la ayuda", "qué buen día",
    "el metro venía lleno", "me gustó mucho la clase", "¿a qué hora es el examen?",
    "voy a comer tacos", "la red neuronal ya converge", "{y}",
]

# --------------------------------------------------------------------------
# 2. Aumentación ruidosa (lo que el profe podría meter de entrada)
# --------------------------------------------------------------------------
EMOTICONOS = [":)", ":(", ":-)", ":-(", ":-/", ":D", "xD", ";)", ":v", "u.u",
              "-_-", ">:(", "<3", ":'(", "😂", "😡", "🙄", "👍", "🤬", "😊"]
PUNTUACION = ["!", "!!!", "?", "¿?", "...", ".", ",", "?!", "¡", ":-", "--", "**"]
LEET = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "$", "t": "7"}


def quitar_acentos(t):
    return "".join(c for c in unicodedata.normalize("NFD", t)
                   if unicodedata.category(c) != "Mn")


def leetspeak(t, p=0.4):
    return "".join(LEET[c] if c in LEET and random.random() < p else c for c in t)


def repetir_letras(t):
    palabras = t.split()
    if not palabras:
        return t
    i = random.randrange(len(palabras))
    w = palabras[i]
    if len(w) > 2:
        j = random.randrange(len(w))
        palabras[i] = w[:j] + w[j] * random.randint(2, 5) + w[j + 1:]
    return " ".join(palabras)


def separar_letras(t):
    """'pendejo' -> 'p.e.n.d.e.j.o' o 'p e n d e j o' en una palabra al azar."""
    palabras = t.split()
    i = random.randrange(len(palabras))
    sep = random.choice([".", " ", "-", "*", "_"])
    palabras[i] = sep.join(palabras[i])
    return " ".join(palabras)


def censurar(t):
    """'pendejo' -> 'p*ndejo' / 'p***jo'"""
    palabras = t.split()
    i = random.randrange(len(palabras))
    w = palabras[i]
    if len(w) > 3:
        k = random.randint(1, len(w) - 2)
        palabras[i] = w[0] + "*" * k + w[k + 1:]
    return " ".join(palabras)


def ruido_puntuacion(t):
    op = random.random()
    if op < 0.33:
        return t + random.choice(PUNTUACION)
    if op < 0.66:
        return random.choice(PUNTUACION) + t + random.choice(PUNTUACION)
    return t.replace(" ", random.choice([" ", "  ", ", ", "... "]), 1)


def ruido_emoticon(t):
    e = random.choice(EMOTICONOS)
    return f"{t} {e}" if random.random() < 0.7 else f"{e} {t}"


def cambiar_caso(t):
    return random.choice([t.upper(), t.lower(), t.capitalize(),
                          "".join(c.upper() if random.random() < .5 else c for c in t)])


TRANSFORMACIONES = [quitar_acentos, leetspeak, repetir_letras, separar_letras,
                    censurar, ruido_puntuacion, ruido_emoticon, cambiar_caso]


def aumentar(texto, n_transf=None):
    n = n_transf or random.randint(1, 3)
    for f in random.sample(TRANSFORMACIONES, n):
        texto = f(texto)
    return texto


# --------------------------------------------------------------------------
# 3. Entradas "basura" que el profe podría probar -> clase 0 (no ofensivo)
# --------------------------------------------------------------------------
def entradas_solo_simbolos(n):
    out = []
    for _ in range(n):
        k = random.randint(1, 4)
        out.append(" ".join(random.choices(EMOTICONOS + PUNTUACION, k=k)))
    return out


# --------------------------------------------------------------------------
# 4. Construcción
# --------------------------------------------------------------------------
def cargar_lexico(ruta):
    if not ruta:
        return []
    with open(ruta, encoding="utf-8") as f:
        return [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]


def cargar_corpus(ruta, col_texto, col_etiqueta):
    filas = []
    if not ruta:
        return filas
    with open(ruta, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            filas.append((r[col_texto].strip(), int(r[col_etiqueta]), "corpus_real"))
    return filas


def generar(insultos, n_por_clase, p_aumento):
    filas = []
    for _ in range(n_por_clase):
        t = random.choice(PLANTILLAS_OFENSIVAS).format(x=random.choice(insultos))
        if random.random() < p_aumento:
            filas.append((aumentar(t), 1, "sintetico_aumentado"))
        else:
            filas.append((t, 1, "sintetico"))

    neutros_fuente = EXPRESIONES_NO_OFENSIVAS + FALSOS_AMIGOS
    for _ in range(n_por_clase):
        t = random.choice(PLANTILLAS_NEUTRAS).format(y=random.choice(neutros_fuente))
        if random.random() < p_aumento:
            filas.append((aumentar(t), 0, "sintetico_aumentado"))
        else:
            filas.append((t, 0, "sintetico"))
    return filas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lexico", help="txt con insultos, uno por línea")
    ap.add_argument("--corpus", help="CSV etiquetado real (opcional)")
    ap.add_argument("--col-texto", default="texto")
    ap.add_argument("--col-etiqueta", default="etiqueta")
    ap.add_argument("--n", type=int, default=2000, help="ejemplos sintéticos por clase")
    ap.add_argument("--p-aumento", type=float, default=0.5)
    ap.add_argument("--n-simbolos", type=int, default=150)
    ap.add_argument("--salida", default="dataset.csv")
    a = ap.parse_args()

    insultos = sorted(set(INSULTOS_BASE + cargar_lexico(a.lexico)))
    filas = cargar_corpus(a.corpus, a.col_texto, a.col_etiqueta)
    filas += generar(insultos, a.n, a.p_aumento)
    filas += [(t, 0, "solo_simbolos") for t in entradas_solo_simbolos(a.n_simbolos)]

    # quitar duplicados exactos
    vistos, unicas = set(), []
    for f in filas:
        if f[0] not in vistos:
            vistos.add(f[0])
            unicas.append(f)
    random.shuffle(unicas)

    with open(a.salida, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["texto", "etiqueta", "origen"])
        w.writerows(unicas)

    n1 = sum(1 for f in unicas if f[1] == 1)
    print(f"{len(unicas)} filas -> {a.salida}  (ofensivo={n1}, no ofensivo={len(unicas)-n1})")


if __name__ == "__main__":
    main()
