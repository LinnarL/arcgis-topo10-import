# -*- coding: utf-8 -*-
"""
ImportTopo10.pyt

Importerar Lantmäteriet Topografi 10 (vektor) från en nedladdad leverans till en
filgeodatabas, klippt mot den omslutande rektangeln (bounding box) för valda
lager i kartan.

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

Krav: ArcGIS Pro 3.x (arcpy). Ingen extra licensnivå — PairwiseClip ingår i Basic.
"""

import os
import shutil
import sqlite3
import tempfile
import zipfile

import arcpy

# ── Konstanter ────────────────────────────────────────────────────────────────

SWEREF99TM_WKID = 3006

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
# Omslutande rektangel från kartans lager
# =============================================================================

def _selection_count(lyr):
    """
    Antal markerade objekt i ett lager, 0 om inget är markerat.

    getSelectionSet() frågas först: Describe().FIDSet kan vara tom trots att
    lagret har en markering.
    """
    try:
        selected = lyr.getSelectionSet()
    except Exception:
        selected = None
    if selected:
        return len(selected)
    try:
        fid_set = arcpy.Describe(lyr).FIDSet
    except Exception:
        fid_set = None
    if fid_set:
        return len([f for f in fid_set.split(";") if f.strip()])
    return 0


def _extent_from_features(lyr):
    """Utbredning beräknad ur lagrets objekt (markering respekteras av cursorn)."""
    ext = None
    with arcpy.da.SearchCursor(lyr, ["SHAPE@"]) as cur:
        for (geom,) in cur:
            if geom is None:
                continue
            e = geom.extent
            if ext is None:
                ext = arcpy.Extent(e.XMin, e.YMin, e.XMax, e.YMax,
                                   spatial_reference=e.spatialReference)
            else:
                ext = arcpy.Extent(min(ext.XMin, e.XMin), min(ext.YMin, e.YMin),
                                   max(ext.XMax, e.XMax), max(ext.YMax, e.YMax),
                                   spatial_reference=ext.spatialReference)
    return ext


def _layer_extent(lyr, use_selection):
    """
    (utbredning, antal markerade objekt) för ett lager. Utbredningen är None om
    den inte går att bestämma. Om lagret har en markering och use_selection är
    True beräknas utbredningen enbart ur de markerade objekten.
    """
    selected = _selection_count(lyr) if use_selection else 0
    if selected:
        ext = _extent_from_features(lyr)
        if ext is not None:
            return ext, selected

    ext = None
    try:
        ext = arcpy.Describe(lyr).extent
    except Exception:
        ext = None
    if ext is None or ext.XMin is None:
        try:
            ext = lyr.getExtent()
        except Exception:
            ext = None
    if ext is None or ext.XMin is None:
        return None, 0
    return ext, 0


def _extent_to_sweref(ext, messages, label=""):
    """Projicera en utbredning till SWEREF99 TM."""
    sr = ext.spatialReference
    if sr is None or sr.name in ("", "Unknown"):
        messages.addWarningMessage(
            "  Lagret {} saknar koordinatsystem — antar SWEREF99 TM.".format(label)
        )
        return ext
    if sr.factoryCode == SWEREF99TM_WKID:
        return ext

    poly = ext.polygon
    span = max(ext.width, ext.height)
    if span > 0:
        try:
            poly = poly.densify("DISTANCE", span / 50.0)
        except Exception:
            pass
    return poly.projectAs(_sr()).extent


def _resolve_layers(map_obj, layer_names):
    """
    Matcha parameterns lagernamn mot kartans lager. Namn som inte hittas
    returneras oförändrade (kan vara en sökväg till en featureklass).
    """
    resolved = []
    for name in layer_names:
        hits = [h for h in map_obj.listLayers(name) if not h.isGroupLayer] if map_obj else []
        if hits:
            resolved.extend(hits)
        else:
            resolved.append(name)
    return resolved


def _map_layers(map_obj):
    """Alla lager i kartan som kan bidra med en utbredning (utan bakgrundskartor)."""
    out = []
    for lyr in map_obj.listLayers():
        try:
            if lyr.isGroupLayer or lyr.isBasemapLayer or lyr.isBroken:
                continue
            if not (lyr.isFeatureLayer or lyr.isRasterLayer):
                continue
        except Exception:
            continue
        out.append(lyr)
    return out


def _bounding_box(layers, use_selection, buffer_m, messages):
    """Gemensam omslutande rektangel i SWEREF99 TM."""
    xmin = ymin = xmax = ymax = None
    used = 0

    for lyr in layers:
        label = getattr(lyr, "name", str(lyr))
        if isinstance(lyr, str) and not arcpy.Exists(lyr):
            messages.addWarningMessage(
                "  Hoppar över '{}' — lagret finns inte i kartan.".format(label)
            )
            continue
        ext, selected = _layer_extent(lyr, use_selection)
        if ext is None:
            messages.addWarningMessage("  Hoppar över '{}' — ingen utbredning.".format(label))
            continue
        if ext.width == 0 and ext.height == 0 and ext.XMin == 0 and ext.YMin == 0:
            messages.addWarningMessage("  Hoppar över '{}' — tomt lager.".format(label))
            continue

        ext = _extent_to_sweref(ext, messages, label)
        if xmin is None:
            xmin, ymin, xmax, ymax = ext.XMin, ext.YMin, ext.XMax, ext.YMax
        else:
            xmin, ymin = min(xmin, ext.XMin), min(ymin, ext.YMin)
            xmax, ymax = max(xmax, ext.XMax), max(ymax, ext.YMax)
        used += 1
        messages.addMessage(
            "  {}{}: {:.0f}, {:.0f} — {:.0f}, {:.0f}".format(
                label,
                " ({} markerade objekt)".format(selected) if selected else "",
                ext.XMin, ext.YMin, ext.XMax, ext.YMax,
            )
        )

    if xmin is None:
        raise ValueError(
            "Kunde inte bestämma något område. Välj minst ett lager med geometri "
            "(bakgrundskartor och trasiga lager används inte)."
        )

    if buffer_m:
        xmin -= buffer_m
        ymin -= buffer_m
        xmax += buffer_m
        ymax += buffer_m

    messages.addMessage(
        "Omslutande rektangel ({} lager): {:.0f}, {:.0f} — {:.0f}, {:.0f} "
        "({:.1f} x {:.1f} km, SWEREF99 TM)".format(
            used, xmin, ymin, xmax, ymax, (xmax - xmin) / 1000.0, (ymax - ymin) / 1000.0
        )
    )
    if max(xmax - xmin, ymax - ymin) > _MAX_SANE_SIDE_M:
        messages.addWarningMessage(
            "Området är över {:.0f} km på en sida — kontrollera att inget "
            "rikstäckande lager ingår. Importen kan ta mycket lång tid.".format(
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


# =============================================================================
# Toolbox
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Lantmäteriet Topo 10"
        self.alias = "topo10"
        self.tools = [ImportTopo10]


class ImportTopo10:
    def __init__(self):
        self.label = "Importera Topo 10 till projektområdet"
        self.description = (
            "Importerar Lantmäteriet Topografi 10 (vektor) från en nedladdad leverans "
            "till en filgeodatabas, klippt mot den omslutande rektangeln för valda lager "
            "i kartan. Data levereras som rikstäckande GeoPackage i SWEREF99 TM; varje "
            "valt tema packas upp en gång till en cache-mapp och återanvänds. "
            "Lantmäteriets symbologi kan läggas till i kartan och pekas om till de "
            "importerade featureklasserna."
        )
        self.canRunInBackground = False

    # ── Parametrar ────────────────────────────────────────────────────────────

    def getParameterInfo(self):
        p_layers = arcpy.Parameter(
            displayName="Lager som definierar området (tomt = alla lager i kartan)",
            name="in_layers", datatype="GPLayer",
            parameterType="Optional", direction="Input", multiValue=True,
        )

        p_selection = arcpy.Parameter(
            displayName="Använd endast markerade objekt (om markering finns)",
            name="use_selection", datatype="GPBoolean",
            parameterType="Optional", direction="Input",
        )
        p_selection.value = True

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

        return [p_layers, p_selection, p_buffer, p_source, p_themes, p_gdb,
                p_prefix, p_overwrite, p_skip_empty, p_cache, p_keep,
                p_add, p_symb, p_lyrx]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        p_source, p_themes = parameters[3], parameters[4]
        p_cache, p_lyrx = parameters[9], parameters[13]

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
        parameters[12].enabled = bool(parameters[11].value)
        parameters[13].enabled = bool(parameters[11].value and parameters[12].value)

    def updateMessages(self, parameters):
        p_source, p_themes, p_gdb = parameters[3], parameters[4], parameters[5]

        folder = p_source.valueAsText
        if folder and os.path.isdir(folder) and not _scan_source_cached(folder):
            p_source.setErrorMessage(
                "Hittade inga .zip- eller .gpkg-filer i mappen "
                "(söker {} nivåer ned).".format(_SCAN_MAX_DEPTH)
            )

        gdb = p_gdb.valueAsText
        if gdb and not gdb.lower().rstrip("\\/").endswith((".gdb", ".sde")):
            p_gdb.setErrorMessage("Utdata måste vara en filgeodatabas (.gdb).")

        if parameters[2].value is not None and parameters[2].value < 0:
            parameters[2].setErrorMessage("Marginalen kan inte vara negativ.")

        if p_themes.values and not p_themes.filter.list:
            p_themes.setWarningMessage("Temalistan kunde inte läsas — kontrollera källmappen.")

        cache = parameters[9].valueAsText
        if cache and any(hint in cache.lower() for hint in _SYNC_HINTS):
            parameters[9].setWarningMessage(
                "Mappen ser ut att synkas till molnet. Ett uppackat tema kan vara "
                "flera GB — välj hellre en lokal mapp, t.ex. {}.".format(_default_cache_dir())
            )

    # ── Körning ───────────────────────────────────────────────────────────────

    def execute(self, parameters, messages):
        # Lagren matchas via namn mot kartans lager: kartlagren (arcpy.mp.Layer)
        # vet om sin markering, vilket parameterns lagerobjekt inte gör.
        layer_names   = [getattr(v, "name", str(v)) for v in (parameters[0].values or [])]
        use_selection = bool(parameters[1].value)
        buffer_m      = float(parameters[2].value or 0)
        source_folder = parameters[3].valueAsText
        themes        = [str(t) for t in parameters[4].values] if parameters[4].values else []
        out_gdb       = parameters[5].valueAsText
        prefix        = (parameters[6].valueAsText or "").strip()
        overwrite     = bool(parameters[7].value)
        skip_empty    = bool(parameters[8].value)
        cache_folder  = parameters[9].valueAsText or _default_cache_dir()
        keep_cache    = bool(parameters[10].value)
        add_to_map    = bool(parameters[11].value)
        apply_symb    = bool(parameters[12].value)
        lyrx_path     = parameters[13].valueAsText

        _aprx, map_obj = _current_map(messages)
        if map_obj is None and not layer_names:
            messages.addErrorMessage(
                "Ingen aktiv karta hittades. Öppna en karta eller ange lager i verktyget."
            )
            raise arcpy.ExecuteError

        try:
            imported = _run_import(
                map_obj, layer_names, use_selection, buffer_m, source_folder, themes,
                out_gdb, prefix, overwrite, skip_empty, cache_folder, keep_cache,
                add_to_map, apply_symb, lyrx_path, messages,
            )
        except ValueError as exc:
            messages.addErrorMessage(str(exc))
            raise arcpy.ExecuteError

        if imported:
            messages.addMessage("Klar!")

    def postExecute(self, parameters):
        return


# =============================================================================
# Körningens innehåll (separat funktion — går att testa utanför Pro)
# =============================================================================

def _run_import(map_obj, layer_names, use_selection, buffer_m, source_folder, themes,
                out_gdb, prefix, overwrite, skip_empty, cache_folder, keep_cache,
                add_to_map, apply_symb, lyrx_path, messages):
    """Utför hela importen. Returnerar {källtabell: utdata-featureklass}."""

    # 1. Område
    messages.addMessage("Beräknar omslutande rektangel...")
    layers = _resolve_layers(map_obj, layer_names) if layer_names else _map_layers(map_obj)
    if not layers:
        raise ValueError("Kartan innehåller inga lager med geometri.")
    ext = _bounding_box(layers, use_selection, buffer_m, messages)
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
