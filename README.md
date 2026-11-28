# Ofensivo-o-no
Analizador de cadenas de texto que arroja si la cadena es ofensiva o no ofensiva

## Estructura

```
recoleccion/
  youtube_comentarios.py   # descarga comentarios con la API oficial de YouTube y los prefiltra
  lexico_mx.csv            # léxico mexicano (termino, categoria, nota) para el prefiltro
  canales_mx.txt           # canales a recolectar (noticieros, deportes, etc.)
  diccionarios_web.py      # descarga "diccionarios de groserías" web y extrae términos + frases
  fuentes_groserias.txt    # URLs de esos diccionarios
datos/
  construir_dataset.py     # genera datos sintéticos + aumentación ruidosa y junta corpus
```

## 1. Recolectar comentarios mexicanos (YouTube)

1. Crea un proyecto en Google Cloud Console, habilita **YouTube Data API v3** y genera una API key.
2. Instala dependencias y define la key:
   ```bash
   pip install -r requirements.txt
   export YOUTUBE_API_KEY="tu_api_key"          # Windows: set YOUTUBE_API_KEY=...
   ```
3. Descomenta y verifica los canales en `recoleccion/canales_mx.txt`, luego:
   ```bash
   cd recoleccion
   python youtube_comentarios.py --canales canales_mx.txt --videos-por-canal 10 \
          --max-por-video 300 --lexico lexico_mx.csv --salida comentarios_crudos.csv
   ```
   También puedes pasar videos sueltos: `--videos ID1 ID2` o un `.txt` con IDs.
   Con `--orden time` trae los comentarios más recientes en vez de los más votados: salen más
   comentarios ofensivos, que casi nunca llegan arriba por likes. Puedes correr ambos órdenes.

La cuota gratuita es de 10,000 unidades diarias; cada página de 100 comentarios cuesta 1.
El script no usa `search.list` (cuesta 100) y se detiene al llegar a `--presupuesto`.

### Salida
`comentarios_crudos.csv` con las columnas
`id, texto, video_id, fuente, sugerencia, terminos_lexico, etiqueta`.

- `sugerencia` es solo un **prefiltro** con el léxico: `candidato_ofensivo`, `revisar`
  (groserías de contexto o amistosas) o `candidato_no_ofensivo`.
- `etiqueta` está vacía: se llena a mano (1 = ofensivo, 0 = no ofensivo) siguiendo los criterios
  de Díaz-Torres et al. (2020), *Defining Linguistic Criteria to build a Mexican Spanish Dataset*.

### Privacidad
No se guarda nombre ni ID del autor; el ID del comentario se reemplaza por un hash, y las
menciones y enlaces se sustituyen por `@usuario` y `<URL>`. Los `.csv` están en `.gitignore`:
no subas los comentarios crudos al repo.

## 2. Complementar el léxico y el corpus con diccionarios web

`diccionarios_web.py` baja páginas de "diccionarios de groserías mexicanas", extrae cada
término con su definición y sus **frases de ejemplo**, y propone categoría y etiqueta.

```bash
cd recoleccion
python diccionarios_web.py descargar --urls fuentes_groserias.txt     # -> html_crudo/
python diccionarios_web.py extraer --lexico lexico_mx.csv             # -> 2 CSV de salida
# cuando hayas revisado lexico_candidatos.csv:
python diccionarios_web.py extraer --lexico lexico_mx.csv --fusionar --min-fuentes 2
```

- **Descarga**: respeta `robots.txt`, espera `--espera` segundos entre peticiones al mismo
  sitio y guarda caché (no vuelve a bajar). Si un sitio bloquea bots, guarda la página desde tu
  navegador ("Guardar como…", solo HTML) dentro de `html_crudo/`: `extraer` procesa todo `.html`.
- **Extracción**: combina `<dl>`, tablas, encabezados numerados ("4. Pinche", "¿Qué significa
  X?") y viñetas "**Término**: definición". Son ejemplos lo que sigue a "Ejemplo:", el texto en
  cursiva/`<blockquote>` y las frases entre comillas que contienen el término.
- **Categoría del término** (`ofensivo | contexto | amistoso | neutro`): léxico existente →
  reglas sobre la definición → red neuronal pequeña (MLP, TF-IDF de n-gramas de caracteres)
  entrenada con lo anterior para lo que las reglas no deciden. Con baja confianza → `revisar`.
  `neutro` = modismo que no es grosería ("¿bueno?", "chamba"): no entra al léxico, pero sus
  ejemplos sirven como clase 0.
- **Salidas**: `lexico_candidatos.csv` (para revisar) y `frases_diccionarios.csv`
  (`id, texto, fuente, termino, categoria_termino, sugerencia, terminos_lexico, etiqueta`),
  que se puede pasar como `--corpus` a `construir_dataset.py`. Las frases de términos de
  `contexto` quedan con `etiqueta` vacía: etiquétalas a mano.
- `--fusionar` agrega al léxico solo términos nuevos con `--min-fuentes` sitios distintos y
  `--min-confianza`; deja respaldo `lexico_mx.csv.bak`. La nota dice solo `web (n fuentes)`:
  no se copian definiciones de terceros al repo. No subas el HTML ni los CSV extraídos.

## 3. Construir el dataset final

```bash
cd datos
python construir_dataset.py --lexico lexico.txt --corpus etiquetados.csv --salida dataset.csv
```
Mezcla el corpus etiquetado (columnas `texto, etiqueta`) con ejemplos sintéticos y aplica
aumentación ruidosa (puntuación, emoticonos, leetspeak, letras repetidas, censura) a ambas clases.
