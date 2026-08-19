# Import Topo 10 for current project

ArcGIS Pro Python toolbox that imports **Lantmäteriet Topografi 10 (vektor)** into a file
geodatabase, clipped to the bounding box of the layers in your map.

Lantmäteriet ships Topo 10 as one ZIP per theme, each containing a **country-wide GeoPackage**
in SWEREF99 TM (EPSG:3006). `mark_sverige.zip` alone is 6.4 GB zipped and 12.5 GB extracted.
This tool cuts that down to your project area in one step.

- Bounding box comes from your map: all layers, selected layers, or just the selected features
- Any subset of themes; tables that can't reach the area are skipped without being read
- Output goes to a chosen file GDB, default the project's default GDB, always in SWEREF99 TM
- Optionally adds the result to the map with Lantmäteriet's own symbology applied

## Requirements

- ArcGIS Pro 3.x (developed and tested on 3.6, Python 3.13). Basic licence is enough
- A Topo 10 delivery downloaded from Lantmäteriet (Geotorget), not included here
- For correct symbols: install `lmtopografisymboler.ttf` from the delivery's `Symbolfiler` folder

## Install

1. Clone or download this repo.
2. In ArcGIS Pro: Catalog, Toolboxes, Add Toolbox, select `ImportTopo10.pyt`.
3. Open Lantmäteriet Topo 10, Importera Topo 10 till projektområdet.

The tool has no dependencies beyond `arcpy` and the standard library.

## The tool dialog

The UI is in Swedish, matching a Swedish ArcGIS Pro install. Parameters below in dialog order;
the last three groups are collapsible sections.

| Parameter | Default | Notes |
|---|---|---|
| Lager som definierar området | *(empty)* | Empty = every layer in the active map. Basemaps and broken layers are never used |
| Använd endast markerade objekt | on | If a layer has a selection, only the selected features define the box |
| Marginal runt rektangeln (m) | 0 | Margin added on all four sides |
| Mapp med nedladdad Topo 10 | auto | Auto-filled if a download folder sits next to the toolbox. Scanned 3 levels deep for `.zip`/`.gpkg` |
| Teman att importera | *(empty = all)* | Picklist built from the folder scan, e.g. `mark_sverige`, `hydro_sverige` |
| Utdata-geodatabas | project default GDB | Must be a file geodatabase |
| **Utdata** | | |
| Prefix på featureklassernas namn | *(none)* | Feature classes otherwise keep the source table names (`mark`, `vaglinje`, ...) |
| Skriv över befintliga featureklasser | on | Off = existing outputs are left untouched and reported as skipped |
| Skapa inte featureklasser utan objekt i området | on | Off = every table gets a feature class, empty ones included |
| **Källdata** | | |
| Mapp för uppackade GeoPackage (cache) | `%TEMP%\LM_Topo10_uppackat` | Warns if you point it at a cloud-synced folder |
| Behåll uppackade GeoPackage efter körningen | on | Off = files extracted during this run are deleted afterwards |
| **Karta och symbologi** | | |
| Lägg till resultatet i kartan | on | |
| Använd Lantmäteriets symbologi (lyrx) | on | Greyed out unless the previous box is ticked |
| Lagerfil med symbologi | auto | Auto-filled from `Symbolfiler\Topografi+10_*.lyrx` in the download folder |

Validation you may see: unknown themes, a non-`.gdb` output, a negative margin, a source folder
with no data, and a warning when the bounding box is wider than 400 km. That last one usually
means a country-wide layer slipped into the selection.

Typical run log:

```
Beräknar omslutande rektangel...
  Utredningsområde (3 markerade objekt): 668000, 6580000 — 678000, 6588000
Omslutande rektangel (1 lager): 668000, 6580000 — 678000, 6588000 (10.0 x 8.0 km, SWEREF99 TM)
Importerar 2 tema: kommunikation_sverige, text_sverige
[1/2] kommunikation_sverige
    Packar upp kommunikation_sverige.zip (839.0 MB komprimerat, 2.0 GB uppackat)...
    vaglinje -> vaglinje (24000 objekt)
    transportled_fjall: utanför området — hoppas över.
...
Symbologi tillämpad på 7 lager; 30 lager utan data togs bort.
```

## How it works

1. **Bounding box.** Unions the extents of the chosen layers, reprojects to SWEREF99 TM, adds the
   margin. Selections are detected with `getSelectionSet()`, because `Describe().FIDSet` can come
   back empty even when a layer has a selection.
2. **Source.** A theme is used straight from an existing `.gpkg`, otherwise its ZIP is extracted
   once to the cache folder and reused on later runs (verified by file size).
3. **Filter.** `gpkg_contents` is read with `sqlite3` to skip tables whose extent can't reach the
   area, so no oversized table is opened unnecessarily.
4. **Clip.** `PairwiseClip` per table into the target GDB, with output coordinate system forced to
   EPSG:3006. Geometries are cut at the box edge.
5. **Map.** Lantmäteriet's `.lyrx` group layer is inserted and each layer repointed to the new
   feature classes; layers with no imported data are removed.

## Performance

Measured on an 11 x 9 km area, including unzipping, on a laptop with the delivery on local disk:

| Themes | Source scale | Time |
|---|---|---|
| naturvård, ledningar, anläggningsområde, polcirkeln | 4 GeoPackages | 10 s |
| kommunikation + text | 2.1 GB GPKG, `vaglinje` 3 024 958 to 24 000 features | 19 s |
| mark + höjd | 12.5 GB + 8.5 GB extracted | 102 s |

## Notes

- Output is always SWEREF99 TM, regardless of the map's coordinate system.
- The cache deliberately defaults to local `%TEMP%` rather than the download folder. Download
  folders often sit in OneDrive, and a single extracted theme can be over 12 GB of sync traffic.
- Lantmäteriet's data, layer files and symbol font are not redistributed here. Download them from
  Geotorget and point the tool at that folder.
