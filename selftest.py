#!/usr/bin/env python3
"""selftest.py - Ende-zu-Ende-Test: Encoder -> (gestoerter) Kanal -> Radio, Bitvergleich.

Prueft, dass jeder dekodierte SC-Strom ein lueckenloser Ausschnitt des
Quell-MP3-Stroms im 432-ms-Raster ist (Vergleich Byte fuer Byte)."""
import os, subprocess, sys
import wsconfig as C, wscore as ws
from ws_radio import Radio

HERE = os.path.dirname(os.path.abspath(__file__)); T = os.path.join(HERE, "test"); os.makedirs(T, exist_ok=True)

def mk(name, freq, sr, ch, br):
    p = os.path.join(T, name)
    if not os.path.exists(p):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=f={freq}:r={sr}:d=6",
                        "-ac", str(ch), "-codec:a", "libmp3lame", "-b:a", f"{br}k", "-write_xing", "0",
                        "-id3v2_version", "0", p], check=True)
    return p

A = mk("jazz_440.mp3", 440, 48000, 2, 64); B = mk("news_1000.mp3", 1000, 24000, 1, 32)
Cc = mk("weather_2000.mp3", 2000, 16000, 1, 24)
SCEN = [("sauber", []), ("C/N 9 dB", ["--cn", "9"]),
        ("CFO +25 kHz, Phase 37 Grad", ["--cfo", "25000", "--phase", "37"]),
        ("Takt-Offset 0.37 Symbol", ["--timing", "0.37"]),
        ("C/N 7 dB, CFO -18 kHz, Phase 120, Takt 0.5", ["--cn", "7", "--cfo", "-18000", "--phase", "120", "--timing", "0.5"]),
        ("C/N 5 dB, CFO 8 kHz", ["--cn", "5", "--cfo", "8000"]),
        ("C/N 4 dB (Grenzbereich)", ["--cn", "4"])]
srcs = {(1, 1): A, (2, 1): B, (2, 2): Cc}
allok = True
for name, extra in SCEN:
    out = os.path.join(T, "st.cs8")
    subprocess.run([sys.executable, os.path.join(HERE, "ws_encode.py"), "-o", out, "--bc", f"Jazz:{A}",
                    "--bc", f"Talk:{B},{Cc}"] + extra, check=True, capture_output=True)
    for f in (out + ".syms", out + ".syms.meta"):
        if os.path.exists(f): os.remove(f)
    try:
        r = Radio(out, quiet=True); res = []
        for (bc, sc), src in srcs.items():
            mp3, st = r.tune(bc, sc)
            _, chunks = ws.mp3_bc_chunks(open(src, "rb").read()); raw = b"".join(chunks)
            ok = len(mp3) > 0.8 * len(raw) and raw.find(mp3[:3456 if bc == 1 else 1296]) >= 0 and \
                 mp3 == raw[raw.find(mp3[:1296]):][:len(mp3)]
            res.append((bc, sc, ok, st["lost"], st["rs_fixed"]))
        good = all(x[2] for x in res)
        print(f"{'OK  ' if good else 'FAIL'} {name:<46} C/N~{r.rx.cn_db:5.1f} dB  " +
              "  ".join(f"BC{b}.{s}:{'ok' if o else 'FEHLER'}(RS-fix {fx}, lost {lo})" for b, s, o, lo, fx in res))
        allok &= good
    except Exception as e:
        print(f"FAIL {name:<46} {type(e).__name__}: {e}"); allok = False
print("\nGESAMT:", "bestanden" if allok else "FEHLER"); sys.exit(0 if allok else 1)
