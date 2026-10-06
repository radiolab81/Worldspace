"""
wsconfig.py - Alle Systemkonstanten des WorldSpace-Nachbaus an EINER Stelle.
=============================================================================

Dieses Modul ist der "Lehrbuch-Teil" des Projekts: Jede Konstante nennt ihre
Quelle und ihren Belegstatus.  Damit ist sofort sichtbar, welche Werte aus den
Unterlagen stammen und welche (noch) Annahmen sind.

QUELLENKUERZEL (die verwendeten Unterlagen)
-------------------------------------------
[DS]   STMicroelectronics STA002 "STARMAN Channel Decoder", Datenblatt
       (Ausgabe Januar 2002, 43 Seiten).  Zitiert mit Abschnitt/Seite,
       z.B. [DS 6.2] = Abschnitt 6.2 "Broadcast Channel RS Decoder".
[PAT]  US 6,201,798 B1 (Campanella et al., WorldSpace, 13.3.2001)
       "Signaling Protocol for Satellite Direct Radio Broadcast System".
       Zitiert mit Figur/Tabelle, z.B. [PAT Fig.20] oder [PAT Tab.1].
[ITU]  ITU-R BO.1130 (System "D"/"DS" = WorldSpace), Abschnitte "System
       overview", "Receiver operation", Tab. 3.  (Fassungen -3 und -5 sind
       fuer die hier benutzten Teile inhaltlich gleich.)

BELEGSTATUS
-----------
[BELEGT]    steht so in mindestens einer Unterlage
[ABGELEITET] folgt rechnerisch aus belegten Werten
[ANNAHME]   plausibel, aber nicht (eindeutig) belegt  -> unten als Schalter
[OFFEN]     nicht in den Unterlagen; Platzhalter, damit alles lauffaehig ist

Wer ein echtes WorldSpace-Signal verarbeiten will, muss nur die Abschnitte
"OFFENE PUNKTE" weiter unten anpassen.
"""
import math

# ---------------------------------------------------------------------------
# 1. PHYSIKALISCHE SCHICHT (QPSK)
# ---------------------------------------------------------------------------
SYMRATE = 1_840_000.0     # [BELEGT] 1,84 MSym/s  [DS S.1][ITU][PAT S.5]
ROLLOFF = 0.4             # [BELEGT] Root-Raised-Cosine 0,4  [DS S.1, 3.2]
BITS_PER_SYMBOL = 2       # [BELEGT] QPSK = 2 Bit/Symbol

# ---------------------------------------------------------------------------
# 2. TDM-RAHMEN (Downlink)  - was der Empfaenger von der Antenne sieht
# ---------------------------------------------------------------------------
#   | MFP 96 Sym | TSCC 2112 Sym | 2622 Saetze x 96 Sym (PRC-Daten) |
# [BELEGT] [PAT Fig.8, Fig.10, Fig.24]; [ITU "Downlink TDM frame"].
# (Achtung: Im STA002-Datenblatt Fig. "Frame synchronization" stehen
#  "2112 symbols" ueber dem TSCC und "251712 symbols" ueber dem Datenfeld.)
MFP_SYMS = 96
TSCC_SYMS = 2112
NPRC = 96                 # 96 Prime Rate Channels je TDM-Trager [DS S.6]
SETS_PER_TDM = 2622       # Symbole je PRC und TDM-Rahmen
DATA_SYMS = SETS_PER_TDM * NPRC               # 251 712
BODY_SYMS = TSCC_SYMS + DATA_SYMS             # alles hinter dem MFP
TDM_SYMS = MFP_SYMS + BODY_SYMS               # 253 920 [BELEGT PAT Sp.15]
assert TDM_SYMS == 253_920
TDM_SECONDS = TDM_SYMS / SYMRATE              # 0,138 s [BELEGT]

# ---------------------------------------------------------------------------
# 3. BROADCAST CHANNEL (BC) / PRIME RATE INCREMENT (PRI)
# ---------------------------------------------------------------------------
BC_FRAME_SECONDS = 0.432          # [BELEGT] [PAT Fig.4][DS Fig. "Frame sync."]
PRI_KBPS = 16                     # Basisinkrement 16 kbit/s [BELEGT]
PRI_SERVICE_BITS = 6912           # 16 kbit/s * 0,432 s       [BELEGT PAT Sp.7]
PRI_SCH_BITS = 224                # Service Control Header je PRI [BELEGT]
PRI_BITS = PRI_SERVICE_BITS + PRI_SCH_BITS    # 7136 [BELEGT]
PRI_BYTES = PRI_BITS // 8                     # 892  [ABGELEITET]
PRI_SCH_BYTES = PRI_SCH_BITS // 8             # 28
PRI_SERVICE_BYTES = PRI_SERVICE_BITS // 8     # 864
BC_FIELDS = 432                   # 432 Datenfelder a 1 ms [BELEGT PAT Fig.14]
SC_KBPS_UNIT = 8                  # SC-Raten sind Vielfache von 8 kbit/s [BELEGT]
SC_BYTES_PER_KBPS = 54            # kbit/s * 0,432 s / 8 = 54 Byte je kbit/s
MAX_PRI = 8                       # max. 8 PRC je BC, max. 128 kbit/s [BELEGT]
MAX_SC = 8                        # max. 8 Service Components [BELEGT]

# Fehlerschutz [BELEGT: DS 5, 6; PAT Fig.19-21; ITU "FEC coding a BC"]
RS_N, RS_K, RS_NR = 255, 223, 32
FEC_BYTES_PER_PRI = 4 * RS_N      # 1020 Byte = 8160 Bit je PRI
BC_CODED_SYMS_PER_PRI = 8160      # 8160 QPSK-Symbole je PRI und BC-Rahmen

# PRC-Rahmen: 48 Sym Preamble + 8160 Sym Daten = 8208 Sym je 432 ms
PRC_PREAMBLE_SYMS = 48            # [BELEGT PAT Fig.4]
PRC_DATA_SYMS = 8160
PRC_FRAME_SYMS = PRC_PREAMBLE_SYMS + PRC_DATA_SYMS     # 8208 -> 19 kSym/s
# [BELEGT PAT Sp.30, Fig.22] 96 Bit, auf I und Q identisch, MSB zuerst.
# Das erste Bit (= "0"-Symbol) ist laut PAT Sp.13 das Stopfsymbol; die
# folgenden 47 Symbole sind das Korrelations-Wort.
PRC_PREAMBLE_HEX = 0x14C181EAC649
PRC_PREAMBLE_BITS = 48
PRC_WORD_SYMS = 47

# Service Preamble (Beginn jedes BC-Rahmens, 20 Bit) [BELEGT PAT Tab.1]
# Das erste Byte ist 0x04 [DS 8.2: "Service preamble (04H)"].
SP_WORD = 0x0474B
SP_BITS = 20

# SCH-Feldbreiten in Bit, Reihenfolge wie [PAT Tab.1] [BELEGT]
SCH_FIELDS = (("SP", 20), ("BRI", 4), ("EC", 4), ("ACI1", 5), ("ACI2", 7),
              ("NSC", 3), ("ADF1", 16), ("SF", 1), ("SOLF", 4), ("ADF2", 64))
SCH_FIXED_BITS = sum(w for _, w in SCH_FIELDS)        # 128 [ABGELEITET]
SCCF_BITS = 32                    # SC-Kontrollfeld je Service Component
# SCCF-Typcodes [PAT Tab.3][DS 7.2 SCH_MEM]
SC_TYPE_MPEG = 0x0
SC_TYPE_DATA = 0x1
SC_TYPE_JPEG = 0x4
SC_TYPE_VIDEO = 0x5
SC_TYPE_INVALID = 0xF             # "invalid data" -> Empfaenger ignoriert
ACI2_SERVICE_LABEL = 0x02         # [PAT Tab.1][DS 7.1 AFCI2_REG]

# TSCC / TSCW [BELEGT PAT Tab.4, Tab.5, Fig.26]
TSCW_BITS = 16
TDM_ID_BITS = 16                  # laut Text 16 Bit  (Tab.5 summiert auf 14!)
TSCC_INFO_BYTES = 223             # RS-Informationslaenge
TSCC_ROUNDOFF1_BITS = 232         # Auffuellmuster VOR dem RS (PAT Sp.32)
TSCC_ROUNDOFF2_BITS = 72          # Auffuellmuster NACH dem RS (PAT Sp.33)
BCID_UNUSED = 0                   # BCID-Nummer 0 = unbenutzter PRC
BCID_TEST = 511                   # Testkanal

# Faltungscode K=7, R=1/2 [BELEGT DS 5; PAT Fig.21: g1=1111001, g2=1011011]
CONV_G1 = 0o171
CONV_G2 = 0o133
CONV_K = 7

# ---------------------------------------------------------------------------
# 4. OFFENE PUNKTE  (Schalter / Platzhalter)
# ---------------------------------------------------------------------------

# --- 4.1 MFP-Syncwort ---------------------------------------------------
# [OFFEN] In keiner Unterlage steht der 96-Bit-Wert (nur: "gleiche 96 Bit auf
# I und Q" [PAT Sp.33]).  None = deterministische Pseudozufallsfolge als
# Platzhalter; sonst 24 Hex-Zeichen (96 Bit), z.B. aus einer Messung.
MFP_HEX = None

# --- 4.2 Bit -> QPSK-Zuordnung -------------------------------------------
# [OFFEN] Weder DS noch PAT nennen die Konstellationszuordnung.  Das Datenblatt
# bietet nur das Bit QCHP, das Q invertiert [DS 3.1, 3.9].
BIT0_POSITIVE = True      # Bit 0 -> +Amplitude, Bit 1 -> -Amplitude
# [ANNAHME] "Symbol wird bei Schalter in Stellung 1 und dann 2 erzeugt"
# [PAT Sp.30] -> erstes Codebit (g1) = I, zweites (g2) = Q.
CONV_FIRST_BIT_TO_I = True

# --- 4.3 Inverter im Faltungscodierer -------------------------------------
# [OFFEN/ANNAHME] [PAT Fig.21] zeigt einen Inverter (340) im g2-Zweig, wie
# im CCSDS-Standard.  Der Text sagt nur "...so dass der Ausgang g1 und g2
# ist".  True = g2 wird invertiert.
CONV_INVERT_G2 = True

# --- 4.4 Zustand des Faltungscodierers zwischen BC-Rahmen -----------------
# [OFFEN] Fuer den TSCC ist es belegt: Register = 0 am Anfang, kein Tail
# [PAT Sp.33].  Fuer den BC steht nichts da.
#   "reset_per_frame": Zustand 0 zu Beginn jedes 432-ms-Rahmens
#   "continuous"     : Zustand laeuft ueber Rahmengrenzen weiter
CONV_MODE = "reset_per_frame"

# --- 4.5 Scrambler-Beginn im BC --------------------------------------------
# [OFFEN/WIDERSPRUCH] Text [PAT Sp.28]: PRS startet mit 111111111 am ersten
# Bit des Rahmens.  [PAT Fig.13A] beschriftet den Scrambler dagegen "all but
# the first 80 bits".  Wert = Anzahl der Bits am Rahmenanfang, die NICHT
# gescrambelt werden (0 = Text, 80 = Abbildung).
BC_SCRAMBLE_SKIP_BITS = 0

# --- 4.6 Ausgabestufe der Pseudozufallsfolgen -------------------------------
# [OFFEN] [PAT Fig.16, Fig.27] zeigen den Abgriff am Ende des Registers.
PRS_OUT_LAST_STAGE = True

# --- 4.7 Interleaver-Bereich bei n > 1 ---------------------------------------
# [OFFEN/AUSLEGUNG] [PAT Fig.20] zeigt nur n = 1 (892 Byte, Tiefe 4).
# [PAT Fig.19] ("n times") deutet an: n-mal dieselbe Gruppe.
#   "per_pri" : n Gruppen zu je 892 Byte, Tiefe 4  (Standard)
#   "whole_bc": 1 Gruppe zu n*892 Byte, Tiefe 4n
INTERLEAVE_SCOPE = "per_pri"

# --- 4.8 TDM-Identifier --------------------------------------------------------
# [WIDERSPRUCH] [PAT Tab.5]: Region 4 + TDM-Nr 4 + Reserve 6 = 14 Bit, Text und
# Fig.26 sagen 16 Bit.  Wir fuellen mit Reserve-Nullen auf 16 auf.
TDM_REGION = 0b0001               # 0001 = AfriStar [PAT Tab.5]
TDM_NUMBER = 0b0001               # TDM 1 (LHCP)    [PAT Tab.5]
TDM_ID_RESERVED_BITS = TDM_ID_BITS - 8

# --- 4.9 TSCW-Details ------------------------------------------------------------
TSCW_BCID_TYPE = 0b00             # 00 lokal, 01 regional, 11 weltweit [PAT Tab.4]
TSCW_FORMAT = 0b00                # 00 = "WorldStar 1" [PAT Tab.4]
TSCW_AUDIENCE = 0                 # 0 oeffentlich, 1 privat

# --- 4.10 Fuellmuster -----------------------------------------------------------------
SERVICE_PAD_BYTE = 0x00           # [OFFEN] Inhalt des Padding-SC (PAT Fig.14)
UNUSED_PRC_FILL = "random"        # [OFFEN] Inhalt unbenutzter PRC (BCID 0)

# --- 4.11 Verschluesselung ---------------------------------------------------------------
# [OFFEN] WES-Algorithmus nicht oeffentlich [DS 8.1].  Es wird nur
# "unverschluesselt" (EC=0, Verschluesselungsflag 0) erzeugt/akzeptiert.
SUPPORT_ENCRYPTION = False


def mfp_hex():
    """Liefert die MFP-Folge als 24-stelligen Hexstring (96 Bit)."""
    if MFP_HEX:
        return MFP_HEX.replace(" ", "").upper()
    import numpy as np
    rs = np.random.RandomState(0x57534D)       # fest -> reproduzierbar
    bits = rs.randint(0, 2, MFP_SYMS)
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return f"{v:0{MFP_SYMS // 4}X}"
