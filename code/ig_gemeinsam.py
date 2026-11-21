"""Gemeinsame Bausteine fuer den Instagram-Cloud-Poster (nur Python-Standardbibliothek).

Wird von cloud_poster.py (GitHub Actions) und cloud_sync.py (PC) benutzt.
- http/GitHub-Contents-API (Lesen, Schreiben, Loeschen, Verzeichnis)
- Verschluesselung der Warteschlange (das Repo ist oeffentlich):
  Schluessel 32 Byte (PLANER_SCHLUESSEL, hex). Daraus per BLAKE2b zwei Teilschluessel.
  Schluesselstrom = BLAKE2b-512(Nonce || Zaehler) mit Teilschluessel 1 (Zaehlermodus),
  Echtheitspruefung = BLAKE2b-256 ueber Nonce + Geheimtext mit Teilschluessel 2 (encrypt-then-MAC).
  Format: b"IGP1" + Nonce(16) + Pruefsumme(32) + Geheimtext.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

KONTO_ORDNER = {"marianne": ("Marianne_Hollweg", "Marianne_Instagram"),
                "hema": ("Hema_Lotfeld", "Hema_Instagram")}
ZWEIG = "planer"
IG = "https://graph.instagram.com/v26.0"


# ---------- Verschluesselung ----------
def _teilschluessel(schluessel_hex):
    k = bytes.fromhex(schluessel_hex.strip())
    if len(k) != 32:
        raise ValueError("PLANER_SCHLUESSEL muss 64 Hex-Zeichen haben")
    enc = hashlib.blake2b(b"enc", key=k, digest_size=64).digest()
    mac = hashlib.blake2b(b"mac", key=k, digest_size=64).digest()
    return enc, mac


def _strom(enc, nonce, laenge):
    teile = []
    n = (laenge + 63) // 64
    for i in range(n):
        teile.append(hashlib.blake2b(nonce + i.to_bytes(8, "big"), key=enc, digest_size=64).digest())
    return b"".join(teile)[:laenge]


def _xor(a, b):
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big") if a else b""


def verschluesseln(schluessel_hex, daten):
    enc, mac = _teilschluessel(schluessel_hex)
    nonce = os.urandom(16)
    geheim = _xor(daten, _strom(enc, nonce, len(daten)))
    tag = hashlib.blake2b(b"IGP1" + nonce + geheim, key=mac, digest_size=32).digest()
    return b"IGP1" + nonce + tag + geheim


def entschluesseln(schluessel_hex, blob):
    if blob[:4] != b"IGP1":
        raise ValueError("kein verschluesselter Planer-Blob")
    enc, mac = _teilschluessel(schluessel_hex)
    nonce, tag, geheim = blob[4:20], blob[20:52], blob[52:]
    soll = hashlib.blake2b(b"IGP1" + nonce + geheim, key=mac, digest_size=32).digest()
    if not hmac.compare_digest(tag, soll):
        raise ValueError("Pruefsumme falsch (falscher Schluessel oder Datei beschaedigt)")
    return _xor(geheim, _strom(enc, nonce, len(geheim)))


def neuer_schluessel():
    return os.urandom(32).hex()


# ---------- HTTP ----------
def http(methode, url, daten=None, kopf=None, json_body=None, timeout=120, roh=False):
    kopf = dict(kopf or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        kopf["Content-Type"] = "application/json"
    elif daten is not None:
        body = urllib.parse.urlencode(daten).encode()
        kopf["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=methode, headers=kopf)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            inhalt = r.read()
            if roh:
                return r.status, inhalt
            return r.status, (json.loads(inhalt) if inhalt[:1] in (b"{", b"[") else {})
    except urllib.error.HTTPError as f:
        inhalt = f.read()
        if roh:
            return f.code, inhalt
        try:
            return f.code, json.loads(inhalt)
        except ValueError:
            return f.code, {"roh": inhalt[:300].decode(errors="replace")}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as f:
        return (0, b"") if roh else (0, {"fehler": str(f)})


# ---------- GitHub ----------
class GitHub:
    def __init__(self, token, repo):
        self.token, self.repo = token, repo
        self.api = f"https://api.github.com/repos/{repo}"

    def kopf(self, roh=False):
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github.raw" if roh else "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _wiederholt(self, methode, url, **kw):
        for i in range(4):
            st, a = http(methode, url, **kw)
            if st not in (0, 500, 502, 503, 504):
                return st, a
            time.sleep(5 * (i + 1))
        return st, a

    def lesen(self, pfad, zweig=ZWEIG):
        """Dateiinhalt (Bytes, byte-genau ueber die Git-Blob-API) oder None, wenn nicht vorhanden.
        (Der raw-Modus der Contents-API wandelt manche Binaerdateien als Text um - nicht verwenden.)"""
        st, a = self._wiederholt("GET", f"{self.api}/contents/{pfad}?ref={zweig}", kopf=self.kopf())
        if st == 404:
            return None
        if st != 200 or not isinstance(a, dict) or "sha" not in a:
            raise RuntimeError(f"GitHub lesen {pfad}: HTTP {st}")
        return self.blob(a["sha"])

    def blob(self, sha):
        st, a = self._wiederholt("GET", f"{self.api}/git/blobs/{sha}", kopf=self.kopf(), timeout=300)
        if st != 200 or a.get("encoding") != "base64":
            raise RuntimeError(f"GitHub Blob {sha}: HTTP {st}")
        daten = base64.b64decode(a["content"])
        if blob_sha(daten) != sha:
            raise RuntimeError(f"GitHub Blob {sha}: Inhalt beschaedigt")
        return daten

    def info(self, pfad, zweig=ZWEIG):
        st, a = self._wiederholt("GET", f"{self.api}/contents/{pfad}?ref={zweig}", kopf=self.kopf())
        return a if st == 200 else None

    def liste(self, pfad, zweig=ZWEIG):
        """{name: sha} eines Verzeichnisses (leer, wenn nicht vorhanden)."""
        st, a = self._wiederholt("GET", f"{self.api}/contents/{pfad}?ref={zweig}", kopf=self.kopf())
        if st == 404:
            return {}
        if st != 200 or not isinstance(a, list):
            raise RuntimeError(f"GitHub Liste {pfad}: HTTP {st}")
        return {e["name"]: e["sha"] for e in a if e.get("type") == "file"}

    # Schreiben/Loeschen ueber die Git-Data-API (Blob -> Baum -> Commit -> Zweig). Die Contents-API (PUT)
    # wandelt manche Binaerdateien beim Speichern als UTF-16-Text um (02.11.2026 beobachtet) - nicht verwenden.
    def _commit(self, aenderungen, nachricht, zweig):
        """aenderungen: [(pfad, blob_sha oder None zum Loeschen)]"""
        for versuch in range(6):
            st, ref = self._wiederholt("GET", f"{self.api}/git/ref/heads/{zweig}", kopf=self.kopf())
            if st != 200:
                raise RuntimeError(f"Zweig {zweig}: HTTP {st}")
            alt = ref["object"]["sha"]
            st, c = self._wiederholt("GET", f"{self.api}/git/commits/{alt}", kopf=self.kopf())
            baum = [{"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in aenderungen]
            st, t = self._wiederholt("POST", f"{self.api}/git/trees", kopf=self.kopf(),
                                     json_body={"base_tree": c["tree"]["sha"], "tree": baum})
            if st != 201:
                if st == 422 and all(s is None for _, s in aenderungen):
                    return  # zu loeschende Datei gibt es nicht (mehr)
                raise RuntimeError(f"Baum: HTTP {st} {t.get('message', '')}")
            if t["sha"] == c["tree"]["sha"]:
                return  # nichts geaendert
            st, neu = self._wiederholt("POST", f"{self.api}/git/commits", kopf=self.kopf(),
                                       json_body={"message": nachricht, "tree": t["sha"], "parents": [alt]})
            st, a = self._wiederholt("PATCH", f"{self.api}/git/refs/heads/{zweig}", kopf=self.kopf(),
                                     json_body={"sha": neu["sha"], "force": False})
            if st == 200:
                return
            time.sleep(2 * (versuch + 1))  # anderer Schreiber war schneller -> neu aufsetzen
        raise RuntimeError(f"Commit auf {zweig}: Konflikt blieb bestehen")

    def schreiben(self, pfad, inhalt, nachricht, zweig=ZWEIG):
        soll = blob_sha(inhalt)
        alt = self.info(pfad, zweig)
        if isinstance(alt, dict) and alt.get("sha") == soll:
            return
        st, b = self._wiederholt("POST", f"{self.api}/git/blobs", kopf=self.kopf(), timeout=300,
                                 json_body={"content": base64.b64encode(inhalt).decode(), "encoding": "base64"})
        if st != 201 or b.get("sha") != soll:
            raise RuntimeError(f"GitHub Blob {pfad}: HTTP {st} (sha {b.get('sha')} statt {soll})")
        self._commit([(pfad, soll)], nachricht, zweig)

    def loeschen(self, pfad, nachricht, zweig=ZWEIG, sha=None):
        self._commit([(pfad, None)], nachricht, zweig)

    def oeffentliche_url(self, pfad):
        besitzer, name = self.repo.split("/", 1)
        basis = f"https://{name}/" if name.endswith(".github.io") else f"https://{besitzer}.github.io/{name}/"
        return basis + pfad


def blob_sha(inhalt):
    return hashlib.sha1(b"blob %d\0" % len(inhalt) + inhalt).hexdigest()


# ---------- .env ----------
def lies_env(pfad):
    werte = {}
    if not pfad.exists():
        return werte
    for z in pfad.read_text(encoding="utf-8-sig").splitlines():
        z = z.strip()
        if z and not z.startswith("#") and "=" in z:
            k, v = z.split("=", 1)
            werte[k.strip()] = v.strip()
    return werte


def setze_env(pfad, schluessel, wert):
    zeilen, da = [], False
    for z in pfad.read_text(encoding="utf-8-sig").splitlines():
        if z.strip().startswith(schluessel + "="):
            zeilen.append(f"{schluessel}={wert}")
            da = True
        else:
            zeilen.append(z)
    if not da:
        zeilen.append(f"{schluessel}={wert}")
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
