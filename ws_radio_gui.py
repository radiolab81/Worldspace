#!/usr/bin/env python3
"""
ws_radio_gui.py - Tk-Oberflaeche fuer das virtuelle Radio (ws_radio.py)
=======================================================================

Bedienung
  1. Basisband-Datei waehlen (Format/Samples je Symbol stehen normalerweise in der
     Begleitdatei <datei>.json, sonst unter "Erweitert" einstellen).
  2. "Scannen": Die GUI ruft  ws_radio.py DATEI --list --json  auf.  Dabei wird
     demoduliert (Fortschrittsanzeige), der TDM-Rahmen synchronisiert, das
     TSCC gelesen und jeder BC dekodiert.  Das Ergebnis erscheint als Baum
     BCID -> Service Components.  Der Symbol-Cache (<datei>.syms) macht
     weitere Aufrufe schnell.
  3. Eine SC doppelt anklicken oder "Abspielen": die GUI ruft
       ws_radio.py DATEI --tune BCID.SC --wav TEMP --json
     auf und spielt die entstandene WAV-Datei ueber miniaudio ab (Pause, Suchen,
     Lautstaerke).  "WAV speichern" / "MP3 speichern" nutzen --wav / --out-mp3.

Die GUI enthaelt keine eigene Signalverarbeitung - alles Dekodieren erledigt
das Konsolenprogramm (siehe wsgui_common.py).
"""
import array
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
import wave
from tkinter import filedialog, messagebox, ttk

import wsconfig as C
import wsgui_common as G

FORMATS = ("auto", "cs8", "cs16", "cf32", "cu8")
OPEN_SWITCHES = ("MFP_HEX", "BIT0_POSITIVE", "CONV_FIRST_BIT_TO_I", "CONV_INVERT_G2", "CONV_MODE",
                 "BC_SCRAMBLE_SKIP_BITS", "PRS_OUT_LAST_STAGE", "INTERLEAVE_SCOPE", "TDM_ID_RESERVED_BITS",
                 "SERVICE_PAD_BYTE", "UNUSED_PRC_FILL", "SUPPORT_ENCRYPTION")


class Player:
    """Spielt eine WAV-Datei ueber miniaudio ab (Pause, Suchen, Lautstaerke)."""
    def __init__(self):
        self.data = None
        self.pos = 0
        self.sr = 0
        self.ch = 0
        self.paused = False
        self.volume = 1.0
        self.dev = None

    def load(self, path):
        import numpy as np
        with wave.open(path) as w:
            self.ch, self.sr = w.getnchannels(), w.getframerate()
            raw = w.readframes(w.getnframes())
        self.data = np.frombuffer(raw, np.int16).reshape(-1, self.ch)
        self.pos = 0

    @property
    def total(self):
        return 0 if self.data is None else len(self.data)

    def _gen(self):
        import numpy as np
        required = yield b""
        while True:
            n = required
            if self.paused or self.data is None or self.pos >= self.total:
                chunk = np.zeros((n, self.ch), np.int16)
            else:
                chunk = self.data[self.pos:self.pos + n]
                self.pos += len(chunk)
                if self.volume != 1.0:
                    chunk = np.clip(chunk * self.volume, -32768, 32767).astype(np.int16)
                if len(chunk) < n:
                    chunk = np.concatenate([chunk, np.zeros((n - len(chunk), self.ch), np.int16)])
            a = array.array("h")
            a.frombytes(chunk.tobytes())
            required = yield a

    def start(self):
        import miniaudio
        self.stop()
        self.paused = False
        self.dev = miniaudio.PlaybackDevice(output_format=miniaudio.SampleFormat.SIGNED16,
                                            nchannels=self.ch, sample_rate=self.sr)
        g = self._gen()
        next(g)
        self.dev.start(g)

    def stop(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

    @property
    def playing(self):
        return self.dev is not None


class RadioApp(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title("WorldSpace-Radio (virtuell)")
        G.setup_style(self)
        self.minsize(900, 700)
        self.tmp = tempfile.mkdtemp(prefix="wsradio_")
        self.runner = G.ProcRunner(self, self.on_line, self.on_done)
        self.player = Player()
        self.job = None
        self.json_result = None
        self.scan = None
        self.cur = None                 # (bcid, sc) des geladenen Tons
        self.cur_wav = None
        self.pending = None
        self.seeking = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        if path:
            self.v_file.set(path)
            self.after(200, self.do_scan)
        self.after(150, self.tick)

    # -- Aufbau ------------------------------------------------------------------------
    def _build(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Datei oeffnen ...", command=self.browse)
        fm.add_command(label="Konfiguration (offene Punkte) anzeigen ...", command=self.show_config)
        fm.add_separator()
        fm.add_command(label="Beenden", command=self.close)
        m.add_cascade(label="Datei", menu=fm)
        self.config(menu=m)

        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=3)
        root.rowconfigure(5, weight=1)

        ff = ttk.LabelFrame(root, text=" Empfangsdatei ", padding=6)
        ff.grid(row=0, column=0, sticky="ew")
        ff.columnconfigure(1, weight=1)
        self.v_file = tk.StringVar()
        self.v_fmt = tk.StringVar(value="auto")
        self.v_sps = tk.StringVar(value="auto")
        self.v_fresh = tk.BooleanVar(value=False)
        self.v_salv = tk.BooleanVar(value=False)
        ttk.Label(ff, text="Basisband-Datei:").grid(row=0, column=0, sticky="w")
        ttk.Entry(ff, textvariable=self.v_file).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(ff, text="Durchsuchen ...", command=self.browse).grid(row=0, column=2)
        self.btn_scan = ttk.Button(ff, text="Scannen", style="Big.TButton", command=self.do_scan)
        self.btn_scan.grid(row=0, column=3, padx=(8, 0))
        adv = ttk.Frame(ff)
        adv.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(adv, text="Erweitert:  Format").pack(side="left")
        ttk.Combobox(adv, textvariable=self.v_fmt, values=FORMATS, width=6, state="readonly").pack(side="left", padx=(4, 10))
        ttk.Label(adv, text="Samples/Symbol").pack(side="left")
        ttk.Combobox(adv, textvariable=self.v_sps, values=("auto", "4", "5", "6", "8"), width=6).pack(side="left", padx=(4, 10))
        ttk.Checkbutton(adv, text="Cache neu erzeugen", variable=self.v_fresh).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(adv, text="Rahmen mit RS-Fehlern trotzdem verwenden", variable=self.v_salv).pack(side="left")

        self.lbl_sig = ttk.Label(root, text="Noch nicht gescannt.", style="Hint.TLabel")
        self.lbl_sig.grid(row=1, column=0, sticky="w", pady=(4, 2))

        cf = ttk.LabelFrame(root, text=" Sender (Broadcast Channels / Service Components) ", padding=6)
        cf.grid(row=2, column=0, sticky="nsew")
        cf.columnconfigure(0, weight=1)
        cf.rowconfigure(0, weight=1)
        cols = ("rate", "type", "prc", "info")
        self.tree = ttk.Treeview(cf, columns=cols, selectmode="browse")
        self.tree.heading("#0", text="BCID / Name / Service Component")
        for c, t, w in (("rate", "kbit/s", 70), ("type", "Typ", 80), ("prc", "PRC", 60), ("info", "Zustand", 300)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("#0", width=330)
        sy = ttk.Scrollbar(cf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda e: self.play_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.lbl_det = ttk.Label(cf, text="", wraplength=840, justify="left")
        self.lbl_det.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        pf = ttk.LabelFrame(root, text=" Wiedergabe ", padding=6)
        pf.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        pf.columnconfigure(5, weight=1)
        self.btn_play = ttk.Button(pf, text="\u25B6 Abspielen", command=self.play_selected, width=14)
        self.btn_pause = ttk.Button(pf, text="\u23F8 Pause", command=self.toggle_pause, width=10, state="disabled")
        self.btn_stop = ttk.Button(pf, text="\u23F9 Stopp", command=self.stop_play, width=10, state="disabled")
        self.btn_play.grid(row=0, column=0, padx=(0, 4))
        self.btn_pause.grid(row=0, column=1, padx=4)
        self.btn_stop.grid(row=0, column=2, padx=4)
        ttk.Label(pf, text="Lautstaerke").grid(row=0, column=3, padx=(14, 4))
        self.v_vol = tk.DoubleVar(value=80)
        ttk.Scale(pf, from_=0, to=100, variable=self.v_vol, length=110,
                  command=lambda v: setattr(self.player, "volume", float(v) / 100.0)).grid(row=0, column=4)
        self.player.volume = 0.8
        self.v_pos = tk.DoubleVar(value=0)
        self.scale = ttk.Scale(pf, from_=0, to=100, variable=self.v_pos)
        self.scale.grid(row=0, column=5, sticky="ew", padx=(14, 6))
        self.scale.bind("<ButtonPress-1>", lambda e: setattr(self, "seeking", True))
        self.scale.bind("<ButtonRelease-1>", self.seek_release)
        self.lbl_time = ttk.Label(pf, text="0:00 / 0:00", width=12)
        self.lbl_time.grid(row=0, column=6)
        sv = ttk.Frame(pf)
        sv.grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))
        self.btn_wav = ttk.Button(sv, text="WAV speichern ...", command=self.save_wav)
        self.btn_mp3 = ttk.Button(sv, text="SC-Rohdaten (MP3) speichern ...", command=self.save_mp3)
        self.btn_wav.pack(side="left", padx=(0, 6))
        self.btn_mp3.pack(side="left")

        self.pb = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.pb.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.log = G.LogView(root, height=7)
        self.log.grid(row=5, column=0, sticky="nsew", pady=(4, 0))
        self.lbl_status = ttk.Label(root, text="Bereit.", style="Hint.TLabel")
        self.lbl_status.grid(row=6, column=0, sticky="w")
        self.set_busy(False)

    # -- Zustand ---------------------------------------------------------------------------
    def set_busy(self, busy):
        st = "disabled" if busy else "normal"
        for b in (self.btn_scan, self.btn_play, self.btn_wav, self.btn_mp3):
            b.config(state=st)

    def status(self, t):
        self.lbl_status.config(text=t)

    def browse(self):
        p = filedialog.askopenfilename(parent=self, title="Basisband-Datei", filetypes=[
            ("Basisband", "*.cs8 *.cs16 *.cf32 *.cu8"), ("Alle Dateien", "*.*")])
        if p:
            self.v_file.set(p)

    # -- Kommandozeilen -----------------------------------------------------------------------
    def base_args(self):
        f = self.v_file.get().strip()
        if not os.path.isfile(f):
            raise ValueError("Bitte zuerst eine existierende Basisband-Datei waehlen.")
        a = [f]
        if self.v_fmt.get() != "auto":
            a += ["--fmt", self.v_fmt.get()]
        if self.v_sps.get().strip() not in ("", "auto"):
            a += ["--sps", self.v_sps.get().strip()]
        return a

    def run(self, job, extra):
        try:
            args = self.base_args() + extra
        except ValueError as e:
            messagebox.showerror("Datei", str(e), parent=self)
            return False
        cmd = G.py_cmd("ws_radio.py", *args)
        self.job, self.json_result = job, None
        self.log.feed("$ " + G.cmd_to_text(cmd), "\n", "cmd")
        self.set_busy(True)
        self.pb.config(mode="indeterminate")
        self.pb.start(12)
        self.runner.start(cmd)
        return True

    def on_line(self, text, kind):
        if text.startswith(G.JSON_TAG):
            try:
                self.json_result = json.loads(text[len(G.JSON_TAG):])
            except ValueError:
                pass
            self.log.feed("(Ergebnis empfangen)", "\n", "ok")
            return
        if "QPSK-Demodulation" in text:
            try:
                pct = float(text.split("Demodulation")[1].strip().rstrip("%").strip())
                if str(self.pb.cget("mode")) != "determinate":
                    self.pb.stop()
                    self.pb.config(mode="determinate")
                self.pb.config(value=pct)
                self.status(f"Demodulation {pct:.0f} %")
            except ValueError:
                pass
        self.log.feed(text, kind)

    def on_done(self, rc):
        self.pb.stop()
        self.pb.config(mode="determinate", value=100 if rc == 0 else 0)
        self.set_busy(False)
        job = self.job
        if rc != 0:
            self.status("Abgebrochen." if rc == -1 else f"Fehler (Code {rc}) - siehe Protokoll.")
            if rc != -1:
                messagebox.showerror("ws_radio.py", f"Das Konsolenprogramm meldete einen Fehler (Code {rc}).\n"
                                                    "Details stehen im Protokoll.", parent=self)
            return
        r = self.json_result
        if job == "scan":
            if r is None:
                self.status("Scan ohne Ergebnis.")
                return
            self.scan = r
            self.fill_tree()
        elif job in ("play", "wav", "mp3"):
            self.after_tune(job, r)

    # -- Scan ------------------------------------------------------------------------------------
    def do_scan(self):
        if self.runner.running:
            return
        self.stop_play()
        f = self.v_file.get().strip()
        if self.v_fresh.get() and f:
            for ext in (".syms", ".syms.meta"):
                try:
                    os.remove(f + ext)
                except OSError:
                    pass
        self.tree.delete(*self.tree.get_children())
        self.scan, self.cur, self.cur_wav = None, None, None
        self.lbl_det.config(text="")
        if self.run("scan", ["--list", "--json"]):
            self.status("Scanne ...")

    def fill_tree(self):
        s = self.scan
        self.lbl_sig.config(text=(
            f"C/N ~ {s['cn_db']:.1f} dB   |   TDM-Rahmen {s['tdm_frames']} (TSCC ok: {s['tscc_ok']})   |   "
            f"Sync-Guete {s['sync_quality']:.0f}   |   belegte PRC {sum(c['n'] for c in s['channels'])}/{C.NPRC}"
            + ("   |   Q invertiert" if s["q_inverted"] else "")))
        for ch in s["channels"]:
            ok = f"{ch['good']}/{ch['frames']} Rahmen ok"
            tot = sum(sc["rate_kbps"] for sc in ch["scs"])
            self.tree.insert("", "end", iid=str(ch["bcid"]), open=True,
                             text=f"BCID {ch['bcid']}   {ch['label'] or '(nicht dekodierbar)'}",
                             values=(tot, "", ch["n"], ok))
            for sc in ch["scs"]:
                info = "verschluesselt (WES)" if sc["encrypted"] else ("Vorlauf/ungueltig" if sc["type"] == 15 else "")
                self.tree.insert(str(ch["bcid"]), "end", iid=f"{ch['bcid']}.{sc['index']}",
                                 text=f"   SC{sc['index']}", values=(sc["rate_kbps"], sc["type_name"], "", info))
        if not s["channels"]:
            self.status("Keine Sender gefunden.")
        else:
            self.status(f"{len(s['channels'])} Sender gefunden. SC doppelt anklicken zum Abspielen.")
            first = s["channels"][0]
            if first["scs"]:
                self.tree.selection_set(f"{first['bcid']}.1")

    def selected_sc(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if "." not in iid:                      # BC gewaehlt -> erste MPEG-SC
            ch = next((c for c in self.scan["channels"] if str(c["bcid"]) == iid), None) if self.scan else None
            if not ch:
                return None
            sc = next((s for s in ch["scs"] if s["type"] == 0), ch["scs"][0] if ch["scs"] else None)
            return (ch["bcid"], sc["index"]) if sc else None
        b, i = iid.split(".")
        return int(b), int(i)

    def show_details(self):
        sel = self.selected_sc()
        if not sel or not self.scan:
            return
        ch = next(c for c in self.scan["channels"] if c["bcid"] == sel[0])
        sc = next(s for s in ch["scs"] if s["index"] == sel[1])
        self.lbl_det.config(text=(
            f"Sender '{ch['label']}' (BCID {ch['bcid']}, PRC {ch['prcs']}) - SC{sc['index']}: {sc['rate_kbps']} kbit/s, "
            f"Typ {sc['type_name']}, Sprache {sc['language']}, Programmtyp {sc['ptype']}\n"
            f"Dynamic Label: {ch['dynamic'] or '-'}"))

    # -- Abspielen / Speichern -----------------------------------------------------------------------
    def check_audio_sc(self):
        sel = self.selected_sc()
        if not sel or not self.scan:
            messagebox.showinfo("Auswahl", "Bitte zuerst scannen und eine Service Component waehlen.", parent=self)
            return None
        ch = next(c for c in self.scan["channels"] if c["bcid"] == sel[0])
        sc = next(s for s in ch["scs"] if s["index"] == sel[1])
        if sc["encrypted"]:
            messagebox.showwarning("Verschluesselt", "Diese SC ist verschluesselt (WES) - nicht unterstuetzt.", parent=self)
            return None
        return sel, sc

    def salvage(self):
        return ["--salvage"] if self.v_salv.get() else []

    def play_selected(self):
        if self.runner.running:
            return
        r = self.check_audio_sc()
        if not r:
            return
        (bcid, idx), sc = r
        if sc["type"] != 0:
            messagebox.showinfo("Kein Audio", f"SC{idx} ist vom Typ {sc['type_name']} - kein MPEG-Audio.\n"
                                "Die Rohdaten koennen mit 'SC-Rohdaten speichern' gesichert werden.", parent=self)
            return
        self.stop_play()
        wav = os.path.join(self.tmp, f"bc{bcid}_sc{idx}.wav")
        if self.cur == (bcid, idx) and self.cur_wav and os.path.exists(self.cur_wav):
            self.start_player(self.cur_wav)
            return
        if self.run("play", ["--tune", f"{bcid}.{idx}", "--wav", wav, "--json"] + self.salvage()):
            self.pending = (bcid, idx, wav)
            self.status(f"Dekodiere BCID {bcid} / SC{idx} ...")

    def save_wav(self):
        r = self.check_audio_sc()
        if not r:
            return
        (bcid, idx), sc = r
        if sc["type"] != 0:
            messagebox.showinfo("Kein Audio", "Diese SC ist kein MPEG-Audio.", parent=self)
            return
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".wav", initialfile=f"bc{bcid}_sc{idx}.wav",
                                         filetypes=[("WAV", "*.wav")])
        if not p:
            return
        if self.cur == (bcid, idx) and self.cur_wav and os.path.exists(self.cur_wav):
            shutil.copyfile(self.cur_wav, p)
            self.status(f"WAV gespeichert: {p}")
            return
        if self.run("wav", ["--tune", f"{bcid}.{idx}", "--wav", p, "--json"] + self.salvage()):
            self.pending = (bcid, idx, p)
            self.status("Dekodiere und speichere WAV ...")

    def save_mp3(self):
        r = self.check_audio_sc()
        if not r:
            return
        (bcid, idx), sc = r
        ext = ".mp3" if sc["type"] == 0 else ".bin"
        p = filedialog.asksaveasfilename(parent=self, defaultextension=ext, initialfile=f"bc{bcid}_sc{idx}{ext}")
        if not p:
            return
        if self.run("mp3", ["--tune", f"{bcid}.{idx}", "--out-mp3", p, "--json"] + self.salvage()):
            self.pending = (bcid, idx, p)
            self.status("Dekodiere und speichere SC-Rohdaten ...")

    def after_tune(self, job, r):
        if not r:
            self.status("Kein Ergebnis vom Dekoder.")
            return
        bcid, idx, path = self.pending
        info = (f"{r['seconds']:.1f} s, RS-Korrekturen {r['rs_fixed']}, verlorene Rahmen {r['lost']}, "
                f"C/N {r['cn_db']:.1f} dB")
        if job == "play":
            self.cur, self.cur_wav = (bcid, idx), path
            self.status(f"BC{bcid}.{idx} '{r['label']}': {info}")
            self.start_player(path)
        elif job == "wav":
            self.status(f"WAV gespeichert: {path} ({info})")
        else:
            self.status(f"SC-Rohdaten gespeichert: {path} ({info})")

    # -- Player -------------------------------------------------------------------------------------------
    def start_player(self, wav):
        try:
            self.player.load(wav)
            self.player.start()
        except ImportError:
            messagebox.showwarning("miniaudio fehlt", "Zum Abspielen wird das Paket 'miniaudio' benoetigt\n"
                                   "(pip install miniaudio).  Die WAV-Datei kann gespeichert werden.", parent=self)
            return
        except Exception as e:
            self.log.feed(f"Wiedergabe nicht moeglich: {e}", "\n", "err")
            messagebox.showwarning("Wiedergabe", f"Kein Audiogeraet nutzbar ({e}).\n"
                                   "Der Ton kann stattdessen als WAV gespeichert werden.", parent=self)
            return
        self.btn_pause.config(state="normal", text="\u23F8 Pause")
        self.btn_stop.config(state="normal")

    def toggle_pause(self):
        if not self.player.playing:
            return
        self.player.paused = not self.player.paused
        self.btn_pause.config(text="\u25B6 Weiter" if self.player.paused else "\u23F8 Pause")

    def stop_play(self):
        self.player.stop()
        self.player.pos = 0
        self.btn_pause.config(state="disabled", text="\u23F8 Pause")
        self.btn_stop.config(state="disabled")
        self.v_pos.set(0)
        self.lbl_time.config(text="0:00 / 0:00")

    def seek_release(self, e):
        if self.player.total:
            self.player.pos = int(self.v_pos.get() / 100.0 * self.player.total)
        self.seeking = False

    @staticmethod
    def fmt_t(s):
        s = int(s)
        return f"{s // 60}:{s % 60:02d}"

    def tick(self):
        p = self.player
        if p.playing and p.total:
            if not self.seeking:
                self.v_pos.set(100.0 * p.pos / p.total)
            self.lbl_time.config(text=f"{self.fmt_t(p.pos / p.sr)} / {self.fmt_t(p.total / p.sr)}")
            if p.pos >= p.total and not p.paused:
                p.stop()
                p.pos = 0
                self.btn_pause.config(state="disabled", text="\u23F8 Pause")
                self.btn_stop.config(state="disabled")
                self.v_pos.set(0)
                self.status("Wiedergabe beendet.")
        self.after(150, self.tick)

    # -- Konfiguration -----------------------------------------------------------------------------------------
    def show_config(self):
        w = tk.Toplevel(self)
        w.title("Konfiguration - offene Punkte (wsconfig.py)")
        w.transient(self)
        tv = ttk.Treeview(w, columns=("val",), height=len(OPEN_SWITCHES))
        tv.heading("#0", text="Schalter")
        tv.heading("val", text="Aktueller Wert")
        tv.column("#0", width=260)
        tv.column("val", width=260)
        for k in OPEN_SWITCHES:
            v = C.mfp_hex() if k == "MFP_HEX" else getattr(C, k)
            tv.insert("", "end", text=k + (" (aktiv: Platzhalter)" if k == "MFP_HEX" and not C.MFP_HEX else ""),
                      values=(v,))
        tv.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Label(w, style="Hint.TLabel", wraplength=540, justify="left", text=(
            "Diese Werte steuern Encoder UND Decoder und stehen in wsconfig.py (mit Quellenangaben). "
            "Zum Decodieren einer echten Aufnahme dort anpassen (v.a. MFP_HEX) und danach mit "
            "'Cache neu erzeugen' erneut scannen.")).pack(padx=8, pady=(0, 8))
        G.center(w, self)

    def close(self):
        self.runner.stop()
        self.player.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.destroy()


def main():
    app = RadioApp(sys.argv[1] if len(sys.argv) > 1 else None)
    app.mainloop()


if __name__ == "__main__":
    main()
