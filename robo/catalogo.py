"""Lê o site da Outlet360: ranking de vendas, preços De/Por e grade de tamanhos."""
import re
import time
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

import os

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 robo-sendflow",
      "Accept-Language": "pt-BR,pt;q=0.9"}
# Liberação no Cloudflare: o site deixa passar quem envia este cabeçalho secreto
if os.environ.get("SITE_TOKEN"):
    UA["X-Robo-Outlet360"] = os.environ["SITE_TOKEN"]


def _preco(txt):
    """'1.234,56' -> 1234.56"""
    if not txt:
        return None
    txt = re.sub(r"[^\d,\.]", "", str(txt))
    if not txt:
        return None
    try:
        return float(txt.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _html_ranking(url, via_leitor, log):
    """HTML de uma página do ranking: direto; se bloqueado e via_leitor=True, pelo leitor (Jina)."""
    try:
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 200 and "widget_tools__product_detail_list" in r.text:
            return r.text
        if not via_leitor:
            r.raise_for_status()
    except Exception as e:  # noqa
        if not via_leitor:
            raise
    h = {"X-Return-Format": "html", "X-Timeout": "60"}
    if os.environ.get("JINA_API_KEY"):
        h["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
    for tentativa in range(3):
        try:
            r = requests.get("https://r.jina.ai/" + url, headers=h, timeout=120)
            if r.status_code == 200 and "widget_tools__product_detail_list" in r.text:
                return r.text
            if r.status_code == 200 and tentativa == 2:
                return r.text          # página sem produtos (fim do ranking)
        except Exception as e:  # noqa
            log(f"   leitor falhou em {url}: {e}")
        time.sleep(4)
    raise RuntimeError(f"ranking indisponível: {url}")


def ler_ranking(cfg, log, via_leitor=False):
    """Percorre o ranking 'Mais vendidos' e devolve a lista ordenada de produtos."""
    url_base = cfg["site"]["ranking_url"]
    base = cfg["site"]["base"].rstrip("/")
    produtos, vistos = [], set()
    sep = "&" if "?" in url_base else "?"
    for pagina in range(1, cfg["site"]["max_paginas"] + 1):
        url = url_base if pagina == 1 else f"{url_base}{sep}page={pagina}"
        try:
            html = _html_ranking(url, via_leitor, log)
        except Exception as e:  # noqa
            log(f"⚠️ Falha ao ler página {pagina} do ranking: {e}")
            break
        soup = BeautifulSoup(html, "lxml")
        widgets = soup.select('[id^="widget_tools__product_detail_list"]')
        if not widgets:
            break
        novos = 0
        for w in widgets:
            card = w.find_parent(class_="product-item__content")
            if card is None:  # fallback: sobe até achar um link de produto
                card = w
                for _ in range(6):
                    card = card.parent
                    if card is None or card.select_one('a[href*="/produto/"]'):
                        break
            a = card.select_one('a[href*="/produto/"]') if card else None
            if not a:
                continue
            link = a["href"]
            if link.startswith("/"):
                link = base + link
            slug = link.rstrip("/").split("/")[-1]
            if slug in vistos:
                continue
            vistos.add(slug)
            novos += 1
            img = card.select_one("img")
            tamanhos, disponiveis = [], []
            for lab in w.select("label.label_radio"):
                t = lab.get_text(strip=True)
                if not t:
                    continue
                tamanhos.append(t)
                if "out-of-stock" not in (lab.get("class") or []):
                    disponiveis.append(t)
            de = _preco(w.get("data-preco-de"))
            por = _preco(w.get("data-preco-por"))
            produtos.append({
                "rank": len(produtos) + 1,
                "slug": slug,
                "link": link,
                "nome": (img.get("alt") if img else slug).strip(),
                "imagem": img.get("src") if img else None,
                "de": de,
                "por": por,
                "tamanhos": tamanhos,
                "disponiveis": disponiveis,
            })
        if novos == 0:
            break
        time.sleep(0.3 if via_leitor else 0.8)
    log(f"Ranking lido: {len(produtos)} produtos")
    return produtos


def _baixar_feed(cfg, log):
    url = cfg["site"]["feed"]
    try:
        r = requests.get(url, headers=UA, timeout=120)
        if r.status_code == 200 and "<feed" in r.text[:500]:
            return r.content
        log(f"Feed direto bloqueado ({r.status_code}); tentando pelo leitor…")
    except Exception as e:  # noqa
        log(f"Feed direto falhou ({e}); tentando pelo leitor…")
    h = {"X-Return-Format": "html"}
    if os.environ.get("JINA_API_KEY"):
        h["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
    for tentativa in range(3):
        try:
            r = requests.get("https://r.jina.ai/" + url, headers=h, timeout=180)
            if r.status_code == 200 and "<entry" in r.text:
                return r.content
            log(f"   leitor devolveu {r.status_code}")
        except Exception as e:  # noqa
            log(f"   leitor falhou: {e}")
        time.sleep(20)
    return None


def _pagina(url):
    """HTML de uma página do site: direto e, se bloqueado, pelo leitor."""
    try:
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code == 200 and "price-of" in r.text:
            return r.text
    except Exception:  # noqa
        pass
    h = {"X-Return-Format": "html"}
    if os.environ.get("JINA_API_KEY"):
        h["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
    for _ in range(2):
        try:
            r = requests.get("https://r.jina.ai/" + url, headers=h, timeout=120)
            if r.status_code == 200 and "price" in r.text:
                return r.text
        except Exception:  # noqa
            pass
        time.sleep(5)
    return None


def preco_de_site(link):
    """Preço 'De' riscado que o site mostra na página do produto (o 1º span.price-of)."""
    html = _pagina(link.split("?")[0])
    if not html:
        return None
    el = BeautifulSoup(html, "lxml").select_one(".price-of")
    return _preco(el.get_text()) if el else None


def ler_feed(cfg, log):
    """Feed XML: preço Por, PIX, fotos, link e descrição, indexado pelo slug."""
    ns = {"a": "http://www.w3.org/2005/Atom", "g": "http://base.google.com/ns/1.0"}
    bruto = _baixar_feed(cfg, log)
    if not bruto:
        log("⚠️ Feed indisponível.")
        return {}
    i = bruto.find(b"<?xml")
    root = ET.fromstring(bruto[i:] if i > 0 else bruto)
    dados = {}
    for entry in root.findall("a:entry", ns):
        def g(tag):
            el = entry.find(f"g:{tag}", ns)
            return el.text.strip() if el is not None and el.text else ""
        link = g("link")
        if not link:
            continue
        slug = link.rstrip("/").split("/")[-1]
        por = _preco(g("price").replace(" BRL", "").replace(".", ","))
        dados[slug] = {
            "id": g("item_group_id") or g("id"),
            "titulo": g("title"),
            "link": link,
            "descricao": g("description")[:1500],
            "marca": g("brand"),
            "categoria": g("product_type"),
            "imagem": g("image_link"),
            "extras": [e.text for e in entry.findall("g:additional_image_link", ns) if e.text][:3],
            "kit": g("is_bundle") == "yes",
            "por": por,
            "disponivel": g("availability") == "in stock",
        }
    log(f"Feed lido: {len(dados)} produtos")
    return dados


def desconto(p):
    if not p.get("de") or not p.get("por") or p["de"] <= p["por"]:
        return 0
    return int((1 - p["por"] / p["de"]) * 100 + 0.5)
