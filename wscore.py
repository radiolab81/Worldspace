"""
wscore.py - Kernbibliothek: WorldSpace-artiger Satelliten-Hoerfunk (Basisband)
=============================================================================

Dieses Modul bildet die komplette Signalkette in Senderrichtung (Encoder) und
Empfaengerrichtung (Decoder) nach.  Es ist so geschrieben, dass man es wie ein
kleines Lehrbuch von oben nach unten lesen kann.  Die Abschnitte folgen dem
Weg eines Audiosignals:

   Sender  (Broadcast-Station + Satellit)            Empfaenger (Radio)
   ------------------------------------------        ------------------------
   1  Bit-Werkzeuge                                   <- gleiche Werkzeuge
   2  Reed-Solomon-Code (aeusserer Code)
   3  Pseudozufallsfolgen (Scrambler)
   4  Faltungscode + Viterbi (innerer Code)
   5  Symbole (QPSK-Zuordnung)
   6  Service-Ebene: SC -> SCH + Multiplex           (SC-Demux)
   7  Transport-Ebene BC: Scramble+RS+Interleave+Conv (Viterbi+RS+Descramble)
   8  TSCC (Zeitschlitz-Steuerkanal)                  (TSCC-Decoder)
   9  PRC-Rahmen und TDM-Rahmen                       (TDM-Demux, PRC-Sync)
  10  PHY Sender: RRC-Filter, Stoerungen, Datei
  11  PHY Empfaenger: Matched Filter, Takt, Traeger
  12  TDM-Empfaenger: Rahmensync, Lineup, BC-Extraktion
  13  MP3-Hilfen (Frame-Raster 432 ms)

Quellenkuerzel stehen in wsconfig.py ([DS], [PAT], [ITU]).

GROSSES BILD  (aus [PAT Fig.2], [ITU "System overview"])
--------------------------------------------------------
Mehrere Audio-Service-Components (SC, MP3) bilden zusammen mit einem
Service Control Header (SCH) einen "Broadcast Channel" (BC).  Der BC wird in
432-ms-Rahmen gepackt, gegen Fehler geschuetzt (Scrambler, Reed-Solomon,
Interleaver, Faltungscode) und dann Symbol fuer Symbol auf n "Prime Rate
Channels" (PRC) verteilt (n = 1..8, je 16 kbit/s).  Der Satellit mischt 96 PRC
in einen Zeitmultiplex-Traeger (TDM) mit 1,84 MSym/s, stellt einen
Master-Frame-Preamble (MFP, Synchronisation) und einen Time-Slot-Control-
Channel (TSCC, "Wer sitzt auf welchem PRC?") voran und scrambelt alles.
Das Radio synchronisiert auf den MFP, liest den TSCC, holt die PRC seines BC
heraus, synchronisiert sie ueber die PRC-Preamble und kehrt alle Schritte um.
"""
import math

import numpy as np
from scipy import signal

import wsconfig as C

try:                                   # optionaler Beschleuniger fuer Viterbi
    from numba import njit
    HAVE_NUMBA = True
except Exception:                      # pragma: no cover
    HAVE_NUMBA = False

SQRT2 = math.sqrt(2.0)


# ===========================================================================
# 1. BIT-WERKZEUGE
# ===========================================================================
# Alle Bitfolgen werden "MSB zuerst" behandelt: das hoechstwertige Bit eines
# Bytes/Feldes wird zuerst uebertragen.  [DS 8.2 Fig.7: "most significant bit
# is transmitted first"], [PAT Sp.30: Preamble "MSB zuerst"].

def int_to_bits(value, nbits):
    """Ganzzahl -> Array aus nbits Bits (MSB zuerst)."""
    return np.array([(value >> (nbits - 1 - i)) & 1 for i in range(nbits)], np.uint8)


def bytes_to_bits(data):
    return np.unpackbits(np.frombuffer(bytes(data), np.uint8)) if not isinstance(data, np.ndarray) \
        else np.unpackbits(data.astype(np.uint8))


def bits_to_bytes(bits):
    return np.packbits(np.asarray(bits, np.uint8))


class BitWriter:
    """Schreibt Felder beliebiger Bitbreite hintereinander (fuer den SCH)."""
    def __init__(self):
        self.bits = []

    def put(self, value, nbits):
        assert 0 <= value < (1 << nbits), f"Wert {value} passt nicht in {nbits} Bit"
        self.bits.extend(int_to_bits(value, nbits).tolist())

    def put_bytes(self, data):
        for b in data:
            self.put(b, 8)

    def to_bytes(self):
        assert len(self.bits) % 8 == 0
        return bytes(bits_to_bytes(self.bits))


class BitReader:
    def __init__(self, data):
        self.bits = bytes_to_bits(data)
        self.pos = 0

    def get(self, nbits):
        v = 0
        for b in self.bits[self.pos:self.pos + nbits]:
            v = (v << 1) | int(b)
        self.pos += nbits
        return v

    def get_bytes(self, n):
        return bytes(self.get(8) for _ in range(n))


# ===========================================================================
# 2. REED-SOLOMON-CODE (aeusserer Code)
# ===========================================================================
# THEORIE: Ein RS(255,223)-Code arbeitet mit 8-Bit-Symbolen im Galoisfeld
# GF(256).  223 Informationsbytes werden um 32 Pruefbytes ergaenzt; der
# Decoder korrigiert bis zu t = 16 falsche Bytes je Block (32/2) und meldet
# schlimmere Bloecke als unkorrigierbar.  Der Code ist "systematisch": die
# Informationsbytes stehen unveraendert im Codewort.
# QUELLEN: [DS 6] "Reed Solomon Decoder", [PAT Sp.29].
#   Broadcast Channel: GF-Polynom x^8+x^4+x^3+x^2+1 (0x11D), g(x)=prod_{j=0}^{31}(x-a^j)
#   TSCC             : GF-Polynom x^8+x^7+x^2+x+1  (0x187), g(x)=prod_{j=112}^{143}(x-a^(11j))
# Das erste Byte in der Zeit gehoert zur hoechsten Potenz x^254 [PAT Sp.29].

class RS:
    def __init__(self, prim, fcr, step, nroots=C.RS_NR):
        self.nr, self.fcr, self.step = nroots, fcr, step
        exp = np.zeros(512, np.int32)
        log = np.zeros(256, np.int32)
        x = 1
        for i in range(255):            # alpha^i durch wiederholtes Verdoppeln
            exp[i] = x
            log[x] = i
            x <<= 1
            if x & 0x100:
                x ^= prim               # Reduktion modulo Feldpolynom
        assert x == 1, "Polynom nicht primitiv"
        exp[255:510] = exp[:255]
        self.exp, self.log = exp, log
        a = np.arange(256)
        mt = exp[(log[a][:, None] + log[a][None, :])].astype(np.uint8)
        mt[0, :] = 0
        mt[:, 0] = 0
        self.mt = mt                    # vollstaendige Multiplikationstabelle
        g = [1]                         # Generatorpolynom ausmultiplizieren
        for i in range(nroots):
            root = int(exp[(step * (fcr + i)) % 255])
            ng = [0] * (len(g) + 1)
            for j, c in enumerate(g):
                ng[j] ^= c
                ng[j + 1] ^= self.gm(c, root)
            g = ng
        self.gen = np.array(g[1:], np.uint8)
        self.roots = np.array([exp[(step * (fcr + i)) % 255] for i in range(nroots)], np.uint8)

    def gm(self, a, b):
        return 0 if (a == 0 or b == 0) else int(self.exp[self.log[a] + self.log[b]])

    def gdiv(self, a, b):
        return 0 if a == 0 else int(self.exp[(self.log[a] - self.log[b]) % 255])

    def bpow(self, e):
        return int(self.exp[(self.step * e) % 255])

    def encode(self, data):
        """Systematische Codierung: data (nb,k) -> Paritaet (nb,32).
        Polynomdivision (Schieberegister, LFSR) durch das Generatorpolynom."""
        data = np.asarray(data, np.uint8)
        nb, k = data.shape
        par = np.zeros((nb, self.nr), np.uint8)
        for i in range(k):
            fb = data[:, i] ^ par[:, 0]
            par = np.concatenate([par[:, 1:], np.zeros((nb, 1), np.uint8)], 1)
            par ^= self.mt[fb[:, None], self.gen[None, :]]
        return par

    def syndromes(self, blocks):
        """Syndrome S_i = r(a^i): alle Null <=> gueltiges Codewort."""
        s = np.zeros((blocks.shape[0], self.nr), np.uint8)
        for j in range(blocks.shape[1]):             # Horner-Schema
            s = self.mt[s, self.roots[None, :]] ^ blocks[:, j, None]
        return s

    def decode(self, blocks):
        """blocks (nb,255) -> (korrigierte Bloecke, Fehlerzahl; -1 = unkorrigierbar)."""
        blocks = np.array(blocks, np.uint8)
        S = self.syndromes(blocks)
        nerr = np.zeros(len(blocks), np.int32)
        for b in np.nonzero(S.any(axis=1))[0]:
            res = self._decode_one(blocks[b], [int(v) for v in S[b]])
            if res is None:
                nerr[b] = -1
            else:
                blocks[b], nerr[b] = res
        return blocks, nerr

    def _decode_one(self, r, S):
        """Berlekamp-Massey (Fehlerort-Polynom) + Chien-Suche + Forney (Werte)."""
        nr = self.nr
        C_, B, L, m, bb = [1], [1], 0, 1, 1
        for n in range(nr):                           # --- Berlekamp-Massey
            d = S[n]
            for i in range(1, L + 1):
                if i < len(C_):
                    d ^= self.gm(C_[i], S[n - i])
            if d == 0:
                m += 1
                continue
            T = C_[:]
            coef = self.gdiv(d, bb)
            need = len(B) + m
            if len(C_) < need:
                C_ = C_ + [0] * (need - len(C_))
            for i, bv in enumerate(B):
                C_[i + m] ^= self.gm(coef, bv)
            if 2 * L <= n:
                L, B, bb, m = n + 1 - L, T, d, 1
            else:
                m += 1
        C_ = C_[:L + 1] + [0] * max(0, L + 1 - len(C_))
        if L == 0 or L > nr // 2:
            return None
        pos = []                                      # --- Chien-Suche
        for j in range(C.RS_N):
            xinv = (-self.step * j) % 255
            v = 0
            for i, c in enumerate(C_):
                if c:
                    v ^= int(self.exp[(self.log[c] + xinv * i) % 255])
            if v == 0:
                pos.append(j)
        if len(pos) != L:
            return None
        omega = []                                    # --- Forney
        for k in range(L):
            v = 0
            for i in range(k + 1):
                if i < len(S) and (k - i) < len(C_):
                    v ^= self.gm(S[i], C_[k - i])
            omega.append(v)
        out = r.copy()
        for j in pos:
            xinv = (-self.step * j) % 255
            num = 0
            for k, c in enumerate(omega):
                if c:
                    num ^= int(self.exp[(self.log[c] + xinv * k) % 255])
            den = 0
            for i in range(1, len(C_), 2):
                if C_[i]:
                    den ^= int(self.exp[(self.log[C_[i]] + xinv * (i - 1)) % 255])
            if den == 0:
                return None
            e = self.gm(self.gdiv(num, den), self.bpow(j * (1 - self.fcr)))
            out[C.RS_N - 1 - j] ^= e
        if self.syndromes(out[None, :]).any():
            return None
        return out, L


RS_BC = RS(0x11D, 0, 1)          # [DS 6.2]
RS_TSCC = RS(0x187, 112, 11)     # [DS 6.1]


# ===========================================================================
# 3. PSEUDOZUFALLSFOLGEN (SCRAMBLER / ENERGY DISPERSAL)
# ===========================================================================
# THEORIE: Lange Folgen gleicher Bits wuerden das Spektrum und die Taktrueck-
# gewinnung stoeren.  Ein linear rueckgekoppeltes Schieberegister (LFSR)
# erzeugt eine reproduzierbare Pseudozufallsfolge, die bitweise per XOR auf die
# Daten addiert wird.  Der Empfaenger addiert dieselbe Folge noch einmal und
# stellt so die Daten wieder her (x XOR p XOR p = x).
#   BC-Scrambler : x^9 + x^5 + 1, Start 111111111 am Rahmenbeginn
#                  [DS 6.3][PAT Fig.16, Sp.28]
#   TDM-Scrambler: x^11 + x^2 + 1, Start 11111111111 direkt hinter dem MFP,
#                  je Symbol zwei Folgenbits (erst I, dann Q)
#                  [PAT Fig.27, Sp.34]

def prs_sequence(nstages, tap, length):
    """Folge des LFSR x^nstages + x^tap + 1 (maximale Periode 2^n - 1)."""
    s = [1] * nstages
    period = (1 << nstages) - 1
    out = []
    for _ in range(period):
        fb = s[-1] ^ s[tap - 1]
        out.append(s[-1] if C.PRS_OUT_LAST_STAGE else fb)
        s = [fb] + s[:-1]
    arr = np.array(out, np.uint8)
    return np.tile(arr, -(-length // period))[:length]


BC_PRS = prs_sequence(9, 5, C.MAX_PRI * C.PRI_BITS)
_tdm_prs = prs_sequence(11, 2, 2 * C.BODY_SYMS)
TDM_SI = (1 - 2.0 * _tdm_prs[0::2]).astype(np.float32)    # Vorzeichen I
TDM_SQ = (1 - 2.0 * _tdm_prs[1::2]).astype(np.float32)    # Vorzeichen Q


def bc_scramble(frame_bytes):
    """Scrambling/Descrambling eines BC-Rahmens (selbstinvers)."""
    bits = bytes_to_bits(frame_bytes).copy()
    skip = C.BC_SCRAMBLE_SKIP_BITS
    bits[skip:] ^= BC_PRS[:len(bits) - skip]
    return bytes(bits_to_bytes(bits))


# ===========================================================================
# 4. FALTUNGSCODE (K=7, R=1/2) UND VITERBI-DECODER (innerer Code)
# ===========================================================================
# THEORIE: Ein Faltungscodierer schiebt jedes Infobit durch ein 6-stufiges
# Register und bildet daraus mit zwei Generatorpolynomen zwei Codebits
# (Rate 1/2).  Der Viterbi-Algorithmus findet im Zustandsdiagramm (64 Zustaende)
# die wahrscheinlichste gesendete Folge ("Soft Decision": er rechnet mit den
# analogen I/Q-Werten statt mit harten Bits und gewinnt so ca. 2 dB).
# QUELLEN: Polynome 171/133 oktal [DS 5][PAT Fig.21: g1=1111001, g2=1011011].
# Tiefe 64 Bit, Latenz 256 Bit im STA002 [DS 5]; hier: ganze Rahmen auf einmal.

def conv_encode(bits, hist=None):
    """Infobits -> (x, y, neue_Historie).  x gehoert zu g1, y zu g2.
    hist = die 6 zuletzt codierten Infobits (aeltestes zuerst); None = Zustand 0."""
    u = np.asarray(bits, np.uint8)
    h = np.zeros(6, np.uint8) if hist is None else np.asarray(hist, np.uint8)
    ue = np.concatenate([h, u])
    L = len(u)

    def tap(d):                         # u[n-d]
        return ue[6 - d:6 - d + L]
    # g1 = 1111001b -> Verzoegerungen 0,1,2,3,6 ; g2 = 1011011b -> 0,2,3,5,6
    x = tap(0) ^ tap(1) ^ tap(2) ^ tap(3) ^ tap(6)
    y = tap(0) ^ tap(2) ^ tap(3) ^ tap(5) ^ tap(6)
    return x, y, ue[-6:].copy()


def pair_codes(x, y):
    """Codebitpaar -> 2-Bit-Symbolcode (bit0 = I, bit1 = Q) gemaess Schaltern
    CONV_INVERT_G2 / CONV_FIRST_BIT_TO_I (siehe wsconfig, Abschnitt 4)."""
    g2 = (y ^ 1) if C.CONV_INVERT_G2 else y
    bi, bq = (x, g2) if C.CONV_FIRST_BIT_TO_I else (g2, x)
    return (bi | (bq << 1)).astype(np.uint8)


def soft_for_viterbi(z):
    """Kehrt die Konstellations-Schalter um: empfangene komplexe Symbole ->
    komplexe 'Weichwerte' (Re=g1, Im=g2; positiv = Bit 0)."""
    z = np.asarray(z)
    si, sq = z.real, z.imag
    if not C.BIT0_POSITIVE:
        si, sq = -si, -sq
    x, y = (si, sq) if C.CONV_FIRST_BIT_TO_I else (sq, si)
    if C.CONV_INVERT_G2:
        y = -y
    return (x + 1j * y).astype(np.complex64)


def _trellis():
    """Vorgaenger-Tabellen des 64-Zustands-Trellis (Zustand = letzte 6 Bits)."""
    p0 = np.zeros(64, np.int64); p1 = p0.copy(); i0 = p0.copy(); i1 = p0.copy()
    par = lambda v: bin(v).count("1") & 1
    for ns in range(64):
        u = ns >> 5
        for b, (P, I) in enumerate(((p0, i0), (p1, i1))):
            p = ((ns & 31) << 1) | b
            reg = (u << 6) | p
            P[ns] = p
            I[ns] = 2 * par(reg & C.CONV_G1) + par(reg & C.CONV_G2)
    return p0, p1, i0, i1


_P0, _P1, _I0, _I1 = _trellis()

if HAVE_NUMBA:
    @njit(cache=True)
    def _vit_nb(si, sq, p0, p1, i0, i1, terminated):
        T = si.shape[0]
        pm = np.full(64, -1e9)
        pm[0] = 0.0                        # Start im Zustand 0
        dec = np.empty((T, 64), np.uint8)
        npm = np.empty(64)
        bm = np.empty(4)
        for t in range(T):
            a = si[t]
            b = sq[t]
            bm[0] = a + b                  # Korrelation mit (x,y) = (0,0)
            bm[1] = a - b                  # (0,1)
            bm[2] = -a + b                 # (1,0)
            bm[3] = -a - b                 # (1,1)
            mx = -1e300
            for ns in range(64):           # Add-Compare-Select
                m0 = pm[p0[ns]] + bm[i0[ns]]
                m1 = pm[p1[ns]] + bm[i1[ns]]
                if m1 > m0:
                    npm[ns] = m1
                    dec[t, ns] = 1
                else:
                    npm[ns] = m0
                    dec[t, ns] = 0
                if npm[ns] > mx:
                    mx = npm[ns]
            for ns in range(64):
                pm[ns] = npm[ns] - mx
        state = 0
        if not terminated:                 # freies Ende: bester Pfad
            best = -1e300
            for s in range(64):
                if pm[s] > best:
                    best = pm[s]
                    state = s
        out = np.empty(T, np.uint8)
        for t in range(T - 1, -1, -1):     # Traceback
            out[t] = state >> 5
            state = ((state & 31) << 1) | dec[t, state]
        return out


def _vit_np(soft, terminated):
    """Reiner numpy-Fallback (Stapel ueber die erste Achse), deutlich langsamer."""
    S, T = soft.shape
    a = soft.real.astype(np.float32)
    b = soft.imag.astype(np.float32)
    pm = np.full((S, 64), -1e9, np.float32)
    pm[:, 0] = 0
    dec = np.empty((T, S, 8), np.uint8)
    for t in range(T):
        at, bt = a[:, t:t + 1], b[:, t:t + 1]
        bm = np.concatenate([at + bt, at - bt, -at + bt, -at - bt], 1)
        m0 = pm[:, _P0] + bm[:, _I0]
        m1 = pm[:, _P1] + bm[:, _I1]
        ch = m1 > m0
        pm = np.where(ch, m1, m0)
        pm -= pm.max(axis=1, keepdims=True)
        dec[t] = np.packbits(ch, axis=1)
    out = np.empty((S, T), np.uint8)
    state = np.zeros(S, np.int64) if terminated else pm.argmax(axis=1).astype(np.int64)
    ar = np.arange(S)
    for t in range(T - 1, -1, -1):
        out[:, t] = state >> 5
        bit = (dec[t, ar, state >> 3] >> (7 - (state & 7))) & 1
        state = ((state & 31) << 1) | bit
    return out


def viterbi(soft, terminated=False):
    """soft: (S,T) komplex, positiv = Bit 0.  Start immer im Zustand 0.
    terminated=True: Ende im Zustand 0 (Tail-Bits), sonst freies Ende."""
    soft = np.atleast_2d(np.asarray(soft, np.complex64))
    if HAVE_NUMBA:
        out = np.empty(soft.shape, np.uint8)
        for s in range(soft.shape[0]):
            out[s] = _vit_nb(np.ascontiguousarray(soft[s].real, dtype=np.float64),
                             np.ascontiguousarray(soft[s].imag, dtype=np.float64),
                             _P0, _P1, _I0, _I1, terminated)
        return out
    return np.concatenate([_vit_np(soft[i:i + 32], terminated)
                           for i in range(0, soft.shape[0], 32)], 0)


# ===========================================================================
# 5. SYMBOLE: 2-Bit-Code <-> komplexer QPSK-Punkt
# ===========================================================================
# Intern wird jedes Symbol als 2-Bit-Code gefuehrt: bit0 = I-Bit, bit1 = Q-Bit.
# Erst direkt vor dem Modulator entsteht daraus ein komplexer Wert mit
# Betrag 1 (Konstellation (+-1 +- j)/sqrt2).  Zuordnung: wsconfig 4.2.

def codes_to_symbols(codes):
    c = np.asarray(codes, np.uint8)
    bi, bq = (c & 1).astype(np.float32), ((c >> 1) & 1).astype(np.float32)
    li, lq = (1 - 2 * bi, 1 - 2 * bq) if C.BIT0_POSITIVE else (2 * bi - 1, 2 * bq - 1)
    return ((li + 1j * lq) / SQRT2).astype(np.complex64)


def _bits_hex(h, nbits):
    v = int(h, 16) if isinstance(h, str) else int(h)
    return int_to_bits(v, nbits)


MFP_BITS = _bits_hex(C.mfp_hex(), C.MFP_SYMS)
MFP_CODES = (MFP_BITS | (MFP_BITS << 1)).astype(np.uint8)    # I = Q  [PAT Sp.33]
MFP_SYMBOLS = codes_to_symbols(MFP_CODES)

_pb = int_to_bits(C.PRC_PREAMBLE_HEX, C.PRC_PREAMBLE_BITS)
PRC_PREAMBLE_CODES = (_pb | (_pb << 1)).astype(np.uint8)      # I = Q  [PAT Sp.30]
PRC_WORD_SYMBOLS = codes_to_symbols(PRC_PREAMBLE_CODES[1:])   # 47-Symbol-Wort


# ===========================================================================
# 6. SERVICE-EBENE: SERVICE COMPONENTS, SCH, MULTIPLEX
# ===========================================================================
# THEORIE: Ein "Service" besteht aus bis zu 8 Service Components (SC), z.B.
# Audio in einer Sprache + Text.  Jeder SC hat eine Rate, die ein Vielfaches von
# 8 kbit/s ist.  Zusammen mit dem Service Control Header (SCH) ergibt das den
# Inhalt eines BC-Rahmens.  [PAT Fig.14, Fig.15, Tab.1, Tab.3]
#
# BC-RAHMEN (432 ms) bei n Prime-Rate-Increments:
#   | SCH: n*28 Byte | Service: 432 Datenfelder zu je 2n Byte |
#   Gesamt n*892 Byte = n*7136 Bit  [PAT Sp.7 "n x 7136"]
# Jedes 1-ms-Datenfeld enthaelt nacheinander n(i) Byte je SC i
# (n(i) = Rate_i / 8 kbit/s) und am Ende Padding-Bytes [PAT Fig.14].
# Der MPEG-Strom eines Audio-SC muss so ausgerichtet sein, dass sein erstes
# Bit im Rahmen das erste Bit eines MPEG-Frame-Headers ist [PAT Sp.22].

class SC:
    """Eine Service Component (hier meist ein MP3-Strom)."""
    def __init__(self, rate_kbps, chunks, stype=C.SC_TYPE_MPEG, ptype=0, language=0):
        assert rate_kbps % C.SC_KBPS_UNIT == 0 and 8 <= rate_kbps <= 128
        self.rate_kbps, self.chunks = rate_kbps, chunks
        self.stype, self.ptype, self.language = stype, ptype, language
        self.loop = False

    @property
    def nbytes(self):                      # n(i): Byte je 1-ms-Feld
        return self.rate_kbps // C.SC_KBPS_UNIT

    def chunk(self, k):
        """Daten des k-ten BC-Rahmens (Rate*54 Byte) oder Nullbytes."""
        if self.loop and self.chunks:
            return self.chunks[k % len(self.chunks)]
        if k < len(self.chunks):
            return self.chunks[k]
        return bytes(self.rate_kbps * C.SC_BYTES_PER_KBPS)


class BC:
    """Ein Broadcast Channel: Label, Dynamic Label, bis zu 8 SC."""
    def __init__(self, bcid, label, scs, dynamic=""):
        assert 1 <= len(scs) <= C.MAX_SC
        self.bcid, self.label, self.scs, self.dynamic = bcid, label, scs, dynamic
        total = sum(s.rate_kbps for s in scs)
        # n = Anzahl der 16-kbit/s-Inkremente; ungerade 8-kbit/s-Summe wird mit
        # einem 8-kbit/s-Padding aufgefuellt [ITU "dummy 8 kbit/s"][PAT Fig.14]
        self.n = -(-total // C.PRI_KBPS)
        if self.n > C.MAX_PRI:
            raise ValueError(f"BC {bcid}: {total} kbit/s > {C.MAX_PRI * C.PRI_KBPS} kbit/s")
        self.prcs = []                     # vom Encoder zugewiesen

    # -- SCH ---------------------------------------------------------------
    def build_sch(self, lead_in=False):
        """Bitgepackter SCH (n*28 Byte).  Feldfolge und Breiten: [PAT Tab.1]."""
        n, scs = self.n, self.scs
        w = BitWriter()
        w.put(C.SP_WORD, 20)                     # Service Preamble 0x0474B
        w.put(n, 4)                              # BRI: n*16 kbit/s
        w.put(0, 4)                              # EC: keine Verschluesselung
        w.put(0, 5)                              # ACI1: ADF1 unbenutzt
        w.put(C.ACI2_SERVICE_LABEL, 7)           # ACI2: ADF2 = Service Label
        w.put(len(scs) - 1, 3)                   # NSC-1
        w.put(0, 16)                             # ADF1
        w.put(1, 1)                              # SF: erstes/einziges Segment
        w.put(0, 4)                              # SOLF: 1 Segment
        lab = self.label.encode("latin-1", "replace")[:8].ljust(8, b" ")
        w.put_bytes(lab)                         # ADF2: Label, ISO-Latin-1
        for sc in scs:                           # SCCF je SC [PAT Tab.3]
            w.put(sc.rate_kbps // 8 - 1, 4)
            w.put(C.SC_TYPE_INVALID if lead_in else sc.stype, 4)
            w.put(0, 1)                          # Verschluesselungsflag
            w.put(sc.ptype, 15)
            w.put(sc.language, 8)
        nlab = n * C.PRI_SCH_BYTES - C.SCH_FIXED_BITS // 8 - 4 * len(scs)
        dl = self.dynamic.encode("latin-1", "replace")[:nlab].ljust(nlab, b" ")
        w.put_bytes(dl)                          # Dynamic Label (Rest)
        out = w.to_bytes()
        assert len(out) == n * C.PRI_SCH_BYTES
        return out

    # -- Multiplex ------------------------------------------------------------
    def build_frame(self, k, lead_in=False):
        """Kompletter BC-Rahmen k (n*892 Byte) = SCH + Service-Multiplex."""
        n = self.n
        cols = []
        for sc in self.scs:
            data = bytes(sc.rate_kbps * C.SC_BYTES_PER_KBPS) if lead_in else sc.chunk(k)
            assert len(data) == sc.rate_kbps * C.SC_BYTES_PER_KBPS
            cols.append(np.frombuffer(data, np.uint8).reshape(C.BC_FIELDS, sc.nbytes))
        pad = 2 * n - sum(s.nbytes for s in self.scs)
        if pad:
            cols.append(np.full((C.BC_FIELDS, pad), C.SERVICE_PAD_BYTE, np.uint8))
        service = np.concatenate(cols, axis=1).reshape(-1).tobytes()
        assert len(service) == n * C.PRI_SERVICE_BYTES
        return self.build_sch(lead_in) + service


def parse_sch(sch):
    """SCH-Bytes -> dict (oder None, wenn die Service Preamble nicht passt)."""
    r = BitReader(sch)
    if r.get(20) != C.SP_WORD:
        return None
    d = {"bri": r.get(4), "ec": r.get(4), "aci1": r.get(5), "aci2": r.get(7)}
    nsc = r.get(3) + 1
    d["nsc"] = nsc
    d["adf1"] = r.get(16)
    d["sf"], d["solf"] = r.get(1), r.get(4)
    adf2 = r.get_bytes(8)
    d["label"] = adf2.decode("latin-1").strip() if d["aci2"] == C.ACI2_SERVICE_LABEL else ""
    d["scs"] = []
    for _ in range(nsc):
        d["scs"].append({"rate_kbps": (r.get(4) + 1) * 8, "type": r.get(4),
                         "encrypted": bool(r.get(1)), "ptype": r.get(15), "language": r.get(8)})
    nlab = len(sch) - C.SCH_FIXED_BITS // 8 - 4 * nsc
    d["dynamic"] = r.get_bytes(nlab).decode("latin-1").rstrip() if nlab > 0 else ""
    return d


def demux_service(frame, sch):
    """Umkehrung des Multiplex: -> Liste von Bytes je SC dieses Rahmens."""
    n = sch["bri"]
    off = n * C.PRI_SCH_BYTES
    service = np.frombuffer(frame[off:off + n * C.PRI_SERVICE_BYTES], np.uint8)
    fields = service.reshape(C.BC_FIELDS, 2 * n)
    out, col = [], 0
    for sc in sch["scs"]:
        nb = sc["rate_kbps"] // 8
        out.append(fields[:, col:col + nb].reshape(-1).tobytes())
        col += nb
    return out


# ===========================================================================
# 7. TRANSPORT-EBENE DES BC:  Scrambler -> RS -> Interleaver -> Faltungscode
# ===========================================================================
# Reihenfolge im Sender [PAT Fig.13A, Fig.18]:
#   BC-Rahmen -> Scrambler (x^9+x^5+1) -> RS(255,223) + Blockinterleaver
#             -> Faltungscode R=1/2 -> 8160 Symbole je PRI -> Verteilung auf PRC
#
# BLOCKINTERLEAVER [PAT Fig.20, Sp.29]:  892 Byte Sy(1..892) werden auf 4 RS-
# Bloecke verteilt, Block b bekommt Sy(b), Sy(b+4), Sy(b+8) ...  Jeder Block
# erhaelt 32 Pruefbytes.  Ausgabe: erst alle 892 Datenbytes unveraendert, dann
# die 128 Pruefbytes spaltenweise R(1),R(33),R(65),R(97),R(2),R(34), ...
# Zweck: ein Fehlerbuendel im Kanal trifft so alle 4 Bloecke nur schwach,
# anstatt einen Block zu zerstoeren.   Gesamtlaenge je PRI: 4*255 = 1020 Byte.

def _groups(n):
    """(Anzahl Gruppen G, Tiefe D) gemaess INTERLEAVE_SCOPE (wsconfig 4.7)."""
    return (n, 4) if C.INTERLEAVE_SCOPE == "per_pri" else (1, 4 * n)


def fec_encode(data, n):
    """n*892 Byte -> n*1020 Byte (RS + Blockinterleaver)."""
    G, D = _groups(n)
    arr = np.frombuffer(bytes(data), np.uint8)
    out = []
    for g in range(G):
        info = arr[g * D * C.RS_K:(g + 1) * D * C.RS_K]
        blocks = info.reshape(C.RS_K, D).T            # Block b <- Sy(b + D*m)
        par = RS_BC.encode(blocks)                    # (D,32)
        out.append(np.concatenate([info, par.T.reshape(-1)]))
    return np.concatenate(out).tobytes()


def fec_decode(data, n):
    """n*1020 Byte -> (n*892 Byte, Liste der Fehlerzahlen je RS-Block)."""
    G, D = _groups(n)
    arr = np.frombuffer(bytes(data), np.uint8)
    info_out, nerrs = [], []
    for g in range(G):
        grp = arr[g * D * C.RS_N:(g + 1) * D * C.RS_N]
        info = grp[:D * C.RS_K]
        par = grp[D * C.RS_K:].reshape(C.RS_NR, D).T
        cw = np.concatenate([info.reshape(C.RS_K, D).T, par], axis=1)   # (D,255)
        cw, ne = RS_BC.decode(cw)
        info_out.append(cw[:, :C.RS_K].T.reshape(-1))
        nerrs.extend(int(v) for v in ne)
    return np.concatenate(info_out).tobytes(), nerrs


class BcEncoder:
    """Codiert aufeinanderfolgende BC-Rahmen eines BC zu Symbolcodes."""
    def __init__(self, n):
        self.n = n
        self.hist = None

    def encode(self, frame):
        n = self.n
        by = bc_scramble(frame)
        fec = fec_encode(by, n)
        bits = bytes_to_bits(fec)                    # n*8160 Bit
        hist = None if C.CONV_MODE == "reset_per_frame" else self.hist
        x, y, h = conv_encode(bits, hist)
        self.hist = h
        return pair_codes(x, y)                      # n*8160 Symbolcodes


def bc_decode_frames(soft, n):
    """soft: (F, n*8160) komplexe Symbole -> Liste von (Rahmenbytes, Fehlerliste)."""
    F = soft.shape[0]
    vin = soft_for_viterbi(soft)
    if C.CONV_MODE == "reset_per_frame":
        bits = viterbi(vin, terminated=False)
    else:                                           # ein durchgehender Lauf
        bits = viterbi(vin.reshape(1, -1), terminated=False).reshape(F, -1)
    res = []
    for f in range(F):
        fec = bits_to_bytes(bits[f]).tobytes()
        info, nerr = fec_decode(fec, n)
        res.append((bc_scramble(info), nerr))
    return res


# ===========================================================================
# 8. TSCC - TIME SLOT CONTROL CHANNEL
# ===========================================================================
# THEORIE: Der Satellit kann jeden der 96 Zeitschlitze (PRC) beliebig einem BC
# zuordnen.  Welcher PRC zu welchem BC gehoert, steht im TSCC am Anfang jedes
# TDM-Rahmens: 96 "Time Slot Control Words" (TSCW) + TDM-Identifier.
#
# AUFBAU [PAT Fig.26, Sp.32-33]:
#   TDM-ID 16 Bit | 96 x TSCW 16 Bit | Auffuellmuster 232 Bit (1010...)
#   = 223 Byte  ->  RS(255,223) (GF 0x187)  ->  255 Byte = 2040 Bit
#   + Auffuellmuster 72 Bit (1010...)  = 2112 Bit
#   -> Faltungscode R=1/2 (Start Zustand 0, KEIN Tail)  -> 4224 Bit = 2112 Symbole
# Das hintere Auffuellmuster nimmt die Unsicherheit am Ende des unterminierten
# Viterbi-Pfads auf (die letzten ~30 Bit sind unzuverlaessig).
# TSCW-Felder [PAT Tab.4]: BCID-Typ 2 | BCID-Nr 9 | Last-PRC 1 | Format 2 |
#                          Audience 1 | Reserve 1.

def make_tscw(bcid_number, last_prc):
    return ((C.TSCW_BCID_TYPE << 14) | (bcid_number << 5) | (int(last_prc) << 4)
            | (C.TSCW_FORMAT << 2) | (C.TSCW_AUDIENCE << 1))


def parse_tscw(w):
    return {"type": w >> 14, "number": (w >> 5) & 0x1FF, "last": (w >> 4) & 1,
            "format": (w >> 2) & 3, "audience": (w >> 1) & 1}


def _alt_bits(n):
    """1,0,1,0,... [PAT Sp.32: 'erstes Bit ist 1']"""
    return (1 - (np.arange(n) & 1)).astype(np.uint8)


def tscc_encode(tscw_list):
    """96 TSCW -> 2112 Symbolcodes."""
    w = BitWriter()
    w.put((C.TDM_REGION << (C.TDM_ID_BITS - 4)) | (C.TDM_NUMBER << (C.TDM_ID_BITS - 8)),
          C.TDM_ID_BITS)
    for t in tscw_list:
        w.put(t, C.TSCW_BITS)
    info_bits = np.concatenate([np.array(w.bits, np.uint8), _alt_bits(C.TSCC_ROUNDOFF1_BITS)])
    info = bits_to_bytes(info_bits).reshape(1, C.TSCC_INFO_BYTES)
    cw = np.concatenate([info, RS_TSCC.encode(info)], axis=1)[0]
    bits = np.concatenate([bytes_to_bits(cw), _alt_bits(C.TSCC_ROUNDOFF2_BITS)])
    x, y, _ = conv_encode(bits)
    codes = pair_codes(x, y)
    assert len(codes) == C.TSCC_SYMS
    return codes


def tscc_decode(soft):
    """2112 komplexe Symbole -> (tdm_id, [96 TSCW]) oder None."""
    bits = viterbi(soft_for_viterbi(soft)[None, :], terminated=False)[0]
    by = bits_to_bytes(bits[:C.RS_N * 8]).reshape(1, C.RS_N)
    cw, ne = RS_TSCC.decode(by)
    if ne[0] < 0:
        return None
    info_bits = bytes_to_bits(cw[0, :C.RS_K])
    pad = info_bits[-C.TSCC_ROUNDOFF1_BITS:]            # Plausibilitaet: Muster?
    if np.mean(pad == _alt_bits(C.TSCC_ROUNDOFF1_BITS)) < 0.95:
        return None
    r = BitReader(cw[0, :C.RS_K].tobytes())
    tdm_id = r.get(C.TDM_ID_BITS)
    return tdm_id, [r.get(C.TSCW_BITS) for _ in range(C.NPRC)]


# ===========================================================================
# 9. PRC-RAHMEN UND TDM-RAHMEN (Senderseite)
# ===========================================================================
# PRC-STROM: Jeder PRC ist ein endloser Symbolstrom mit 19 000 Sym/s
# (= 2622 Symbole je 138-ms-TDM-Rahmen).  Er besteht aus aufeinander folgenden
# 432-ms-Rahmen:  [48 Sym Preamble][8160 Sym Daten]  = 8208 Symbole.
# PRC i bekommt vom codierten BC die Symbole S(i), S(i+n), S(i+2n), ...
# [PAT Fig.4, Fig.22, Sp.9, 30].  Weil 8208 und 2622 nicht gleich sind
# (ggT 114: 23 PRC-Rahmen = 72 TDM-Rahmen), fallen BC-Rahmen- und TDM-Rahmen-
# grenzen nur alle 9,936 s zusammen; der Empfaenger richtet sich deshalb nach
# der PRC-Preamble, nicht nach dem TDM-Rahmen.
#
# TDM-RAHMEN [PAT Fig.8, Sp.15]:  |MFP 96|TSCC 2112| Satz 1 | Satz 2 | ... Satz 2622|
# Satz j enthaelt das j-te Symbol der 96 PRC in aufsteigender Reihenfolge
# (PRC 1..96).  Dieses "Verschachteln" haelt Speicher und Umschaltlogik im
# Satelliten klein.  TSCC + Daten werden danach mit der TDM-PRS gescrambelt,
# der MFP bleibt unverschluesselt (Synchronisationswort).

def bc_codes_to_prc_streams(codes, n):
    """Symbolcodes (n*8160) eines BC-Rahmens -> n Arrays zu je 8208 Codes."""
    arr = codes.reshape(C.PRC_DATA_SYMS, n)
    return [np.concatenate([PRC_PREAMBLE_CODES, arr[:, i]]) for i in range(n)]


def build_prc_streams(bcs, nframes, lead_in_frames):
    """-> dict {prc_index: uint8-Array der Laenge (lead+nframes)*8208}."""
    streams = {}
    total = lead_in_frames + nframes
    for bc in bcs:
        enc = BcEncoder(bc.n)
        parts = [[] for _ in range(bc.n)]
        for f in range(total):
            lead = f < lead_in_frames
            codes = enc.encode(bc.build_frame(max(0, f - lead_in_frames), lead_in=lead))
            for i, s in enumerate(bc_codes_to_prc_streams(codes, bc.n)):
                parts[i].append(s)
        for i, p in enumerate(bc.prcs):
            streams[p] = np.concatenate(parts[i])
    return streams


class TdmBuilder:
    """Setzt TDM-Rahmen t aus den PRC-Stroemen zusammen (liefert Symbole)."""
    def __init__(self, bcs, streams, seed=1):
        self.streams = streams
        self.rng = np.random.RandomState(seed)
        tscw = [make_tscw(C.BCID_UNUSED, False)] * C.NPRC
        tscw = list(tscw)
        for bc in bcs:
            for k, p in enumerate(bc.prcs):
                tscw[p] = make_tscw(bc.bcid, k == len(bc.prcs) - 1)
        self.tscc_codes = tscc_encode(tscw)
        self.tscw = tscw

    def frame(self, t):
        data = self.rng.randint(0, 4, (C.SETS_PER_TDM, C.NPRC)).astype(np.uint8)  # unbenutzt
        a, b = t * C.SETS_PER_TDM, (t + 1) * C.SETS_PER_TDM
        for p, s in self.streams.items():
            data[:, p] = s[a:b]
        body = np.concatenate([self.tscc_codes, data.reshape(-1)])
        # TDM-Scrambler: XOR der PRS-Bits auf I und Q [PAT Fig.27]
        bi = (body & 1) ^ _tdm_prs[0::2]
        bq = ((body >> 1) & 1) ^ _tdm_prs[1::2]
        body = (bi | (bq << 1)).astype(np.uint8)
        return np.concatenate([MFP_SYMBOLS, codes_to_symbols(body)])


# ===========================================================================
# 10. PHY SENDER:  RRC-FILTER, KANALSTOERUNGEN, DATEIAUSGABE
# ===========================================================================
# THEORIE: Rechteckige Symbole haetten ein unendlich breites Spektrum.  Das
# Root-Raised-Cosine-Filter (Roll-off 0,4) begrenzt die Bandbreite auf
# (1+0,4)*0,92 MHz = 1,29 MHz.  Sender- und Empfaengerfilter sind identisch
# ("matched"): zusammen ergeben sie ein Raised-Cosine ohne Intersymbol-
# interferenz an den Abtastzeitpunkten.  [DS 3.2 "Nyquist root filter"].

def rrc(beta, sps, span=8, tau=0.0):
    n = np.arange(-span * sps, span * sps + 1)
    t = n / sps - tau
    h = np.zeros(len(t))
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            h[i] = 1 - beta + 4 * beta / np.pi
        elif abs(abs(ti) - 1 / (4 * beta)) < 1e-7:
            h[i] = (beta / np.sqrt(2)) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * beta)) +
                                          (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            h[i] = (np.sin(np.pi * ti * (1 - beta)) + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta))) / \
                   (np.pi * ti * (1 - (4 * beta * ti) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


FORMATS = {"cf32": (np.float32, 2, 8), "cs16": (np.int16, 2, 4),
           "cs8": (np.int8, 2, 2), "cu8": (np.uint8, 2, 2)}


class Modulator:
    """Symbole -> RRC-geformtes komplexes Basisband -> Datei (mit Stoerungen).
    C/N ist wie im Datenblatt Es/N0 (= Eb/N0|QPSK + 3 dB) [DS 3.8]."""
    def __init__(self, fh, fmt="cs8", sps=4, cn_db=None, cfo_hz=0.0, phase_deg=0.0,
                 timing=0.0, seed=1):
        self.fh, self.fmt, self.sps = fh, fmt, sps
        self.h = rrc(C.ROLLOFF, sps, 8, tau=timing)
        self.tail = np.zeros(len(self.h) - 1, np.complex128)
        self.cn_db, self.cfo, self.phase = cn_db, cfo_hz, np.deg2rad(phase_deg)
        self.n = 0
        self.rng = np.random.RandomState(seed)
        self.total = 0

    def _emit(self, x):
        fs = self.sps * C.SYMRATE
        if self.cfo or self.phase:
            nn = self.n + np.arange(len(x))
            x = x * np.exp(1j * (2 * np.pi * self.cfo * nn / fs + self.phase))
        self.n += len(x)
        if self.cn_db is not None:
            var = self.sps / 10 ** (self.cn_db / 10)
            x = x + np.sqrt(var / 2) * (self.rng.randn(len(x)) + 1j * self.rng.randn(len(x)))
        dt = FORMATS[self.fmt][0]
        if self.fmt == "cf32":
            o = np.empty(2 * len(x), np.float32)
            o[0::2], o[1::2] = x.real, x.imag
        else:
            rms = np.sqrt(self.sps / 10 ** (self.cn_db / 10) + 1) if self.cn_db is not None else 1.0
            full = {"cs16": 32767, "cs8": 127, "cu8": 127}[self.fmt]
            sc = 0.25 * full / rms
            o = np.empty(2 * len(x), np.float64)
            o[0::2], o[1::2] = x.real * sc, x.imag * sc
            if self.fmt == "cu8":
                o += 127.5
            lo = 0 if self.fmt == "cu8" else -full - 1
            hi = 255 if self.fmt == "cu8" else full
            o = np.clip(np.round(o), lo, hi).astype(dt)
        self.fh.write(o.tobytes())
        self.total += len(x)

    def write(self, syms):
        up = np.zeros(len(syms) * self.sps, np.complex128)
        up[::self.sps] = syms * np.sqrt(self.sps)
        y = signal.fftconvolve(up, self.h)
        y[:len(self.tail)] += self.tail
        self.tail = y[len(up):].copy()
        self._emit(y[:len(up)])

    def close(self):
        self._emit(self.tail)


# ===========================================================================
# 11. PHY EMPFAENGER: MATCHED FILTER, TAKT, TRAEGER
# ===========================================================================
# Der Chip STA002 nutzt Regelschleifen (Costas/Frequenz-Phasen-Detektor fuer
# den Traeger, Takt-Detektor + Interpolator fuer den Symboltakt) [DS 3.3, 3.4].
# Fuer die dateibasierte, nicht echtzeitgebundene Verarbeitung verwenden wir
# gleichwertige VORWAERTSGEKOPPELTE Schaetzer (vektorisierbar, kein
# Einschwingen):
#   * Takt:    Oerder-Meyr - die Spektrallinie von |y|^2 bei der Symbolrate
#              verraet die Abtastphase.
#   * Traeger: Vierte-Potenz-Verfahren - QPSK^4 ist konstant (-1), eine
#              Frequenzablage zeigt sich als Spektrallinie bei 4*df; die
#              Restphase wird blockweise aus arg(sum z^4) gewonnen.
# Die verbleibende 4-fache Phasenmehrdeutigkeit loest der MFP [DS 6 "TDM
# synchronization": "known sync word is also used to correct the phase
# ambiguity"].

def iter_iq(path, fmt, chunk):
    dt = FORMATS[fmt][0]
    with open(path, "rb") as f:
        while True:
            a = np.fromfile(f, dtype=dt, count=2 * chunk)
            if len(a) < 2:
                return
            a = a[:len(a) // 2 * 2]
            if fmt == "cu8":
                x = (a[0::2].astype(np.float32) - 127.5) + 1j * (a[1::2].astype(np.float32) - 127.5)
            else:
                x = a[0::2].astype(np.float32) + 1j * a[1::2].astype(np.float32)
            yield x.astype(np.complex64)


class Demod:
    BLK_T = 16384          # Symbole je Takt-Schaetzblock
    BLK_P = 2048           # Symbole je Phasen-Schaetzblock

    def __init__(self, sps=4):
        if sps < 4:
            raise ValueError("sps >= 4 noetig (Oerder-Meyr)")
        self.sps = sps
        self.h = rrc(C.ROLLOFF, sps, 8).astype(np.float32)
        self.Lh = len(self.h)
        self.carry = np.zeros(0, np.complex64)
        self.g_buf0 = 0
        self.k_next = None
        self.t_prev = None
        self.facc = 0.0
        self.p_prev = None
        self.ksym = 0
        self.cfo_hist = []
        self.ycarry = np.zeros(0, np.complex64)

    def feed(self, raw):
        sps, Lh = self.sps, self.Lh
        buf = np.concatenate([self.carry, raw])
        if len(buf) < Lh + 8 * sps:
            self.carry = buf
            return np.zeros(0, np.complex64)
        # 1) Matched Filter (RRC) - 'valid' vermeidet Randeffekte
        y = signal.fftconvolve(buf, self.h, mode="valid").astype(np.complex64)
        gy0 = self.g_buf0 + (Lh - 1) // 2
        self.carry = buf[-(Lh - 1):]
        self.g_buf0 += len(buf) - (Lh - 1)
        ynew_tail = y[-4:].copy()          # Interpolation ueber Chunk-Grenzen
        y = np.concatenate([self.ycarry, y])
        gy0 -= len(self.ycarry)
        self.ycarry = ynew_tail
        # 2) Takt (Oerder-Meyr)
        p = (np.abs(y) ** 2).astype(np.float64)
        nb = max(1, len(y) // (self.BLK_T * sps))
        edges = np.linspace(0, len(y), nb + 1).astype(int)
        ng = gy0 + np.arange(len(y))
        ph = np.exp(-2j * np.pi * (ng % sps) / sps)
        cs, th = [], []
        for i in range(nb):
            sl = slice(edges[i], edges[i + 1])
            cs.append(gy0 + 0.5 * (edges[i] + edges[i + 1]))
            th.append(np.angle(np.sum(p[sl] * ph[sl])))
        cs, th = np.array(cs), np.array(th)
        if self.t_prev is not None:
            th = np.unwrap(np.concatenate([[self.t_prev[1]], th]))[1:]
        else:
            th = np.unwrap(th)
        tau = -th * sps / (2 * np.pi)
        ccs = cs if self.t_prev is None else np.concatenate([[self.t_prev[0]], cs])
        ctau = tau if self.t_prev is None else np.concatenate(
            [[-self.t_prev[1] * sps / (2 * np.pi)], tau])
        self.t_prev = (cs[-1], th[-1])
        # 3) Abtastzeitpunkte + kubische Lagrange-Interpolation
        if self.k_next is None:
            self.k_next = int(np.ceil((gy0 + 2) / sps)) + 1
        nest = (len(y) + 2 * sps) // sps + 2
        ks = self.k_next + np.arange(nest)
        t = sps * ks + np.interp(sps * ks, ccs, ctau)
        ok = (t >= gy0 + 1) & (t <= gy0 + len(y) - 3)
        last = np.nonzero(~ok & (t > gy0 + 1))[0]
        end = last[0] if len(last) else len(ks)
        sel = np.arange(end)[ok[:end]]
        if len(sel) == 0:
            self.k_next = int(ks[end - 1]) + 1 if end else self.k_next
            return np.zeros(0, np.complex64)
        t = t[sel]
        self.k_next = int(ks[sel[-1]]) + 1
        i0 = np.floor(t).astype(np.int64)
        mu = (t - i0).astype(np.float32)
        i0 -= gy0
        w = [-mu * (mu - 1) * (mu - 2) / 6, (mu + 1) * (mu - 1) * (mu - 2) / 2,
             -(mu + 1) * mu * (mu - 2) / 2, (mu + 1) * mu * (mu - 1) / 6]
        z = (w[0] * y[i0 - 1] + w[1] * y[i0] + w[2] * y[i0 + 1] + w[3] * y[i0 + 2]).astype(np.complex64)
        z /= np.sqrt(np.mean(np.abs(z) ** 2) + 1e-20)          # AGC2
        # 4) Frequenzablage aus der z^4-Spektrallinie
        n = len(z)
        if n >= 4096:
            nf = 1 << int(np.ceil(np.log2(n)) + 1)
            sp = np.abs(np.fft.fft(z.astype(np.complex128) ** 4, nf))
            kpk = int(np.argmax(sp))
            a, b, c = sp[(kpk - 1) % nf], sp[kpk], sp[(kpk + 1) % nf]
            den = a - 2 * b + c
            dlt = 0.5 * (a - c) / den if den != 0 else 0.0
            f4 = (kpk + dlt) / nf
            if f4 > 0.5:
                f4 -= 1
            df = f4 / 4.0
            if abs(df) < 0.12:
                self.cfo_hist.append(df)
            else:
                df = 0.0
        else:
            df = self.cfo_hist[-1] if self.cfo_hist else 0.0
        nn = np.arange(n)
        z *= np.exp(-1j * (self.facc + 2 * np.pi * df * nn)).astype(np.complex64)
        self.facc += 2 * np.pi * df * n
        # 5) blockweise Restphase (QPSK^4 = -1  ->  Winkel von -sum(z^4))
        nbp = max(1, n // self.BLK_P)
        ed = np.linspace(0, n, nbp + 1).astype(int)
        z4 = z.astype(np.complex128) ** 4
        pc = np.array([self.ksym + 0.5 * (ed[i] + ed[i + 1]) for i in range(nbp)])
        pt = np.array([np.angle(-np.sum(z4[ed[i]:ed[i + 1]])) for i in range(nbp)])
        if self.p_prev is not None:
            pt = np.unwrap(np.concatenate([[self.p_prev[1]], pt]))[1:]
            cc = np.concatenate([[self.p_prev[0]], pc])
            ct = np.concatenate([[self.p_prev[1]], pt])
        else:
            pt = np.unwrap(pt)
            cc, ct = pc, pt
        self.p_prev = (pc[-1], pt[-1])
        phi = np.interp(self.ksym + nn, cc, ct) / 4.0
        z *= np.exp(-1j * phi).astype(np.complex64)
        self.ksym += n
        return z


def demod_file(path, out_path, fmt="cs8", sps=4, progress=None):
    """IQ-Datei -> Symboldatei (complex64, 1 Sample/Symbol)."""
    import os
    dm = Demod(sps)
    size = os.path.getsize(path) // FORMATS[fmt][2]
    done = 0
    with open(out_path, "wb") as fo:
        for raw in iter_iq(path, fmt, 1 << 19):
            z = dm.feed(raw)
            if len(z):
                fo.write(z.tobytes())
            done += len(raw)
            if progress:
                progress(done / size)
    return dm


# ===========================================================================
# 12. TDM-EMPFAENGER:  RAHMENSYNC, LINEUP, BC-EXTRAKTION
# ===========================================================================
# Ablauf im Radio [DS "TDM Demultiplexer", Fig.Frame-Synchronization;
#                  PAT Fig.9, 10, 11, 28a/b]:
#   1. MFP-Suche: Korrelation mit dem 96-Symbol-Wort; Treffer wiederholen sich
#      alle 253 920 Symbole (138 ms).  Der Korrelationswinkel liefert die
#      Quadranten-Korrektur (QPSK-Phasenmehrdeutigkeit).
#   2. Descrambling von TSCC + Daten mit der TDM-PRS.
#   3. TSCC decodieren (Viterbi + RS) -> Lineup: welcher PRC gehoert zu welchem BC.
#   4. PRC des gewaehlten BC aus den 2622 Saetzen je Rahmen herausloesen und zu
#      Stroemen aneinanderreihen.
#   5. In jedem PRC-Strom die PRC-Preamble (47-Symbol-Wort) per Korrelation
#      finden; die Preambles der PRC desselben BC duerfen bis zu 4 Symbole
#      gegeneinander versetzt sein [PAT Sp.18] -> ueber Korrelationsspitzen
#      ausrichten.
#   6. Symbole reihum zum codierten BC-Rahmen verschachteln (n*8160), dann
#      Viterbi -> Deinterleaver -> RS -> Descrambler -> SCH + Service.

class TdmReceiver:
    def __init__(self, z):
        self.z = z
        self.N = len(z)
        self.conj = False
        self.starts, self.quad, self.cn = [], [], []
        self.sync_quality = 0.0
        # QPSK-Mehrdeutigkeit: Drehung (4x) UND Q-Inversion.  Da der MFP auf I=Q
        # liegt, ist "Konjugation" bei ihm identisch mit "Drehung um -90 Grad" -
        # der MFP allein kann beides nicht trennen.  Die Entscheidung faellt
        # daher anhand des TSCC: nur die richtige Hypothese ergibt ein gueltiges
        # Reed-Solomon-Codewort.  (Das Datenblatt bietet dafuer das Bit QCHP.)
        for conj in (False, True):
            self.conj = conj
            self.starts, self.quad, self.cn = [], [], []
            self._sync()
            probe = [self._tscc(i) for i in range(min(4, len(self.starts)))]
            if any(p is not None for p in probe):
                break
        self.tscc = [self._tscc(i) for i in range(len(self.starts))]

    def _zz(self, a, b):
        x = np.asarray(self.z[a:b])
        return np.conj(x) if self.conj else x

    def _sync(self):
        F = C.TDM_SYMS
        nfr = min(6, self.N // F)
        if nfr < 2:
            raise RuntimeError("Weniger als 2 TDM-Rahmen in der Datei")
        seg = np.asarray(self.z[:nfr * F])
        seg = np.conj(seg) if self.conj else seg
        c = signal.fftconvolve(seg, np.conj(MFP_SYMBOLS[::-1]), mode="valid")
        pad = np.zeros(nfr * F)
        pad[:len(c)] = np.abs(c)
        fold = pad.reshape(nfr, F).sum(0)              # Treffer alle F Symbole
        off = int(np.argmax(fold))
        self.sync_quality = fold[off] / (np.median(fold) + 1e-9)
        if self.sync_quality < 4:
            raise RuntimeError("Kein MFP gefunden - Signal fehlt oder MFP_HEX/Format falsch")
        W = 24
        base = off
        k = 0
        ref_p = float(np.mean(np.abs(MFP_SYMBOLS) ** 2))
        while True:
            s = base + k * F
            if s + F > self.N:
                break
            a = max(0, s - W)
            x = self._zz(a, s + C.MFP_SYMS + W)
            c = np.correlate(x, MFP_SYMBOLS, "valid")
            j = int(np.argmax(np.abs(c)))
            st = a + j
            if st + F > self.N:
                break
            qd = int(np.round(np.angle(c[j]) / (np.pi / 2))) % 4
            fr = self._zz(st, st + C.MFP_SYMS) * np.exp(-1j * qd * np.pi / 2)
            err = fr - MFP_SYMBOLS
            self.cn.append(10 * np.log10(ref_p / (np.mean(np.abs(err) ** 2) + 1e-12)))
            base = st - k * F
            self.starts.append(st)
            self.quad.append(qd)
            k += 1

    def body(self, i):
        """TSCC+Daten des Rahmens i: gedreht und TDM-descrambelt (soft)."""
        s = self.starts[i] + C.MFP_SYMS
        x = self._zz(s, s + C.BODY_SYMS) * np.exp(-1j * self.quad[i] * np.pi / 2)
        return (x.real * TDM_SI + 1j * x.imag * TDM_SQ).astype(np.complex64)

    def _tscc(self, i):
        return tscc_decode(self.body(i)[:C.TSCC_SYMS])

    @property
    def cn_db(self):
        return float(np.median(self.cn)) if self.cn else float("nan")

    def lineup(self):
        """{BCID: [PRC, ...]} aus dem ersten gueltigen TSCC.  BCID = Typ<<9 | Nr."""
        for t in self.tscc:
            if t is not None:
                out = {}
                for p, w in enumerate(t[1]):
                    d = parse_tscw(w)
                    if d["number"] != C.BCID_UNUSED:
                        out.setdefault((d["type"] << 9) | d["number"], []).append(p)
                return out
        return {}

    # -- BC-Extraktion --------------------------------------------------------
    def _prc_stream_cols(self, prcs):
        cols = []
        for i in range(len(self.starts)):
            data = self.body(i)[C.TSCC_SYMS:].reshape(C.SETS_PER_TDM, C.NPRC)
            cols.append(data[:, prcs])
        return np.concatenate(cols, axis=0) if cols else np.zeros((0, len(prcs)), np.complex64)

    @staticmethod
    def _find_preambles(stream):
        """Datenanfaenge (Index direkt hinter dem 47-Symbol-Wort) in einem PRC-Strom."""
        c = signal.fftconvolve(stream, np.conj(PRC_WORD_SYMBOLS[::-1]), mode="valid").real
        cand = np.nonzero(c > 0.5 * C.PRC_WORD_SYMS)[0]
        if len(cand) == 0:
            return np.zeros(0, int)
        groups = np.split(cand, np.nonzero(np.diff(cand) > 100)[0] + 1)
        return np.array([g[np.argmax(c[g])] for g in groups]) + C.PRC_WORD_SYMS

    def bc_soft_frames(self, prcs):
        """-> (F, n*8160) komplexe Symbole je gefundenem, vollstaendigem BC-Rahmen."""
        n = len(prcs)
        cols = self._prc_stream_cols(prcs)
        starts = [self._find_preambles(cols[:, i]) for i in range(n)]
        rows = []
        for d0 in starts[0]:
            pos = [int(d0)]
            for other in starts[1:]:
                if len(other) == 0:
                    pos = None
                    break
                j = int(np.argmin(np.abs(other - d0)))
                if abs(int(other[j]) - int(d0)) > 8:
                    pos = None
                    break
                pos.append(int(other[j]))
            if pos is None or max(pos) + C.PRC_DATA_SYMS > len(cols):
                continue
            blk = np.stack([cols[p:p + C.PRC_DATA_SYMS, i] for i, p in enumerate(pos)], axis=1)
            rows.append(blk.reshape(-1))        # Index j*n+i = S(j*n+i)
        if not rows:
            return np.zeros((0, n * C.PRC_DATA_SYMS), np.complex64)
        return np.stack(rows)

    def decode_bc(self, bcid):
        """-> Liste von dicts je BC-Rahmen: frame, sch, nerr, ok."""
        prcs = self.lineup().get(bcid)
        if prcs is None:
            raise KeyError(f"BCID {bcid} nicht im Lineup")
        n = len(prcs)
        soft = self.bc_soft_frames(prcs)
        out = []
        for i in range(0, len(soft), 16):
            for frame, nerr in bc_decode_frames(soft[i:i + 16], n):
                sch = parse_sch(frame[:n * C.PRI_SCH_BYTES])
                ok = sch is not None and all(e >= 0 for e in nerr) and sch["bri"] == n
                out.append({"frame": frame, "sch": sch, "nerr": nerr, "ok": ok})
        return out


# ===========================================================================
# 13. MP3-HILFEN (Frame-Raster 432 ms)
# ===========================================================================
# Anforderung [PAT Sp.22]: Das erste Bit des Audio-SC in einem BC-Rahmen ist das
# erste Bit eines MPEG-Frame-Headers; Abtastraten 48/32 (MPEG-1), 24/16 (MPEG-2),
# 12/8 kHz (MPEG-2.5), Layer III.  432 ms ist ein ganzzahliges Vielfaches der
# Framedauer (24, 36, 48 oder 72 ms).  Bei konstanter Bitrate r hat der Strom
# dann je BC-Rahmen genau r*54 Byte.

_BR1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
ALLOWED_SR = (48000, 32000, 24000, 16000, 12000, 8000)


class NonConformant(Exception):
    pass


def mp3_header(b, i):
    """(Framelaenge, Samplerate, Samples/Frame, Kanaele, kbit/s, Padding) oder None."""
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return None
    ver, layer = (b[i + 1] >> 3) & 3, (b[i + 1] >> 1) & 3
    if ver == 1 or layer != 1:
        return None
    bri, sri, pad = (b[i + 2] >> 4) & 15, (b[i + 2] >> 2) & 3, (b[i + 2] >> 1) & 1
    if bri in (0, 15) or sri == 3:
        return None
    kb = (_BR1 if ver == 3 else _BR2)[bri]
    sr = _SR[ver][sri]
    spf = 1152 if ver == 3 else 576
    fl = (144 if ver == 3 else 72) * kb * 1000 // sr + pad
    return fl, sr, spf, 1 if ((b[i + 3] >> 6) & 3) == 3 else 2, kb, pad


def mp3_bc_chunks(data):
    """MP3-Bytes -> (rate_kbps, Liste von Chunks zu je rate*54 Byte).
    Wirft NonConformant mit Erklaerung, wenn der Strom nicht ins Raster passt."""
    n = len(data)
    i = 0
    if data[:3] == b"ID3" and n > 10:
        i = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 |
                  (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    while i < n - 4 and not (mp3_header(data, i) and mp3_header(data, i + mp3_header(data, i)[0])):
        i += 1
    if i >= n - 4:
        raise NonConformant("kein MPEG-Layer-III-Strom gefunden")
    frames = []
    while i < n - 4:
        h = mp3_header(data, i)
        if not h or i + h[0] > n:
            break
        frames.append((i, h))
        i += h[0]
    if frames and (b"Xing" in data[frames[0][0]:frames[0][0] + 60] or
                   b"Info" in data[frames[0][0]:frames[0][0] + 60]):
        frames = frames[1:]                       # VBR/Info-Tag-Frame verwerfen
    if not frames:
        raise NonConformant("keine Audioframes")
    fl0, sr, spf, ch, kb, _ = frames[0][1]
    if any(h[0] != fl0 or h[1] != sr or h[4] != kb for _, h in frames):
        raise NonConformant("keine konstante Bitrate / wechselnde Framelaenge (Padding)")
    if sr not in ALLOWED_SR:
        raise NonConformant(f"Abtastrate {sr} Hz nicht erlaubt (erlaubt: {ALLOWED_SR})")
    if kb % 8 or not 8 <= kb <= 128:
        raise NonConformant(f"Bitrate {kb} kbit/s muss Vielfaches von 8 sein und <= 128")
    fpb = C.BC_FRAME_SECONDS * sr / spf
    if abs(fpb - round(fpb)) > 1e-9:
        raise NonConformant("432 ms ist kein ganzzahliges Vielfaches der Framedauer")
    fpb = int(round(fpb))
    if fpb * fl0 != kb * C.SC_BYTES_PER_KBPS:
        raise NonConformant("Bytes je 432 ms stimmen nicht mit Rate*54 ueberein")
    off0 = frames[0][0]
    per = fpb * fl0
    nchunks = len(frames) // fpb
    chunks = [bytes(data[off0 + k * per: off0 + (k + 1) * per]) for k in range(nchunks)]
    return kb, chunks
