#!/usr/bin/env python3
"""
ws_radio.py - virtuelles WorldSpace-artiges Radio
=================================================

Liest eine Basisband-Datei (von ws_encode.py oder kompatibel) und macht einen
Service Component eines Broadcast Channels hoerbar.  Die Verarbeitungsschritte
entsprechen dem STA002-Blockdiagramm [DS Fig.1]:

  QPSK-Demodulator -> TDM-Rahmensync (MFP) -> TSCC -> PRC-Extraktion/-Sync
   -> Viterbi -> Deinterleaver -> Reed-Solomon -> Descrambler
   -> SCH-Auswertung / SC-Demux  -> MPEG-Decoder (hier: miniaudio) -> Audio

  python ws_radio.py mux.cs8                 # interaktiv (l, t 1.1, i, w, q)
  python ws_radio.py mux.cs8 --list          # nur Senderliste
  python ws_radio.py mux.cs8 --tune 1.1      # BCID 1, SC 1 abspielen
  python ws_radio.py mux.cs8 --tune 2.2 --wav out.wav
"""
import argparse
import json
import os
import sys
import time
import wave

import numpy as np

import wsconfig as C
import wscore as ws


class Radio:
    def __init__(self, path, fmt=None, sps=None, cache=None, quiet=False, logf=None):
        self.path = path
        self.logf = logf or sys.stdout          # Fortschritt/Logs (GUI: stderr)
        meta = json.load(open(path + ".json")) if os.path.exists(path + ".json") else {}
        self.fmt = fmt or meta.get("format", "cs8")
        self.sps = sps or meta.get("sps", 4)
        self.cache = cache or path + ".syms"
        self.quiet = quiet
        self._ensure_symbols()
        z = np.memmap(self.cache, dtype=np.complex64, mode="r")
        self.rx = ws.TdmReceiver(z)                    # MFP-Sync + TSCC aller Rahmen
        self._bc = {}
        self.channels = {}
        self._scan()

    def _log(self, *a, **k):
        if not self.quiet:
            print(*a, file=self.logf, flush=True, **k)

    def _ensure_symbols(self):
        meta = self.cache + ".meta"
        sig = f"{self.fmt},{self.sps},{C.mfp_hex()}"
        if (os.path.exists(self.cache) and os.path.exists(meta) and open(meta).read() == sig
                and os.path.getmtime(self.cache) >= os.path.getmtime(self.path)):
            self._log(f"Symbol-Cache verwendet: {self.cache}")
            return
        self._log(f"Demoduliere {self.path} ({self.fmt}, {self.sps} Samples/Symbol) ...")
        t0 = time.time()
        last = [-1]

        def prog(p):
            if not self.quiet and int(p * 20) != last[0]:
                last[0] = int(p * 20)
                print(f"\r  QPSK-Demodulation {p * 100:5.1f}%", end="", flush=True, file=self.logf)
        ws.demod_file(self.path, self.cache, self.fmt, self.sps, prog)
        open(meta, "w").write(sig)
        self._log(f"\n  fertig in {time.time() - t0:.1f} s")

    def bc_frames(self, bcid):
        if bcid not in self._bc:
            self._bc[bcid] = self.rx.decode_bc(bcid)
        return self._bc[bcid]

    def _scan(self):
        rx = self.rx
        good = sum(t is not None for t in rx.tscc)
        self._log(f"Rahmensync: {len(rx.starts)} TDM-Rahmen, TSCC ok: {good}, "
                  f"C/N ~ {rx.cn_db:.1f} dB" + (" (Q invertiert)" if rx.conj else ""))
        for bcid, prcs in sorted(rx.lineup().items()):
            frames = self.bc_frames(bcid)
            sch = next((f["sch"] for f in frames if f["ok"]), None)
            self.channels[bcid] = {"prcs": prcs, "sch": sch, "frames": len(frames),
                                   "good": sum(f["ok"] for f in frames)}

    def listing(self):
        L = [f"{'BCID':>5} {'Name':<9} {'PRC':>3} {'SC':>2} {'kbit/s':>6} {'Typ':<6} Info"]
        for bcid, ch in sorted(self.channels.items()):
            s = ch["sch"]
            if s is None:
                L.append(f"{bcid:>5}  (nicht dekodierbar, PRC {ch['prcs']})")
                continue
            for i, sc in enumerate(s["scs"], 1):
                typ = {0: "MPEG", 1: "Daten", 4: "JPEG", 5: "Video", 15: "ungueltig"}.get(sc["type"], "?")
                L.append(f"{bcid:>5} {s['label'] if i == 1 else '':<9} {len(ch['prcs']) if i == 1 else '':>3} "
                         f"{i:>2} {sc['rate_kbps']:>6} {typ:<6} {s['dynamic'] if i == 1 else ''}"
                         + (" [verschluesselt]" if sc["encrypted"] else ""))
        return "\n".join(L)

    def to_dict(self):
        """Maschinenlesbarer Scan (fuer die GUI, Option --json)."""
        types = {0: "MPEG", 1: "Daten", 4: "JPEG", 5: "Video", 15: "ungueltig"}
        rx = self.rx
        out = {"file": self.path, "fmt": self.fmt, "sps": self.sps, "cn_db": rx.cn_db,
               "tdm_frames": len(rx.starts), "tscc_ok": sum(t is not None for t in rx.tscc),
               "sync_quality": float(rx.sync_quality), "q_inverted": bool(rx.conj),
               "mfp_hex": C.mfp_hex(), "channels": []}
        for bcid, ch in sorted(self.channels.items()):
            s = ch["sch"]
            d = {"bcid": bcid, "type": bcid >> 9, "number": bcid & 0x1FF, "prcs": ch["prcs"],
                 "n": len(ch["prcs"]), "frames": ch["frames"], "good": ch["good"],
                 "label": s["label"] if s else "", "dynamic": s["dynamic"] if s else "",
                 "ec": s["ec"] if s else 0, "scs": []}
            for i, sc in enumerate(s["scs"] if s else [], 1):
                d["scs"].append({"index": i, "rate_kbps": sc["rate_kbps"], "type": sc["type"],
                                 "type_name": types.get(sc["type"], "?"),
                                 "encrypted": sc["encrypted"], "ptype": sc["ptype"],
                                 "language": sc["language"]})
            out["channels"].append(d)
        return out

    def tune(self, bcid, sc=1, salvage=False):
        """Alle BC-Rahmen des BC dekodieren und die Bytes der gewaehlten SC sammeln."""
        ch = self.channels.get(bcid)
        if ch is None or ch["sch"] is None:
            raise KeyError(f"BCID {bcid} nicht verfuegbar")
        buf, lost, fixed, used = bytearray(), 0, 0, 0
        for fr in self.bc_frames(bcid):
            if not fr["ok"] and not (salvage and fr["sch"]):
                lost += 1
                continue
            sch = fr["sch"]
            if sc > sch["nsc"]:
                continue
            info = sch["scs"][sc - 1]
            if info["encrypted"] or sch["ec"] != 0:
                raise PermissionError("Verschluesselt (WES) - nicht unterstuetzt")
            fixed += sum(1 for e in fr["nerr"] if e > 0)
            if info["type"] == C.SC_TYPE_INVALID:           # z.B. Vorlauf-Rahmen
                continue
            buf += ws.demux_service(fr["frame"], sch)[sc - 1]
            used += 1
        return bytes(buf), {"frames": used, "lost": lost, "rs_fixed": fixed,
                            "seconds": used * C.BC_FRAME_SECONDS, "cn_db": self.rx.cn_db}


def decode_pcm(mp3):
    import miniaudio
    return miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16)


def save_wav(d, path):
    with wave.open(path, "wb") as w:
        w.setnchannels(d.nchannels)
        w.setsampwidth(2)
        w.setframerate(d.sample_rate)
        w.writeframes(d.samples.tobytes())


def play(d, label=""):
    import miniaudio
    dev = miniaudio.PlaybackDevice(output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=d.nchannels, sample_rate=d.sample_rate)
    stream = miniaudio.stream_raw_pcm_memory(d.samples.tobytes(), d.nchannels, 2)
    next(stream)
    dev.start(stream)
    dur = len(d.samples) / d.nchannels / d.sample_rate
    t0 = time.time()
    try:
        while time.time() - t0 < dur:
            print(f"\r  \u266a {label}  {time.time() - t0:5.1f}/{dur:.1f} s  (Strg+C = Stopp)", end="", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
        print()


def tune_and_play(radio, spec, wav=None, salvage=False, mp3_out=None, as_json=False):
    """Abstimmen auf 'BCID[.SC]'.  Ausgabe: Lautsprecher (Standard), --wav oder --out-mp3.
    Mit as_json wird nichts abgespielt und eine Zusammenfassung als @@JSON@@-Zeile ausgegeben."""
    bcid, _, sc = spec.partition(".")
    bcid, sc = int(bcid), int(sc or 1)
    mp3, st = radio.tune(bcid, sc, salvage)
    sch = radio.channels[bcid]["sch"]
    say = (lambda *a: None) if as_json else print
    say(f"BCID {bcid} '{sch['label']}' SC{sc}: {len(mp3)} Byte, {st['seconds']:.1f} s, "
        f"RS-Korrekturen {st['rs_fixed']}, verlorene Rahmen {st['lost']}, C/N {st['cn_db']:.1f} dB")
    if sch["dynamic"]:
        say(f"  Dynamic Label: {sch['dynamic']}")
    info = dict(st, bcid=bcid, sc=sc, bytes=len(mp3), label=sch["label"], dynamic=sch["dynamic"],
                wav=None, mp3=None, sample_rate=None, channels=None, duration=None)
    if mp3_out:
        open(mp3_out, "wb").write(mp3)
        info["mp3"] = mp3_out
        say(f"  SC-Rohdaten -> {mp3_out}")
    is_audio = sch["scs"][sc - 1]["type"] == C.SC_TYPE_MPEG
    d = None
    if is_audio and (wav or not (as_json or mp3_out)):
        d = decode_pcm(mp3)
        info.update(sample_rate=d.sample_rate, channels=d.nchannels,
                    duration=len(d.samples) / d.nchannels / d.sample_rate)
        say(f"  Audio: {d.sample_rate} Hz, {d.nchannels} Kanal/Kanaele, {info['duration']:.1f} s")
    if wav and d is not None:
        save_wav(d, wav)
        info["wav"] = wav
        say(f"  -> {wav}")
    elif d is not None and not as_json and not mp3_out:
        try:
            play(d, f"BC{bcid}.{sc} {sch['label']}")
        except Exception as e:
            fn = f"bc{bcid}_sc{sc}.wav"
            save_wav(d, fn)
            say(f"  (Wiedergabe nicht moeglich: {e}) -> stattdessen {fn} geschrieben")
    if as_json:
        print("@@JSON@@ " + json.dumps(info), flush=True)
    return info


def interactive(radio):
    print("\n" + radio.listing())
    print("\nBefehle:  t <BCID>[.<SC>] | l Liste | i Info | w <BCID.SC> <datei.wav> | q Ende")
    while True:
        try:
            line = input("radio> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        cmd, *arg = line.split()
        try:
            if cmd in ("q", "quit", "exit"):
                return
            elif cmd in ("l", "list"):
                print(radio.listing())
            elif cmd in ("i", "info"):
                rx = radio.rx
                print(f"TDM-Rahmen {len(rx.starts)}, C/N {rx.cn_db:.1f} dB, Sync-Guete {rx.sync_quality:.0f}, "
                      f"belegte PRC {sum(len(c['prcs']) for c in radio.channels.values())}/{C.NPRC}")
            elif cmd in ("t", "tune") and arg:
                tune_and_play(radio, arg[0])
            elif cmd in ("w", "wav") and len(arg) == 2:
                tune_and_play(radio, arg[0], arg[1])
            else:
                print("?")
        except Exception as e:
            print("Fehler:", e)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--fmt", choices=list(ws.FORMATS))
    ap.add_argument("--sps", type=int)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--tune", help="BCID[.SC], z.B. 2.1")
    ap.add_argument("--wav", help="Audio in WAV-Datei schreiben")
    ap.add_argument("--salvage", action="store_true", help="auch Rahmen mit RS-Fehlern verwenden")
    ap.add_argument("--out-mp3", help="rohen SC-Datenstrom (MP3) in Datei schreiben")
    ap.add_argument("--json", action="store_true",
                    help="maschinenlesbar: Logs auf stderr, Ergebnis als '@@JSON@@ {...}' (fuer GUIs)")
    ap.add_argument("--cache")
    a = ap.parse_args()
    radio = Radio(a.file, a.fmt, a.sps, a.cache, logf=sys.stderr if a.json else None)
    if a.list:
        if a.json:
            print("@@JSON@@ " + json.dumps(radio.to_dict()), flush=True)
        else:
            print(radio.listing())
    elif a.tune:
        tune_and_play(radio, a.tune, a.wav, a.salvage, a.out_mp3, a.json)
    else:
        interactive(radio)


if __name__ == "__main__":
    main()
