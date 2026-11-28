# Ofensivo-o-no
Analizador de cadenas de texto que arroja si la cadena es ofensiva o no ofensiva

## Estructura

```
recoleccion/
  youtube_comentarios.py   # descarga comentarios con la API oficial de YouTube y los prefiltra
  lexico_mx.csv            # léxico mexicano (termino, categoria, nota) para el prefiltro
  canales_mx.txt           # canales a recolectar (noticieros, deportes, etc.)
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

## 2. Construir el dataset final

```bash
cd datos
python construir_dataset.py --lexico lexico.txt --corpus etiquetados.csv --salida dataset.csv
```
Mezcla el corpus etiquetado (columnas `texto, etiqueta`) con ejemplos sintéticos y aplica
aumentación ruidosa (puntuación, emoticonos, leetspeak, letras repetidas, censura) a ambas clases.
