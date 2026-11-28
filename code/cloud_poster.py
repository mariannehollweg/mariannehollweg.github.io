"""Instagram-Cloud-Poster fuer beide Konten (Marianne Hollweg, Hema Lotfeld) - laeuft in GitHub Actions,
ohne eingeschalteten PC.

Ablauf (Workflow .github/workflows/instagram_planer.yml im Repo mariannehollweg/mariannehollweg.github.io,
alle 15 Minuten von 06 bis 23 Uhr):
  - Tokens kommen aus GitHub-Secrets (IG_TOKEN_MARIANNE/HEMA, IG_USER_ID_...); erneuert wird alle 7 Tage,
    der neue Token wird wieder als Secret gespeichert
  - liest je Konto den verschluesselten Zustand (letzter Post, offene Loesungen) aus Zweig "planer"
  - postet faellige Raetsel-Loesungen als Kommentar (Hema)
  - prueft die verschluesselte Warteschlange konten/<konto>/warteschlange/<ordner>__<hash>.bin
  - veroeffentlicht hoechstens EINEN faelligen Post je Konto und Lauf (>= 3 h Abstand), danach die Story (Marianne)
  - schreibt das Ergebnis sofort nach konten/<konto>/erledigt.json (der PC holt es mit cloud_sync.py ab)
Die Warteschlange fuellt der PC (cloud_sync.py) aus den gepruefen und freigegebenen Ordnern.

Aufruf:
    python3 code/cloud_poster.py              # normaler Lauf
    python3 code/cloud_poster.py --probe      # nur anzeigen, was faellig waere (schreibt nichts)
    python3 code/cloud_poster.py --aufraeumen # Zweig planer auf einen Commit eindampfen (naechtlich)
Umgebung: GH_PAT (GitHub-Token: Contents + Secrets schreiben), PLANER_SCHLUESSEL, GITHUB_REPOSITORY, IG_TOKEN_*/IG_USER_ID_*.
Lokal (Test auf dem PC) werden die Werte aus Marianne_Instagram\\.env gelesen.
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ig_gemeinsam import IG, KONTO_ORDNER, GitHub, entschluesseln, http, lies_env, verschluesseln  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
KONTEN = {
    # Marianne seit 06.11.2026 (Sebastian): nur jeden zweiten Tag ein Post (Posttage: gerader Abstand zum Anker), 1 je Tag
    "marianne": {"medien": "medien/", "medien_loeschen": False, "thumb_offset": "1500",
                 "rhythmus_tage": 2, "rhythmus_anker": "2026-11-06", "max_pro_tag": 1},
    "hema": {"medien": "medien/hema/", "medien_loeschen": True, "thumb_offset": None},
}
ABSTAND_STUNDEN = 3
VORMITTAG_BIS = "15:00"
MAX_PRO_TAG = 2              # Sebastian 02.11.2026: nie mehr als zwei Beitraege an einem Kalendertag (auch beim Nachholen)
MAX_FEHLVERSUCHE = 6          # bei 15-min-Takt rund 1,5 Stunden
TOKEN_ERNEUERN_TAGE = 7


def jetzt_berlin():
    return dt.datetime.now(BERLIN).replace(tzinfo=None, microsecond=0)


class Konto:
    def __init__(self, name, gh, schluessel, probe):
        self.name, self.gh, self.schl, self.probe = name, gh, schluessel, probe
        self.cfg = KONTEN[name]
        self.basis = f"konten/{name}"
        self.zeilen = []
        roh = gh.lesen(f"{self.basis}/zustand.bin")
        if roh is None:
            raise RuntimeError(f"{name}: zustand.bin fehlt (einrichten.py noch nicht gelaufen?)")
        self.zustand = json.loads(entschluesseln(schluessel, roh))
        # Tokens liegen nur als GitHub-Secrets vor (IG_TOKEN_<KONTO>, IG_USER_ID_<KONTO>), nie im Repo
        self.token = os.environ.get(f"IG_TOKEN_{name.upper()}")
        self.uid = os.environ.get(f"IG_USER_ID_{name.upper()}")
        if not self.token or not self.uid:
            raise RuntimeError(f"{name}: Secret IG_TOKEN_{name.upper()} / IG_USER_ID_{name.upper()} fehlt")
        self.erledigt = json.loads(gh.lesen(f"{self.basis}/erledigt.json") or b"{}")

    # ----- Protokoll und Speichern -----
    def log(self, text):
        zeile = f"{jetzt_berlin():%Y-%m-%d %H:%M:%S}  {text}"
        print(f"[{self.name}] {zeile}", flush=True)
        self.zeilen.append(zeile)

    def speichere_erledigt(self, nachricht):
        if not self.probe:
            self.gh.schreiben(f"{self.basis}/erledigt.json",
                              json.dumps(self.erledigt, ensure_ascii=False, indent=1).encode("utf-8"), nachricht)

    def speichere_zustand(self, nachricht):
        if not self.probe:
            self.gh.schreiben(f"{self.basis}/zustand.bin",
                              verschluesseln(self.schl, json.dumps(self.zustand, ensure_ascii=False).encode("utf-8")), nachricht)

    def speichere_protokoll(self):
        if self.probe or not self.zeilen:
            return
        alt = (self.gh.lesen(f"{self.basis}/protokoll.txt") or b"").decode("utf-8")
        zeilen = (alt.splitlines() + self.zeilen)[-3000:]
        self.gh.schreiben(f"{self.basis}/protokoll.txt", ("\n".join(zeilen) + "\n").encode("utf-8"), f"{self.name}: Protokoll")
        self.zeilen = []

    # ----- Instagram -----
    def ig(self, methode, pfad, **daten):
        daten["access_token"] = self.token
        if methode == "GET":
            return http("GET", f"{IG}/{pfad}?" + urllib.parse.urlencode(daten))
        return http("POST", f"{IG}/{pfad}", daten=daten)

    def pruefe(self, st, a, was):
        if st != 200 or "id" not in a:
            raise RuntimeError(f"{was}: HTTP {st} {a.get('error', a)}")
        return a["id"]

    def warte_fertig(self, container, minuten=10):
        ende = time.time() + minuten * 60
        while time.time() < ende:
            st, a = self.ig("GET", container, fields="status_code,status")
            code = a.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Container {container}: {a}")
            time.sleep(10)
        raise RuntimeError(f"Container {container} nicht fertig nach {minuten} Minuten")

    def veroeffentlichen_mit_wiederholung(self, cid, versuche=4):
        uid = self.uid
        letzter = None
        for i in range(versuche):
            st, a = self.ig("POST", f"{uid}/media_publish", creation_id=cid)
            if st == 200 and "id" in a:
                return a["id"]
            letzter = (st, a)
            self.log(f"Veroeffentlichen Versuch {i + 1}: HTTP {st} {a.get('error', a)}")
            time.sleep(30 * (i + 1))
            st2, c = self.ig("GET", cid, fields="status_code")
            if c.get("status_code") == "PUBLISHED":
                st3, m = self.ig("GET", f"{uid}/media", fields="id,timestamp", limit="1")
                if m.get("data"):
                    self.log("Container war trotz Fehler veroeffentlicht - kein zweiter Versuch")
                    return m["data"][0]["id"]
        raise RuntimeError(f"Veroeffentlichen: HTTP {letzter[0]} {letzter[1].get('error', letzter[1])}")

    def schon_auf_instagram(self, caption):
        """Schutz gegen Doppelposts: steht dieselbe Bildunterschrift schon unter einem der letzten Beitraege?"""
        st, m = self.ig("GET", f"{self.uid}/media", fields="id,caption,permalink,timestamp", limit="10")
        if st != 200:
            return None
        for e in m.get("data", []):
            if (e.get("caption") or "").strip() == caption.strip():
                return e
        return None

    def token_erneuern(self):
        letzte = self.zustand.get("INSTAGRAM_TOKEN_ERNEUERT", "2000-01-01")
        if (jetzt_berlin().date() - dt.date.fromisoformat(letzte)).days < TOKEN_ERNEUERN_TAGE:
            return
        st, a = http("GET", "https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
            {"grant_type": "ig_refresh_token", "access_token": self.token}))
        if st == 200 and a.get("access_token"):
            if a["access_token"] != self.token:
                secret_setzen(self.gh, f"IG_TOKEN_{self.name.upper()}", a["access_token"])
                self.token = a["access_token"]
            self.zustand["INSTAGRAM_TOKEN_ERNEUERT"] = jetzt_berlin().date().isoformat()
            self.speichere_zustand(f"{self.name}: Token erneuert")
            self.log(f"Token erneuert, gueltig noch ca. {int(a.get('expires_in', 0)) // 86400} Tage")
        else:
            self.log(f"WARNUNG Token-Erneuerung fehlgeschlagen: {a.get('error', a)}")

    # ----- Medien -----
    def medium_hochladen(self, pfad, inhalt):
        self.gh.schreiben(pfad, inhalt, f"Medien {pfad}", zweig="main")
        url = self.gh.oeffentliche_url(pfad)
        start = time.time(); ende = start + 10 * 60; angestossen = 0
        while time.time() < ende:
            # 09.11.2026: Schlaegt der Pages-Build fehl, loest eine erneute Ablage derselben Datei keinen neuen Build aus
            # (gleicher Blob -> kein Commit). Darum nach 3 und 6 Minuten einen kleinen Anstoss-Commit setzen.
            if angestossen < 2 and time.time() - start > 180 * (angestossen + 1):
                angestossen += 1
                try:
                    self.gh.schreiben("medien/_pages_anstoss.txt", f"{dt.datetime.utcnow().isoformat()} {pfad}\n".encode(), "Pages-Build anstossen", zweig="main")
                    self.log(f"HINWEIS Pages-Build angestossen fuer {pfad} ({angestossen}.)")
                except Exception as e:  # noqa: BLE001
                    self.log(f"WARNUNG Pages-Anstoss fehlgeschlagen: {e}")
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
                    if r.status == 200:
                        return url
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(15)
        raise RuntimeError(f"Medium nach 10 Minuten nicht erreichbar: {url}")

    # ----- Loesungen (Raetsel) -----
    def loesungen_posten(self):
        offen = self.zustand.get("loesungen_offen") or []
        if not offen:
            return
        rest, geaendert = [], False
        for e in offen:
            if jetzt_berlin() < dt.datetime.fromisoformat(e["ab"]):
                rest.append(e)
                continue
            if self.probe:
                print(f"[{self.name}]   wuerde Loesung posten: {e['ordner']}")
                rest.append(e)
                continue
            st, a = self.ig("POST", f"{e['media_id']}/comments", message=e["text"])
            geaendert = True
            if st == 200 and "id" in a:
                self.log(f"LOESUNG gepostet {e['ordner']} (Kommentar {a['id']})")
            else:
                e["versuche"] = e.get("versuche", 0) + 1
                self.log(f"WARNUNG Loesung {e['ordner']} nicht gepostet (Versuch {e['versuche']}): HTTP {st} {a.get('error', a)}")
                if e["versuche"] < MAX_FEHLVERSUCHE:
                    rest.append(e)
        if geaendert:
            self.zustand["loesungen_offen"] = rest
            self.speichere_zustand(f"{self.name}: Loesungen")

    # ----- Warteschlange -----
    def warteschlange(self):
        """[(ordnername, dateiname, sha)] - nur Eintraege, deren Datum (Ordneranfang JJJJ-MM-TT) heute oder frueher ist."""
        heute = jetzt_berlin().date().isoformat()
        liste = self.gh.liste(f"{self.basis}/warteschlange")
        aus = []
        for datei, sha in sorted(liste.items()):
            if not datei.endswith(".bin") or "__" not in datei:
                continue
            ordner = datei[:-4].rsplit("__", 1)[0]
            if ordner in self.erledigt:
                if not self.probe:  # Rest eines bereits erledigten Posts
                    self.gh.loeschen(f"{self.basis}/warteschlange/{datei}", f"{self.name}: Rest {ordner}", sha=sha)
                continue
            if ordner[:10] <= heute:
                aus.append((ordner, datei, sha))
        return aus

    def paket(self, datei):
        roh = self.gh.lesen(f"{self.basis}/warteschlange/{datei}")
        return zipfile.ZipFile(io.BytesIO(entschluesseln(self.schl, roh)))

    def faellig(self, ordner, meta, jetzt):
        if not meta.get("freigegeben"):
            return False, "nicht freigegeben"
        zp = dt.datetime.fromisoformat(f"{meta['datum']}T{meta.get('uhrzeit', '18:00')}")
        if zp > jetzt:
            return False, f"erst {zp:%d.%m. %H:%M}"
        verfall = meta.get("verfaellt_nach_stunden")
        if verfall is not None and jetzt > zp + dt.timedelta(hours=float(verfall)):
            return False, "verpasst"
        if ordner.endswith("_vormittag") and jetzt > dt.datetime.fromisoformat(f"{meta['datum']}T{VORMITTAG_BIS}"):
            return False, "verpasst"
        return True, "faellig"

    def abstand_ok(self, jetzt):
        try:
            letzter = dt.datetime.fromisoformat(self.zustand.get("letzter_post", ""))
        except ValueError:
            return True, "kein letzter Post bekannt"
        frei_ab = letzter + dt.timedelta(hours=ABSTAND_STUNDEN)
        if jetzt < frei_ab:
            return False, f"Abstand: letzter Post {letzter:%d.%m. %H:%M}, naechster fruehestens {frei_ab:%d.%m. %H:%M}"
        return True, "Abstand ok"

    def heute_gepostet(self, jetzt):
        tag = jetzt.date().isoformat()
        n = sum(1 for e in self.erledigt.values() if e.get("status") == "veroeffentlicht" and str(e.get("erfasst", "")).startswith(tag))
        return n + int((self.zustand.get("vorher_gepostet") or {}).get(tag, 0))  # vom PC vor der Umstellung

    def erledigt_setzen(self, ordner, datei, sha, eintrag, nachricht):
        eintrag["erfasst"] = jetzt_berlin().isoformat(timespec="seconds")
        self.erledigt[ordner] = eintrag
        self.speichere_erledigt(nachricht)          # zuerst festhalten (Schutz gegen Doppelposts)
        if not self.probe:
            self.gh.loeschen(f"{self.basis}/warteschlange/{datei}", nachricht, sha=sha)

    # ----- Veroeffentlichen -----
    def veroeffentlichen(self, ordner, paket, meta):
        uid = self.uid
        typ = meta["typ"]
        namen = paket.namelist()
        ki = "true" if meta.get("is_ai_generated") else "false"
        praefix = f"{self.cfg['medien']}{ordner}/"
        hochgeladen = []

        def hoch(name):
            pfad = praefix + name
            url = self.medium_hochladen(pfad, paket.read(name))
            hochgeladen.append(pfad)
            return url

        try:
            if typ == "REELS":
                extra = {}
                if "cover.jpg" in namen:
                    extra["cover_url"] = hoch("cover.jpg")
                to = meta.get("thumb_offset") or self.cfg["thumb_offset"]
                if to:
                    extra["thumb_offset"] = str(to)
                url = hoch("reel.mp4")
                cid = self.pruefe(*self.ig("POST", f"{uid}/media", media_type="REELS", video_url=url, caption=meta["caption"],
                                           share_to_feed="true", is_ai_generated=ki, **extra), "Reel-Container")
                self.warte_fertig(cid, 15)
            else:
                bilder = sorted(n for n in namen if n.startswith("bild_") and n.endswith(".jpg"))
                if not bilder:
                    raise RuntimeError("keine Mediendateien im Paket")
                urls = [hoch(b) for b in bilder]
                alts = meta.get("alt_text") or []
                if typ == "IMAGE":
                    extra = {"alt_text": alts[0]} if alts else {}
                    cid = self.pruefe(*self.ig("POST", f"{uid}/media", image_url=urls[0], caption=meta["caption"],
                                               is_ai_generated=ki, **extra), "Bild-Container")
                elif typ == "CAROUSEL":
                    kinder = []
                    for i, u in enumerate(urls):
                        extra = {"alt_text": alts[i]} if i < len(alts) else {}
                        kinder.append(self.pruefe(*self.ig("POST", f"{uid}/media", image_url=u, is_carousel_item="true", **extra),
                                                  f"Karussell-Bild {i + 1}"))
                    for k in kinder:
                        self.warte_fertig(k, 3)
                    cid = self.pruefe(*self.ig("POST", f"{uid}/media", media_type="CAROUSEL", children=",".join(kinder),
                                               caption=meta["caption"], is_ai_generated=ki), "Karussell-Container")
                else:
                    raise RuntimeError(f"unbekannter Typ {typ}")
                self.warte_fertig(cid)
            mid = self.veroeffentlichen_mit_wiederholung(cid)
        finally:
            if self.cfg["medien_loeschen"]:
                for pf in hochgeladen:
                    try:
                        self.gh.loeschen(pf, f"Medien entfernt {pf}", zweig="main")
                    except Exception as f:  # noqa: BLE001
                        self.log(f"WARNUNG Medien-Loeschen {pf}: {f}")
        st, info = self.ig("GET", mid, fields="permalink,timestamp")
        return {"media_id": mid, "permalink": info.get("permalink"), "zeit": info.get("timestamp")}

    def story(self, ordner, paket, meta):
        if "story/story.jpg" not in paket.namelist():
            return None
        ki = "true" if (meta.get("is_ai_generated") or "story/ki.txt" in paket.namelist()) else "false"
        try:
            uid = self.uid
            url = self.medium_hochladen(f"{self.cfg['medien']}{ordner}/story.jpg", paket.read("story/story.jpg"))
            cid = self.pruefe(*self.ig("POST", f"{uid}/media", image_url=url, media_type="STORIES", is_ai_generated=ki), "Story-Container")
            self.warte_fertig(cid, 5)
            mid = self.pruefe(*self.ig("POST", f"{uid}/media_publish", creation_id=cid), "Story veroeffentlichen")
            self.log(f"STORY {ordner} veroeffentlicht (media_id {mid})")
            return mid
        except Exception as f:  # noqa: BLE001
            self.log(f"FEHLER Story {ordner}: {f}")
            return None

    # ----- Hauptlauf je Konto -----
    def lauf(self):
        jetzt = jetzt_berlin()
        try:
            self.loesungen_posten()
        except Exception as f:  # noqa: BLE001
            self.log(f"WARNUNG Loesungen: {f}")
        kandidaten = []
        for ordner, datei, sha in self.warteschlange():
            paket = self.paket(datei)
            meta = json.loads(paket.read("post.json").decode("utf-8"))
            ok, grund = self.faellig(ordner, meta, jetzt)
            print(f"[{self.name}]   {ordner}: {grund}", flush=True)
            if grund == "verpasst":
                if not self.probe:
                    self.erledigt_setzen(ordner, datei, sha, {"status": "verpasst"}, f"{self.name}: verpasst {ordner}")
                    self.log(f"VERPASST {ordner} (Termin zu lange verstrichen, nicht mehr veroeffentlicht)")
                continue
            if ok:
                zp = f"{meta['datum']}T{meta.get('uhrzeit', '18:00')}"
                kandidaten.append((zp, ordner, datei, sha, paket, meta))
        if not kandidaten:
            return
        kandidaten.sort()
        sofort = [k for k in kandidaten if k[5].get("sofort")]   # 02.11.2026: Ersatz fuer geloeschten Post auf ausdrueckliche Anweisung
        if sofort:
            kandidaten = sofort
            print(f"[{self.name}]   SOFORT {sofort[0][1]}: Abstand und Tageslimit einmalig ausgesetzt", flush=True)
        else:
            ok, grund = self.abstand_ok(jetzt)
            print(f"[{self.name}]   {grund}", flush=True)
            if not ok:
                return
            rh = self.cfg.get("rhythmus_tage")
            if rh and (jetzt.date() - dt.date.fromisoformat(self.cfg["rhythmus_anker"])).days % rh:
                print(f"[{self.name}]   heute kein Posttag (nur jeden {rh}. Tag)", flush=True)
                return
            n = self.heute_gepostet(jetzt)
            if n >= self.cfg.get("max_pro_tag", MAX_PRO_TAG):
                print(f"[{self.name}]   Tageslimit: heute schon {n} Beitraege", flush=True)
                return
        zp, ordner, datei, sha, paket, meta = kandidaten[0]
        if self.probe:
            print(f"[{self.name}]   wuerde veroeffentlichen: {ordner}")
            return
        self.token_erneuern()
        try:
            schon = self.schon_auf_instagram(meta["caption"])
            if schon:
                ergebnis = {"media_id": schon["id"], "permalink": schon.get("permalink"), "zeit": schon.get("timestamp")}
                self.log(f"HINWEIS {ordner} stand schon auf Instagram - nicht erneut gepostet")
            else:
                ergebnis = self.veroeffentlichen(ordner, paket, meta)
        except Exception as fehler:  # noqa: BLE001
            fv = self.zustand.setdefault("fehlversuche", {})
            fv[ordner] = fv.get(ordner, 0) + 1
            self.log(f"FEHLER {ordner} (Versuch {fv[ordner]}): {fehler}")
            if fv[ordner] >= MAX_FEHLVERSUCHE:
                self.erledigt_setzen(ordner, datei, sha, {"status": "fehler", "grund": str(fehler)[:300]},
                                     f"{self.name}: aufgegeben {ordner}")
                self.log(f"AUFGEGEBEN {ordner} nach {fv[ordner]} Fehlversuchen -> PC verschiebt nach warteschlange_fehler")
                fv.pop(ordner, None)
            self.speichere_zustand(f"{self.name}: Fehlversuch {ordner}")
            return
        eintrag = {"status": "veroeffentlicht", "typ": meta["typ"], "plan_nr": meta.get("plan_nr"), "quelle": meta.get("quelle"), **ergebnis}
        self.erledigt_setzen(ordner, datei, sha, eintrag, f"{self.name}: veroeffentlicht {ordner}")
        self.log(f"VEROEFFENTLICHT {ordner} {meta['typ']} -> {ergebnis['permalink']}")
        self.zustand["letzter_post"] = jetzt_berlin().isoformat(timespec="seconds")
        self.zustand.get("fehlversuche", {}).pop(ordner, None)
        if meta.get("loesung_kommentar"):
            ab = jetzt_berlin() + dt.timedelta(hours=float(meta.get("loesung_nach_stunden", 20)))
            self.zustand.setdefault("loesungen_offen", []).append(
                {"media_id": ergebnis["media_id"], "ordner": ordner, "text": meta["loesung_kommentar"], "ab": ab.isoformat(timespec="minutes")})
            self.log(f"Loesung fuer {ordner} wird ab {ab:%d.%m. %H:%M} als Kommentar gepostet")
        self.speichere_zustand(f"{self.name}: Zustand nach {ordner}")
        sid = self.story(ordner, paket, meta)
        if sid:
            self.erledigt[ordner]["story_media_id"] = sid
            self.speichere_erledigt(f"{self.name}: Story {ordner}")


def secret_setzen(gh, name, wert):
    """Repository-Secret setzen (braucht PyNaCl und ein Token mit 'Secrets: Read and write')."""
    import base64
    from nacl import encoding, public
    st, k = http("GET", f"{gh.api}/actions/secrets/public-key", kopf=gh.kopf())
    if st != 200:
        raise RuntimeError(f"Secret {name}: Schluessel nicht lesbar (HTTP {st})")
    box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
    st, a = http("PUT", f"{gh.api}/actions/secrets/{name}", kopf=gh.kopf(),
                 json_body={"encrypted_value": base64.b64encode(box.encrypt(wert.encode())).decode(), "key_id": k["key_id"]})
    if st not in (201, 204):
        raise RuntimeError(f"Secret {name}: HTTP {st}")


def aufraeumen(gh):
    """Zweig planer auf einen einzigen Commit eindampfen (sonst waechst das Repo mit jedem Video)."""
    st, ref = http("GET", f"{gh.api}/git/ref/heads/planer", kopf=gh.kopf())
    if st != 200:
        print("kein Zweig planer")
        return
    sha = ref["object"]["sha"]
    st, c = http("GET", f"{gh.api}/git/commits/{sha}", kopf=gh.kopf())
    if not c.get("parents"):
        print("schon eingedampft")
        return
    st, neu = http("POST", f"{gh.api}/git/commits", kopf=gh.kopf(),
                   json_body={"message": f"Planer-Stand {jetzt_berlin():%Y-%m-%d}", "tree": c["tree"]["sha"], "parents": []})
    st2, ref2 = http("GET", f"{gh.api}/git/ref/heads/planer", kopf=gh.kopf())
    if ref2["object"]["sha"] != sha:
        print("Zweig hat sich gerade geaendert - morgen erneut")
        return
    st, a = http("PATCH", f"{gh.api}/git/refs/heads/planer", kopf=gh.kopf(), json_body={"sha": neu["sha"], "force": True})
    print(f"eingedampft: HTTP {st}")


def zugang():
    token, repo, schl = os.environ.get("GH_PAT"), os.environ.get("GITHUB_REPOSITORY"), os.environ.get("PLANER_SCHLUESSEL")
    if token and schl:
        return token, repo or "mariannehollweg/mariannehollweg.github.io", schl
    p = Path(__file__).resolve()
    while p != p.parent and not (p / "BUCHPROJEKTE_WURZEL.txt").exists():
        p = p.parent
    env = lies_env(p.joinpath(*KONTO_ORDNER["marianne"], ".env"))
    for k in KONTO_ORDNER:  # lokaler Test: Tokens aus den .env-Dateien (nur im Speicher)
        kenv = lies_env(p.joinpath(*KONTO_ORDNER[k], ".env"))
        os.environ.setdefault(f"IG_TOKEN_{k.upper()}", kenv.get("INSTAGRAM_TOKEN", ""))
        os.environ.setdefault(f"IG_USER_ID_{k.upper()}", kenv.get("INSTAGRAM_USER_ID", ""))
    return env["GITHUB_TOKEN"], env["GITHUB_REPO"], env["PLANER_SCHLUESSEL"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--aufraeumen", action="store_true")
    ap.add_argument("--konto", choices=sorted(KONTEN))
    ap.add_argument("--dauer", type=int, default=0, help="Minuten, die der Lauf aktiv bleibt (alle 5 min pruefen)")
    a = ap.parse_args()
    token, repo, schl = zugang()
    gh = GitHub(token, repo)
    if a.aufraeumen:
        aufraeumen(gh)
        return
    # GitHub startet Zeitplan-Laeufe oft nur alle 1-3 Stunden. Deshalb bleibt ein Lauf bis zu --dauer Minuten
    # aktiv und prueft alle 5 Minuten selbst (oeffentliches Repo: Actions-Minuten kostenlos).
    ende = time.time() + a.dauer * 60
    fehler = False
    while True:
        print(f"Lauf {jetzt_berlin():%Y-%m-%d %H:%M} (Berlin){' PROBE' if a.probe else ''}", flush=True)
        for name in ([a.konto] if a.konto else list(KONTEN)):
            konto = None
            try:
                konto = Konto(name, gh, schl, a.probe)
                konto.lauf()
            except Exception as f:  # noqa: BLE001
                fehler = True
                print(f"[{name}] FEHLER im Lauf: {f}", flush=True)
                if konto:
                    konto.log(f"FEHLER im Lauf: {f}")
            finally:
                if konto:
                    try:
                        konto.speichere_protokoll()
                    except Exception as f:  # noqa: BLE001
                        print(f"[{name}] Protokoll nicht gespeichert: {f}")
        if a.probe or time.time() + 300 > ende or jetzt_berlin().hour >= 23:
            break
        time.sleep(300)
    sys.exit(1 if fehler else 0)


if __name__ == "__main__":
    main()
