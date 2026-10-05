# -*- coding: utf-8 -*-
"""
ImportTopo10.pyt

Importerar Lantmäteriet Topografi 10 (vektor) från en nedladdad leverans till en
filgeodatabas, klippt mot en rektangel (bounding box). Rektangeln är antingen
den omslutande rektangeln för polygoner (ur ett lager, med markering, eller
ritade i kartan) eller en utbredning (kartvyn, ett lagers utbredning, en ritad
rektangel eller koordinater), plus en valfri marginal. Klippningen sker alltid
mot hela rektangeln, inte mot polygonernas form: en topografisk bakgrundskarta
ska täcka en hel rektangel.

Leveransen från Lantmäteriet består av en ZIP per tema, där varje ZIP innehåller
ett rikstäckande GeoPackage i SWEREF99 TM (EPSG:3006), t.ex.:

    mark_sverige.zip        -> mark_sverige.gpkg       (mark, sankmark, markkantlinje)
    naturvard_sverige.zip   -> naturvard_sverige.gpkg  (skyddadnatur, ...)

GeoPackage kan inte läsas direkt ur en ZIP, så varje valt tema packas upp en gång
till en cache-mapp och återanvänds vid kommande körningar.

Symbologi: Lantmäteriets lagerfil (Symbolfiler\\Topografi+10_*.lyrx) kan läggas
till i kartan och pekas om till de importerade featureklasserna. Teckensnittet
lmtopografisymboler.ttf måste vara installerat i Windows för att symbolerna ska
visas korrekt.

Verktygstips (parameterförklaringar) skrivs till ImportTopo10.ImportTopo10.pyt.xml
från TOOLTIPS nedan när verktygslådan laddas, så att texten bara finns på ett ställe.

Krav: ArcGIS Pro 3.x (arcpy). Ingen extra licensnivå — PairwiseClip ingår i Basic.
"""

import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from xml.sax.saxutils import escape

import arcpy

# ── Konstanter ────────────────────────────────────────────────────────────────

SWEREF99TM_WKID = 3006

AOI_POLYGONS = "Polygoner (lager eller ritade i kartan)"
AOI_EXTENT = "Utbredning (kartvy, lager eller koordinater)"
# Tidigare etikett, accepteras från skript men visas inte i listan.
AOI_POLYGONS_OLD = "Polygoner i ett lager"

TOOL_SUMMARY = (
    "Importerar Lantmäteriet Topografi 10 (vektor) från en nedladdad leverans till en "
    "filgeodatabas, klippt mot en rektangel runt området. Området anges med polygoner "
    "(ur ett lager eller ritade i kartan) eller med en utbredning (kartvyn, ett lagers "
    "utbredning, en ritad rektangel eller koordinater). Data levereras som rikstäckande "
    "GeoPackage i SWEREF99 TM; varje valt tema packas upp en gång till en cache-mapp och "
    "återanvänds. Lantmäteriets symbologi kan läggas till i kartan och pekas om till de "
    "importerade featureklasserna."
)

# Verktygstips per parameter, visas i verktygsdialogen. Se _write_tool_metadata.
TOOLTIPS = {
    "aoi_mode": (
        "Hur området avgränsas. Resultatet klipps alltid mot en hel rektangel, aldrig mot "
        "polygonernas form, eftersom en topografisk bakgrundskarta ska täcka hela "
        "rektangeln.\n"
        "'Polygoner' använder den omslutande rektangeln för polygoner i ett lager eller "
        "polygoner du ritar i kartan. 'Utbredning' använder en rektangel direkt: kartvyns "
        "aktuella utbredning, ett lagers utbredning, en ritad rektangel eller inskrivna "
        "koordinater."
    ),
    "aoi": (
        "Polygoner som definierar området. Välj ett polygonlager i listan, eller använd "
        "ritverktyget och rita en eller flera polygoner i kartan. Har lagret en markering "
        "används bara de markerade objekten. Lagret kan ha vilket koordinatsystem som helst "
        "men måste ha ett. Data klipps mot polygonernas gemensamma omslutande rektangel "
        "(bounding box) plus marginalen, inte mot själva polygonerna."
    ),
    "aoi_extent": (
        "Rektangel som avgränsar området. I listan kan du välja kartvyns aktuella "
        "utbredning, eller ett lager för att använda hela lagrets utbredning. Du kan också "
        "rita en rektangel i kartan eller skriva in koordinater. Resultatet täcker hela "
        "rektangeln plus marginalen. "
        "Inskrivna koordinater tolkas i den aktiva kartans koordinatsystem, utan aktiv karta "
        "i SWEREF99 TM; vilket som användes står i meddelandena."
    ),
    "buffer_m": (
        "Marginal i meter som läggs till på alla fyra sidor av rektangeln, i SWEREF99 TM. "
        "0 = ingen marginal. Kan inte vara negativ."
    ),
    "source_folder": (
        "Mappen med Lantmäteriets nedladdade Topo 10-leverans: en ZIP per tema, och/eller "
        "redan uppackade GeoPackage. Mappen genomsöks 3 nivåer ned. Fylls i automatiskt om "
        "en sådan mapp ligger bredvid verktygslådan. Leveransen ändras aldrig."
    ),
    "themes": (
        "Vilka teman som importeras, t.ex. mark_sverige eller hydro_sverige. Listan byggs "
        "från källmappen. Tomt = alla teman. Stora teman (mark, höjd) är flera GB och "
        "packas upp till cache-mappen första gången, vilket kan ta några minuter."
    ),
    "out_gdb": (
        "Filgeodatabas som featureklasserna skrivs till, som standard projektets "
        "standardgeodatabas. Utdata är alltid i SWEREF99 TM, oavsett kartans "
        "koordinatsystem."
    ),
    "prefix": (
        "Text som sätts före varje featureklass namn, t.ex. 'topo_' ger topo_mark. Tomt = "
        "källtabellernas namn (mark, vaglinje, ...). Användbart för att hålla flera områden "
        "i samma geodatabas."
    ),
    "overwrite": (
        "Skriv över featureklasser med samma namn som redan finns i geodatabasen. Avbockad: "
        "befintliga featureklasser lämnas orörda och rapporteras som överhoppade."
    ),
    "skip_empty": (
        "Skapa ingen featureklass för tabeller som saknar objekt inom rektangeln. Tabeller "
        "vars utbredning inte når området hoppas då över utan att läsas, vilket går "
        "snabbare. Avbockad: varje tabell i valda teman får en featureklass, även tomma."
    ),
    "cache_folder": (
        "Mapp där ZIP-filerna packas upp till GeoPackage. Ett tema kan vara över 12 GB "
        "uppackat, så välj en lokal disk med plats, inte en mapp som synkas till molnet "
        "(OneDrive, SharePoint, Dropbox). Uppackade filer återanvänds vid nästa körning."
    ),
    "keep_extracted": (
        "Behåll de GeoPackage som packades upp under körningen, så att nästa import går "
        "snabbt. Avbockad: filer som packades upp nu tas bort efteråt (tidigare uppackade "
        "filer lämnas kvar)."
    ),
    "add_to_map": (
        "Lägg till de importerade featureklasserna i den aktiva kartan. Kräver ett öppet "
        "projekt med en karta."
    ),
    "apply_symbology": (
        "Lägg till Lantmäteriets lagerfil och peka om dess lager till de importerade "
        "featureklasserna. Lager utan data tas bort. Symbolerna visas bara rätt om "
        "teckensnittet lmtopografisymboler.ttf från leveransens mapp Symbolfiler är "
        "installerat i Windows."
    ),
    "lyrx_file": (
        "Lantmäteriets lagerfil (.lyrx) med symbologi. Fylls i automatiskt från mappen "
        "Symbolfiler i källmappen. Tomt = verktyget letar själv i källmappen; hittas ingen "
        "läggs lagren till utan symbologi."
    ),
}

# Varning om den omslutande rektangeln blir orimligt stor (troligen ett
# rikstäckande lager som råkat komma med).
_MAX_SANE_SIDE_M = 400_000

# Hur djupt källmappen genomsöks efter zip/gpkg/lyrx
_SCAN_MAX_DEPTH = 3

_CACHE_DIRNAME = "LM_Topo10_uppackat"

# Mappar som synkas till molnet — olämpliga för flera GB uppackad data
_SYNC_HINTS = ("onedrive", "sharepoint", "dropbox", "google drive")

# Cache för mappgenomsökning (updateParameters anropas ofta)
_scan_cache = {}


def _sr():
    return arcpy.SpatialReference(SWEREF99TM_WKID)


# =============================================================================
# Filsystem: hitta och packa upp källdata
# =============================================================================

def _walk_limited(folder, max_depth=_SCAN_MAX_DEPTH):
    """Yield (root, filename) för filer högst max_depth nivåer under folder."""
    folder = os.path.abspath(folder)
    base_depth = folder.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        for name in files:
            yield root, name


def _scan_source(folder):
    """
    Genomsök en nedladdningsmapp och returnera
    {tema: {'zip': sökväg|None, 'gpkg': sökväg|None}}.

    Temanamnet är filnamnet utan ändelse, t.ex. 'mark_sverige'. En uppackad
    GeoPackage matchas mot sin ZIP via samma temanamn.
    """
    found = {}
    if not folder or not os.path.isdir(folder):
        return found

    for root, name in _walk_limited(folder):
        low = name.lower()
        if low.endswith(".gpkg"):
            key = "gpkg"
        elif low.endswith(".zip"):
            key = "zip"
        else:
            continue
        theme = os.path.splitext(name)[0]
        entry = found.setdefault(theme, {"zip": None, "gpkg": None})
        if entry[key] is None:
            entry[key] = os.path.join(root, name)

    return found


def _scan_source_cached(folder):
    """_scan_source med enkel cache, för anrop från updateParameters."""
    if not folder:
        return {}
    key = os.path.abspath(str(folder))
    if key not in _scan_cache:
        _scan_cache[key] = _scan_source(str(folder))
    return _scan_cache[key]


def _find_lyrx(folder):
    """Returnera Lantmäteriets lagerfil i mappen, eller None."""
    if not folder or not os.path.isdir(folder):
        return None
    best, best_score = None, -1
    for root, name in _walk_limited(folder):
        if not name.lower().endswith(".lyrx"):
            continue
        score = 2 if "topografi" in name.lower() else 1
        if score > best_score:
            best, best_score = os.path.join(root, name), score
    return best


def _human_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "{:.1f} {}".format(size, unit)
        size /= 1024.0


def _extract_member(zf, info, target, messages):
    """Packa upp en zip-medlem till target, med förloppsindikator."""
    tmp = target + ".part"
    total = info.file_size or 1
    done = 0
    step = max(total // 100, 1)
    next_report = step

    arcpy.SetProgressor("step", "Packar upp {}...".format(os.path.basename(target)), 0, 100, 1)
    try:
        with zf.open(info, "r") as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(8 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                if done >= next_report:
                    arcpy.SetProgressorPosition(int(done * 100 / total))
                    next_report += step
        if os.path.exists(target):
            os.remove(target)
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    finally:
        arcpy.ResetProgressor()

    messages.addMessage("    Uppackat till {} ({}).".format(target, _human_size(total)))


def _ensure_gpkg(theme, entry, cache_dir, messages):
    """
    Returnera sökvägen till temats GeoPackage. Packar upp ZIP till cache_dir om
    det behövs och återanvänder en tidigare uppackad fil med rätt storlek.
    """
    gpkg = entry.get("gpkg")
    if gpkg and os.path.isfile(gpkg):
        return gpkg

    zpath = entry.get("zip")
    if not zpath or not os.path.isfile(zpath):
        raise ValueError("Hittade varken GeoPackage eller ZIP för temat '{}'.".format(theme))

    with zipfile.ZipFile(zpath) as zf:
        members = [i for i in zf.infolist() if i.filename.lower().endswith(".gpkg")]
        if not members:
            raise ValueError(
                "ZIP-filen {} innehåller ingen .gpkg-fil.".format(os.path.basename(zpath))
            )
        info = max(members, key=lambda i: i.file_size)

        if not os.path.isdir(cache_dir):
            os.makedirs(cache_dir)

        target = os.path.join(cache_dir, os.path.basename(info.filename))
        if os.path.isfile(target) and os.path.getsize(target) == info.file_size:
            messages.addMessage("    Återanvänder uppackad fil: {}".format(target))
            return target

        free = shutil.disk_usage(cache_dir).free
        if free < info.file_size * 1.05:
            raise ValueError(
                "För lite ledigt diskutrymme för att packa upp {}: {} krävs, "
                "{} ledigt på {}.".format(
                    os.path.basename(zpath), _human_size(info.file_size),
                    _human_size(free), cache_dir
                )
            )

        messages.addMessage(
            "    Packar upp {} ({} komprimerat, {} uppackat)...".format(
                os.path.basename(zpath), _human_size(info.compress_size),
                _human_size(info.file_size)
            )
        )
        _extract_member(zf, info, target, messages)

    return target


# =============================================================================
# GeoPackage-metadata (via sqlite3 — snabbare än att öppna varje tabell i arcpy)
# =============================================================================

def _gpkg_feature_tables(gpkg_path):
    """
    Returnera [(tabellnamn, (min_x, min_y, max_x, max_y) | None), ...] för
    feature-tabellerna i en GeoPackage, enligt gpkg_contents.
    """
    con = sqlite3.connect(gpkg_path)
    try:
        rows = con.execute(
            "select table_name, min_x, min_y, max_x, max_y "
            "from gpkg_contents where data_type = 'features' order by table_name"
        ).fetchall()
    finally:
        con.close()

    tables = []
    for name, min_x, min_y, max_x, max_y in rows:
        if None in (min_x, min_y, max_x, max_y):
            tables.append((name, None))
        else:
            tables.append((name, (min_x, min_y, max_x, max_y)))
    return tables


def _bbox_overlaps(a, b):
    """Överlappar två (xmin, ymin, xmax, ymax)-rutor?"""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


# =============================================================================
# Område: polygoner eller utbredning -> rektangel i SWEREF99 TM
# =============================================================================

def _polygon_schema():
    """
    Tom polygonfeatureklass i SWEREF99 TM som standardvärde för polygon-
    parametern, så att dialogens ritverktyg ritar polygoner. Unikt namn i
    stället för Exists/Delete: memory-arbetsytan delas av hela Pro-sessionen.
    """
    name = "aoi_schema_{}".format(uuid.uuid4().hex[:12])
    arcpy.management.CreateFeatureclass("memory", name, "POLYGON",
                                        spatial_reference=_sr())
    return "memory/" + name


def _is_map_layer(value):
    """Är parametervärdet ett kartlager (inte ritade objekt eller en sökväg)?"""
    return hasattr(value, "isFeatureLayer") or type(value).__name__ == "Layer"


def _has_any_feature(value):
    """Minst ett objekt? Läser bara första raden."""
    with arcpy.da.SearchCursor(value, ["OID@"]) as cur:
        for _row in cur:
            return True
    return False


def _aoi_box_from_polygons(value, messages):
    """
    (xmin, ymin, xmax, ymax) i SWEREF99 TM för polygonerna i parametervärdet.

    Värdet är ett kartlager (markering respekteras av cursorn), ritade objekt
    (record set) eller en sökväg. SearchCursor och Describe fungerar på alla tre.
    """
    try:
        sr_in = arcpy.Describe(value).spatialReference
    except Exception:
        sr_in = None
    if sr_in is None or not (sr_in.factoryCode or sr_in.exportToString()) \
            or sr_in.name in ("", "Unknown"):
        raise ValueError(
            "Polygonerna saknar koordinatsystem. Ange ett koordinatsystem för lagret "
            "(Definiera projektion) eller rita polygonerna i kartan."
        )

    label = getattr(value, "name", None)
    selected = 0
    if label and _is_map_layer(value):
        try:
            selected = len(value.getSelectionSet() or ())
        except Exception:
            selected = 0

    xmin = ymin = xmax = ymax = None
    count = 0
    with arcpy.da.SearchCursor(value, ["SHAPE@"], spatial_reference=_sr()) as cur:
        for (shape,) in cur:
            if shape is None or shape.area <= 0:
                continue
            e = shape.extent
            if xmin is None:
                xmin, ymin, xmax, ymax = e.XMin, e.YMin, e.XMax, e.YMax
            else:
                xmin, ymin = min(xmin, e.XMin), min(ymin, e.YMin)
                xmax, ymax = max(xmax, e.XMax), max(ymax, e.YMax)
            count += 1

    if count == 0:
        raise ValueError("Rita minst en polygon i kartan eller välj ett polygonlager.")

    if label and _is_map_layer(value):
        source = "lagret '{}'{}".format(
            label, ", bara markerade objekt" if selected else "")
    else:
        source = "ritade eller angivna polygoner"
    messages.addMessage(
        "  {} polygon{} från {} ({}): {:.0f}, {:.0f} - {:.0f}, {:.0f}".format(
            count, "" if count == 1 else "er", source, sr_in.name,
            xmin, ymin, xmax, ymax)
    )
    return xmin, ymin, xmax, ymax


def _active_map_sr():
    try:
        m = arcpy.mp.ArcGISProject("CURRENT").activeMap
        if m is not None and m.spatialReference is not None:
            return m.spatialReference, m.name
    except Exception:
        pass
    return None, None


def _aoi_box_from_extent(value, text, messages):
    """
    (xmin, ymin, xmax, ymax) i SWEREF99 TM för en GPExtent-parameter.

    .value är ett geoprocessing-extentobjekt, inte arcpy.Extent. Hörnen är
    vanliga tal. Koordinatsystemet följer bara med när utbredningen kommer från
    ett lager eller en datakälla, och då bara som WKT2 i valueAsText efter de
    fyra talen. Inskrivna koordinater har inget; de tolkas i den aktiva kartans
    koordinatsystem, eftersom en utbredning vald i dialogen anges i kartans
    koordinater. Utan aktiv karta antas SWEREF99 TM (Topo 10:s eget system).
    """
    xmin, ymin, xmax, ymax = (float(value.XMin), float(value.YMin),
                              float(value.XMax), float(value.YMax))
    if not (xmax > xmin and ymax > ymin):
        raise ValueError("Utbredningen har ingen yta.")

    sr = None
    parts = (text or "").split(" ", 4)
    if len(parts) == 5 and parts[4].strip():
        sr = arcpy.SpatialReference()
        try:
            sr.loadFromString(parts[4].strip())
        except Exception:
            sr = None
    if sr is not None and (sr.factoryCode or sr.exportToString()):
        source = "utbredningens eget koordinatsystem"
    else:
        sr, map_name = _active_map_sr()
        if sr is not None and (sr.factoryCode or sr.exportToString()):
            source = "den aktiva kartans koordinatsystem ({})".format(map_name)
        else:
            sr = _sr()
            source = "ingen aktiv karta, så SWEREF99 TM antas"
    messages.addMessage("  Utbredningen tolkas i {}: {}.".format(sr.name, source))

    if sr.factoryCode == SWEREF99TM_WKID:
        return xmin, ymin, xmax, ymax

    # Förtäta kanterna, så att en utbredning i grader eller ett annat system
    # inte blir en för liten fyrhörning efter omprojicering.
    n = 16
    pts = ([(xmin + (xmax - xmin) * i / n, ymin) for i in range(n)]
           + [(xmax, ymin + (ymax - ymin) * i / n) for i in range(n)]
           + [(xmax - (xmax - xmin) * i / n, ymax) for i in range(n)]
           + [(xmin, ymax - (ymax - ymin) * i / n) for i in range(n)])
    poly = arcpy.Polygon(arcpy.Array([arcpy.Point(x, y) for x, y in pts]), sr)
    poly = poly.projectAs(_sr())
    if poly is None or poly.area <= 0:
        raise ValueError("Utbredningen kunde inte omvandlas till SWEREF99 TM.")
    e = poly.extent
    return e.XMin, e.YMin, e.XMax, e.YMax


def _aoi_box(mode, polygons, extent_value, extent_text, messages):
    """Områdets omslutande rektangel i SWEREF99 TM, utan marginal."""
    if mode == AOI_EXTENT:
        if extent_value is None or not extent_text:
            raise ValueError("Ange en utbredning.")
        messages.addMessage("Område från utbredning:")
        return _aoi_box_from_extent(extent_value, extent_text, messages)
    if mode in (AOI_POLYGONS, AOI_POLYGONS_OLD):
        if polygons is None:
            raise ValueError("Rita minst en polygon i kartan eller välj ett polygonlager.")
        messages.addMessage("Område från polygoner:")
        return _aoi_box_from_polygons(polygons, messages)
    raise ValueError("Okänt val för 'Avgränsa området med': {}".format(mode))


def _bounding_box(box, buffer_m, messages):
    """Lägg till marginalen och returnera rektangeln som arcpy.Extent i SWEREF99 TM."""
    xmin, ymin, xmax, ymax = box
    if buffer_m:
        xmin -= buffer_m
        ymin -= buffer_m
        xmax += buffer_m
        ymax += buffer_m

    messages.addMessage(
        "Omslutande rektangel{}: {:.0f}, {:.0f} - {:.0f}, {:.0f} "
        "({:.1f} x {:.1f} km, SWEREF99 TM)".format(
            " med {:g} m marginal".format(buffer_m) if buffer_m else "",
            xmin, ymin, xmax, ymax, (xmax - xmin) / 1000.0, (ymax - ymin) / 1000.0
        )
    )
    if max(xmax - xmin, ymax - ymin) > _MAX_SANE_SIDE_M:
        messages.addWarningMessage(
            "Området är över {:.0f} km på en sida. Kontrollera att utbredningen eller "
            "polygonerna är de avsedda; importen kan ta mycket lång tid.".format(
                _MAX_SANE_SIDE_M / 1000.0
            )
        )
    return arcpy.Extent(xmin, ymin, xmax, ymax, spatial_reference=_sr())


def _extent_polygon(ext):
    """Rektangeln som arcpy.Polygon i SWEREF99 TM."""
    arr = arcpy.Array([
        arcpy.Point(ext.XMin, ext.YMin),
        arcpy.Point(ext.XMin, ext.YMax),
        arcpy.Point(ext.XMax, ext.YMax),
        arcpy.Point(ext.XMax, ext.YMin),
        arcpy.Point(ext.XMin, ext.YMin),
    ])
    return arcpy.Polygon(arr, _sr())


# =============================================================================
# Import
# =============================================================================

def _clip_table(src_fc, clip_poly, out_fc, messages):
    """Klipp en tabell mot rektangeln. Returnerar antal objekt i utdata."""
    try:
        arcpy.analysis.PairwiseClip(src_fc, clip_poly, out_fc)
    except arcpy.ExecuteError:
        messages.addWarningMessage("    PairwiseClip misslyckades — försöker med Clip.")
        arcpy.analysis.Clip(src_fc, clip_poly, out_fc)
    return int(arcpy.management.GetCount(out_fc)[0])


def _import_theme(gpkg_path, ext, clip_poly, out_gdb, prefix, overwrite,
                  skip_empty, messages):
    """
    Klipp alla feature-tabeller i en GeoPackage till out_gdb.
    Returnerar {källtabell: utdata-featureklass}.
    """
    imported = {}
    bbox = (ext.XMin, ext.YMin, ext.XMax, ext.YMax)

    for table, tbl_bbox in _gpkg_feature_tables(gpkg_path):
        # Tabellens utbredning enligt gpkg_contents — slipper klippa tabeller
        # som omöjligt kan nå området. Om tomma featureklasser ändå ska skapas
        # klipps de som vanligt, så att resultatet blir komplett.
        if skip_empty and tbl_bbox is not None and not _bbox_overlaps(bbox, tbl_bbox):
            messages.addMessage("    {}: utanför området — hoppas över.".format(table))
            continue

        src_fc = os.path.join(gpkg_path, "main." + table)
        if not arcpy.Exists(src_fc):
            messages.addWarningMessage("    {}: kunde inte öppnas — hoppas över.".format(table))
            continue

        out_name = arcpy.ValidateTableName((prefix or "") + table, out_gdb)
        out_fc = os.path.join(out_gdb, out_name)

        if arcpy.Exists(out_fc):
            if not overwrite:
                messages.addWarningMessage(
                    "    {}: {} finns redan — hoppas över.".format(table, out_name)
                )
                continue
            arcpy.management.Delete(out_fc)

        arcpy.SetProgressorLabel("Klipper {}...".format(table))
        count = _clip_table(src_fc, clip_poly, out_fc, messages)

        if count == 0 and skip_empty:
            arcpy.management.Delete(out_fc)
            messages.addMessage(
                "    {}: 0 objekt i området — ingen featureklass skapad.".format(table)
            )
            continue

        messages.addMessage("    {} -> {} ({} objekt)".format(table, out_name, count))
        imported[table] = out_fc

    return imported


# =============================================================================
# Karta och symbologi
# =============================================================================

def _dataset_table_name(conn_props):
    """'main.%mark' -> 'mark' (datasetnamn i Lantmäteriets lagerfil)."""
    dataset = (conn_props or {}).get("dataset", "") or ""
    return dataset.split("%")[-1].split(".")[-1].lower()


def _repoint_layer(lyr, out_gdb, fc_name):
    """
    Peka om ett lager i lagerfilen från GeoPackage till filgeodatabasen.
    Lagren är query layers (CIMSqlQueryDataConnection), därför byts hela
    dataConnection ut mot en CIMStandardDataConnection — definitionsfrågor,
    etiketter och renderer ligger kvar på featureTable och följer med.
    """
    cim = lyr.getDefinition("V3")
    conn = arcpy.cim.CreateCIMObjectFromClassName("CIMStandardDataConnection", "V3")
    conn.workspaceConnectionString = "DATABASE=" + out_gdb
    conn.workspaceFactory = "FileGDB"
    conn.dataset = fc_name
    conn.datasetType = "esriDTFeatureClass"
    cim.featureTable.dataConnection = conn
    lyr.setDefinition(cim)


def _add_with_symbology(map_obj, lyrx_path, out_gdb, imported, messages):
    """
    Lägg till Lantmäteriets lagerfil i kartan, peka om lagren till de
    importerade featureklasserna och ta bort lager utan data.
    Returnerar mängden källtabeller som fick symbologi.
    """
    lyr_file = arcpy.mp.LayerFile(lyrx_path)
    added = map_obj.addLayer(lyr_file, "TOP")
    if not added:
        messages.addWarningMessage("Kunde inte lägga till lagerfilen i kartan.")
        return set()

    root = added[0]
    matched = set()
    unused = []

    for lyr in root.listLayers():
        if not lyr.isFeatureLayer:
            continue
        try:
            table = _dataset_table_name(lyr.connectionProperties)
        except Exception:
            table = ""
        out_fc = imported.get(table)
        if out_fc:
            try:
                _repoint_layer(lyr, out_gdb, os.path.basename(out_fc))
                matched.add(table)
            except Exception as exc:
                messages.addWarningMessage(
                    "  Kunde inte peka om lagret '{}': {}".format(lyr.name, exc)
                )
                unused.append(lyr)
        else:
            unused.append(lyr)

    removed = 0
    for lyr in unused:
        try:
            map_obj.removeLayer(lyr)
            removed += 1
        except Exception:
            pass

    messages.addMessage(
        "Symbologi tillämpad på {} lager; {} lager utan data togs bort.".format(
            len(matched), removed
        )
    )
    return matched


def _add_plain(map_obj, fcs, messages):
    """Lägg till featureklasser i kartan utan symbologi."""
    for fc in fcs:
        try:
            map_obj.addDataFromPath(fc)
        except Exception as exc:
            messages.addWarningMessage("  Kunde inte lägga till {}: {}".format(fc, exc))


# =============================================================================
# Projekt- och standardvärden
# =============================================================================

def _current_map(messages=None):
    """(projekt, aktiv karta) för det öppna projektet, annars (None, None)."""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:
        return None, None
    map_obj = aprx.activeMap
    if map_obj is None:
        maps = aprx.listMaps()
        map_obj = maps[0] if maps else None
        if map_obj is not None and messages is not None:
            messages.addWarningMessage("Ingen aktiv karta — använder '{}'.".format(map_obj.name))
    return aprx, map_obj


def _default_gdb():
    """Projektets standardgeodatabas."""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        if aprx.defaultGeodatabase:
            return aprx.defaultGeodatabase
    except Exception:
        pass
    ws = arcpy.env.workspace
    return ws if ws and str(ws).lower().endswith(".gdb") else None


def _default_cache_dir():
    """
    Standardmapp för uppackade GeoPackage: lokal temp-mapp.

    Medvetet inte i nedladdningsmappen — den ligger ofta i OneDrive, och ett
    uppackat tema kan vara flera GB som då skulle synkas till molnet.
    """
    return os.path.join(tempfile.gettempdir(), _CACHE_DIRNAME)


def _default_source_folder():
    """En nedladdningsmapp bredvid verktygslådan, om en sådan finns."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [here] + [os.path.join(here, d) for d in os.listdir(here)
                               if os.path.isdir(os.path.join(here, d))]
    except OSError:
        return None
    for folder in candidates:
        try:
            for name in os.listdir(folder):
                if name.lower().endswith((".zip", ".gpkg")):
                    return folder
        except OSError:
            continue
    return None


def _write_tool_metadata(tool_cls, toolbox_alias):
    """
    Skriv verktygets metadatafil med parameterförklaringar från TOOLTIPS.

    Pro läser verktygstipsen i dialogen från <verktygslåda>.<verktyg>.pyt.xml
    (elementet dialogReference per parameter). Det finns inget attribut på
    arcpy.Parameter för detta. Filen skrivs bara om innehållet har ändrats.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    toolbox = os.path.splitext(os.path.basename(__file__))[0]
    path = os.path.join(here, "{}.{}.pyt.xml".format(toolbox, tool_cls.__name__))

    def html(text):
        body = escape(text).replace("\n", "</SPAN></P><P><SPAN>")
        return escape('<DIV STYLE="text-align:Left;"><P><SPAN>{}</SPAN></P></DIV>'.format(body))

    tool = tool_cls()
    params = []
    for p in tool.getParameterInfo(for_metadata=True):
        tip = TOOLTIPS.get(p.name)
        if not tip:
            continue
        params.append(
            '<param name="{n}" displayname="{d}" type="{t}" direction="{r}">'
            "<dialogReference>{h}</dialogReference>"
            "<pythonReference>{h}</pythonReference></param>".format(
                n=p.name, d=escape(p.displayName, {'"': "&quot;"}),
                t=p.parameterType, r=p.direction, h=html(tip))
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xml:lang="sv"><Esri><ArcGISFormat>1.0</ArcGISFormat></Esri>'
        '<tool name="{name}" displayname="{label}" toolboxalias="{alias}" xmlns="">'
        "<parameters>{params}</parameters><summary>{summary}</summary></tool>"
        "<dataIdInfo><idCitation><resTitle>{label}</resTitle></idCitation>"
        "<idAbs>{summary}</idAbs></dataIdInfo></metadata>\n"
    ).format(name=tool_cls.__name__, label=escape(tool.label), alias=toolbox_alias,
             params="".join(params), summary=html(TOOL_SUMMARY))

    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == xml:
                return
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
    except OSError:
        # Skrivskyddad plats: verktyget fungerar ändå, bara utan verktygstips.
        pass


# =============================================================================
# Toolbox
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Lantmäteriet Topo 10"
        self.alias = "topo10"
        self.tools = [ImportTopo10]
        _write_tool_metadata(ImportTopo10, self.alias)


class ImportTopo10:
    def __init__(self):
        self.label = "Importera Topo 10 till projektområdet"
        self.description = TOOL_SUMMARY
        self.canRunInBackground = False

    # ── Parametrar ────────────────────────────────────────────────────────────

    def getParameterInfo(self, for_metadata=False):
        p_mode = arcpy.Parameter(
            displayName="Avgränsa området med", name="aoi_mode", datatype="GPString",
            parameterType="Required", direction="Input",
        )
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = [AOI_POLYGONS, AOI_EXTENT]
        p_mode.value = AOI_POLYGONS

        # Båda är Optional i ramverket; updateMessages kräver den som valts.
        # Feature Set: ett polygonlager (markering respekteras) eller ritade
        # polygoner. Standardvärdet är ett tomt schema i SWEREF99 TM, så att
        # ritverktyget ritar polygoner. Inget schema behövs för metadatafilen.
        p_aoi = arcpy.Parameter(
            displayName="Polygoner som definierar området", name="aoi",
            datatype="GPFeatureRecordSetLayer",
            parameterType="Optional", direction="Input",
        )
        p_aoi.filter.list = ["Polygon"]
        if not for_metadata:
            try:
                p_aoi.value = _polygon_schema()
            except Exception:
                pass

        p_extent = arcpy.Parameter(
            displayName="Utbredning", name="aoi_extent", datatype="GPExtent",
            parameterType="Optional", direction="Input",
        )
        p_extent.enabled = False

        p_buffer = arcpy.Parameter(
            displayName="Marginal runt rektangeln (m)",
            name="buffer_m", datatype="GPDouble",
            parameterType="Optional", direction="Input",
        )
        p_buffer.value = 0

        p_source = arcpy.Parameter(
            displayName="Mapp med nedladdad Topo 10 (zip och/eller gpkg)",
            name="source_folder", datatype="DEFolder",
            parameterType="Required", direction="Input",
        )
        p_source.value = _default_source_folder()

        p_themes = arcpy.Parameter(
            displayName="Teman att importera (tomt = alla)",
            name="themes", datatype="GPString",
            parameterType="Optional", direction="Input", multiValue=True,
        )
        p_themes.filter.type = "ValueList"
        p_themes.filter.list = sorted(_scan_source_cached(p_source.value))

        p_gdb = arcpy.Parameter(
            displayName="Utdata-geodatabas",
            name="out_gdb", datatype="DEWorkspace",
            parameterType="Required", direction="Input",
        )
        p_gdb.filter.list = ["Local Database"]
        p_gdb.value = _default_gdb()

        p_prefix = arcpy.Parameter(
            displayName="Prefix på featureklassernas namn",
            name="prefix", datatype="GPString",
            parameterType="Optional", direction="Input", category="Utdata",
        )

        p_overwrite = arcpy.Parameter(
            displayName="Skriv över befintliga featureklasser",
            name="overwrite", datatype="GPBoolean",
            parameterType="Optional", direction="Input", category="Utdata",
        )
        p_overwrite.value = True

        p_skip_empty = arcpy.Parameter(
            displayName="Skapa inte featureklasser utan objekt i området",
            name="skip_empty", datatype="GPBoolean",
            parameterType="Optional", direction="Input", category="Utdata",
        )
        p_skip_empty.value = True

        p_cache = arcpy.Parameter(
            displayName="Mapp för uppackade GeoPackage (cache)",
            name="cache_folder", datatype="DEFolder",
            parameterType="Optional", direction="Input", category="Källdata",
        )
        p_cache.value = _default_cache_dir()

        p_keep = arcpy.Parameter(
            displayName="Behåll uppackade GeoPackage efter körningen",
            name="keep_extracted", datatype="GPBoolean",
            parameterType="Optional", direction="Input", category="Källdata",
        )
        p_keep.value = True

        p_add = arcpy.Parameter(
            displayName="Lägg till resultatet i kartan",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input", category="Karta och symbologi",
        )
        p_add.value = True

        p_symb = arcpy.Parameter(
            displayName="Använd Lantmäteriets symbologi (lyrx)",
            name="apply_symbology", datatype="GPBoolean",
            parameterType="Optional", direction="Input", category="Karta och symbologi",
        )
        p_symb.value = True

        p_lyrx = arcpy.Parameter(
            displayName="Lagerfil med symbologi",
            name="lyrx_file", datatype="DEFile",
            parameterType="Optional", direction="Input", category="Karta och symbologi",
        )
        p_lyrx.filter.list = ["lyrx"]

        return [p_mode, p_aoi, p_extent, p_buffer, p_source, p_themes, p_gdb,
                p_prefix, p_overwrite, p_skip_empty, p_cache, p_keep,
                p_add, p_symb, p_lyrx]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        p = {q.name: q for q in parameters}

        # Gammal etikett från skript: byt till den nya innan listfiltret kontrolleras.
        if p["aoi_mode"].valueAsText == AOI_POLYGONS_OLD:
            p["aoi_mode"].value = AOI_POLYGONS
        by_extent = p["aoi_mode"].valueAsText == AOI_EXTENT
        p["aoi"].enabled = not by_extent
        p["aoi_extent"].enabled = by_extent

        p_source, p_themes = p["source_folder"], p["themes"]
        p_cache, p_lyrx = p["cache_folder"], p["lyrx_file"]

        if p_source.altered and not p_source.hasBeenValidated:
            folder = p_source.valueAsText
            if folder:
                _scan_cache.pop(os.path.abspath(folder), None)
            themes = sorted(_scan_source_cached(folder))
            p_themes.filter.list = themes
            if p_themes.values:
                keep = [t for t in p_themes.values if t in themes]
                p_themes.values = keep or None
            if not p_lyrx.altered:
                p_lyrx.value = _find_lyrx(folder)
            if not p_cache.valueAsText:
                p_cache.value = _default_cache_dir()

        # Symbologi kräver att resultatet läggs till i kartan
        p["apply_symbology"].enabled = bool(p["add_to_map"].value)
        p["lyrx_file"].enabled = bool(p["add_to_map"].value and p["apply_symbology"].value)

    def updateMessages(self, parameters):
        p = {q.name: q for q in parameters}
        p_source, p_themes, p_gdb = p["source_folder"], p["themes"], p["out_gdb"]

        mode = p["aoi_mode"].valueAsText
        if mode == AOI_EXTENT:
            if not p["aoi_extent"].valueAsText:
                p["aoi_extent"].setErrorMessage("Ange en utbredning.")
        elif mode in (AOI_POLYGONS, AOI_POLYGONS_OLD):
            p_aoi = p["aoi"]
            empty = not p_aoi.valueAsText
            # Ritade polygoner: titta efter minst ett objekt (en rad, billigt).
            # Ett kartlager räknas inte här, det kan vara stort eller en tjänst;
            # ett tomt urval fångas vid körningen.
            if not empty and not _is_map_layer(p_aoi.value):
                try:
                    empty = not _has_any_feature(p_aoi.value)
                except Exception:
                    empty = False
            if empty:
                p_aoi.setErrorMessage(
                    "Rita minst en polygon i kartan eller välj ett polygonlager.")

        folder = p_source.valueAsText
        if folder and os.path.isdir(folder) and not _scan_source_cached(folder):
            p_source.setErrorMessage(
                "Hittade inga .zip- eller .gpkg-filer i mappen "
                "(söker {} nivåer ned).".format(_SCAN_MAX_DEPTH)
            )

        gdb = p_gdb.valueAsText
        if gdb and not gdb.lower().rstrip("\\/").endswith((".gdb", ".sde")):
            p_gdb.setErrorMessage("Utdata måste vara en filgeodatabas (.gdb).")

        if p["buffer_m"].value is not None and p["buffer_m"].value < 0:
            p["buffer_m"].setErrorMessage("Marginalen kan inte vara negativ.")

        if p_themes.values and not p_themes.filter.list:
            p_themes.setWarningMessage("Temalistan kunde inte läsas — kontrollera källmappen.")

        cache = p["cache_folder"].valueAsText
        if cache and any(hint in cache.lower() for hint in _SYNC_HINTS):
            p["cache_folder"].setWarningMessage(
                "Mappen ser ut att synkas till molnet. Ett uppackat tema kan vara "
                "flera GB — välj hellre en lokal mapp, t.ex. {}.".format(_default_cache_dir())
            )

    # ── Körning ───────────────────────────────────────────────────────────────

    def execute(self, parameters, messages):
        p = {q.name: q for q in parameters}
        aoi_mode      = p["aoi_mode"].valueAsText
        polygons      = p["aoi"].value if p["aoi"].valueAsText else None
        extent_value  = p["aoi_extent"].value
        extent_text   = p["aoi_extent"].valueAsText
        buffer_m      = float(p["buffer_m"].value or 0)
        source_folder = p["source_folder"].valueAsText
        themes        = [str(t) for t in p["themes"].values] if p["themes"].values else []
        out_gdb       = p["out_gdb"].valueAsText
        prefix        = (p["prefix"].valueAsText or "").strip()
        overwrite     = bool(p["overwrite"].value)
        skip_empty    = bool(p["skip_empty"].value)
        cache_folder  = p["cache_folder"].valueAsText or _default_cache_dir()
        keep_cache    = bool(p["keep_extracted"].value)
        add_to_map    = bool(p["add_to_map"].value)
        apply_symb    = bool(p["apply_symbology"].value)
        lyrx_path     = p["lyrx_file"].valueAsText

        # Kartan behövs bara för att lägga till resultatet; utan karta
        # importeras ändå.
        _aprx, map_obj = _current_map(messages)

        try:
            imported = _run_import(
                map_obj, aoi_mode, polygons, extent_value, extent_text, buffer_m,
                source_folder, themes, out_gdb, prefix, overwrite, skip_empty,
                cache_folder, keep_cache, add_to_map, apply_symb, lyrx_path, messages,
            )
        except ValueError as exc:
            error = str(exc)
        else:
            error = None
        # Utanför except-blocket, så att Pro inte skriver ut hela kedjan av
        # undantag under det läsbara felmeddelandet.
        if error:
            messages.addErrorMessage(error)
            raise arcpy.ExecuteError

        if imported:
            messages.addMessage("Klar!")

    def postExecute(self, parameters):
        return


# =============================================================================
# Körningens innehåll (separat funktion — går att testa utanför Pro)
# =============================================================================

def _run_import(map_obj, aoi_mode, polygons, extent_value, extent_text, buffer_m,
                source_folder, themes, out_gdb, prefix, overwrite, skip_empty,
                cache_folder, keep_cache, add_to_map, apply_symb, lyrx_path, messages):
    """
    Utför hela importen. Returnerar {källtabell: utdata-featureklass}.

    aoi_mode är AOI_POLYGONS eller AOI_EXTENT. polygons är parametervärdet för
    polygonerna (lager, ritade objekt eller sökväg), extent_value/extent_text
    GPExtent-parameterns value och valueAsText.
    """

    # 1. Område
    messages.addMessage("Beräknar omslutande rektangel...")
    arcpy.SetProgressorLabel("Beräknar omslutande rektangel...")
    box = _aoi_box(aoi_mode, polygons, extent_value, extent_text, messages)
    ext = _bounding_box(box, buffer_m, messages)
    clip_poly = _extent_polygon(ext)

    # 2. Källdata
    available = _scan_source(source_folder)
    if not available:
        raise ValueError("Hittade inga .zip- eller .gpkg-filer i {}.".format(source_folder))
    if not themes:
        themes = sorted(available)
    missing = [t for t in themes if t not in available]
    if missing:
        raise ValueError("Okända teman: {}".format(", ".join(missing)))

    messages.addMessage("Importerar {} tema: {}".format(len(themes), ", ".join(themes)))

    # 3. Klippning
    env_extent = arcpy.env.extent
    env_ocs    = arcpy.env.outputCoordinateSystem
    env_ovr    = arcpy.env.overwriteOutput
    arcpy.env.extent = None
    arcpy.env.outputCoordinateSystem = _sr()
    arcpy.env.overwriteOutput = True

    imported = {}
    extracted_now = []
    try:
        arcpy.SetProgressor("step", "Importerar Topo 10...", 0, len(themes), 1)
        for i, theme in enumerate(themes):
            messages.addMessage("[{}/{}] {}".format(i + 1, len(themes), theme))
            arcpy.SetProgressorPosition(i)
            entry = available[theme]
            try:
                had_gpkg = bool(entry.get("gpkg"))
                gpkg = _ensure_gpkg(theme, entry, cache_folder, messages)
                if not had_gpkg:
                    extracted_now.append(gpkg)
            except Exception as exc:
                messages.addWarningMessage("    {} — temat hoppas över.".format(exc))
                continue

            imported.update(
                _import_theme(gpkg, ext, clip_poly, out_gdb, prefix,
                              overwrite, skip_empty, messages)
            )
        arcpy.SetProgressorPosition(len(themes))
    finally:
        arcpy.ResetProgressor()
        arcpy.env.extent = env_extent
        arcpy.env.outputCoordinateSystem = env_ocs
        arcpy.env.overwriteOutput = env_ovr

    if not imported:
        messages.addWarningMessage(
            "Inga featureklasser skapades — området saknar data i valda teman, "
            "eller så fanns utdata redan (se meddelanden ovan)."
        )
        return imported

    messages.addMessage("{} featureklasser skrivna till {}.".format(len(imported), out_gdb))

    # 4. Karta och symbologi
    if add_to_map and map_obj is not None:
        if not lyrx_path:
            lyrx_path = _find_lyrx(source_folder)
        if apply_symb and lyrx_path and os.path.isfile(lyrx_path):
            messages.addMessage(
                "Lägger till lager med symbologi från {}...".format(os.path.basename(lyrx_path))
            )
            try:
                matched = _add_with_symbology(map_obj, lyrx_path, out_gdb, imported, messages)
            except Exception as exc:
                messages.addWarningMessage(
                    "Kunde inte tillämpa symbologin: {}. "
                    "Lägger till lagren utan symbologi.".format(exc)
                )
                matched = set()
            rest = [fc for t, fc in imported.items() if t not in matched]
            if rest:
                messages.addMessage(
                    "{} featureklasser saknar motsvarighet i lagerfilen och läggs till "
                    "utan symbologi.".format(len(rest))
                )
                _add_plain(map_obj, rest, messages)
        else:
            if apply_symb:
                messages.addWarningMessage(
                    "Ingen lagerfil (.lyrx) hittades — lagren läggs till utan symbologi."
                )
            _add_plain(map_obj, list(imported.values()), messages)
    elif add_to_map:
        messages.addWarningMessage("Ingen aktiv karta — lagren lades inte till.")

    # 5. Cache
    if not keep_cache and extracted_now:
        messages.addMessage("Tar bort uppackade GeoPackage...")
        for path in extracted_now:
            try:
                os.remove(path)
            except OSError as exc:
                messages.addWarningMessage("  Kunde inte ta bort {}: {}".format(path, exc))

    return imported
