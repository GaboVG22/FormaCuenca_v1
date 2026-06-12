import io
import json
import math
import os
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import rasterio
from rasterio.features import shapes
from rasterio.mask import mask
from rasterio.transform import xy
import simplekml
import streamlit as st
from pyproj import CRS, Transformer
from shapely.geometry import (
    shape,
    mapping,
    Point,
    LineString,
    MultiLineString,
    GeometryCollection,
    MultiPolygon,
)
from shapely.ops import transform as shp_transform, unary_union

try:
    import ee
except Exception:  # pragma: no cover
    ee = None

try:
    from pysheds.grid import Grid
except Exception:  # pragma: no cover
    Grid = None

st.set_page_config(page_title="Cuenca desde KMZ + ASTER GDEM v3", layout="wide")

ASTER_GEE_ASSET = "projects/sat-io/open-datasets/ASTER/GDEM"

# ==========================================================
# Utilidades geográficas
# ==========================================================

def read_point_from_kmz(uploaded_file):
    """Lee el primer punto encontrado en un archivo KMZ o KML y retorna lon, lat."""
    data = uploaded_file.read()
    name = uploaded_file.name.lower()

    if name.endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(data), "r") as z:
            kml_names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("El KMZ no contiene un archivo KML interno.")
            kml_text = z.read(kml_names[0]).decode("utf-8", errors="ignore")
    else:
        kml_text = data.decode("utf-8", errors="ignore")

    root = ET.fromstring(kml_text)
    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    coords_nodes = root.findall(".//kml:Point/kml:coordinates", ns)
    if not coords_nodes:
        coords_nodes = root.findall(".//Point/coordinates")
    if not coords_nodes:
        raise ValueError("No se encontró geometría tipo Point en el KMZ/KML.")

    text = coords_nodes[0].text.strip()
    lon, lat, *_ = [float(v) for v in text.replace("\n", " ").split()[0].split(",")]
    return lon, lat


def lonlat_to_local_utm_crs(lon, lat):
    zone = int((lon + 180) // 6) + 1
    epsg = 32700 + zone if lat < 0 else 32600 + zone
    return CRS.from_epsg(epsg)


def project_geom(geom, src_crs, dst_crs):
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return shp_transform(transformer.transform, geom)


def polygon_area_km2_ha(poly_wgs84):
    centroid = poly_wgs84.centroid
    utm = lonlat_to_local_utm_crs(centroid.x, centroid.y)
    poly_utm = project_geom(poly_wgs84, CRS.from_epsg(4326), utm)
    area_m2 = abs(poly_utm.area)
    return area_m2 / 1_000_000, area_m2 / 10_000


def km_buffer_to_deg(lat, km):
    # Aproximación conservadora para construir bbox de descarga DEM.
    deg_lat = km / 111.0
    deg_lon = km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return deg_lon, deg_lat


def bbox_from_point_radius(lon, lat, radius_km):
    deg_lon, deg_lat = km_buffer_to_deg(lat, radius_km)
    south = max(-83.0, lat - deg_lat)
    north = min(83.0, lat + deg_lat)
    west = max(-180.0, lon - deg_lon)
    east = min(180.0, lon + deg_lon)
    return west, south, east, north

# ==========================================================
# DEM ASTER GDEM v3 desde Google Earth Engine
# ==========================================================

def initialize_earth_engine(service_account_json=None):
    """Inicializa Earth Engine desde secrets, JSON subido o credenciales locales."""
    if ee is None:
        raise RuntimeError("No se pudo importar earthengine-api. Revise requirements.txt.")

    # 1) JSON subido por el usuario.
    if service_account_json is not None:
        info = json.loads(service_account_json.read().decode("utf-8"))
        service_account = info.get("client_email")
        if not service_account:
            raise ValueError("El JSON no contiene client_email de una cuenta de servicio.")
        credentials = ee.ServiceAccountCredentials(service_account, key_data=json.dumps(info))
        ee.Initialize(credentials)
        return "Cuenta de servicio cargada desde JSON."

    # 2) Secrets de Streamlit Cloud.
    try:
        service_account = st.secrets.get("EE_SERVICE_ACCOUNT", "")
        private_key = st.secrets.get("EE_PRIVATE_KEY", "")
        project = st.secrets.get("EE_PROJECT", None)
        if service_account and private_key:
            private_key = private_key.replace("\\n", "\n")
            key_data = {
                "type": "service_account",
                "client_email": service_account,
                "private_key": private_key,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            credentials = ee.ServiceAccountCredentials(service_account, key_data=json.dumps(key_data))
            if project:
                ee.Initialize(credentials, project=project)
            else:
                ee.Initialize(credentials)
            return "Cuenta de servicio cargada desde secrets de Streamlit."
    except Exception:
        # Continúa con credenciales locales.
        pass

    # 3) Credenciales locales ya autenticadas con earthengine authenticate.
    try:
        ee.Initialize()
        return "Credenciales locales de Earth Engine."
    except Exception as exc:
        raise RuntimeError(
            "Earth Engine no está autenticado. En Streamlit Cloud use secrets "
            "EE_SERVICE_ACCOUNT y EE_PRIVATE_KEY, o suba un JSON de cuenta de servicio."
        ) from exc


def download_aster_gdem_gee(lon, lat, radius_km, out_dir, service_account_json=None):
    """Descarga ASTER GDEM v3 como GeoTIFF para la caja alrededor del punto."""
    auth_msg = initialize_earth_engine(service_account_json)
    west, south, east, north = bbox_from_point_radius(lon, lat, radius_km)

    region = ee.Geometry.Rectangle([west, south, east, north], geodesic=False)
    image = ee.Image(ASTER_GEE_ASSET).rename("elevation")

    url = image.getDownloadURL({
        "region": region,
        "scale": 30,
        "crs": "EPSG:4326",
        "format": "GEO_TIFF",
        "filePerBand": False,
    })

    r = requests.get(url, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"Earth Engine respondió {r.status_code}: {r.text[:500]}")

    out_dir = Path(out_dir)
    out_tif = out_dir / "aster_gdem_v3.tif"

    # Earth Engine puede entregar un GeoTIFF directo o un ZIP con GeoTIFF.
    first_bytes = r.content[:4]
    if first_bytes == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(r.content), "r") as z:
            tif_names = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff"))]
            if not tif_names:
                raise RuntimeError("La descarga de Earth Engine no contiene GeoTIFF.")
            with z.open(tif_names[0]) as src, open(out_tif, "wb") as dst:
                dst.write(src.read())
    else:
        with open(out_tif, "wb") as f:
            f.write(r.content)

    # Validación rápida de raster.
    with rasterio.open(out_tif) as src:
        if src.crs is None:
            raise RuntimeError("El GeoTIFF ASTER descargado no tiene CRS.")
        if src.width < 5 or src.height < 5:
            raise RuntimeError("La descarga ASTER es demasiado pequeña; aumente el radio.")

    return out_tif, auth_msg, (west, south, east, north)

# ==========================================================
# DEM OpenTopography alternativo
# ==========================================================

def download_opentopography_dem(lon, lat, radius_km, demtype, api_key, out_path):
    west, south, east, north = bbox_from_point_radius(lon, lat, radius_km)

    url = "https://portal.opentopography.org/API/globaldem"
    params = {
        "demtype": demtype,
        "south": south,
        "north": north,
        "west": west,
        "east": east,
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }
    r = requests.get(url, params=params, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"OpenTopography respondió {r.status_code}: {r.text[:500]}")

    ctype = r.headers.get("content-type", "").lower()
    if "text" in ctype or r.content[:20].lower().startswith(b"<html"):
        raise RuntimeError(r.text[:800])

    with open(out_path, "wb") as f:
        f.write(r.content)
    return out_path, (west, south, east, north)

# ==========================================================
# Delineación hidrológica
# ==========================================================

def delineate_watershed(dem_path, lon, lat, acc_threshold_cells=200):
    if Grid is None:
        raise RuntimeError("No se pudo importar pysheds. Revise requirements.txt e instalación.")

    with rasterio.open(dem_path) as src:
        dem_crs = src.crs
        if dem_crs is None:
            raise ValueError("El DEM no tiene sistema de referencia definido.")
        to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
        x_dem, y_dem = to_dem.transform(lon, lat)
        bounds = src.bounds
        if not (bounds.left <= x_dem <= bounds.right and bounds.bottom <= y_dem <= bounds.top):
            raise ValueError("El punto de control está fuera de la extensión del DEM.")

    grid = Grid.from_raster(str(dem_path))
    dem = grid.read_raster(str(dem_path))

    # Corrección hidrológica básica del MDE.
    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)

    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
    fdir = grid.flowdir(inflated, dirmap=dirmap)
    acc = grid.accumulation(fdir, dirmap=dirmap)

    # Ajuste del punto a la celda de mayor acumulación cercana.
    snap_mask = acc > int(acc_threshold_cells)
    try:
        x_snap, y_snap = grid.snap_to_mask(snap_mask, (x_dem, y_dem))
    except Exception:
        x_snap, y_snap = x_dem, y_dem

    catch = grid.catchment(
        x=x_snap,
        y=y_snap,
        fdir=fdir,
        dirmap=dirmap,
        xytype="coordinate",
    )
    grid.clip_to(catch)
    catch_view = grid.view(catch, dtype=np.uint8)

    affine = grid.affine
    catch_polys = []
    for geom, value in shapes(catch_view.astype(np.uint8), mask=catch_view.astype(bool), transform=affine):
        if int(value) == 1:
            catch_polys.append(shape(geom))

    if not catch_polys:
        raise RuntimeError("No se pudo generar polígono de cuenca. Revise DEM, punto y umbral de ajuste.")

    watershed_dem_crs = unary_union(catch_polys).buffer(0)
    if watershed_dem_crs.geom_type == "GeometryCollection":
        watershed_dem_crs = unary_union([g for g in watershed_dem_crs.geoms if g.geom_type in ("Polygon", "MultiPolygon")])

    watershed_wgs84 = project_geom(watershed_dem_crs, dem_crs, CRS.from_epsg(4326)).buffer(0)
    snap_wgs84 = project_geom(Point(x_snap, y_snap), dem_crs, CRS.from_epsg(4326))

    touches_edge = catch_view[0, :].any() or catch_view[-1, :].any() or catch_view[:, 0].any() or catch_view[:, -1].any()

    return {
        "polygon_dem_crs": watershed_dem_crs,
        "polygon_wgs84": watershed_wgs84,
        "snap_point_wgs84": snap_wgs84,
        "dem_crs": dem_crs,
        "touches_edge": bool(touches_edge),
    }

# ==========================================================
# Curvas de nivel
# ==========================================================

def iter_lines(geom):
    if geom.is_empty:
        return
    if isinstance(geom, LineString):
        yield geom
    elif isinstance(geom, MultiLineString):
        for g in geom.geoms:
            yield g
    elif isinstance(geom, GeometryCollection):
        for g in geom.geoms:
            yield from iter_lines(g)


def generate_contours(dem_path, watershed_dem_crs, interval_m, max_cells_for_contours=4_000_000):
    with rasterio.open(dem_path) as src:
        out_image, out_transform = mask(src, [mapping(watershed_dem_crs)], crop=True, filled=False)
        z = out_image[0]
        dem_crs = src.crs

    if np.ma.is_masked(z):
        z_arr = z.filled(np.nan).astype(float)
    else:
        z_arr = z.astype(float)
    z_arr[z_arr <= -9990] = np.nan

    rows, cols = z_arr.shape
    stride = max(1, int(math.ceil(math.sqrt((rows * cols) / max_cells_for_contours))))
    if stride > 1:
        z_arr = z_arr[::stride, ::stride]
        rows, cols = z_arr.shape

    finite = np.isfinite(z_arr)
    if finite.sum() < 10:
        raise RuntimeError("No hay suficientes celdas válidas de elevación dentro de la cuenca.")

    z_min = float(np.nanmin(z_arr))
    z_max = float(np.nanmax(z_arr))
    start = math.ceil(z_min / interval_m) * interval_m
    end = math.floor(z_max / interval_m) * interval_m
    if end < start:
        raise ValueError("La equidistancia de curvas es mayor que el rango altimétrico de la cuenca.")
    levels = np.arange(start, end + interval_m, interval_m, dtype=float)

    col_idx = np.arange(cols) * stride
    row_idx = np.arange(rows) * stride
    xs_top = np.array([xy(out_transform, 0, c, offset="center")[0] for c in col_idx])
    ys_left = np.array([xy(out_transform, r, 0, offset="center")[1] for r in row_idx])
    X, Y = np.meshgrid(xs_top, ys_left)

    fig, ax = plt.subplots(figsize=(8, 6))
    cs = ax.contour(X, Y, z_arr, levels=levels)
    plt.close(fig)

    contour_records = []
    for level, segs in zip(cs.levels, cs.allsegs):
        for seg in segs:
            if len(seg) < 2:
                continue
            line = LineString(seg)
            if not line.is_valid or line.length == 0:
                continue
            clipped = line.intersection(watershed_dem_crs)
            for ln in iter_lines(clipped):
                if ln.length > 0:
                    contour_records.append({"elev_m": float(level), "geometry_dem_crs": ln})

    contour_wgs84 = []
    for rec in contour_records:
        contour_wgs84.append({
            "elev_m": rec["elev_m"],
            "geometry_wgs84": project_geom(rec["geometry_dem_crs"], dem_crs, CRS.from_epsg(4326)),
        })

    return contour_wgs84, levels, stride

# ==========================================================
# KMZ
# ==========================================================

def exterior_coords_wgs84(poly):
    return [(float(x), float(y), 0) for x, y in poly.exterior.coords]


def interior_coords_wgs84(poly):
    return [[(float(x), float(y), 0) for x, y in ring.coords] for ring in poly.interiors]


def add_polygon_to_kml(kml_folder, geom, name, description, style):
    polygons = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    for i, poly in enumerate(polygons, start=1):
        p = kml_folder.newpolygon(
            name=name if len(polygons) == 1 else f"{name} parte {i}",
            outerboundaryis=exterior_coords_wgs84(poly),
            innerboundaryis=interior_coords_wgs84(poly),
            description=description,
        )
        p.style = style


def add_line_to_kml(kml_folder, geom, name, style):
    lines = geom.geoms if isinstance(geom, MultiLineString) else [geom]
    for ln in lines:
        coords = [(float(x), float(y), 0) for x, y in ln.coords]
        if len(coords) >= 2:
            ls = kml_folder.newlinestring(name=name, coords=coords)
            ls.style = style


def create_result_kmz(out_path, lon, lat, snap_point, watershed_wgs84, area_km2, area_ha, contours, dem_label):
    kml = simplekml.Kml(name="Cuenca delimitada")

    sty_poly = simplekml.Style()
    sty_poly.polystyle.color = simplekml.Color.changealphaint(65, simplekml.Color.blue)
    sty_poly.linestyle.color = simplekml.Color.blue
    sty_poly.linestyle.width = 3

    sty_point = simplekml.Style()
    sty_point.iconstyle.color = simplekml.Color.red
    sty_point.iconstyle.scale = 1.1

    sty_snap = simplekml.Style()
    sty_snap.iconstyle.color = simplekml.Color.green
    sty_snap.iconstyle.scale = 1.0

    sty_contour = simplekml.Style()
    sty_contour.linestyle.color = simplekml.Color.brown
    sty_contour.linestyle.width = 1.2

    f_main = kml.newfolder(name="Cuenca")
    desc = (
        f"DEM utilizado: {dem_label}<br>"
        f"Superficie cuenca: {area_km2:,.3f} km²<br>"
        f"Superficie cuenca: {area_ha:,.2f} ha<br>"
        f"Punto original: lon {lon:.7f}, lat {lat:.7f}<br>"
        f"Punto ajustado a drenaje: lon {snap_point.x:.7f}, lat {snap_point.y:.7f}"
    )
    add_polygon_to_kml(f_main, watershed_wgs84, f"Cuenca - {area_km2:.3f} km²", desc, sty_poly)

    p0 = f_main.newpoint(name="Punto de control original", coords=[(lon, lat, 0)])
    p0.style = sty_point
    ps = f_main.newpoint(name="Punto ajustado al drenaje", coords=[(snap_point.x, snap_point.y, 0)])
    ps.style = sty_snap

    f_contours = kml.newfolder(name="Curvas de nivel")
    for rec in contours:
        elev = rec["elev_m"]
        geom = rec["geometry_wgs84"]
        add_line_to_kml(f_contours, geom, f"Curva {elev:.0f} m", sty_contour)

    kml.savekmz(out_path)
    return out_path

# ==========================================================
# Vista previa
# ==========================================================

def preview_plot(dem_path, watershed_dem_crs, contours, dem_crs, lon, lat):
    with rasterio.open(dem_path) as src:
        out_image, out_transform = mask(src, [mapping(watershed_dem_crs)], crop=True, filled=False)
        z = out_image[0]
    z_arr = z.filled(np.nan) if np.ma.is_masked(z) else z.astype(float)

    rows, cols = z_arr.shape
    max_cells = 1_500_000
    stride = max(1, int(math.ceil(math.sqrt((rows * cols) / max_cells))))
    z_arr = z_arr[::stride, ::stride]
    rows, cols = z_arr.shape
    col_idx = np.arange(cols) * stride
    row_idx = np.arange(rows) * stride
    xs = np.array([xy(out_transform, 0, c, offset="center")[0] for c in col_idx])
    ys = np.array([xy(out_transform, r, 0, offset="center")[1] for r in row_idx])

    fig, ax = plt.subplots(figsize=(9, 7))
    ax.imshow(z_arr, extent=[xs.min(), xs.max(), ys.min(), ys.max()], origin="upper")

    boundary = watershed_dem_crs.boundary
    for ln in iter_lines(boundary):
        x, y = ln.xy
        ax.plot(x, y, linewidth=2)

    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    x0, y0 = to_dem.transform(lon, lat)
    ax.scatter([x0], [y0], s=40)

    transformer = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    count = 0
    for rec in contours:
        if count > 500:
            break
        geom_d = shp_transform(transformer.transform, rec["geometry_wgs84"])
        for ln in iter_lines(geom_d):
            x, y = ln.xy
            ax.plot(x, y, linewidth=0.4)
            count += 1

    ax.set_title("Vista preliminar DEM + cuenca + curvas de nivel")
    ax.set_xlabel(f"X ({dem_crs})")
    ax.set_ylabel(f"Y ({dem_crs})")
    ax.set_aspect("equal", adjustable="box")
    return fig

# ==========================================================
# Interfaz Streamlit
# ==========================================================

st.title("Delineación automática de cuenca desde KMZ con ASTER GDEM v3")
st.caption("Carga un KMZ/KML con punto de control, descarga ASTER GDEM v3, genera el polígono de cuenca, calcula superficie y exporta KMZ con curvas de nivel.")

with st.expander("Criterios técnicos usados", expanded=False):
    st.markdown(
        """
        - La opción principal trabaja directamente con **ASTER GDEM v3 Worldwide Elevation Data**, resolución aproximada de 1 arc-second / 30 m.
        - El DEM se descarga desde el asset público de Google Earth Engine: `projects/sat-io/open-datasets/ASTER/GDEM`.
        - El KMZ debe contener un punto de control o punto de salida de la cuenca.
        - La cuenca se delinea sobre el DEM mediante dirección de flujo D8, acumulación y ajuste del punto al drenaje cercano.
        - Si el DEM descargado no cubre toda la cuenca aportante, el resultado puede quedar truncado. En ese caso aumente el radio de descarga o cargue un DEM más amplio.
        - Las curvas de nivel se generan dentro del polígono de cuenca con equidistancia definida manualmente.
        - ASTER GDEM puede contener artefactos; para diseño definitivo conviene revisar el resultado con cartografía local, cauces observados y/o DEM de mayor resolución.
        """
    )

left, right = st.columns([0.38, 0.62])

with left:
    st.subheader("1. Entrada")
    kmz_file = st.file_uploader("KMZ/KML con punto de control", type=["kmz", "kml"])

    dem_source = st.radio(
        "Fuente del DEM",
        [
            "ASTER GDEM v3 - directo",
            "OpenTopography Global DEM alternativo",
            "Cargar DEM GeoTIFF manual",
        ],
        index=0,
    )

    dem_file = None
    ee_json_file = None
    api_key = ""
    demtype = "COP30"
    radius_km = 50.0
    auth_mode = "Secrets/local"

    if dem_source == "ASTER GDEM v3 - directo":
        st.info("Fuente directa: ASTER GDEM v3, asset Earth Engine `projects/sat-io/open-datasets/ASTER/GDEM`.")
        radius_km = st.number_input(
            "Radio inicial de descarga DEM alrededor del punto (km)",
            min_value=5.0,
            max_value=150.0,
            value=50.0,
            step=5.0,
            help="Debe cubrir toda la cuenca. Si el polígono toca el borde, aumente el radio.",
        )
        auth_mode = st.selectbox(
            "Autenticación Earth Engine",
            ["Secrets/local", "Subir JSON de cuenta de servicio"],
            index=0,
            help="Para Streamlit Cloud se recomienda usar secrets; para prueba puntual puede subir un JSON de cuenta de servicio.",
        )
        if auth_mode == "Subir JSON de cuenta de servicio":
            ee_json_file = st.file_uploader("JSON cuenta de servicio Earth Engine", type=["json"])

    elif dem_source == "OpenTopography Global DEM alternativo":
        st.warning("OpenTopography Global DEM API se deja como alternativa. Para seguir exactamente la instrucción, use ASTER GDEM v3 - directo.")
        api_key = st.text_input("OpenTopography API Key", type="password", help="No la suba a GitHub. Ingrésela solo en la app o como secret.")
        demtype = st.selectbox("DEM", ["COP30", "NASADEM", "SRTMGL1", "SRTMGL3", "COP90", "AW3D30"], index=0)
        radius_km = st.number_input("Radio de descarga DEM alrededor del punto (km)", min_value=5.0, max_value=300.0, value=50.0, step=5.0)

    else:
        dem_file = st.file_uploader("DEM GeoTIFF", type=["tif", "tiff"])

    st.subheader("2. Parámetros")
    contour_interval = st.number_input("Equidistancia curvas de nivel (m)", min_value=1.0, max_value=500.0, value=25.0, step=1.0)
    acc_threshold = st.number_input("Umbral de acumulación para ajustar punto al cauce (celdas)", min_value=1, max_value=100000, value=200, step=50)

    run = st.button("Generar cuenca y KMZ", type="primary")

with right:
    st.subheader("Resultado")
    if not run:
        st.info("Ingrese el KMZ, seleccione DEM y presione **Generar cuenca y KMZ**.")
    else:
        if kmz_file is None:
            st.error("Debe cargar un KMZ/KML con el punto de control.")
            st.stop()
        if dem_source == "ASTER GDEM v3 - directo" and auth_mode == "Subir JSON de cuenta de servicio" and ee_json_file is None:
            st.error("Debe subir el JSON de cuenta de servicio de Earth Engine o usar Secrets/local.")
            st.stop()
        if dem_source == "OpenTopography Global DEM alternativo" and not api_key:
            st.error("Debe ingresar una API Key de OpenTopography o usar ASTER GDEM v3 / DEM manual.")
            st.stop()
        if dem_source == "Cargar DEM GeoTIFF manual" and dem_file is None:
            st.error("Debe cargar un DEM GeoTIFF.")
            st.stop()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            try:
                lon, lat = read_point_from_kmz(kmz_file)
                st.success(f"Punto leído: lon {lon:.7f}, lat {lat:.7f}")

                dem_path = tmpdir / "dem.tif"
                bbox = None
                dem_label = ""

                if dem_source == "ASTER GDEM v3 - directo":
                    with st.spinner("Descargando ASTER GDEM v3 desde Earth Engine..."):
                        dem_path, auth_msg, bbox = download_aster_gdem_gee(lon, lat, radius_km, tmpdir, ee_json_file)
                        dem_label = "ASTER GDEM v3 / projects/sat-io/open-datasets/ASTER/GDEM"
                        st.caption(f"Autenticación EE: {auth_msg}")
                elif dem_source == "OpenTopography Global DEM alternativo":
                    with st.spinner("Descargando DEM desde OpenTopography..."):
                        dem_path, bbox = download_opentopography_dem(lon, lat, radius_km, demtype, api_key, dem_path)
                        dem_label = f"OpenTopography {demtype}"
                else:
                    with open(dem_path, "wb") as f:
                        f.write(dem_file.read())
                    dem_label = "DEM GeoTIFF manual"

                with rasterio.open(dem_path) as src:
                    st.caption(f"DEM: {src.width:,} x {src.height:,} celdas | CRS: {src.crs}")

                with st.spinner("Procesando DEM y delineando cuenca..."):
                    result = delineate_watershed(dem_path, lon, lat, acc_threshold)

                watershed_wgs84 = result["polygon_wgs84"]
                watershed_dem_crs = result["polygon_dem_crs"]
                area_km2, area_ha = polygon_area_km2_ha(watershed_wgs84)

                with st.spinner("Generando curvas de nivel..."):
                    contours, levels, stride = generate_contours(dem_path, watershed_dem_crs, contour_interval)

                out_kmz = tmpdir / "cuenca_curvas_nivel_aster_gdem_v3.kmz"
                create_result_kmz(out_kmz, lon, lat, result["snap_point_wgs84"], watershed_wgs84, area_km2, area_ha, contours, dem_label)

                c1, c2, c3 = st.columns(3)
                c1.metric("Superficie", f"{area_km2:,.3f} km²")
                c2.metric("Superficie", f"{area_ha:,.2f} ha")
                c3.metric("Curvas generadas", f"{len(contours):,}")

                if result["touches_edge"]:
                    st.warning("La cuenca toca el borde del DEM procesado. Aumente el radio de descarga o cargue un DEM más amplio para evitar truncamiento.")

                summary_rows = [
                    {"Concepto": "DEM utilizado", "Valor": dem_label},
                    {"Concepto": "Longitud punto original", "Valor": f"{lon:.7f}"},
                    {"Concepto": "Latitud punto original", "Valor": f"{lat:.7f}"},
                    {"Concepto": "Longitud punto ajustado", "Valor": f"{result['snap_point_wgs84'].x:.7f}"},
                    {"Concepto": "Latitud punto ajustado", "Valor": f"{result['snap_point_wgs84'].y:.7f}"},
                    {"Concepto": "Superficie km²", "Valor": f"{area_km2:.6f}"},
                    {"Concepto": "Superficie ha", "Valor": f"{area_ha:.2f}"},
                    {"Concepto": "Equidistancia curvas m", "Valor": f"{contour_interval:.1f}"},
                    {"Concepto": "Rango curvas m", "Valor": f"{float(levels.min()):.0f} - {float(levels.max()):.0f}"},
                ]
                if bbox:
                    summary_rows.append({"Concepto": "BBOX DEM", "Valor": f"W {bbox[0]:.5f}, S {bbox[1]:.5f}, E {bbox[2]:.5f}, N {bbox[3]:.5f}"})
                summary = pd.DataFrame(summary_rows)
                st.dataframe(summary, hide_index=True, use_container_width=True)

                fig = preview_plot(dem_path, watershed_dem_crs, contours, result["dem_crs"], lon, lat)
                st.pyplot(fig, clear_figure=True)

                with open(out_kmz, "rb") as f:
                    st.download_button(
                        "Descargar KMZ cuenca + curvas de nivel",
                        data=f.read(),
                        file_name="cuenca_curvas_nivel_aster_gdem_v3.kmz",
                        mime="application/vnd.google-earth.kmz",
                    )

            except Exception as exc:
                st.error(f"No se pudo completar el procesamiento: {exc}")
                st.exception(exc)
