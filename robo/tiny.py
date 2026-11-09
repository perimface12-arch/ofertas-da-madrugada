"""Plano B: escolhe produtos pelo ERP Tiny (vendas, preço cheio e estoque por tamanho)
+ feed do site (preço Por, PIX, fotos e link)."""
import datetime as dt
import os
import random
import re
import time

import requests

from robo import catalogo

API = "https://api.tiny.com.br/api2/{}.php"
_ultimo = [0.0]


def tiny(metodo, pausa=1.1, **params):
    for tentativa in range(6):
        espera = pausa - (time.time() - _ultimo[0])
        if espera > 0:
            time.sleep(espera)
        _ultimo[0] = time.time()
        try:
            r = requests.post(API.format(metodo), timeout=60, data={
                "token": os.environ["TINY_API_TOKEN"], "formato": "JSON", **params})
            ret = r.json().get("retorno", {})
        except Exception:  # noqa
            time.sleep(10)
            continue
        if ret.get("status") == "OK":
            return ret
        erros = str(ret.get("erros", "")) + str(ret.get("codigo_erro", ""))
        if "limite" in erros.lower() or ret.get("codigo_erro") in ("6", 6):
            time.sleep(60)  # API bloqueada por excesso de chamadas: espera e tenta de novo
            continue
        return ret  # erro "de verdade" (ex.: nenhum registro encontrado)
    return {}


def pai(codigo):
    m = re.match(r"^([A-Z]+\d+)", codigo or "")
    return m.group(1) if m else codigo


def vendas(dias, log):
    """Soma de peças vendidas por produto (código pai) nos últimos N dias."""
    fim = dt.date.today()
    ini = fim - dt.timedelta(days=dias)
    ids, pagina = [], 1
    while True:
        r = tiny("pedidos.pesquisa", dataInicial=ini.strftime("%d/%m/%Y"),
                 dataFinal=fim.strftime("%d/%m/%Y"), pagina=pagina)
        for p in r.get("pedidos", []):
            if "cancel" not in p["pedido"].get("situacao", "").lower():
                ids.append(p["pedido"]["id"])
        if pagina >= int(r.get("numero_paginas", 1) or 1):
            break
        pagina += 1
    log(f"Tiny: {len(ids)} pedidos nos últimos {dias} dias")
    total = {}
    for i, pid in enumerate(ids):
        r = tiny("pedido.obter", id=pid)
        for it in r.get("pedido", {}).get("itens", []):
            c = pai(it["item"].get("codigo"))
            total[c] = total.get(c, 0) + float(it["item"].get("quantidade") or 0)
        if i and i % 100 == 0:
            log(f"   … {i} pedidos lidos")
    return total


def vendas_ga4(dias, log):
    """Itens comprados por produto (ID do site) no GA4, via Windsor."""
    r = requests.get("https://connectors.windsor.ai/googleanalytics4", timeout=180, params={
        "api_key": os.environ["WINDSOR_API_KEY"], "date_preset": f"last_{dias}d",
        "fields": "item_id,item_name,items_purchased"})
    r.raise_for_status()
    total = {}
    for row in r.json().get("data", []):
        k = str(row.get("item_id") or "").strip()
        if k:
            total[k] = total.get(k, 0) + float(row.get("items_purchased") or 0)
    log(f"GA4 (Windsor): {len(total)} produtos com vendas nos últimos {dias} dias")
    return total


def produto(codigo):
    """Busca o produto pai no Tiny: preço cheio, slug do site e variações."""
    r = tiny("produtos.pesquisa", pesquisa=codigo)
    achado = [p["produto"] for p in r.get("produtos", []) if p["produto"].get("codigo") == codigo]
    if not achado:
        return None
    r = tiny("produto.obter", id=achado[0]["id"])
    return r.get("produto")


def produto_por_nome(titulo, slug):
    """Acha no Tiny o produto do feed (pelo nome) e confere pelo slug do site."""
    r = tiny("produtos.pesquisa", pesquisa=titulo.strip())
    nome = titulo.strip().lower()
    candidatos = [p["produto"] for p in r.get("produtos", [])
                  if p["produto"].get("nome", "").strip().lower() == nome]
    for c in candidatos[:3]:
        prod = tiny("produto.obter", id=c["id"]).get("produto")
        if prod and prod.get("slug") == slug:
            return prod
    return None


def tamanhos_com_estoque(prod):
    disp = []
    for v in prod.get("variacoes", []) or []:
        v = v.get("variacao", v)
        tam = (v.get("grade") or {}).get("Tamanho") or next(iter((v.get("grade") or {}).values()), None)
        if not tam:
            continue
        r = tiny("produto.obter.estoque", id=v["id"])
        if float((r.get("produto") or {}).get("saldo") or 0) > 0:
            disp.append(tam)
    return disp


def _pct(de, por):
    # mesmo % do selo do site: (De − Por) / De, arredondado normalmente (0,5 sobe), sem PIX
    return int((1 - por / de) * 100 + 0.5) if de > por > 0 else 0


def _montar(prod, feed_item, rank, vendidos, disp, conferir_site=True):
    por = feed_item["por"]
    de = float(prod.get("preco") or 0)
    # o "De" vale o que o SITE mostra (às vezes difere do Tiny); Tiny só se o site não responder
    if conferir_site:
        de_site = catalogo.preco_de_site(feed_item["link"])
        if de_site and de_site > 0:
            de = de_site
    pct = _pct(de, por)
    return {"rank": rank, "slug": prod.get("slug"), "link": feed_item["link"],
            "nome": prod.get("nome"), "imagem": feed_item["imagem"], "de": de, "por": por,
            "desconto": pct, "disponiveis": disp, "tamanhos": disp, "vendidos": vendidos,
            "codigo": prod.get("codigo")}


def escolher(cfg, feed, usados, hoje, log):
    R = cfg["regras"]
    G = cfg.get("ga4", {})
    T = cfg.get("tiny", {})
    por_id = {f["id"]: slug for slug, f in feed.items() if f.get("id")}

    # ---------- Curva A: ranking de itens comprados (GA4, 15 dias)
    if os.environ.get("WINDSOR_API_KEY"):
        v_curva = vendas_ga4(G.get("dias_curva_a", 15), log)
        v_longo = vendas_ga4(G.get("dias_pouca_saida", 30), log)
        ranking = [(por_id[i], q) for i, q in sorted(v_curva.items(), key=lambda x: -x[1]) if i in por_id]
        vendas_30 = {por_id[i]: q for i, q in v_longo.items() if i in por_id}
        limite_pouca = G.get("vendas_max_pouca_saida", 2)
    else:  # sem GA4: soma pedidos do Tiny (mais lento)
        vend = vendas(T.get("dias_vendas", 2), log)
        ranking, vendas_30, limite_pouca = [], {}, 0
        for cod, q in sorted(vend.items(), key=lambda x: -x[1])[:60]:
            prod = produto(cod)
            if prod and prod.get("slug") in feed:
                ranking.append((prod["slug"], q))
                vendas_30[prod["slug"]] = q

    mais = []
    for rank, (slug, qtd) in enumerate(ranking[: G.get("max_consultas_mais_vendidos", 40)], 1):
        if len(mais) >= G.get("candidatos_por_tipo", 6) or qtd < G.get("min_vendas_curva_a", 5):
            break
        f = feed[slug]
        if slug in usados or (R.get("ignorar_kits") and f.get("kit")):
            continue
        prod = produto_por_nome(f.get("titulo", ""), slug)
        if not prod:
            continue
        cand = _montar(prod, f, rank, qtd, [])
        if cand["desconto"] < R["desconto_minimo"]:
            continue
        cand["disponiveis"] = cand["tamanhos"] = tamanhos_com_estoque(prod)
        if len(cand["disponiveis"]) >= R["tamanhos_minimos_mais_vendido"]:
            mais.append(cand)
            log(f"   ⭐ curva A #{rank}: {cand['nome']} ({qtd:.0f} vendidos, De R$ {cand['de']:.2f}, {cand['desconto']}% OFF)")

    # ---------- Pouca saída: grade disponível e quase nenhuma venda em 30 dias
    pool = [s for s, f in feed.items() if s not in usados and f.get("disponivel", True)
            and vendas_30.get(s, 0) <= limite_pouca
            and not (R.get("ignorar_kits") and f.get("kit"))]
    random.Random(hoje.toordinal()).shuffle(pool)
    poucos = []
    for slug in pool[: G.get("max_consultas_pouca_saida", 80)]:
        if len(poucos) >= G.get("candidatos_por_tipo", 6):
            break
        f = feed[slug]
        prod = produto_por_nome(f.get("titulo", ""), slug)
        if not prod:
            continue
        # pouca saída: só confere o "De" no site se pelo Tiny já passa do mínimo (poupa acessos)
        if _pct(float(prod.get("preco") or 0), f["por"]) < R["desconto_minimo"]:
            continue
        cand = _montar(prod, f, 9999, vendas_30.get(slug, 0), [])
        if cand["desconto"] < R["desconto_minimo"]:
            continue
        cand["disponiveis"] = cand["tamanhos"] = tamanhos_com_estoque(prod)
        if len(cand["disponiveis"]) >= R["tamanhos_minimos_pouca_saida"]:
            poucos.append(cand)
            log(f"   📦 pouca saída: {cand['nome']} ({cand['vendidos']:.0f} vendas/30d, "
                f"{len(cand['disponiveis'])} tamanhos, De R$ {cand['de']:.2f}, {cand['desconto']}% OFF)")
    return mais, poucos


def achar_por_busca(cfg, feed, padrao, usados, hoje, log, max_testes=15):
    """1º produto elegível (≥ desconto mínimo, grade boa) cujo nome casa com o padrão."""
    R = cfg["regras"]
    rx = re.compile(padrao, re.I)
    pool = [s for s, f in feed.items() if s not in usados and f.get("disponivel", True)
            and rx.search(f.get("titulo", ""))]
    random.Random(hoje.toordinal()).shuffle(pool)
    for slug in pool[:max_testes]:
        f = feed[slug]
        prod = produto_por_nome(f.get("titulo", ""), slug)
        if not prod:
            continue
        cand = _montar(prod, f, 9999, 0, [])
        if cand["desconto"] < R["desconto_minimo"]:
            continue
        cand["disponiveis"] = cand["tamanhos"] = tamanhos_com_estoque(prod)
        if len(cand["disponiveis"]) >= R["tamanhos_minimos_pouca_saida"]:
            log(f"   🎯 achei p/ o resultado: {cand['nome']} (De R$ {cand['de']:.2f}, {cand['desconto']}% OFF)")
            return cand
    log(f"   ⚠️ nenhum produto elegível para '{padrao}'")
    return None
