"""El Clipódromo: detecta clips virales de Twitch, los edita en vertical y los publica.

Uso:
  python bot/main.py detectar   -> busca clips virales, los descarga y edita (deja los MP4 en clips/)
  python bot/main.py publicar   -> publica en Buffer los clips ya subidos al repo (o abre issues de aprobación)

Entre las dos fases el workflow hace commit + push, para que los MP4 tengan URL pública.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

RAIZ = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((RAIZ / "bot" / "config.yaml").read_text(encoding="utf-8"))
ESTADO_F = RAIZ / "state" / "state.json"
CLIPS_DIR = RAIZ / "clips"
REPO = os.environ.get("GITHUB_REPOSITORY", "mochivalorant05/clipodromo-bot")
RAMA = os.environ.get("GITHUB_REF_NAME", "main")
AHORA = datetime.now(timezone.utc)
DOMINIO = {"twitch": "twitch.tv", "kick": "kick.com"}


# ---------------------------------------------------------------- estado
def cargar_estado():
    if ESTADO_F.exists():
        return json.loads(ESTADO_F.read_text(encoding="utf-8"))
    return {"publicados": {}, "ids": {}, "aprobados": 0, "cola": [], "esperando": {}}


def guardar_estado(e):
    ESTADO_F.parent.mkdir(exist_ok=True)
    # olvidar claves de más de 3 días
    limite = (AHORA - timedelta(days=3)).isoformat()
    e["publicados"] = {k: v for k, v in e["publicados"].items() if v > limite}
    ESTADO_F.write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- Twitch
class Twitch:
    def __init__(self):
        r = requests.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": os.environ["TWITCH_CLIENT_ID"],
            "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
            "grant_type": "client_credentials"}, timeout=20)
        r.raise_for_status()
        self.h = {"Client-ID": os.environ["TWITCH_CLIENT_ID"],
                  "Authorization": "Bearer " + r.json()["access_token"]}
        self._juegos = {}

    def get(self, ruta, **params):
        r = requests.get("https://api.twitch.tv/helix/" + ruta, headers=self.h, params=params, timeout=20)
        r.raise_for_status()
        return r.json()["data"]

    def ids(self, logins, cache):
        faltan = [l for l in logins if l not in cache]
        for i in range(0, len(faltan), 100):
            for u in self.get("users", login=faltan[i:i + 100]):
                cache[u["login"]] = {"id": u["id"], "nombre": u["display_name"]}
        return cache

    def juego(self, gid):
        if not gid:
            return ""
        if gid not in self._juegos:
            d = self.get("games", id=gid)
            self._juegos[gid] = d[0]["name"] if d else ""
        return self._juegos[gid]


def clips_kick(slug, desde):
    """Clips recientes de un canal de Kick, convertidos al mismo formato que los de Twitch.
    Kick no ofrece clips en su API oficial; se usa la API web con huella de navegador (curl_cffi)."""
    from curl_cffi import requests as creq
    r = creq.get(f"https://kick.com/api/v2/channels/{slug}/clips", params={"sort": "date", "time": "day"},
                 impersonate="chrome", timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Kick respondió {r.status_code}")
    datos = r.json()
    lista = datos.get("clips") or datos.get("data") or []
    out = []
    for c in lista:
        creado = c.get("created_at") or ""
        if not creado or creado.replace("Z", "+00:00") < desde.replace("Z", "+00:00"):
            continue
        canal = c.get("channel") or {}
        cat = c.get("category") or {}
        out.append({
            "id": "kick_" + str(c.get("id")),
            "broadcaster_id": "kick:" + slug,
            "broadcaster_name": canal.get("username") or slug,
            "created_at": creado if creado.endswith("Z") or "+" in creado[10:] else creado + "Z",
            "view_count": max(int(c.get("view_count") or 0), int(c.get("views") or 0)),
            "title": c.get("title") or "",
            "url": c.get("clip_url") or c.get("video_url") or "",
            "vod_id": None, "vod_offset": None,
            "_cat": cat.get("name") or "",
            "_plataforma": "kick",
        })
    return out


def puntuar(clips, media_esp, det):
    """Devuelve los clips virales: uno por 'momento', con su puntuación."""
    for c in clips:
        creado = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        c["_min"] = max((AHORA - creado).total_seconds() / 60, 2)
        c["_vpm"] = c["view_count"] / c["_min"]
    # agrupar clips del mismo momento del directo
    grupos = []
    for c in sorted(clips, key=lambda x: x["created_at"]):
        for g in grupos:
            r = g[0]
            if c.get("vod_id") and c.get("vod_id") == r.get("vod_id") and c.get("vod_offset") is not None \
                    and r.get("vod_offset") is not None \
                    and abs(c["vod_offset"] - r["vod_offset"]) <= det["cluster_segundos"]:
                g.append(c); break
            if not c.get("vod_id") and abs(c["_min"] - r["_min"]) <= 2:
                g.append(c); break
        else:
            grupos.append([c])
    umbral = max(det["vpm_minimo"], media_esp * det["vpm_ratio"])
    virales = []
    for g in grupos:
        mejor = max(g, key=lambda x: x["view_count"])
        vistas = sum(x["view_count"] for x in g)
        vpm = sum(x["_vpm"] for x in g)
        if vistas < det["vistas_minimas"]:
            continue
        if len(g) >= det["cluster_minimo"] or vpm >= umbral:
            mejor["_punt"] = vpm / umbral + len(g)
            mejor["_cluster"] = len(g)
            bucket = (mejor.get("vod_offset") or 0) // 90
            mejor["_clave"] = f'{mejor["broadcaster_id"]}:{mejor.get("vod_id") or "novod"}:{bucket}'
            virales.append(mejor)
    return virales


# ---------------------------------------------------------------- edición
def descargar(clip, destino):
    if clip.get("_plataforma") == "kick" or clip["url"].endswith(".m3u8"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", clip["url"], "-c", "copy",
                        "-bsf:a", "aac_adtstoasc", str(destino)], check=True)
        return
    subprocess.run(["yt-dlp", "-q", "-f", "b", "-o", str(destino), clip["url"]], check=True)


def subtitulos_ass(entrada, ass_path):
    from faster_whisper import WhisperModel
    modelo = WhisperModel(CFG["edicion"]["modelo_whisper"], device="cpu", compute_type="int8")
    import numpy as np
    # el audio se decodifica con ffmpeg (no con PyAV) para evitar incompatibilidades de versiones
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(entrada), "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segs, _ = modelo.transcribe(audio, language="es", word_timestamps=True, vad_filter=True)
    palabras = [w for s in segs for w in (s.words or [])]

    def t(x):
        h, r = divmod(x, 3600); m, s = divmod(r, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    lineas = []
    for i in range(0, len(palabras), 3):
        grupo = palabras[i:i + 3]
        texto = " ".join(w.word.strip() for w in grupo).upper().replace("{", "").replace("}", "")
        lineas.append(f"Dialogue: 0,{t(grupo[0].start)},{t(grupo[-1].end)},S,,0,0,0,,{texto}")
    cab = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
           "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV, Encoding\n"
           "Style: S,DejaVu Sans,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,"
           "2,60,60,560,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
           "Effect, Text\n")
    ass_path.write_text(cab + "\n".join(lineas) + "\n", encoding="utf-8")
    return bool(lineas)


def editar(clip, streamer, entrada, salida):
    tmp = salida.with_suffix("")
    titulo = re.sub(r"\s+", " ", clip["title"]).strip()[:60] or f"Momentazo de {streamer}"
    lineas, actual = [], ""
    for p in titulo.upper().split():
        if len(actual) + len(p) + 1 > 20:
            lineas.append(actual); actual = p
        else:
            actual = (actual + " " + p).strip()
    lineas.append(actual)
    (tmp.parent / (tmp.name + "_t.txt")).write_text("\n".join(lineas[:3]), encoding="utf-8")
    (tmp.parent / (tmp.name + "_c.txt")).write_text(f"{DOMINIO[clip.get('_plataforma', 'twitch')]}/{clip['broadcaster_name']}", encoding="utf-8")
    (tmp.parent / (tmp.name + "_m.txt")).write_text(CFG["edicion"]["marca"], encoding="utf-8")
    fuente = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    filtro = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:3,"
        "eq=brightness=-0.2[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"drawtext=fontfile={fuente}:textfile={tmp}_t.txt:fontcolor=white:fontsize=64:line_spacing=12:"
        "x=(w-text_w)/2:y=330:box=1:boxcolor=0x7b2cffE6:boxborderw=28,"
        f"drawtext=fontfile={fuente}:textfile={tmp}_c.txt:fontcolor=0xffe600:fontsize=46:"
        "x=(w-text_w)/2:y=h-330,"
        f"drawtext=fontfile={fuente}:textfile={tmp}_m.txt:fontcolor=white@0.85:fontsize=40:"
        "x=(w-text_w)/2:y=h-260"
    )
    ass = tmp.parent / (tmp.name + ".ass")
    if CFG["edicion"]["subtitulos"]:
        try:
            if subtitulos_ass(entrada, ass):
                filtro += f",ass={ass}"
        except Exception as ex:  # los subtítulos nunca deben tumbar la publicación
            print("subtítulos fallidos:", ex)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(entrada), "-filter_complex", filtro,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
                    "-r", "30", "-movflags", "+faststart", str(salida)], check=True)
    for f in tmp.parent.glob(tmp.name + "_*.txt"):
        f.unlink()
    if ass.exists():
        ass.unlink()


# ---------------------------------------------------------------- fase 1
def detectar():
    e = cargar_estado()
    tw = Twitch()
    tw.ids(list(CFG["streamers"]), e["ids"])
    excluidas = [p.lower() for p in CFG["palabras_excluidas"]]

    def apuestas(c):
        texto = (tw.juego(c.get("game_id")) + " " + c.get("title", "")).lower()
        return any(p in texto for p in excluidas)
    det = CFG["deteccion"]
    desde = (AHORA - timedelta(minutes=det["ventana_minutos"])).isoformat().replace("+00:00", "Z")
    candidatos, todos = [], []
    for login, media in CFG["streamers"].items():
        info = e["ids"].get(login)
        if not info:
            continue
        try:
            clips = tw.get("clips", broadcaster_id=info["id"], started_at=desde, first=50)
        except Exception as ex:
            print(login, "error:", ex); continue
        clips = [c for c in clips if not apuestas(c)]
        todos += [(login, c["view_count"], c["created_at"][11:16], c["title"][:40]) for c in clips]
        for c in puntuar(clips, media, det):
            if c["_clave"] not in e["publicados"]:
                c["_streamer"] = info["nombre"]
                c["_juego"] = tw.juego(c["game_id"])
                candidatos.append(c)
    for slug, media in (CFG.get("streamers_kick") or {}).items():
        try:
            clips = clips_kick(slug, desde)
        except Exception as ex:
            print("kick", slug, "error:", ex); continue
        clips = [c for c in clips if not any(p in (c["_cat"] + " " + c["title"]).lower() for p in excluidas)]
        todos += [("kick:" + slug, c["view_count"], c["created_at"][11:16], c["title"][:40]) for c in clips]
        for c in puntuar(clips, media or 2000, det):
            if c["_clave"] not in e["publicados"]:
                c["_streamer"] = c["broadcaster_name"]
                c["_juego"] = c["_cat"]
                candidatos.append(c)
    candidatos.sort(key=lambda c: -c["_punt"])
    print(f"{len(todos)} clips en la última hora; los 8 más vistos:")
    for t in sorted(todos, key=lambda x: -x[1])[:8]:
        print("  ", t)
    print(f"{len(candidatos)} clips virales nuevos")
    CLIPS_DIR.mkdir(exist_ok=True)
    # borrar vídeos de más de 3 días
    for f in CLIPS_DIR.glob("*.mp4"):
        if time.time() - f.stat().st_mtime > 3 * 86400:
            f.unlink()
    for c in candidatos[:det["max_por_ejecucion"]]:
        crudo = CLIPS_DIR / f"{c['id']}_raw.mp4"
        final = CLIPS_DIR / f"{c['id']}.mp4"
        try:
            descargar(c, crudo)
            editar(c, c["_streamer"], crudo, final)
        except Exception as ex:
            print("fallo editando", c["id"], ex); continue
        finally:
            if crudo.exists():
                crudo.unlink()
        e["publicados"][c["_clave"]] = AHORA.isoformat()
        e["cola"].append({"id": c["id"], "titulo": c["title"], "streamer": c["_streamer"],
                          "login": c["broadcaster_name"].lower(), "juego": c["_juego"],
                          "vistas": c["view_count"], "cluster": c["_cluster"], "url_clip": c["url"],
                          "creado": AHORA.isoformat(), "plataforma": c.get("_plataforma", "twitch")})
        print("listo:", c["_streamer"], "-", c["title"])
    guardar_estado(e)


# ---------------------------------------------------------------- fase 2
def gh(metodo, ruta, **kw):
    r = requests.request(metodo, f"https://api.github.com/repos/{REPO}/{ruta}", timeout=20,
                         headers={"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                                  "Accept": "application/vnd.github+json"}, **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


CATEGORIAS = {  # categoría de Twitch -> (cómo se dice en la descripción, hashtags extra)
    "just chatting": ("en directo", "#directo #humor #reaccion"),
    "grand theft auto vi": ("jugando a GTA 6", "#gta6 #gtavi #gta"),
    "grand theft auto v": ("jugando a GTA V", "#gta #gtarp #gtav"),
    "minecraft": ("jugando a Minecraft", "#minecraft"),
    "league of legends": ("jugando a LoL", "#leagueoflegends #lol"),
    "fortnite": ("jugando a Fortnite", "#fortnite"),
    "valorant": ("jugando a Valorant", "#valorant"),
    "sports": ("viendo deporte", "#futbol #deportes"),
    "ea sports fc 26": ("jugando a EA FC", "#eafc #fifa"),
}


def texto_post(item):
    """Descripción corta de lo que pasa + hashtags adecuados al streamer y a la categoría."""
    titulo = re.sub(r"\s+", " ", item["titulo"]).strip()
    cat = item["juego"].lower()
    como, extra = CATEGORIAS.get(cat, (f"jugando a {item['juego']}" if item["juego"] else "en directo",
                                       "#" + re.sub(r"[^a-z0-9]", "", cat) if cat else "#gaming"))
    if cat not in CATEGORIAS and cat:
        extra += " #gaming"
    streamer_tag = "#" + re.sub(r"[^a-z0-9]", "", item["login"].lower())
    descripcion = f"{item['streamer']} {como}: {titulo} 😂" if titulo else f"Momentazo de {item['streamer']} {como} 😂"
    tags = f"{streamer_tag} {extra} {CFG['buffer']['hashtags']} #twitchespaña #streamerslatam"
    plat = item.get("plataforma", "twitch")
    if plat == "kick":
        tags = tags.replace("#twitch ", "#kick ").replace("#twitchespaña", "#kickespaña")
    return f"{descripcion}\n\n🎥 {DOMINIO[plat]}/{item['login']}\n\n{tags}"


def publicar_buffer(item):
    url = f"https://raw.githubusercontent.com/{REPO}/{RAMA}/clips/{item['id']}.mp4"
    clave = os.environ.get("BUFFER_API_KEY")
    canales = {k: v for k, v in CFG["buffer"]["canales"].items() if v}
    if not clave or not canales:
        print("Buffer sin configurar: no se publica", item["id"]); return False
    meta = {
        "instagram": {"instagram": {"type": "reel", "shouldShareToFeed": True}},
        "youtube": {"youtube": {"title": f"{item['titulo']} | {item['streamer']}"[:95] + " #shorts",
                                 "categoryId": "20"}},
        "tiktok": {},
    }
    q = """mutation($input: CreatePostInput!) { createPost(input: $input) {
      ... on PostActionSuccess { post { id } } ... on MutationError { message } } }"""
    ok = True
    for red, canal in canales.items():
        entrada = {"text": texto_post(item), "channelId": canal, "schedulingType": "automatic",
                   "mode": "shareNow", "assets": [{"video": {"url": url}}]}
        if meta.get(red):
            entrada["metadata"] = meta[red]
        r = requests.post("https://api.buffer.com", json={"query": q, "variables": {"input": entrada}},
                          headers={"Authorization": "Bearer " + clave}, timeout=60)
        print(red, r.status_code, r.text[:300])
        ok = ok and r.ok and "MutationError" not in r.text and '"errors"' not in r.text
    return ok


def publicar():
    e = cargar_estado()
    # 1) revisar issues de aprobación pendientes
    for num, item in list(e["esperando"].items()):
        issue = gh("GET", f"issues/{num}")
        coms = gh("GET", f"issues/{num}/comments")
        texto = " ".join(c["body"].lower() for c in coms
                         if c["author_association"] in ("OWNER", "COLLABORATOR", "MEMBER"))
        if re.search(r"\b(ok|si|sí|vale|aprobado|publica)\b", texto):
            if publicar_buffer(item):
                e["aprobados"] += 1
                gh("POST", f"issues/{num}/comments", json={"body": "✅ Publicado en TikTok, Instagram y YouTube."})
                gh("PATCH", f"issues/{num}", json={"state": "closed"})
                del e["esperando"][num]
        elif re.search(r"\b(no|descarta|descartado)\b", texto) or issue["state"] == "closed":
            gh("PATCH", f"issues/{num}", json={"state": "closed"})
            del e["esperando"][num]
    # 2) clips nuevos de la cola
    # un clip que lleva más de 12 horas esperando ya no es noticia: se descarta
    limite = (AHORA - timedelta(hours=12)).isoformat()
    e["cola"] = [i for i in e["cola"] if i.get("creado", AHORA.isoformat()) > limite]
    while e["cola"]:
        item = e["cola"].pop(0)
        if e["aprobados"] + len(e["esperando"]) < CFG["aprobaciones_manuales"]:
            video = f"https://github.com/{REPO}/raw/{RAMA}/clips/{item['id']}.mp4"
            cuerpo = (f"**{item['streamer']}** · {item['juego']} · {item['vistas']} visitas · "
                      f"{item['cluster']} clips del mismo momento\n\n▶️ Vídeo editado: {video}\n"
                      f"🔗 Clip original: {item['url_clip']}\n\nTexto del post:\n```\n{texto_post(item)}\n```\n\n"
                      "Responde **ok** para publicarlo o **no** para descartarlo.")
            n = gh("POST", "issues", json={"title": f"Aprobar clip: {item['titulo'][:80]}", "body": cuerpo})
            e["esperando"][str(n["number"])] = item
        elif e["aprobados"] >= CFG["aprobaciones_manuales"]:
            if not publicar_buffer(item):
                e["cola"].insert(0, item); break
        else:
            # aún hay aprobaciones manuales sin responder: el resto espera
            e["cola"].insert(0, item); break
    guardar_estado(e)


if __name__ == "__main__":
    {"detectar": detectar, "publicar": publicar}[sys.argv[1]]()
