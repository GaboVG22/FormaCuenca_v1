# Aplicación: Cuenca desde KMZ con ASTER GDEM v3 y curvas de nivel

Aplicación Streamlit para cargar un KMZ/KML con un punto de control o punto final de una cuenca, descargar un DEM, delinear la cuenca aportante, calcular su superficie y generar un KMZ con:

- Polígono de cuenca.
- Superficie en km² y ha.
- Punto original.
- Punto ajustado al drenaje.
- Curvas de nivel con equidistancia ingresada manualmente.

## DEM principal

La versión actual trabaja directamente con **ASTER GDEM v3 Worldwide Elevation Data**, usando el asset público de Google Earth Engine:

```text
projects/sat-io/open-datasets/ASTER/GDEM
```

También se mantiene una opción alternativa para OpenTopography Global DEM y otra para cargar un DEM GeoTIFF manual.

## Archivos

```text
app.py
requirements.txt
.streamlit/config.toml
.streamlit/secrets.toml.example
```

## Despliegue en Streamlit Cloud

En Streamlit Cloud, el campo **Main file path** debe ser:

```text
app.py
```

## Autenticación Earth Engine

Para usar ASTER GDEM v3 desde Streamlit Cloud se requiere una cuenta de servicio de Google Earth Engine habilitada.

En **Settings > Secrets** de Streamlit Cloud, usar el formato:

```toml
EE_SERVICE_ACCOUNT = "nombre-cuenta@proyecto.iam.gserviceaccount.com"
EE_PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
EE_PROJECT = "id-proyecto-google-cloud"
```

`EE_PROJECT` es opcional, pero recomendable.

También se puede probar la app subiendo un JSON de cuenta de servicio en la interfaz. No se recomienda dejar archivos JSON de credenciales dentro del repositorio.

## Uso

1. Cargar KMZ/KML con un punto.
2. Seleccionar **ASTER GDEM v3 - directo**.
3. Definir radio inicial de descarga DEM.
4. Ingresar equidistancia de curvas de nivel.
5. Ingresar umbral de acumulación para ajuste al cauce.
6. Presionar **Generar cuenca y KMZ**.
7. Descargar el KMZ resultante.

## Recomendaciones técnicas

- Si la cuenca toca el borde del DEM descargado, aumentar el radio de descarga.
- ASTER GDEM v3 tiene resolución aproximada de 30 m, suficiente para análisis preliminar o regional.
- Para diseño definitivo, revisar el resultado con cartografía local, cauces observados y/o DEM de mayor resolución.
- El punto de control debe ubicarse sobre el cauce o muy cerca del cauce. Si se ubica lejos, aumentar o disminuir el umbral de acumulación.

## Limitaciones

- Earth Engine puede limitar descargas demasiado grandes. Para cuencas extensas se recomienda usar DEM GeoTIFF manual.
- ASTER GDEM puede contener artefactos topográficos. Revise el polígono y las curvas antes de usar el resultado en ingeniería de detalle.
