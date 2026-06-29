#!/usr/bin/env python3
"""
Steam Sale Ranker — backend Flask
=================================
Serve o frontend estático e expõe 2 endpoints JSON:

  GET /api/games
      Devolve o JSON gerado 1x/dia pelo cron (steam_sale_ranker.py --json).
      Lê de DATA_FILE (default: data/games.json). Inclui a data de geração.

  GET /api/steam-user?profile=<vanity-ou-url>
      Resolve o perfil Steam informado (URL .../id/<vanity> ou .../profiles/<id>,
      ou só o vanity), e busca SERVER-SIDE, sem API key, usando só endpoints
      públicos:
        - wishlist : store.steampowered.com/wishlist/.../wishlistdata/?p=N
        - owned    : steamcommunity.com/.../games?tab=all&xml=1  (XML)
      Devolve {"ok":true,"wishlist":[appids],"owned":[appids]} ou
      {"ok":false,"error":"..."} em caso de perfil privado / inexistente.

Tudo é same-origin (front + API no mesmo host) → sem CORS.
"""

import os
import re
import xml.etree.ElementTree as ET

import requests
from flask import Flask, jsonify, request, send_from_directory

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR  = os.path.join(BASE_DIR, "static")
# Caminho do JSON gerado pelo cron. Override via env DATA_FILE.
DATA_FILE   = os.environ.get(
    "DATA_FILE", os.path.join(BASE_DIR, "data", "games.json")
)
HTTP_TIMEOUT = 10  # segundos — todos os fetches externos

# Headers de browser (a Steam bloqueia/limita user-agents "robôs").
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/javascript, text/xml, */*; q=0.01",
}

app = Flask(__name__, static_folder=None)

# ─── Frontend estático ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/favicon.svg")
def favicon():
    # favicon.svg vive na raiz do projeto (mantido junto do gerador)
    return send_from_directory(BASE_DIR, "favicon.svg", mimetype="image/svg+xml")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

# ─── /api/games ───────────────────────────────────────────────────────────────

@app.route("/api/games")
def api_games():
    """Devolve o JSON gerado pelo cron. 503 se ainda não houver dados."""
    if not os.path.exists(DATA_FILE):
        return jsonify({
            "ok": False,
            "error": "dados ainda nao gerados — rode o cron (steam_sale_ranker.py --json)",
            "blocks": [],
        }), 503
    # send_from_directory aplica caching/etag/last-modified de graça.
    directory = os.path.dirname(DATA_FILE) or "."
    filename  = os.path.basename(DATA_FILE)
    return send_from_directory(directory, filename, mimetype="application/json")

# ─── /api/steam-user ──────────────────────────────────────────────────────────

# Aceita: URL completa, ".../id/<vanity>", ".../profiles/<steamid64>", ou só o vanity.
_RE_ID       = re.compile(r"steamcommunity\.com/id/([^/?#]+)", re.IGNORECASE)
_RE_PROFILES = re.compile(r"steamcommunity\.com/profiles/(\d+)", re.IGNORECASE)
_RE_STEAMID  = re.compile(r"^\d{17}$")


def _parse_profile(raw: str):
    """
    Normaliza a entrada do usuário em (kind, value):
      kind == 'id'        → vanity URL  (.../id/<value>)
      kind == 'profiles'  → steamid64   (.../profiles/<value>)
    Retorna (None, None) se não der pra extrair nada.
    """
    s = (raw or "").strip()
    if not s:
        return None, None

    m = _RE_PROFILES.search(s)
    if m:
        return "profiles", m.group(1)

    m = _RE_ID.search(s)
    if m:
        return "id", m.group(1)

    # Sem URL: se forem 17 dígitos é um steamid64, senão tratamos como vanity.
    bare = s.rstrip("/").split("/")[-1]
    if _RE_STEAMID.match(bare):
        return "profiles", bare
    # remove um eventual "@" colado por engano e espaços
    bare = bare.lstrip("@").strip()
    if bare:
        return "id", bare
    return None, None


def _fetch_wishlist(kind: str, value: str):
    """
    Busca a wishlist pública paginando ?p=0,1,2...
    O endpoint devolve um dict {appid: {...}} por página, ou [] quando acaba.
    Retorna (appids:list[int]|None, fatal_error:str|None).
      - lista (possivelmente vazia) em sucesso
      - None + erro quando a wishlist é privada / perfil inexistente
    """
    base = f"https://store.steampowered.com/wishlist/{kind}/{value}/wishlistdata/"
    appids: list[int] = []
    page = 0
    MAX_PAGES = 50  # ~5000 itens; trava de segurança contra loop infinito
    saw_any = False

    while page < MAX_PAGES:
        try:
            r = requests.get(
                base, params={"p": page},
                headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException:
            # erro de rede na 1a página = fatal; nas seguintes, devolve o que tem
            if page == 0:
                return None, "falha de rede ao buscar a wishlist"
            break

        # 500/404 etc. — privado ou inexistente (só fatal se for logo na 1a página)
        if r.status_code != 200:
            if page == 0:
                return None, "wishlist privada ou perfil nao encontrado"
            break

        # A Steam responde 200 com corpo vazio/`{"success":2}`/lista vazia quando
        # a wishlist é privada ou acabou a paginação.
        try:
            data = r.json()
        except ValueError:
            if page == 0:
                return None, "wishlist privada (sem dados publicos)"
            break

        if isinstance(data, dict):
            if "success" in data and not any(k.isdigit() for k in data.keys()):
                # {"success": 2} → privada (só fatal se nunca vimos nada)
                if not saw_any:
                    return None, "wishlist privada (ative 'wishlist publica' na Steam)"
                break
            keys = [k for k in data.keys() if str(k).isdigit()]
            if not keys:
                break
            for k in keys:
                try:
                    appids.append(int(k))
                except (TypeError, ValueError):
                    pass
            saw_any = True
            page += 1
            continue

        # lista vazia [] = fim da paginação
        break

    return appids, None


def _fetch_owned(kind: str, value: str):
    """
    Busca os jogos do perfil via .../games?tab=all&xml=1 (XML público).
    Retorna (appids:list[int]|None, fatal_error:str|None).
    Perfil privado → o XML traz <privacyState>private</privacyState> sem <games>.
    """
    url = f"https://steamcommunity.com/{kind}/{value}/games"
    try:
        r = requests.get(
            url, params={"tab": "all", "xml": 1},
            headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException:
        return None, "falha de rede ao buscar a biblioteca"

    if r.status_code != 200:
        return None, "perfil nao encontrado"

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return None, "resposta invalida do perfil"

    # <response><error>...</error></response> → vanity/id inexistente
    err = root.find("error")
    if err is not None and (err.text or "").strip():
        return None, "perfil nao encontrado"

    privacy = root.find("privacyState")
    games_el = root.find("games")
    if games_el is None:
        if privacy is not None and (privacy.text or "").strip().lower() != "public":
            return None, "detalhes de jogos privados (deixe 'Detalhes do jogo: Publico')"
        # público mas sem jogos listados
        return [], None

    appids: list[int] = []
    for game in games_el.findall("game"):
        ap = game.find("appID")
        if ap is not None and (ap.text or "").strip().isdigit():
            appids.append(int(ap.text.strip()))
    return appids, None


@app.route("/api/steam-user")
def api_steam_user():
    profile_raw = request.args.get("profile", "")
    kind, value = _parse_profile(profile_raw)
    if not kind:
        return jsonify({
            "ok": False,
            "error": "informe seu perfil, ex.: https://steamcommunity.com/id/seu_perfil",
        })

    # Tentamos como informado; se for vanity (id) e falhar, ainda assim
    # devolvemos erro amigável (não temos como adivinhar o steamid64 sem API key).
    wishlist, w_err = _fetch_wishlist(kind, value)
    owned,    o_err = _fetch_owned(kind, value)

    # Se AMBOS falharam, é perfil privado/inexistente → erro único e amigável.
    if wishlist is None and owned is None:
        return jsonify({
            "ok": False,
            "error": o_err or w_err or "perfil privado ou nao encontrado",
        })

    return jsonify({
        "ok": True,
        "profile": {"kind": kind, "value": value},
        "wishlist": sorted(set(wishlist or [])),
        "owned":    sorted(set(owned or [])),
        # avisos não-fatais (ex.: wishlist privada mas biblioteca pública)
        "warnings": [e for e in (w_err if wishlist is None else None,
                                 o_err if owned is None else None) if e],
    })

# ─── Healthcheck ──────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "data_present": os.path.exists(DATA_FILE)})


if __name__ == "__main__":
    # Dev server. Em produção usa-se gunicorn (ver Dockerfile).
    app.run(host="0.0.0.0", port=8000, debug=True)
