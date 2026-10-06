#!/usr/bin/env python3
"""
ws_encode.py - WorldSpace-artiger Multiplex-Encoder (Basisband-Datei)
=====================================================================

Weg der Daten (Details und Quellen: wscore.py, wsconfig.py):

  MP3 (Service Components) -> BC-Rahmen 432 ms (SCH + Multiplex)
    -> Scrambler -> RS(255,223) + Blockinterleaver -> Faltungscode R=1/2
    -> Verteilung auf n PRC (+ 48-Symbol-PRC-Preamble)
    -> 96-PRC-TDM-Rahmen (MFP + TSCC + Daten, TDM-Scrambler)
    -> QPSK 1,84 MSym/s, RRC 0,4 -> komplexes Basisband als Datei

Aufruf:
  python ws_encode.py -o mux.cs8 \
      --bc "Jazz:jazz.mp3@64" \
      --bc "Talk:news.mp3@32@24000,wetter.mp3@24@16000"

Syntax  --bc "[NR=]LABEL:DATEI[@KBPS[@SR]][,DATEI[@KBPS[@SR]]...]"
  NR     BCID-Nummer 1..510 (Standard: fortlaufend)
  @KBPS  Bitrate des SC (Vielfaches von 8, max. 128).  Ist die Datei nicht
         schon "raster-konform" (siehe unten), wird sie mit ffmpeg umkodiert.
  @SR    Abtastrate: 48000, 32000, 24000, 16000, 12000 oder 8000.

RASTER-KONFORMITAET (Grund: [PAT Sp.22])
  Der MP3-Strom muss konstante Bitrate ohne Padding haben, Layer III, eine der
  erlaubten Abtastraten, und je 432 ms exakt rate*54 Byte liefern.  Dann
  beginnt jeder BC-Rahmen mit einem MPEG-Frame-Header.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

import wsconfig as C
import wscore as ws


def convert_with_ffmpeg(path, kbps, sr):
    """Erzeugt einen rasterkonformen MP3-Strom (CBR, ohne Cover/Metadaten)."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg nicht gefunden - bitte installieren oder die Datei selbst "
                           "umkodieren (siehe README, Abschnitt Raster)")

    sr = sr or (48000 if kbps >= 64 else 24000)
    ch = 2 if kbps >= 64 else 1

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",

        "-i", path,

        # Nur den ersten Audiostream verwenden.
        "-map", "0:a:0",

        # Keine Cover-/Attached-Picture-Streams.
        "-vn",

        # Keine Metadaten aus der Quelldatei übernehmen.
        "-map_metadata", "-1",

        "-ar", str(sr),
        "-ac", str(ch),
        "-codec:a", "libmp3lame",
        "-b:a", f"{kbps}k",

        # Kein Xing/LAME-Info-Tag und kein ID3v2.
        "-write_xing", "0",
        "-id3v2_version", "0",

        "-f", "mp3",
        tmp.name,
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",          # ffmpeg-Meldungen mit exotischen Zeichen duerfen nicht abstuerzen
        )

        if result.returncode != 0:
            raise RuntimeError(
                "ffmpeg fehlgeschlagen (Code %d):\n%s\nKommando: %s"
                % (result.returncode, result.stderr.strip(), " ".join(cmd))
            )

        with open(tmp.name, "rb") as f:
            return f.read()

    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)




def load_sc(spec):
    """'datei[@kbps[@sr]]' -> SC-Objekt (mit Chunks im 432-ms-Raster)."""
    parts = spec.split("@")
    path = parts[0]
    kbps = int(parts[1]) if len(parts) > 1 and parts[1] else None
    sr = int(parts[2]) if len(parts) > 2 and parts[2] else None
    raw = open(path, "rb").read()
    try:
        if kbps or sr or not path.lower().endswith(".mp3"):
            raise ws.NonConformant("Umkodierung angefordert")
        rate, chunks = ws.mp3_bc_chunks(raw)
    except ws.NonConformant as why:
        if kbps is None:
            if str(why) == "Umkodierung angefordert":
                sys.exit(f"{path}: bitte Bitrate angeben, z.B. {path}@64")
            sys.exit(f"{path}: nicht rasterkonform ({why}). Bitrate angeben ({path}@64), "
                     f"dann wird mit ffmpeg umkodiert.")
        # Bereits konform mit gewuenschten Parametern?
        try:
            rate, chunks = ws.mp3_bc_chunks(raw)
            if rate != kbps:
                raise ws.NonConformant("andere Bitrate")
        except ws.NonConformant:
            try:
                raw = convert_with_ffmpeg(path, kbps, sr)
                rate, chunks = ws.mp3_bc_chunks(raw)
            except RuntimeError as e:
                sys.exit(f"{path}: Umkodierung nicht moeglich:\n{e}")
            except ws.NonConformant as e:
                sys.exit(f"{path}: auch nach der Umkodierung nicht rasterkonform: {e}")
    print(f"    SC {os.path.basename(path)}: {rate} kbit/s, {len(chunks)} BC-Rahmen "
          f"({len(chunks) * C.BC_FRAME_SECONDS:.1f} s)")
    return ws.SC(rate, chunks), os.path.splitext(os.path.basename(path))[0]


def parse_bc(spec, idx):
    num = idx + 1
    head = spec.split(":", 1)[0]
    if "=" in head:
        n, spec = spec.split("=", 1)
        num = int(n)
    label, files = spec.split(":", 1)
    scs, names = [], []
    print(f"  BC {num} '{label}'")
    for f in files.split(","):
        sc, nm = load_sc(f)
        scs.append(sc)
        names.append(nm)
    if not 1 <= num <= 510:
        sys.exit("BCID-Nummer muss 1..510 sein (0 = unbenutzt, 511 = Test)")
    return ws.BC(num, label, scs, dynamic=" | ".join(names))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--bc", action="append", required=True)
    ap.add_argument("--fmt", default="cs8", choices=list(ws.FORMATS))
    ap.add_argument("--sps", type=int, default=4, help="Samples je Symbol (>=4)")
    ap.add_argument("--max-seconds", type=float, help="Dauer begrenzen")
    ap.add_argument("--loop", action="store_true", help="kuerzere SC wiederholen statt abbrechen")
    ap.add_argument("--lead-in", type=int, default=1, help="stumme Vorlauf-BC-Rahmen (Einschwingen)")
    ap.add_argument("--first-prc", type=int, default=0, help="erster belegter PRC (0..95)")
    ap.add_argument("--cn", type=float, help="AWGN: C/N (=Es/N0) in dB")
    ap.add_argument("--cfo", type=float, default=0.0, help="Traegerfrequenzablage in Hz")
    ap.add_argument("--phase", type=float, default=0.0, help="Phasenversatz in Grad")
    ap.add_argument("--timing", type=float, default=0.0, help="Symboltaktversatz (0..1 Symbol)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true",
                    help="nur Quellen pruefen/konvertieren und Zusammenfassung zeigen")
    a = ap.parse_args()

    print("Lade Quellen ...")
    bcs = [parse_bc(s, i) for i, s in enumerate(a.bc)]
    p = a.first_prc
    for b in bcs:
        b.prcs = list(range(p, p + b.n))
        p += b.n
        if b.n and p > C.NPRC:
            sys.exit(f"Mehr als {C.NPRC} PRC belegt")
        net = b.n * C.PRI_BITS / C.BC_FRAME_SECONDS / 1000
        print(f"BC {b.bcid} '{b.label}': n={b.n} PRC {b.prcs[0]}..{b.prcs[-1]}, "
              f"{net:.2f} kbit/s brutto (SCH+Service)")
        for sc in b.scs:
            sc.loop = a.loop
    nfr = (max if a.loop else min)(len(sc.chunks) for b in bcs for sc in b.scs)
    if a.max_seconds:
        nfr = min(nfr, int(a.max_seconds / C.BC_FRAME_SECONDS))
    if nfr < 1:
        sys.exit("Keine vollstaendigen BC-Rahmen: die Quelle ist kuerzer als 432 ms "
                 "(oder --max-seconds < 0,432).")
    nlead = a.lead_in
    L = (nlead + nfr) * C.PRC_FRAME_SYMS
    ntdm = L // C.SETS_PER_TDM
    print(f"{nfr} BC-Rahmen (+{nlead} Vorlauf) = {(nfr + nlead) * C.BC_FRAME_SECONDS:.1f} s, "
          f"{ntdm} TDM-Rahmen")

    prc_used = sum(b.n for b in bcs)
    est = ntdm * C.TDM_SYMS * a.sps * ws.FORMATS[a.fmt][2] / 1e6
    print(f"PRC belegt: {prc_used}/{C.NPRC}; Dateigroesse ca. {est:.0f} MB")
    if a.dry_run:
        print("Trockenlauf beendet - alle Quellen sind in Ordnung.")
        return

    t0 = time.time()
    print("Codiere BC -> PRC-Stroeme ...")
    streams = ws.build_prc_streams(bcs, nfr, nlead)
    tdm = ws.TdmBuilder(bcs, streams, a.seed)
    with open(a.out, "wb") as fh:
        mod = ws.Modulator(fh, a.fmt, a.sps, a.cn, a.cfo, a.phase, a.timing, a.seed)
        for t in range(ntdm):
            mod.write(tdm.frame(t))
            if t % 5 == 0 or t == ntdm - 1:
                print(f"\r  TDM-Rahmen {t + 1}/{ntdm}", end="", flush=True)
        mod.close()
    meta = {"format": a.fmt, "sps": a.sps, "symbol_rate": C.SYMRATE,
            "sample_rate": a.sps * C.SYMRATE, "samples": mod.total,
            "impairments": {"cn_db": a.cn, "cfo_hz": a.cfo, "phase_deg": a.phase, "timing": a.timing}}
    json.dump(meta, open(a.out + ".json", "w"), indent=1)
    print(f"\nFertig in {time.time() - t0:.1f} s -> {a.out} "
          f"({os.path.getsize(a.out) / 1e6:.0f} MB, {a.sps * C.SYMRATE / 1e6:.2f} MS/s {a.fmt})")


if __name__ == "__main__":
    main()
