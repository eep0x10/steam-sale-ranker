#!/usr/bin/env python3
import io, sys
if sys.platform == "win32":
    for _s in ("stdout", "stderr"):
        _cur = getattr(sys, _s)
        if hasattr(_cur, "reconfigure"):
            _cur.reconfigure(encoding="utf-8", errors="replace")

"""
Steam Sale Ranker
=================
Lista jogos em promoção na Steam ordenados por score composto.

Fórmula:
  score = (review% / 100) × log10(total_reviews + 1) × (1 + desconto / 200)

  - review%   : qualidade percebida (peso maior)
  - log10(...)  : fama/popularidade (cresce devagar — 100k reviews ≠ 10x melhor que 10k)
  - desconto  : bônus de 0.5% por ponto de desconto (50% off = +25% no score final)

Blocos seguem a classificação oficial da Steam:
  Overwhelmingly Positive : 95%+  (500+ reviews)
  Very Positive           : 80-94% (500+ reviews)
  Mostly Positive         : 70-79%
  Mixed                   : 40-69%
  Mostly Negative         : 20-39%
  Overwhelmingly Negative : 0-19%  (500+ reviews)

Uso:
  python steam_sale_ranker.py             # 10 páginas (~500 jogos)
  python steam_sale_ranker.py 20          # 20 páginas (~1000 jogos)
  python steam_sale_ranker.py 20 --html   # gera steam_sale_ranker.html
"""

import math
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("[!] Dependências necessárias:")
    print("    pip install requests beautifulsoup4")
    sys.exit(1)

# ─── Config ──────────────────────────────────────────────────────────────────

STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
COUNT_PER_PAGE   = 50
MAX_PER_BLOCK    = 30   # máximo exibido por bloco no terminal
MIN_REVIEWS      = 2000  # jogos com menos reviews são ignorados
MIN_DISCOUNT     = 15    # descontos abaixo disso são ignorados

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://store.steampowered.com/",
}
COOKIES = {
    "birthtime":           "631152001",
    "mature_content":      "1",
    "wants_mature_content": "1",
    "lastagecheckage":     "1-0-2000",
}

# ─── Blocos ──────────────────────────────────────────────────────────────────

BLOCK_ORDER = [
    "Overwhelmingly Positive",
    "Very Positive",
    "Mostly Positive",
    "Mixed",
    "Mostly Negative",
    "Overwhelmingly Negative",
    "Sem Avaliações",
]

BLOCK_COLORS = {
    "Overwhelmingly Positive": "\033[92m",   # verde brilhante
    "Very Positive":           "\033[32m",   # verde
    "Mostly Positive":         "\033[33m",   # amarelo
    "Mixed":                   "\033[93m",   # amarelo brilhante
    "Mostly Negative":         "\033[91m",   # vermelho brilhante
    "Overwhelmingly Negative": "\033[31m",   # vermelho
    "Sem Avaliações":          "\033[90m",   # cinza
}

BLOCK_HEX = {
    "Overwhelmingly Positive": "#4fc24f",
    "Very Positive":           "#66c0f4",
    "Mostly Positive":         "#a4d4a4",
    "Mixed":                   "#f5c518",
    "Mostly Negative":         "#f06c6c",
    "Overwhelmingly Negative": "#c0392b",
    "Sem Avaliações":          "#888",
}

RESET = "\033[0m"
BOLD  = "\033[1m"
GRAY  = "\033[90m"
CYAN  = "\033[96m"

# ─── Score e classificação ────────────────────────────────────────────────────

def calc_score(pct: int, total: int, discount: int) -> float:
    """Score composto: qualidade × fama × bônus desconto."""
    if total < 10 or pct == 0:
        return 0.0
    quality        = pct / 100
    fame           = math.log10(total + 1)
    discount_bonus = 1.0 + (discount / 200.0)
    return quality * fame * discount_bonus


def review_block(pct: int, total: int) -> str:
    """Classifica o jogo no bloco correto conforme o sistema Steam."""
    if total < 10:
        return "Sem Avaliações"
    if total >= 500:
        if pct >= 95: return "Overwhelmingly Positive"
        if pct >= 80: return "Very Positive"
        if pct < 20:  return "Overwhelmingly Negative"
    if pct >= 70: return "Mostly Positive"
    if pct >= 40: return "Mixed"
    return "Mostly Negative"

# ─── Coleta ───────────────────────────────────────────────────────────────────

def fetch_page(start: int, sort_by: str = "Reviews_DESC") -> tuple[list[dict], int]:
    """Busca uma página do search da Steam. Retorna (jogos_parsed, total_count)."""
    params = {
        "specials": 1,
        "json":     1,
        "count":    COUNT_PER_PAGE,
        "start":    start,
        "infinite": 1,
    }
    if sort_by:
        params["sort_by"] = sort_by
    resp = requests.get(
        STEAM_SEARCH_URL,
        params=params,
        headers=HEADERS,
        cookies=COOKIES,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    total = int(data.get("total_count", 0))

    # A Steam retorna HTML no items_html ou results_html
    raw_html = (
        data.get("items_html")
        or data.get("results_html")
        or ""
    )
    if not raw_html:
        # Fallback: items como lista de strings
        items = data.get("items", [])
        if isinstance(items, list):
            raw_html = "\n".join(str(x) for x in items)

    soup  = BeautifulSoup(raw_html, "html.parser")
    rows  = soup.find_all("a", class_="search_result_row")
    games = [g for r in rows if (g := _parse_row(r)) is not None]
    return games, total


def _parse_row(row) -> dict | None:
    """Extrai metadados de um search_result_row."""
    try:
        # ── Nome ──────────────────────────────────────────────────────────
        name_tag = row.find("span", class_="title")
        name = name_tag.get_text(strip=True) if name_tag else "?"

        appid = row.get("data-ds-appid", "").split(",")[0]

        # ── Desconto ──────────────────────────────────────────────────────
        discount = 0

        # Tentativa 1: atributo data-ds-discount
        if row.get("data-ds-discount"):
            try:
                discount = int(row["data-ds-discount"])
            except ValueError:
                pass

        # Tentativa 2: div.search_discount > span
        if discount == 0:
            disc_tag = row.find("div", class_="search_discount")
            if disc_tag:
                m = re.search(r"(\d+)%", disc_tag.get_text())
                if m:
                    discount = int(m.group(1))

        # Tentativa 3: div.discount_pct
        if discount == 0:
            disc_tag = row.find("div", class_="discount_pct")
            if disc_tag:
                m = re.search(r"(\d+)", disc_tag.get_text())
                if m:
                    discount = int(m.group(1))

        if discount < MIN_DISCOUNT:
            return None

        # ── Preços ────────────────────────────────────────────────────────
        orig_price = ""
        sale_price = ""

        orig_tag = row.find(class_="discount_original_price")
        sale_tag = row.find(class_="discount_final_price")
        if orig_tag:
            orig_price = orig_tag.get_text(strip=True)
        if sale_tag:
            sale_price = sale_tag.get_text(strip=True)

        # Fallback: search_price genérico
        if not sale_price:
            price_block = row.find("div", class_="search_price")
            if price_block:
                strike = price_block.find("strike")
                if strike:
                    orig_price = strike.get_text(strip=True)
                texts = [t.strip() for t in price_block.get_text("\n").split("\n") if t.strip()]
                if texts:
                    sale_price = texts[-1]

        # ── Reviews ───────────────────────────────────────────────────────
        pct_positive  = 0
        total_reviews = 0

        review_span = row.find("span", class_="search_review_summary")
        if review_span:
            tooltip = review_span.get("data-tooltip-html", "")
            # "94% of 28,521 user reviews for this game are positive."
            # "94% das 28.521 análises dos usuários recomendam este jogo."
            m_pct = re.search(r"(\d+)%", tooltip)
            m_tot = re.search(
                r"([\d,\.]+)\s*(user reviews|análises|reviews)",
                tooltip, re.IGNORECASE
            )
            if m_pct:
                pct_positive = int(m_pct.group(1))
            if m_tot:
                total_reviews = int(re.sub(r"[,\.]", "", m_tot.group(1)))

        if total_reviews < MIN_REVIEWS:
            return None

        # ── Imagem ────────────────────────────────────────────────────────
        img_url = ""
        img_tag = row.find("div", class_="search_capsule")
        if img_tag:
            img_el = img_tag.find("img")
            if img_el:
                img_url = img_el.get("src", "")
        if not img_url and appid:
            img_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_231x87.jpg"

        return {
            "name":          name,
            "appid":         appid,
            "discount":      discount,
            "pct_positive":  pct_positive,
            "total_reviews": total_reviews,
            "orig_price":    orig_price,
            "sale_price":    sale_price,
            "score":         calc_score(pct_positive, total_reviews, discount),
            "block":         review_block(pct_positive, total_reviews),
            "url":           row.get("href", f"https://store.steampowered.com/app/{appid}/"),
            "img_url":       img_url,
        }
    except Exception:
        return None

# ─── Coleta com paginação ─────────────────────────────────────────────────────

# Duas passagens para cobrir jogos diferentes:
# Reviews_DESC → jogos populares (muitos reviews, desconto variado)
# sem sort     → relevância Steam para promoções (tende a priorizar descontos maiores)
FETCH_STRATEGIES = ["Reviews_DESC", ""]

def _fetch_strategy(sort_by: str, max_pages: int, label: str) -> tuple[list[dict], int]:
    games: list[dict] = []
    total_available = 0
    for page in range(max_pages):
        start = page * COUNT_PER_PAGE
        print(f"\r  {label} [{page + 1}/{max_pages}] offset={start}...", end="", flush=True)
        try:
            batch, total = fetch_page(start, sort_by=sort_by)
            total_available = total
            if not batch:
                break
            games.extend(batch)
            if start + COUNT_PER_PAGE >= total:
                break
            time.sleep(0.3)
        except requests.HTTPError as e:
            print(f"\n[!] HTTP {e.response.status_code}")
            break
        except Exception as e:
            print(f"\n[!] Erro: {e}")
            break
    return games, total_available


def collect_all(max_pages: int) -> list[dict]:
    seen:      set[str]   = set()
    all_games: list[dict] = []
    total_available = 0

    for i, sort_by in enumerate(FETCH_STRATEGIES):
        label = f"[pass {i+1}/{len(FETCH_STRATEGIES)} {'reviews' if sort_by else 'relevância'}]"
        batch, total = _fetch_strategy(sort_by, max_pages, label)
        total_available = max(total_available, total)
        new = 0
        for g in batch:
            if g["appid"] not in seen:
                seen.add(g["appid"])
                all_games.append(g)
                new += 1
        print(f"\r  pass {i+1}: +{new} novos (total único: {len(all_games)})          ")

    print(f"  Total disponível na Steam: ~{total_available} jogos em promoção")
    return all_games


# ─── Baixa histórica (CheapShark) ─────────────────────────────────────────────
# Estratégia: 2 chamadas por jogo
#   1. GET /games?steamAppID={appid}  → {gameID, cheapest_usd_ever}
#   2. GET /games?id={gameID}         → {cheapestPriceEver.price, deals[storeID=1].price}
# Rate limit: lock compartilhado garante ≤ 4.5 req/s (não estourar o CheapShark)

import threading as _threading
_cs_lock       = _threading.Lock()
_cs_last_call  = [0.0]
_CS_INTERVAL   = 0.5    # 2 req/s — seguro para uso diário


def _cs_get(path: str, params: dict, _retry: int = 1) -> any:
    """GET ao CheapShark com rate limiting global e retry em 429."""
    with _cs_lock:
        gap = _CS_INTERVAL - (time.time() - _cs_last_call[0])
        if gap > 0:
            time.sleep(gap)
        _cs_last_call[0] = time.time()
    try:
        r = requests.get(
            f"https://www.cheapshark.com/api/1.0{path}",
            params=params, timeout=10,
        )
        if r.status_code == 429:
            if _retry > 0:
                time.sleep(45)   # backoff longo antes de retry
                return _cs_get(path, params, _retry=_retry - 1)
            return None
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _parse_brl(price_str: str) -> float:
    """Extrai valor numérico de 'R$ 9,99' ou 'R$9.99'. Retorna 0.0 se falhar."""
    try:
        s = re.sub(r"[R$\s]", "", price_str)   # remove R$, espaços
        s = s.replace(".", "").replace(",", ".")  # "9.999,99" → "9999.99"
        return float(s)
    except Exception:
        return 0.0


def _fmt_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _check_one_low(game: dict) -> tuple[str, bool, float, float]:
    """
    Retorna (appid, is_historical_low, cheapest_ever_usd, current_usd).
    A conversão para BRL é feita em enrich_historical_lows usando o preço BRL do jogo.
    """
    appid = game["appid"]
    try:
        step1 = _cs_get("/games", {"steamAppID": appid})
        if not step1 or not isinstance(step1, list) or not step1:
            return appid, False, 0.0, 0.0
        game_id = step1[0].get("gameID", "")
        if not game_id:
            return appid, False, 0.0, 0.0

        step2 = _cs_get("/games", {"id": game_id})
        if not step2 or not isinstance(step2, dict):
            return appid, False, 0.0, 0.0

        cheapest_str = step2.get("cheapestPriceEver", {}).get("price", "")
        if not cheapest_str:
            return appid, False, 0.0, 0.0
        cheapest_ever = float(cheapest_str)
        if cheapest_ever <= 0:
            return appid, False, 0.0, 0.0

        steam_deal = next(
            (d for d in step2.get("deals", []) if str(d.get("storeID")) == "1"),
            None,
        )
        if not steam_deal:
            return appid, False, cheapest_ever, 0.0

        current_usd = float(steam_deal.get("price", 0))
        is_low = current_usd > 0 and current_usd <= cheapest_ever * 1.02
        return appid, is_low, cheapest_ever, current_usd
    except Exception:
        return appid, False, 0.0, 0.0


def enrich_historical_lows(games: list[dict]) -> None:
    """
    Adiciona game['historical_low'] e game['low_price_brl'] via CheapShark.
    Conversão USD→BRL usa a proporção real do próprio jogo na Steam BR.
    """
    n = len(games)
    print(f"  Verificando baixas históricas via CheapShark ({n} jogos, ~{n*2*_CS_INTERVAL:.0f}s)...")
    results: dict[str, tuple[bool, float, float]] = {}  # appid → (is_low, cheapest_usd, current_usd)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_check_one_low, g): g["appid"] for g in games}
        done = 0
        for fut in as_completed(futures):
            appid, is_low, cheapest_usd, current_usd = fut.result()
            results[appid] = (is_low, cheapest_usd, current_usd)
            done += 1
            lows_so_far = sum(v[0] for v in results.values())
            print(f"\r  CheapShark: {done}/{n} | {lows_so_far} lows encontrados...", end="", flush=True)

    lows = sum(v[0] for v in results.values())
    print(f"\r  CheapShark: {lows} jogos em baixa histórica de {n} verificados          ")

    for g in games:
        is_low, cheapest_usd, current_usd = results.get(g["appid"], (False, 0.0, 0.0))
        g["historical_low"] = is_low

        # Converte cheapest_usd → BRL usando o ratio do próprio jogo
        # ratio = preço_brl_atual / preço_usd_atual (específico por jogo, não câmbio genérico)
        low_brl_str = ""
        if cheapest_usd > 0:
            brl_val = _parse_brl(g.get("sale_price", ""))
            if brl_val > 0 and current_usd > 0:
                ratio = brl_val / current_usd
                low_brl_str = _fmt_brl(cheapest_usd * ratio)
            else:
                # fallback: só mostra em USD se não tiver BRL
                low_brl_str = f"~${cheapest_usd:.2f}"
        g["low_price_brl"] = low_brl_str

# ─── Output terminal ──────────────────────────────────────────────────────────

def fmt_num(n: int) -> str:
    if n >= 1_000_000: return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:     return f"{n / 1_000:.0f}k"
    return str(n)


def print_results(by_block: dict[str, list[dict]], total_collected: int):
    W = 75

    print(f"\n{BOLD}{CYAN}{'═' * W}")
    print(f"  STEAM SALE RANKER  —  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Score = (review%/100) × log10(reviews) × (1 + desconto/200)")
    print(f"  Quanto maior, melhor a relação qualidade + fama + desconto")
    print(f"{'═' * W}{RESET}\n")

    total_shown = 0
    for block_name in BLOCK_ORDER:
        games = by_block.get(block_name, [])
        if not games:
            continue

        color = BLOCK_COLORS[block_name]
        print(f"\n{color}{BOLD}{'═' * W}")
        print(f"  {block_name.upper()}  ({len(games)} jogos encontrados)")
        print(f"{'═' * W}{RESET}")

        print(
            f"{GRAY}{'#':>3}  {'Nome':<39} {'Desc':>5}  "
            f"{'Rev%':>4}  {'Reviews':>7}  {'Low Ever (BRL)':>14}  {'Score':>6}{RESET}"
        )
        print(f"{GRAY}{'─' * W}{RESET}")

        top = games[:MAX_PER_BLOCK]
        for i, g in enumerate(top, 1):
            is_low   = g.get("historical_low", False)
            is_low    = g.get("historical_low", False)
            low_usd   = g.get("low_price_brl", "")
            row_col   = "\033[92m" if is_low else ""
            low_tag   = f" {BOLD}\033[92m★{RESET}" if is_low else ""
            low_brl   = g.get("low_price_brl", "")
            low_col   = "\033[92m" if is_low else "\033[90m"
            low_str   = f"{low_col}{low_brl if low_brl else '—':>12}{RESET}"
            disc_str  = f"{color}-{g['discount']}%{RESET}"
            name_str  = g["name"][:38].ljust(38)
            print(
                f"{row_col}{i:>3}  {name_str}{low_tag} "
                f"{disc_str}  "
                f"{g['pct_positive']:>3}%  "
                f"{fmt_num(g['total_reviews']):>7}  "
                f"{low_str}  "
                f"{g['score']:>6.2f}{RESET}"
            )
            total_shown += 1

        extra = len(games) - MAX_PER_BLOCK
        if extra > 0:
            print(f"{GRAY}  ... +{extra} jogos omitidos (use --html para ver todos){RESET}")

    omitted = total_collected - total_shown
    print(f"\n{BOLD}Exibidos: {total_shown}  |  Total coletado: {total_collected}{RESET}")
    if omitted > 0:
        print(f"{GRAY}Use --html para relatório completo.{RESET}")

# ─── Output HTML ──────────────────────────────────────────────────────────────

def generate_html(by_block: dict[str, list[dict]], total_collected: int) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    rows_by_block = ""
    for block_name in BLOCK_ORDER:
        games = by_block.get(block_name, [])
        if not games:
            continue
        hex_color = BLOCK_HEX[block_name]
        rows_html = ""
        for i, g in enumerate(games, 1):
            store_url = g.get("url", f"https://store.steampowered.com/app/{g['appid']}/")
            img_url   = g.get("img_url", "")
            img_html  = (
                f'<img src="{img_url}" alt="" loading="lazy">'
                if img_url else ""
            )
            is_low   = g.get("historical_low", False)
            low_badge = '<span class="low-badge">BAIXA HISTÓRICA</span>' if is_low else ""
            tr_class  = ' class="hist-low"' if is_low else ""
            rows_html += f"""
              <tr{tr_class}>
                <td class="rank">{i}</td>
                <td class="name">
                  <a href="{store_url}" target="_blank">
                    {img_html}
                    <span>{g['name']}{low_badge}</span>
                  </a>
                </td>
                <td class="disc" style="color:{hex_color}">-{g['discount']}%</td>
                <td class="pct">{g['pct_positive']}%</td>
                <td class="reviews">{fmt_num(g['total_reviews'])}</td>
                <td class="orig">{g['orig_price']}</td>
                <td class="sale">{g['sale_price']}</td>
                <td class="low-ever">{g.get('low_price_brl') or '—'}</td>
                <td class="score">{g['score']:.2f}</td>
              </tr>"""

        rows_by_block += f"""
        <div class="block">
          <div class="block-header" style="background:{hex_color}">
            <span class="block-name">{block_name}</span>
            <span class="block-count">{len(games)} jogos</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Nome</th>
                <th>Desconto</th>
                <th>Review%</th>
                <th>Reviews</th>
                <th>Preço Original</th>
                <th>Preço Promo</th>
                <th>Low Ever (BRL)</th>
                <th>Score ▼</th>
              </tr>
            </thead>
            <tbody>{rows_html}
            </tbody>
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Steam Sale Ranker</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "Segoe UI", sans-serif;
      background: #1b2838;
      color: #c6d4df;
      padding: 20px;
    }}
    h1 {{
      color: #66c0f4;
      font-size: 1.6rem;
      margin-bottom: 6px;
    }}
    .subtitle {{
      color: #8f98a0;
      font-size: 0.85rem;
      margin-bottom: 24px;
    }}
    .formula {{
      background: #16202d;
      border-left: 3px solid #66c0f4;
      padding: 8px 14px;
      border-radius: 4px;
      font-family: monospace;
      font-size: 0.9rem;
      color: #c7d5e0;
      margin-bottom: 28px;
      display: inline-block;
    }}
    .block {{
      margin-bottom: 32px;
      border-radius: 6px;
      overflow: hidden;
      box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    }}
    .block-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 16px;
      color: #fff;
    }}
    .block-name {{ font-weight: 700; font-size: 1rem; }}
    .block-count {{ font-size: 0.82rem; opacity: 0.85; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #16202d;
      font-size: 0.83rem;
    }}
    thead th {{
      background: #0e1822;
      padding: 7px 10px;
      text-align: left;
      color: #8f98a0;
      font-weight: 600;
      white-space: nowrap;
    }}
    tbody tr:nth-child(even) {{ background: #1a2535; }}
    tbody tr:hover {{ background: #2a3f5a; }}
    td {{ padding: 5px 10px; vertical-align: middle; }}
    td.rank  {{ color: #8f98a0; width: 36px; text-align: right; }}
    td.name  {{ min-width: 260px; }}
    td.name a {{
      display: flex;
      align-items: center;
      gap: 10px;
      color: #c6d4df;
      text-decoration: none;
    }}
    td.name a:hover span {{ color: #66c0f4; text-decoration: underline; }}
    td.name img {{
      width: 116px;
      height: 43px;
      object-fit: cover;
      border-radius: 3px;
      flex-shrink: 0;
      background: #0e1822;
    }}
    td.name span {{
      font-size: 0.84rem;
      line-height: 1.3;
    }}
    td.disc  {{ font-weight: 700; width: 70px; white-space: nowrap; }}
    td.pct   {{ width: 55px; white-space: nowrap; }}
    td.reviews {{ width: 75px; color: #8f98a0; white-space: nowrap; }}
    td.orig  {{ width: 105px; color: #8f98a0; text-decoration: line-through; white-space: nowrap; }}
    td.sale      {{ width: 105px; font-weight: 600; color: #beee11; white-space: nowrap; }}
    td.low-ever  {{ width: 90px; font-family: monospace; color: #8f98a0; white-space: nowrap; font-size: 0.8rem; }}
    tr.hist-low td.low-ever {{ color: #4fc24f; font-weight: 700; }}
    td.score {{ width: 65px; font-family: monospace; color: #66c0f4; white-space: nowrap; }}
    tr.hist-low {{ background: #1a3320 !important; border-left: 3px solid #4fc24f; }}
    tr.hist-low:hover {{ background: #1f4028 !important; }}
    .low-badge {{
      display: inline-block;
      margin-left: 7px;
      padding: 1px 5px;
      border-radius: 3px;
      background: #4fc24f;
      color: #0a1a0a;
      font-size: 0.68rem;
      font-weight: 700;
      vertical-align: middle;
      letter-spacing: 0.03em;
    }}
    footer {{
      margin-top: 30px;
      color: #8f98a0;
      font-size: 0.78rem;
    }}
  </style>
</head>
<body>
  <h1>Steam Sale Ranker</h1>
  <div class="subtitle">Gerado em {now}  —  {total_collected} jogos coletados</div>
  <div class="formula">
    score = (review% / 100) × log10(total_reviews + 1) × (1 + desconto / 200)
  </div>
  {rows_by_block}
  <footer>
    Fórmula: qualidade × fama × bônus de desconto.<br>
    O desconto pesa 50% do seu valor real para não suplantar qualidade e popularidade.
  </footer>
</body>
</html>"""


def save_html(html: str, path: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n[✓] HTML salvo em: {path}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args       = sys.argv[1:]
    max_pages  = 10
    output_html = "--html" in args

    numeric = [a for a in args if a.isdigit()]
    if numeric:
        max_pages = max(1, int(numeric[0]))

    print(f"\n{BOLD}Steam Sale Ranker{RESET}")
    print(f"Buscando até {max_pages * COUNT_PER_PAGE} jogos em promoção...\n")

    all_games = collect_all(max_pages)

    if not all_games:
        print("\n[!] Nenhum jogo encontrado. Verifique conexão ou tente com VPN.")
        return

    # Deduplicar por appid
    seen: set[str] = set()
    unique: list[dict] = []
    for g in all_games:
        if g["appid"] not in seen:
            seen.add(g["appid"])
            unique.append(g)

    # Agrupar e ordenar por score
    by_block: dict[str, list[dict]] = defaultdict(list)
    for g in unique:
        by_block[g["block"]].append(g)
    for k in by_block:
        by_block[k].sort(key=lambda x: x["score"], reverse=True)

    enrich_historical_lows(unique)

    print_results(by_block, len(unique))

    if output_html:
        html_path = "steam_sale_ranker.html"
        save_html(generate_html(by_block, len(unique)), html_path)
        print(f"  Abra no browser: start {html_path}")


if __name__ == "__main__":
    main()
