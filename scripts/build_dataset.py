"""Weekly pipeline: pull German BESS (battery storage) data from the MaStR
Gesamtdatenexport without downloading the full ~3GB archive -- only the
Stromspeicher-related members are fetched via HTTP range requests against
the remote ZIP's central directory.

Filter: Batterietechnologie field present (excludes pumped-hydro/other
storage tech) AND Bruttoleistung >= POWER_THRESHOLD_KW (excludes
residential/behind-the-meter systems). Validated against real data:
100/102 (98%) of records passing this filter have coordinates.

Output: data/bess_de.geojson (mappable subset) and data/bess_de.csv (all
filtered records, including the few without coordinates).
"""
import csv
import io
import json
import sys
import zipfile
from xml.etree import ElementTree as ET

from rangefile import HTTPRangeFile, find_latest_zip_url, fetch_member_bytes

POWER_THRESHOLD_KW = 100

FIELDS = [
    "EinheitMastrNummer", "NameStromerzeugungseinheit", "AnlagenbetreiberMastrNummer",
    "EinheitBetriebsstatus", "Land", "Bundesland", "Landkreis", "Gemeinde",
    "Postleitzahl", "Ort", "Strasse", "Hausnummer", "Laengengrad", "Breitengrad",
    "Bruttoleistung", "Nettonennleistung", "Registrierungsdatum",
    "GeplantesInbetriebnahmedatum", "Inbetriebnahmedatum", "Batterietechnologie",
    "Technologie", "Energietraeger", "SpeMastrNummer",
]
CODED_FIELDS = ["EinheitBetriebsstatus", "Land", "Bundesland", "Batterietechnologie",
                "Technologie", "Energietraeger"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_katalog(rf, zf):
    raw = fetch_member_bytes(rf, zf, "Katalogwerte.xml")
    lookup = {}
    for _, elem in ET.iterparse(io.BytesIO(raw)):
        if elem.tag == "Katalogwert":
            d = {c.tag: c.text for c in elem}
            lookup[d["Id"]] = d["Wert"]
            elem.clear()
    return lookup


def load_capacity_map(rf, zf, shard_names):
    cap = {}
    for i, name in enumerate(shard_names, 1):
        raw = fetch_member_bytes(rf, zf, name)
        for _, elem in ET.iterparse(io.BytesIO(raw)):
            if elem.tag == "AnlageStromSpeicher":
                mastr = elem.findtext("MaStRNummer")
                nutzbar = elem.findtext("NutzbareSpeicherkapazitaet")
                if mastr and nutzbar:
                    cap[mastr] = nutzbar
                elem.clear()
        log(f"  Anlagen shard {i}/{len(shard_names)} done, capacity map size={len(cap)}")
    return cap


def load_units(rf, zf, shard_names, capacity_map, katalog):
    rows = []
    for i, name in enumerate(shard_names, 1):
        raw = fetch_member_bytes(rf, zf, name)
        shard_count = 0
        for _, elem in ET.iterparse(io.BytesIO(raw)):
            if elem.tag == "EinheitStromSpeicher":
                d = {c.tag: c.text for c in elem}
                elem.clear()
                if "Batterietechnologie" not in d:
                    continue
                try:
                    power = float((d.get("Bruttoleistung") or "0").replace(",", "."))
                except ValueError:
                    power = 0.0
                if power < POWER_THRESHOLD_KW:
                    continue
                row = {f: d.get(f) for f in FIELDS}
                for f in CODED_FIELDS:
                    if row.get(f):
                        row[f] = katalog.get(row[f], row[f])
                spe = d.get("SpeMastrNummer")
                row["NutzbareSpeicherkapazitaet_kWh"] = capacity_map.get(spe)
                rows.append(row)
                shard_count += 1
        log(f"  Einheiten shard {i}/{len(shard_names)} done, {shard_count} matches, total={len(rows)}")
    return rows


def write_outputs(rows):
    with open("data/bess_de.csv", "w", newline="", encoding="utf-8") as f:
        cols = FIELDS + ["NutzbareSpeicherkapazitaet_kWh"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    features = []
    for row in rows:
        lat, lon = row.get("Breitengrad"), row.get("Laengengrad")
        if not lat or not lon:
            continue
        try:
            lat_f, lon_f = float(lat), float(lon)
        except ValueError:
            continue
        props = {k: v for k, v in row.items() if k not in ("Laengengrad", "Breitengrad")}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
            "properties": props,
        })
    geojson = {"type": "FeatureCollection", "features": features}
    with open("data/bess_de.geojson", "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)

    log(f"Wrote {len(rows)} rows to data/bess_de.csv, {len(features)} mappable to data/bess_de.geojson")


if __name__ == "__main__":
    url = find_latest_zip_url()
    log("ZIP URL:", url)
    rf = HTTPRangeFile(url)
    zf = zipfile.ZipFile(rf)
    names = zf.namelist()

    einheiten_shards = sorted(
        [n for n in names if n.startswith("EinheitenStromSpeicher_")],
        key=lambda n: int(n.split("_")[1].split(".")[0]),
    )
    anlagen_shards = sorted(
        [n for n in names if n.startswith("AnlagenStromSpeicher_")],
        key=lambda n: int(n.split("_")[1].split(".")[0]),
    )
    log(f"{len(einheiten_shards)} Einheiten shards, {len(anlagen_shards)} Anlagen shards")

    log("Loading Katalogwerte...")
    katalog = load_katalog(rf, zf)
    log(f"  {len(katalog)} catalog values loaded")

    log("Loading capacity map from Anlagen shards...")
    capacity_map = load_capacity_map(rf, zf, anlagen_shards)

    log("Loading and filtering Einheiten shards...")
    rows = load_units(rf, zf, einheiten_shards, capacity_map, katalog)

    write_outputs(rows)
