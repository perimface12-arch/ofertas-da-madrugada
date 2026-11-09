"""Ofertas da Madrugada — seleciona os 20 produtos e gera a landing page.

Roda no GitHub (workflow "Ofertas da Madrugada"):
  python -m madrugada.gerar --modo selecionar   # todo dia 19h: escolhe os 20 da noite e gera a página
  python -m madrugada.gerar --modo atualizar    # de hora em hora, 20h–6h: preços/estoque do site, troca quem esgotou

A página abre todo dia às 20h e as ofertas acabam às 6h (horário de Brasília). Fora disso, mostra "abre às 20h".
  python -m madrugada.gerar --demo              # dados simulados, sem acessar o site (para revisar o layout)

Preços e estoque SEMPRE do site (ranking "Mais vendidos": De, Por e tamanhos com estoque).
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import random
import re
import signal
import unicodedata
from zoneinfo import ZoneInfo

import requests
import yaml

RAIZ = pathlib.Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
SAIDA = RAIZ / "docs"                     # pasta publicada no GitHub Pages (main /docs)
SELECAO_F = DADOS / "madrugada_selecao.json"
MODELO = RAIZ / "madrugada" / "pagina.html"
PRAZO_MIN = {"selecionar": 38, "atualizar": 14}  # passou disso (Tiny lento/bloqueado): publica a última vitrine conferida
QTD_RESERVAS = 15                                # candidatos já conferidos às 19h para repor quem esgotar


class PrazoEsgotado(Exception):
    pass


def _estourou(*_):
    raise PrazoEsgotado()

MARCAS = [("tommy jeans", "Tommy Jeans"), ("tommy", "Tommy Hilfiger"), (r"\brl\b|ralph", "Ralph Lauren"),
          ("lacoste", "Lacoste"), (r"calvin|\bck\b", "Calvin Klein"), ("reserva", "Reserva"), ("aramis", "Aramis"),
          ("john john", "John John"), ("acostamento", "Acostamento"), ("hugo|boss", "Hugo Boss"), ("armani", "Armani"),
          ("diesel", "Diesel"), ("levi", "Levi's"), ("osklen", "Osklen"), ("colcci", "Colcci"), ("nautica", "Nautica"),
          ("hering", "Hering"), ("ellus", "Ellus"), ("vans", "Vans"), ("nike", "Nike"), ("adidas", "Adidas"),
          ("puma", "Puma"), ("mizuno", "Mizuno"), ("polo play", "Polo Play")]
CATEGORIAS = [("camiseta", "Camiseta"), ("polo", "Polo"), ("camisa", "Camisa"), (r"cal[cç]a", "Calça"),
              ("bermuda|short", "Bermuda"), (r"t[eê]nis|sapat|mocassim|bota|chinelo|sand[aá]lia", "Calçado"),
              ("moletom|blus[aã]o|su[eé]ter|tric[oô]|jaqueta|casaco|colete", "Agasalho"), ("cueca|meia", "Íntimo")]
CORES = ["preta", "preto", "branca", "branco", "azul", "marinho", "cinza", "mescla", "verde", "oliva", "bege",
         "areia", "caqui", "khaki", "vermelha", "vermelho", "rosa", "amarela", "amarelo", "marrom", "vinho", "bordo",
         "off", "white", "royal", "petroleo", "petróleo", "claro", "clara", "escuro", "escura", "chumbo", "grafite",
         "laranja", "lilas", "roxo", "roxa", "creme", "nude", "jeans", "navy", "militar", "indigo", "índigo"]


def slugify(t):
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def marca_de(nome):
    n = (nome or "").lower()
    for pad, m in MARCAS:
        if re.search(pad, n):
            return m
    return ""


def categoria_de(nome):
    n = (nome or "").lower()
    for pad, c in CATEGORIAS:
        if re.search(pad, n):
            return c
    return "Outros"


def modelo_de(p):
    """Chave do modelo sem a cor (para não repetir o mesmo produto em cores diferentes)."""
    partes = [x for x in slugify(p["nome"]).split("-") if x not in {slugify(c) for c in CORES}]
    return "-".join(partes)


def desconto(p):
    if not p.get("de") or not p.get("por") or p["de"] <= p["por"]:
        return 0
    return int((1 - p["por"] / p["de"]) * 100 + 0.5)


def enriquecer(p, cfg):
    R = cfg["regras"]
    p = dict(p)
    p["desconto"] = desconto(p)
    p["pix"] = round(p["por"] * (1 - R.get("pix_percentual", 5) / 100), 2) if p.get("por") else None
    n = max(1, min(R.get("parcelas_max", 12), int((p.get("por") or 0) // R.get("parcela_minima", 10))))
    p["parcelas"] = [n, round((p.get("por") or 0) / n, 2)]
    p["marca"] = marca_de(p["nome"])
    p["categoria"] = categoria_de(p["nome"])
    p["modelo"] = modelo_de(p)
    p["n_tamanhos"] = len(p.get("disponiveis") or [])
    p["kit"] = bool(re.search(r"\bkit\b", p["nome"], re.I))
    return p


# ------------------------------------------------------------------ seleção
def selecionar(produtos, cfg, hist, evitar=frozenset()):
    """evitar: slugs da noite anterior (só entram se faltar produto para fechar os 20)."""
    M = cfg["madrugada"]
    agora = dt.datetime.now(ZoneInfo(cfg["fuso"]))
    recentes = {s for s, d in (hist.get("produtos") or {}).items()
                if d >= (agora.date() - dt.timedelta(days=1)).isoformat()}
    total = len(produtos) or 1

    def ok(p, desc_min):
        return (p.get("por") and p.get("imagem") and not p["kit"] and p["slug"] not in recentes and p["slug"] not in evitar
                and p["n_tamanhos"] >= M["tamanhos_minimos"] and p["desconto"] >= desc_min)

    escolhidos, modelos, por_marca, por_cat = [], set(), {}, {}

    def cabe(p):
        return (p["modelo"] not in modelos
                and por_marca.get(p["marca"] or "?", 0) < M["max_por_marca"]
                and por_cat.get(p["categoria"], 0) < M["max_por_categoria"])

    def entra(p, origem):
        p = dict(p, origem=origem)
        escolhidos.append(p)
        modelos.add(p["modelo"])
        por_marca[p["marca"] or "?"] = por_marca.get(p["marca"] or "?", 0) + 1
        por_cat[p["categoria"]] = por_cat.get(p["categoria"], 0) + 1

    # 1) mais vendidos (ordem do ranking do site)
    for p in sorted(produtos, key=lambda x: x["rank"]):
        if sum(1 for x in escolhidos if x["origem"] == "mais_vendido") >= M["qtd_mais_vendidos"]:
            break
        if ok(p, M["desconto_minimo_mais_vendidos"]) and cabe(p):
            entra(p, "mais_vendido")
    # 2) maior desconto
    ja = {p["slug"] for p in escolhidos}
    for p in sorted(produtos, key=lambda x: (-x["desconto"], x["rank"])):
        if len(escolhidos) >= M["qtd_total"]:
            break
        if p["slug"] not in ja and ok(p, M["desconto_minimo_maior_desconto"]) and cabe(p):
            entra(p, "maior_desconto")
    # 3) completa até 20: relaxa marca/categoria e aceita até 2 cores do mesmo modelo
    if len(escolhidos) < M["qtd_total"]:
        for p in sorted(produtos, key=lambda x: (x["rank"], -x["desconto"])):
            if len(escolhidos) >= M["qtd_total"]:
                break
            cores = sum(1 for x in escolhidos if x["modelo"] == p["modelo"])
            if p["slug"] not in {x["slug"] for x in escolhidos} and cores < M.get("max_cores_por_modelo", 2) \
                    and ok(p, min(M["desconto_minimo_mais_vendidos"], M["desconto_minimo_maior_desconto"])):
                entra(p, "mais_vendido" if p["rank"] < 9999 else "maior_desconto")

    # 4) ainda faltou: aceita os da noite anterior
    if len(escolhidos) < M["qtd_total"] and evitar:
        return selecionar(produtos, cfg, hist)

    # ordem: 4 de maior apelo (venda + desconto) e depois alternando as duas listas
    for p in escolhidos:
        p["apelo"] = round(p["desconto"] / 100 + max(0, 1 - min(p["rank"], 200) / 200), 4)
    topo = sorted(escolhidos, key=lambda p: -p["apelo"])[:4]
    resto_v = [p for p in escolhidos if p not in topo and p["origem"] == "mais_vendido"]
    resto_d = [p for p in escolhidos if p not in topo and p["origem"] == "maior_desconto"]
    ordem = list(topo)
    while resto_v or resto_d:
        if resto_v:
            ordem.append(resto_v.pop(0))
        if resto_d:
            ordem.append(resto_d.pop(0))
    return ordem


def atualizar(selecao, produtos, cfg, hist, log=print):
    """Preços e estoque do site; quem esgotou (ou ficou abaixo do mínimo de tamanhos) é trocado."""
    M = cfg["madrugada"]
    por_slug = {p["slug"]: p for p in produtos}
    mantidos, trocas = [], 0
    for p in selecao:
        novo = por_slug.get(p["slug"])
        if novo and novo.get("por") and novo["n_tamanhos"] >= M["tamanhos_minimos"]:
            mantidos.append(dict(novo, origem=p["origem"], apelo=p.get("apelo", 0)))
        else:
            trocas += 1
            log(f"   ↺ sai {p['nome']} (esgotou ou saiu do site)")
    if trocas:
        candidatos = [p for p in produtos if p["slug"] not in {x["slug"] for x in mantidos}]
        base = selecionar([p for p in candidatos], cfg, hist)
        modelos = {x["modelo"] for x in mantidos}
        for p in base:
            if len(mantidos) >= M["qtd_total"]:
                break
            if p["modelo"] not in modelos:
                mantidos.append(p)
                modelos.add(p["modelo"])
                log(f"   ＋ entra {p['nome']}")
    return mantidos, trocas



# ------------------------------------------------------------------ fonte: feed do site + GA4 + Tiny (+ De conferido no site)
def _confirmar(slug, f, rank, cfg, log, cache_tiny):
    """Produto com preço e grade conferidos: Por = feed do site, De = página do produto, grade = estoque do Tiny."""
    from robo import tiny as Ti
    prod = cache_tiny.get(slug)
    if prod is None:
        prod = Ti.produto_por_nome(f.get("titulo", ""), slug) or False
        cache_tiny[slug] = prod
    if not prod:
        return None
    cand = Ti._montar(prod, f, rank, 0, [])          # De conferido na página do produto (site)
    cand["disponiveis"] = cand["tamanhos"] = Ti.tamanhos_com_estoque(prod)
    return cand


def produtos_do_site(cfg, log=print, so_slugs=None):
    """Candidatos com preço e grade confirmados. so_slugs = só reconferir esses (atualização de hora em hora)."""
    from robo import catalogo as Cat
    from robo import tiny as Ti
    M = cfg["madrugada"]
    feed = Cat.ler_feed(cfg, log)
    if len(feed) < 100:
        raise SystemExit("❌ Feed do site indisponível. Página NÃO atualizada.")
    feed = {s: f for s, f in feed.items() if f.get("disponivel", True) and f.get("por") and f.get("imagem")
            and not re.search(r"\bkit\b", f.get("titulo", ""), re.I)}
    por_id = {f["id"]: s for s, f in feed.items() if f.get("id")}
    vendas = Ti.vendas_ga4(15, log) if os.environ.get("WINDSOR_API_KEY") else {}
    rank_venda = {}
    for i, (iid, q) in enumerate(sorted(vendas.items(), key=lambda x: -x[1]), 1):
        if iid in por_id and por_id[iid] not in rank_venda:
            rank_venda[por_id[iid]] = (len(rank_venda) + 1, q)
    cache_tiny = {}
    out = []
    if so_slugs:
        for s in so_slugs:
            if s in feed:
                c = _confirmar(s, feed[s], rank_venda.get(s, (9999, 0))[0], cfg, log, cache_tiny)
                if c:
                    out.append(c)
        return out, feed, rank_venda

    # 1) mais vendidos: GA4 15 dias, na ordem
    alvo_v = M["qtd_mais_vendidos"] * 3
    for s, (rk, q) in sorted(rank_venda.items(), key=lambda x: x[1][0]):
        if sum(1 for c in out if c.get("_lista") == "v") >= alvo_v or rk > 150:
            break
        c = _confirmar(s, feed[s], rk, cfg, log, cache_tiny)
        if c and c["desconto"] >= M["desconto_minimo_mais_vendidos"] and len(c["disponiveis"]) >= M["tamanhos_minimos"]:
            c["_lista"] = "v"
            out.append(c)
            log(f"   ⭐ #{rk} {c['nome']} · {q:.0f} vendidos · {c['desconto']}% OFF · {len(c['disponiveis'])} tam.")
    # 2) maior desconto: preço cheio do Tiny (estimativa) x Por do site; confirma os melhores no site
    estim = []
    pagina = 1
    nomes = {f.get("titulo", "").strip().lower(): s for s, f in feed.items()}
    while pagina <= 80:
        r = Ti.tiny("produtos.pesquisa", pesquisa="", pagina=pagina)
        for it in r.get("produtos", []) or []:
            pr = it["produto"]
            s = nomes.get((pr.get("nome") or "").strip().lower())
            de = float(pr.get("preco") or 0)
            if s and de > 0 and s not in {c["slug"] for c in out}:
                d = int((1 - feed[s]["por"] / de) * 100 + 0.5)
                estim.append((d, s))
        if pagina >= int(r.get("numero_paginas", 1) or 1):
            break
        pagina += 1
    log(f"Tiny: {len(estim)} produtos do feed com preço cheio para estimar o desconto")
    alvo_d = (M["qtd_total"] - M["qtd_mais_vendidos"]) * 3
    n_d = 0
    for d, s in sorted(set(estim), reverse=True):
        if n_d >= alvo_d or d < M["desconto_minimo_maior_desconto"]:
            break
        c = _confirmar(s, feed[s], rank_venda.get(s, (9999, 0))[0], cfg, log, cache_tiny)
        if c and c["desconto"] >= M["desconto_minimo_maior_desconto"] and len(c["disponiveis"]) >= M["tamanhos_minimos"]:
            c["_lista"] = "d"
            out.append(c)
            n_d += 1
            log(f"   🏷️ {c['desconto']}% OFF {c['nome']} · {len(c['disponiveis'])} tam.")
    return out, feed, rank_venda

# ------------------------------------------------------------------ página
def ids_rastreamento(cfg):
    """GA4 e Pixel da Meta: do config, ou lidos da home do site."""
    M = cfg["madrugada"]
    ga4, pixel = M.get("ga4_id"), M.get("meta_pixel_id")
    if ga4 and pixel:
        return ga4, pixel
    try:
        from robo import catalogo as Cat
        html = Cat._pagina(cfg["site"]["base"]) or ""
        ga4 = ga4 or (re.findall(r"\bG-[A-Z0-9]{6,12}\b", html) or [None])[0]
        pixel = pixel or (re.findall(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{10,20})", html) or [None])[0]
    except Exception as e:  # noqa
        print(f"⚠️ não li GA4/Pixel da home: {e}")
    return ga4, pixel


def janela_da_noite(agora, M, nova=False):
    """Seleção no ar: entra às 20h e fica até as 20h do dia seguinte.
    nova=True (seleção das 19h45): já vale para a seleção que entra hoje às 20h."""
    h_abre, h_fecha = M.get("abre_hora", 20), M.get("fecha_hora", 20)
    hoje = agora.replace(minute=0, second=0, microsecond=0)
    if agora.hour >= h_abre or (nova and agora.hour >= h_abre - 2):
        abre = hoje.replace(hour=h_abre)                  # seleção de hoje
    else:
        abre = (hoje - dt.timedelta(days=1)).replace(hour=h_abre)   # seleção de ontem, ainda no ar
    fecha = (abre + dt.timedelta(days=1)).replace(hour=h_fecha)
    return abre, fecha


def renderizar(selecao, cfg, ga4=None, pixel=None, nova=False):
    M = cfg["madrugada"]
    tz = ZoneInfo(cfg["fuso"])
    agora = dt.datetime.now(tz)
    abre, fecha = janela_da_noite(agora, M, nova)
    campos = ("slug", "nome", "marca", "categoria", "imagem", "link", "de", "por", "pix", "parcelas", "desconto",
              "disponiveis", "origem")
    dados = {
        "gerado_em": agora.isoformat(timespec="minutes"),
        "abre": abre.isoformat(), "fecha": fecha.isoformat(),
        "campanha": M["utm_campaign"], "semana": abre.strftime("%Y-%m-%d"), "edicao": abre.strftime("%Y-%m-%d"),
        "abre_hora": M.get("abre_hora", 20), "fecha_hora": M.get("fecha_hora", 20),
        "cupom_fim_hora": M.get("cupom_fim_hora", 6),
        "cupom": M.get("cupom") or "", "cupom_desconto": M.get("cupom_desconto") or "", "cupom_regra": M.get("cupom_regra") or "",
        "produtos": [{k: p.get(k) for k in campos} for p in selecao],
        "beneficios": M["beneficios"],
    }
    html = MODELO.read_text("utf-8")
    html = html.replace("/*__DADOS__*/null", json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    rastreio = ""
    gtm = M.get("gtm_id")
    if gtm:                                   # Google Tag Manager (o GA4 e as tags de mídia ficam dentro do GTM)
        rastreio += (f'<script async src="https://www.googletagmanager.com/gtm.js?id={gtm}"></script>'
                     "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
                     "gtag('js',new Date());</script>")
        ga4 = None                            # sem GA4 direto: evita contar visita em dobro
    if ga4:
        rastreio += (f'<script async src="https://www.googletagmanager.com/gtag/js?id={ga4}"></script>'
                     f"<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments);}}"
                     f"gtag('js',new Date());gtag('config','{ga4}');</script>")
    if pixel:
        rastreio += ("<script>!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod?"
                     "n.callMethod.apply(n,arguments):n.queue.push(arguments)};if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;"
                     "n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;t.src=v;s=b.getElementsByTagName(e)[0];"
                     "s.parentNode.insertBefore(t,s)}(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');"
                     f"fbq('init','{pixel}');fbq('track','PageView');</script>")
    html = html.replace("<!--__RASTREIO__-->", rastreio)
    if gtm:
        html = html.replace("<body>", f'<body><noscript><iframe src="https://www.googletagmanager.com/ns.html?id={gtm}" '
                            'height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>', 1)
    return html


def publicar_pasta(html, cfg):
    SAIDA.mkdir(exist_ok=True)
    (SAIDA / "index.html").write_text(html, "utf-8")
    dominio = cfg["madrugada"].get("dominio")
    if dominio:
        (SAIDA / "CNAME").write_text(dominio + "\n", "utf-8")
    (SAIDA / ".nojekyll").write_text("", "utf-8")
    (SAIDA / "robots.txt").write_text("User-agent: *\nDisallow: /\n", "utf-8")


# ------------------------------------------------------------------ demo
def demo_produtos():
    rnd = random.Random(4)
    nomes = ["Camisa Polo Tommy Hilfiger Preta", "Camiseta Básica RL Azul Marinho", "Calça Sarja Tommy Hilfiger Bege",
             "Camisa RL Linho Custom Fit Branca", "Moletom Tommy Jeans Script Preta", "Camiseta Lacoste Regular Branca",
             "Bermuda Reserva Sarja Areia", "Tênis Calvin Klein Couro Branco", "Camisa Aramis Overshirt Xadrez Khaki",
             "Camiseta John John Brand Preta", "Chinelo Acostamento Vibe Line Azul", "Polo Lacoste Piquet Marinho",
             "Camiseta Tommy Hilfiger 1985 Verde Oliva", "Camisa Social Calvin Klein Slim Azul", "Jaqueta Tommy Jeans Puffer Preta",
             "Calça Jeans Reserva Slim Escura", "Polo RL Custom Fit Branca", "Camiseta Reserva Básica Rosa",
             "Sapatênis Reserva Couro Marrom", "Bermuda Tommy Hilfiger Chino Marinho", "Camisa Linho Reserva Azul Claro",
             "Moletom Lacoste Sport Cinza", "Tênis Lacoste Carnaby Branco", "Camiseta Calvin Klein Logo Preta"]
    cores = ["0b1a3a", "1e3a8a", "334155", "475569", "64748b", "1f2937"]
    out = []
    for i, n in enumerate(nomes):
        de = rnd.choice([199.9, 249.9, 299.9, 349.9, 399.9, 499.9, 599.9, 799.9])
        por = round(de * (1 - rnd.choice([0.18, 0.22, 0.31, 0.38, 0.45, 0.52, 0.6])), 2)
        tam = ["P", "M", "G", "GG", "XG"] if "tênis" not in n.lower() else ["39", "40", "41", "42", "43"]
        disp = rnd.sample(tam, rnd.randint(2, 5))
        out.append({"rank": i + 1, "slug": slugify(n), "nome": n, "link": f"https://www.outlet360.com.br/produto/{slugify(n)}/",
                    "imagem": f"https://placehold.co/600x750/{rnd.choice(cores)}/ffffff/png?text={n.split()[0]}",
                    "de": de, "por": por, "tamanhos": tam, "disponiveis": sorted(disp, key=tam.index)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", choices=["selecionar", "atualizar"], default="selecionar")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load((RAIZ / "config.yaml").read_text("utf-8"))
    hist = json.loads((DADOS / "historico.json").read_text("utf-8")) if (DADOS / "historico.json").exists() else {}

    if a.demo:
        produtos = [enriquecer(p, cfg) for p in demo_produtos()]
        ga4 = pixel = None
    else:
        salvo = json.loads(SELECAO_F.read_text("utf-8")) if SELECAO_F.exists() else {}
        anterior = salvo.get("produtos", [])
        so = [p["slug"] for p in anterior] if a.modo == "atualizar" and anterior else None
        signal.signal(signal.SIGALRM, _estourou)
        signal.alarm(max(1, int(PRAZO_MIN[a.modo] * 60)))
        try:
            brutos, feed, rank_venda = produtos_do_site(cfg, print, so)
        except PrazoEsgotado:
            if not anterior:
                raise SystemExit(f"❌ Passou de {PRAZO_MIN[a.modo]} min (Tiny lento) e não há vitrine anterior.")
            print(f"⚠️ Passou de {PRAZO_MIN[a.modo]} min (Tiny lento ou bloqueado): publica a última vitrine conferida")
            salvo["produtos"] = anterior
            html = renderizar(anterior, cfg, *ids_rastreamento(cfg))
            publicar_pasta(html, cfg)
            print(f"Página gerada (vitrine anterior): {SAIDA / 'index.html'}")
            return
        produtos = [enriquecer(p, cfg) for p in brutos if p.get("por") and p.get("de")]
        print(f"Conferidos no site: {len(produtos)} produtos (Por do feed, De da página do produto, grade do Tiny)")
        ga4, pixel = ids_rastreamento(cfg)
        print(f"Rastreamento: GTM={cfg['madrugada'].get('gtm_id') or '—'} · GA4 direto={'—' if cfg['madrugada'].get('gtm_id') else (ga4 or '—')} · Pixel Meta={pixel or '—'}")
        if a.modo == "atualizar" and anterior:
            # quem saiu é reposto com novos candidatos conferidos
            faltam = [p for p in anterior if p["slug"] not in {x["slug"] for x in produtos
                      if x["n_tamanhos"] >= cfg["madrugada"]["tamanhos_minimos"]}]
            reservas = [s for s in salvo.get("reservas", []) if s not in {p["slug"] for p in anterior}]
            if faltam and reservas:
                print(f"{len(faltam)} saíram: reconferindo {len(reservas)} reservas")
                extra, _, _ = produtos_do_site(cfg, print, reservas)
                produtos += [enriquecer(p, cfg) for p in extra if p["slug"] not in {x["slug"] for x in produtos}]

    signal.alarm(0)
    reservas_novas = None
    if a.modo == "atualizar" and SELECAO_F.exists() and not a.demo:
        selecao, trocas = atualizar(anterior, produtos, cfg, hist)
        reservas_novas = [s for s in salvo.get("reservas", []) if s not in {p["slug"] for p in selecao}]
        print(f"Atualização: {trocas} troca(s)")
    else:
        noite_anterior = set() if a.demo else {p["slug"] for p in (json.loads(SELECAO_F.read_text("utf-8"))["produtos"]
                                                                   if SELECAO_F.exists() else [])}
        selecao = selecionar(produtos, cfg, hist, noite_anterior)
        escolhidos = {p["slug"] for p in selecao}
        resto = [p for p in produtos if p["slug"] not in escolhidos]
        reservas_novas = [p["slug"] for p in selecionar(resto, cfg, hist)][:QTD_RESERVAS] if resto else []

    v = sum(1 for p in selecao if p["origem"] == "mais_vendido")
    print(f"Vitrine: {len(selecao)} produtos ({v} mais vendidos · {len(selecao) - v} maior desconto)")
    for i, p in enumerate(selecao, 1):
        print(f"  {i:2}. [{p['origem'][:5]}] #{p['rank']:<4} {p['desconto']:>3}% OFF  {p['n_tamanhos']} tam.  {p['nome']}")

    DADOS.mkdir(exist_ok=True)
    if not a.demo:
        SELECAO_F.write_text(json.dumps({"atualizado": dt.datetime.now().isoformat(timespec="minutes"), "produtos": selecao,
                                         "reservas": reservas_novas or []},
                                        ensure_ascii=False, indent=1), "utf-8")
    html = renderizar(selecao, cfg, ga4, pixel, nova=(a.modo == "selecionar"))
    publicar_pasta(html, cfg)
    print(f"Página gerada: {SAIDA / 'index.html'} ({len(html) // 1024} KB)")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"## Ofertas da Madrugada — {a.modo}\n\n| # | Lista | Produto | % OFF | Por | Tamanhos |\n|---|---|---|---|---|---|\n")
            for i, p in enumerate(selecao, 1):
                fh.write(f"| {i} | {'Mais vendido' if p['origem'] == 'mais_vendido' else 'Maior desconto'} | "
                         f"[{p['nome']}]({p['link']}) | {p['desconto']}% | R$ {p['por']:.2f} | {', '.join(p['disponiveis'])} |\n")


if __name__ == "__main__":
    main()
