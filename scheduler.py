"""
scheduler.py — Orquestrador de buscas + raspagem da OLX
  Fase SCAN:      varre TODAS as páginas (1º scan e full re-scan diário)
  Fase MONITOR:   a cada intervalo, as págs mais novas (por DATA) até o já conhecido
  Avaliação:      avaliador.py (regras) ao fim de cada ciclo

Extração OLX SEM navegador: HTTP com impressão TLS de Chrome (curl_cffi —
requests puro leva 403 do Cloudflare). A busca vem do payload RSC do Next.js
(header `RSC: 1`: só os dados, 3,4x menor que o HTML, anúncios idênticos em 12
páginas testadas); se não vier, o mesmo JSON é lido dos <script>
self.__next_f.push do HTML. A descrição vem do JSON-LD da página do anúncio
(lá o RSC é ignorado). OLX não manda ETag/Last-Modified (no-store): sem
requisição condicional possível — o incremental é o monitor parar no conhecido.
Sem flight data = falha explícita (OlxIndisponivel), nunca raspar às cegas:
o fallback CSS antigo escondeu por meses que só ~5 de 50 anúncios entravam.
"""
import asyncio
import ctypes
import gc
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from curl_cffi.requests import AsyncSession

import avaliador
import database as db
import notifier
from models import FUSO

logger = logging.getLogger(__name__)

_broadcast_fn = None
_tarefas: set = set()        # o loop só guarda weakref: task sem referência pode sumir

def set_broadcast(fn):
    global _broadcast_fn
    _broadcast_fn = fn

def _broadcast(msg: dict):
    if _broadcast_fn:
        t = asyncio.create_task(_broadcast_fn(msg))
        _tarefas.add(t)
        t.add_done_callback(_tarefas.discard)


class OlxIndisponivel(Exception):
    """403 do Cloudflare ou página sem flight data (layout mudou)."""


class LocalDesconhecido(Exception):
    """A OLX não reconheceu a cidade da busca de imóvel."""


# ── EXTRAÇÃO ──────────────────────────────────────────────

_RE_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S)
_RE_LDJSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)
_JSON = json.JSONDecoder()


def _extrair_preco(texto) -> float:
    if isinstance(texto, bool) or not texto:
        return 0.0
    if isinstance(texto, (int, float)):          # número já pronto: "1500.5" não é 15005
        return float(texto)
    nums = re.sub(r"[^\d,]", "", str(texto)).replace(",", ".")
    try:
        return float(nums)
    except Exception:
        return 0.0


def _achar_array_json(texto: str, chave: str):
    """Acha `chave` no texto e parseia o array JSON que a segue. Retorna list ou
    None. raw_decode lê só aquele valor (em C: 11x mais rápido que casar
    colchetes em Python, ~7 ms -> 0,6 ms numa página de 50 anúncios)."""
    # só se o VALOR da chave for uma lista ("ads": null seguido de outra lista
    # qualquer não pode virar a lista de anúncios)
    m = re.search(re.escape(chave) + r"\s*:\s*\[", texto)
    if not m:
        return None
    try:
        return _JSON.raw_decode(texto, m.end() - 1)[0]
    except ValueError:
        return None


def _mapear_flight_ad(ad: dict) -> dict | None:
    """Um anúncio do JSON da OLX -> dict do upsert_anuncio. None se incompleto
    (inclui os espaços de propaganda, que vêm só com advertisingId). Cada campo
    tem o tipo checado: formato inesperado vira vazio, não exceção."""
    if not isinstance(ad, dict):
        return None
    url, titulo = ad.get("url"), ad.get("subject")
    if not (isinstance(url, str) and url and isinstance(titulo, str) and titulo.strip()):
        return None
    d = lambda k: ad.get(k) if isinstance(ad.get(k), dict) else {}
    lista = lambda k: ad.get(k) if isinstance(ad.get(k), list) else []
    texto = lambda k: ad.get(k).strip() if isinstance(ad.get(k), str) else ""
    preco = _extrair_preco(ad.get("price") or ad.get("priceValue"))
    atributos = []
    if texto("oldPrice"):
        atributos.append(f"Preço anterior: {texto('oldPrice')} (baixou)")
    # 12: imóvel traz condomínio, IPTU, área, quartos, banheiros, vagas e os detalhes
    for p in lista("properties")[:12]:
        if isinstance(p, dict) and p.get("label") and p.get("value") \
                and p.get("name") != "category":
            atributos.append(f"{p['label']}: {p['value']}")
    try:
        data = (datetime.fromtimestamp(int(ad["date"]), FUSO).isoformat(timespec="seconds")
                if ad.get("date") else "")
    except Exception:
        data = ""
    categoria = texto("category") or texto("categoryName")
    # imóvel: venda e aluguel vêm na MESMA categoria ("Apartamentos"); separar
    # deixa a mediana e o /categorias com sentido (aluguel 3 mil x venda 1 milhão)
    for p in lista("properties"):
        if isinstance(p, dict) and p.get("name") == "real_estate_type" and categoria \
                and (m := re.match(r"(Venda|Aluguel) - ", str(p.get("value")))):
            categoria += f" ({m.group(1).lower()})"
    imgs = lista("images")
    imagem = imgs[0].get("original") if imgs and isinstance(imgs[0], dict) else ""
    entrega = bool(d("olxDelivery").get("enabled") or ad.get("olxDeliveryBadgeEnabled") is True)
    return {
        "url":            url,
        "list_id":        ad.get("listId") if isinstance(ad.get("listId"), (int, str)) else None,
        "titulo":         titulo.strip(),
        "preco":          preco,
        "data_postagem":  data,
        "local":          texto("location"),
        "envio":          "com_envio" if entrega else "sem_envio",
        "vendedor":       "loja" if ad.get("professionalAd") is True else "particular",
        "imagem":         imagem if isinstance(imagem, str) else "",
        "atributos":      " · ".join(atributos),
        "categoria":      categoria,
        "olx_pay":        bool(d("olxPay").get("enabled") or ad.get("olxPayBadgeEnabled") is True),
        "descricao":      "",
    }


def _texto_flight(scripts: list[str]) -> str:
    """Junta os chunks de `self.__next_f.push([1,"..."])` dos <script> inline.
    O array self.__next_f em RUNTIME vem vazio (o Next consome após hidratar) —
    ler dele fez o bot cair no fallback CSS por meses (~5 de 50 anúncios/pág)."""
    dec = json.JSONDecoder()
    partes = []
    for s in scripts:
        pos = 0
        while (i := s.find("self.__next_f.push(", pos)) >= 0:
            pos = i + 19
            try:
                arr, _ = dec.raw_decode(s, pos)
            except ValueError:
                continue
            if isinstance(arr, list) and len(arr) > 1 and isinstance(arr[1], str):
                partes.append(arr[1])
    return "".join(partes)


def extrair_anuncios(pagina_html: str) -> list[dict] | None:
    """Do HTML: junta o flight data dos <script> e lê os anúncios."""
    return _anuncios_do_flight(
        _texto_flight([s for s in _RE_SCRIPT.findall(pagina_html) if "__next_f" in s]))


def _anuncios_do_flight(texto: str) -> list[dict] | None:
    """None = SEM flight data (layout mudou). [] = busca sem resultados (os
    cards visíveis são recomendações fora da busca — nunca ingerir)."""
    if len(texto) < 1000:
        return None
    if '"ads"' not in texto:
        # busca vazia vem com "ads":[] e totalOfAds 0; sem a lista E sem o total
        # zerado = formato novo. Tratar como "vazia" deixaria o bot cego em silêncio
        return [] if re.search(r'"totalOfAds"\s*:\s*0\b', texto) else None
    lista = _achar_array_json(texto, '"ads"')
    if lista is None and re.search(r'"ads"\s*:\s*\[', texto):
        return None        # lista que abre e não fecha = resposta truncada, não "fim"
    produtos = []
    for ad in lista or []:
        try:
            if (p := _mapear_flight_ad(ad)):
                produtos.append(p)
        except Exception as e:      # 1 anúncio fora do formato não derruba a página
            logger.warning(f"anúncio ignorado (formato inesperado): {e!r}")
    return produtos


def _achar_chave(obj, chave: str):
    """Primeiro valor string não vazio de `chave` em qualquer nível do JSON."""
    if isinstance(obj, dict):
        if isinstance(obj.get(chave), str) and obj[chave].strip():
            return obj[chave]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            if (achado := _achar_chave(v, chave)):
                return achado
    return None


def extrair_descricao(pagina_html: str) -> str:
    """Descrição do anúncio pelo JSON-LD (schema.org) da página. Produto traz
    no topo; carro vem como Person > makesOffer > ... > description."""
    for bloco in _RE_LDJSON.findall(pagina_html):
        try:
            desc = _achar_chave(json.loads(bloco), "description")
        except ValueError:
            continue
        if desc:
            txt = re.sub(r"<br\s*/?>", "\n", desc)
            return html.unescape(re.sub(r"<[^>]+>", " ", txt)).strip()[:2000]
    return ""


async def _baixar(sessao, url: str, **kw):
    r = await sessao.get(url, timeout=30, **kw)
    if r.status_code in (403, 429):
        raise OlxIndisponivel(f"{r.status_code} ({'bloqueio' if r.status_code == 403 else 'limite de requisições'}) em {url}")
    r.raise_for_status()
    return r


def _faltando(texto: str, lidos: int, url: str) -> str | None:
    """A OLX declara o total da busca (totalOfAds/pageSize): página que deveria
    trazer N anúncios e rendeu bem menos = campo renomeado ou formato novo. É o
    defeito silencioso: o bot já passou meses lendo 5 de 50 sem nenhum erro."""
    total, tam = re.search(r'"totalOfAds"\s*:\s*(\d+)', texto), re.search(r'"pageSize"\s*:\s*(\d+)', texto)
    if not (total and tam):
        return None
    pagina = int(m.group(1)) if (m := re.search(r"[?&]o=(\d+)", url)) else 1
    esperado = max(0, min(int(tam.group(1)), int(total.group(1)) - (pagina - 1) * int(tam.group(1))))
    if esperado >= 5 and lidos < 0.8 * esperado:
        return f"a OLX declarou {esperado} anúncios na página e o bot leu só {lidos}"
    return None


async def _pagina_de_busca(sessao, url: str, exige: str = "") -> list[dict] | None:
    """Payload RSC (3,4x menor) e, se não vier no formato esperado, o HTML.
    Confere o lido com o total que a própria OLX declara. `exige`: filtro que a
    OLX tem de ecoar na resposta (cidade): nome errado ela ignora em silêncio e
    devolve o Brasil inteiro."""
    # "&_rsc" já na URL: sem ele a OLX responde 307 p/ a mesma URL com "&_rsc" e
    # cada página custava 2 requisições (medido: 0,24 s e 1 ida ao Cloudflare a mais)
    r = await _baixar(sessao, url + "&_rsc", headers={"RSC": "1"})
    texto = r.text if "x-component" in (r.headers.get("content-type") or "") else ""
    ads = _anuncios_do_flight(texto) if texto else None
    if ads is None:
        html_ = (await _baixar(sessao, url)).text
        texto = _texto_flight([s for s in _RE_SCRIPT.findall(html_) if "__next_f" in s])
        ads = _anuncios_do_flight(texto)
    if ads is not None and exige and not re.search(r'"loc_(?:municipality|neighbourhood)_id"', texto):
        raise LocalDesconhecido("a resposta veio sem o filtro de local")
    if ads is not None and (falta := _faltando(texto, len(ads), url)):
        logger.warning(f"formato da OLX: {falta} ({url})")
        await asyncio.to_thread(notifier.alertar, "olx_formato",
                                f"⚠️ A OLX pode ter mudado o formato: {falta}. Anúncios podem "
                                "estar passando sem alerta — o bot precisa de ajuste.", 24)
    return ads


def _sessao():
    return AsyncSession(impersonate="chrome")


# ── URL E FILTROS ─────────────────────────────────────────

def _montar_url(busca: dict, pagina: int, teto: bool = True) -> str:
    estado   = busca.get("estado") or "brasil"
    termo    = quote(avaliador.termo_de_busca(busca.get("termo") or ""))
    preco_min = busca.get("preco_min") or 0
    preco_max = busca.get("preco_max") or 0
    vendedor  = busca.get("filtro_vendedor") or "qualquer"

    # sf=1 = ordem por DATA (verificado no campo "sort" do JSON da OLX). O
    # antigo sf=3 era "relevance": o monitor (págs 1-2) nunca via os novos.
    # A ordenação escolhida pelo usuário não importa: o scan lê tudo e o
    # monitor só faz sentido por data.
    params = "&sf=1"
    if preco_min > 0: params += f"&ps={int(preco_min)}"
    # até 15% acima do teto: quem aceita proposta ainda pode caber no seu preço
    if preco_max > 0 and teto:          # sem teto: medição do mercado (_atualizar_mercado)
        params += f"&pe={round(preco_max * (1 + avaliador.MARGEM_NEGOCIACAO))}"
    # semântica REAL da OLX (verificada): f=p -> particular, f=c -> loja/comercial
    if vendedor == "particular": params += "&f=p"
    elif vendedor == "loja":     params += "&f=c"

    # a busca geral (/brasil?q=) alcança TODAS as categorias, carros e imóveis
    # inclusive; a URL da categoria serve p/ os filtros
    # dela (ano rs/re) e p/ "carro sem modelo" = categoria inteira
    tipo = busca.get("tipo", "produto")
    extras = busca.get("filtros_extra") or {}
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except Exception:
            extras = {}
    uf = f"/estado-{estado}" if estado != "brasil" else ""
    if tipo == "carro":
        base = f"https://www.olx.com.br/autos-e-pecas/carros-vans-e-utilitarios{uf}"
        if extras.get("ano_min"):
            params += f"&rs={extras['ano_min']}"
        if extras.get("ano_max"):
            params += f"&re={extras['ano_max']}"
    elif tipo == "imovel":
        # formato verificado na OLX: /imoveis/<aluguel|venda>[/apartamentos|/casas]
        # /estado-uf[/cidade[/bairro]]. Região ("grande-sao-paulo") não
        # existe mais: a OLX redireciona p/ o estado inteiro
        seg = {"apartamento": "/apartamentos", "casa": "/casas"}.get(extras.get("imovel"), "")
        base = f"https://www.olx.com.br/imoveis/{extras.get('negocio') or 'aluguel'}{seg}{uf}"
        if extras.get("cidade") and uf:
            base += "/" + _slug(extras["cidade"])
            bairros = extras.get("bairros") or []
            # 1 bairro: direto na URL; vários: a cidade toda e o filtro local escolhe
            # (bairro que a OLX não reconhece ela ignora: o filtro local garante)
            if len(bairros) == 1:
                base += "/" + _slug(bairros[0])
        if extras.get("quartos_min") or extras.get("quartos_max"):   # lista: "ros" repetido (5 = "5 ou mais")
            params += "".join(f"&ros={n}" for n in range(min(extras.get("quartos_min") or 1, 5),
                                                         min(extras.get("quartos_max") or 5, 5) + 1))
        if extras.get("vagas_min"):
            params += "".join(f"&gsp={n}" for n in range(min(extras["vagas_min"], 5), 6))
        if extras.get("area_min"):
            params += f"&ss={extras['area_min']}"
    else:
        # /go?q= devolve 0 resultados + 80 recomendações
        base = f"https://www.olx.com.br{uf or '/brasil'}"
    q = f"q={termo}&" if termo else ""     # carro sem modelo = categoria inteira
    return f"{base}?{q}o={pagina}{params}"


def _slug(nome: str) -> str:
    """'São José dos Campos' -> 'sao-jose-dos-campos' (como a OLX escreve na URL)."""
    return re.sub(r"[^a-z0-9]+", "-", avaliador.norm(nome)).strip("-")


def filtrar_produtos(busca: dict, produtos: list[dict]) -> list[dict]:
    """Aplica faixa de preço, envio, vendedor e garantia da busca. A URL da OLX
    já filtra preço/vendedor (o pós-filtro é a rede de segurança); envio e
    garantia não existem na URL (o parâmetro olx_pay_and_ship é ignorado pela OLX — verificado)."""
    pmin = busca.get("preco_min") or 0
    pmax = busca.get("preco_max") or 0
    fe = busca.get("filtro_envio") or "qualquer"
    garantia = (busca.get("garantia") or "").lower()
    if busca.get("tipo") == "imovel":
        garantia = ""                       # Garantia da OLX é de produto
    out = []
    for p in produtos:
        preco = p.get("preco") or 0
        if preco:                       # preço 0 = "a combinar" -> deixa o avaliador julgar
            if pmin and preco < pmin:
                continue
            if pmax and preco > pmax * (1 + avaliador.MARGEM_NEGOCIACAO):
                continue
        if busca.get("tipo") == "imovel":
            if not avaliador.passa_filtros_imovel(busca, p)[0]:
                continue
            fe = "qualquer"                 # imóvel não tem envio
        if fe == "frete_gratis" and p.get("envio") not in ("frete_gratis", "com_envio"):
            continue
        if fe == "com_envio" and p.get("envio") == "sem_envio":
            continue
        if fe == "sem_envio" and p.get("envio") != "sem_envio":
            continue
        fv = busca.get("filtro_vendedor") or "qualquer"
        if fv in ("particular", "loja") and p.get("vendedor") and p["vendedor"] != fv:
            continue
        if garantia and "olx_pay" in p:
            if not p["olx_pay"] or ("entrega" in garantia and p.get("envio") == "sem_envio"):
                continue
        if not _passa_filtros_carro(busca, p):
            continue
        out.append(p)
    return out


def _passa_filtros_carro(busca: dict, p: dict) -> bool:
    """Pós-filtros determinísticos de carro (ano/km/motor) sobre título+atributos.
    Info ausente no anúncio -> passa (o avaliador decide com as instruções)."""
    extras = busca.get("filtros_extra") or {}
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except Exception:
            extras = {}
    if not extras:
        return True
    titulo = p.get("titulo") or ""
    texto = f"{titulo} {p.get('atributos') or ''}".lower()

    anos = re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", titulo)
    if anos and (extras.get("ano_min") or extras.get("ano_max")):
        ano = int(anos[-1])          # padrão OLX: ano no fim do título
        if extras.get("ano_min") and ano < extras["ano_min"]:
            return False
        if extras.get("ano_max") and ano > extras["ano_max"]:
            return False

    if extras.get("km_max"):
        # atributo da OLX vem "Quilometragem: 150000" (sem sufixo km)
        m = (re.search(r"quilometragem:\s*([\d.]{1,9})", texto)
             or re.search(r"([\d.]{2,7})\s*km\b", texto))
        if m:
            try:
                if int(m.group(1).replace(".", "")) > extras["km_max"]:
                    return False
            except ValueError:
                pass

    if extras.get("motor"):
        litragens = re.findall(r"\b(\d\.\d)\b", titulo)
        if litragens and extras["motor"] not in litragens:
            return False
    return True


# ── RAIO (imóvel) ─────────────────────────────────────────

GEOCODER = "https://nominatim.openstreetmap.org/search"


def _geocodificar_sinc(consulta: str):
    import requests
    r = requests.get(GEOCODER, params={"q": consulta, "format": "json", "limit": 1, "countrycodes": "br"},
                     headers={"User-Agent": "monitor-olx/1.0"}, timeout=15)
    r.raise_for_status()
    achados = r.json()
    return (float(achados[0]["lat"]), float(achados[0]["lon"])) if achados else (None, None)


GEOCODER_CEP = "https://cep.awesomeapi.com.br/json/"


class _LimiteCep(Exception):
    pass


def _extrair_cep(pagina_html: str) -> str:
    m = re.search(r'"(?:postalCode|zipcode)"\s*:\s*"(\d{5})-?(\d{3})"', pagina_html)
    return m.group(1) + m.group(2) if m else ""


_cep_pausa_ate = 0.0        # o serviço gratuito limita as consultas (429): espera 1 h


def _geocodificar_cep_sinc(cep: str):
    import requests
    r = requests.get(GEOCODER_CEP + cep, headers={"User-Agent": "monitor-olx/1.0"}, timeout=15)
    if r.status_code in (400, 404):
        return None, None
    if r.status_code == 429:
        raise _LimiteCep()
    r.raise_for_status()
    d = r.json()
    return (float(d["lat"]), float(d["lng"])) if d.get("lat") and d.get("lng") else (None, None)


async def _geocodificar_cep(busca: dict, cep: str):
    """Busca com raio: coordenadas do CEP do anúncio (1x por CEP). A mais de 100 km
    do ponto de referência é erro da base: vale como não achado e fica o bairro.
    Falha de rede não grava nada."""
    global _cep_pausa_ate
    centro = avaliador._extras(busca).get("centro")
    if busca.get("tipo") != "imovel" or not centro or not cep or db.coords_local(f"cep:{cep}") is not None \
            or time.monotonic() < _cep_pausa_ate:
        return
    try:
        lat, lon = await asyncio.to_thread(_geocodificar_cep_sinc, cep)
    except _LimiteCep:
        _cep_pausa_ate = time.monotonic() + 3600
        logger.info("serviço de CEP no limite de consultas: 1 h só com o centro do bairro")
        return
    except Exception as e:
        logger.warning(f"geocodificação do CEP falhou: {e!r}")
        return
    db.salvar_coords(f"cep:{cep}", lat, lon)
    if lat is not None and (avaliador.distancia_km(busca, {"cep": cep}) or 0) > 100:
        db.salvar_coords(f"cep:{cep}", None, None)


async def _geocodificar(busca: dict, ads: list[dict]):
    """Busca com raio: acha (1x, guardado p/ sempre) o centro de cada bairro novo
    no OpenStreetMap — 1 consulta por segundo, como o serviço pede. Resultado a
    mais de 100 km do ponto de referência é homônimo de outra cidade: conta como
    não achado. Falha de rede não grava nada (tenta na próxima rodada) e o
    anúncio passa sem a distância."""
    extras = avaliador._extras(busca)
    if busca.get("tipo") != "imovel" or not extras.get("centro"):
        return
    uf = busca.get("estado") or ""
    for chave in dict.fromkeys(avaliador.chave_local(uf, a) for a in ads):
        if not chave or db.coords_local(chave) is not None:
            continue
        _, cidade, bairro = chave.split("|")
        try:
            lat, lon = await asyncio.to_thread(_geocodificar_sinc, f"{bairro}, {cidade}, {uf}, Brasil")
        except Exception as e:
            logger.warning(f"geocodificação de '{bairro}' falhou: {e!r}")
            return
        db.salvar_coords(chave, lat, lon)
        d = avaliador.distancia_km(busca, {"local": f"{cidade}, {bairro}"}) if lat is not None else None
        if d is not None and d > 100:
            db.salvar_coords(chave, None, None)
        await asyncio.sleep(1.1)


# ── RASPAGEM ──────────────────────────────────────────────

def _ja_lido(ads: list[dict], busca: dict) -> bool:
    """Página sem nada que passe nos filtros locais (ex.: busca "com envio" e só
    anúncios sem envio): o monitor só para quando o mais velho dela é anterior à
    última leitura. Antes parava ali e o novo da pág. 2 esperava o full diário."""
    try:
        ultimo = datetime.fromisoformat(busca["ultimo_scan"]).replace(tzinfo=timezone.utc)
        return min(datetime.fromisoformat(a["data_postagem"]) for a in ads) < ultimo
    except (KeyError, TypeError, ValueError):
        return True        # sem datas: para, como antes


async def raspar_busca(busca: dict, fase: str) -> list[dict]:
    """SCAN: todas as páginas (até 25). MONITOR: as mais novas por data, até a
    primeira página sem NENHUM anúncio novo p/ o banco (antes eram 2 fixas: mais
    de 100 anúncios novos entre dois scans = os excedentes só no full diário)."""
    todos, vistos = [], set()
    # imóvel com cidade: a OLX ecoa o filtro de local; se não ecoou, não reconheceu
    exige = "loc_" if busca.get("tipo") == "imovel" and avaliador._extras(busca).get("cidade") else ""
    # ponytail: 25 págs (~1250 anúncios) cobre qualquer busca útil; suba se precisar
    max_pags = 25 if fase == "scan" else 10
    async with _sessao() as sessao:
        for pagina in range(1, max_pags + 1):
            url = _montar_url(busca, pagina)
            logger.info(f"  [{busca['nome']}] pág {pagina}: {url}")
            ads = None
            for _ in range(2):                    # resposta truncada ocasional: 1 retry
                # só na 1ª página: basta p/ saber se a cidade existe
                ads = await _pagina_de_busca(sessao, url, exige=exige if pagina == 1 else "")
                if ads is not None:
                    break
                await asyncio.sleep(3)
            if ads is None:
                raise OlxIndisponivel(f"página sem flight data (layout mudou?): {url}")
            novos = [a for a in ads if (a["list_id"] or a["url"]) not in vistos]
            if not novos:                         # fim dos resultados
                break
            vistos.update(a["list_id"] or a["url"] for a in novos)
            todos.extend(novos)
            await _geocodificar(busca, novos)
            # alcançou o que já tinha visto (descartado pelos filtros locais nunca é
            # gravado: se contasse como "novo", o monitor nunca pararia cedo)
            relevantes = filtrar_produtos(busca, novos)
            if fase == "monitor" and (len(db.conhecidos(busca["id"], relevantes)) == len(relevantes)
                                      if relevantes else _ja_lido(novos, busca)):
                break
            _broadcast({"tipo": "scraping", "busca_id": busca["id"],
                        "pagina": pagina, "encontrados": len(todos)})
            await asyncio.sleep(1.5)
    return todos


async def _preencher_descricoes(busca: dict, limite: int = 60):
    """Baixa a descrição dos anúncios ativos que ainda não têm (os critérios
    costumam viver nela)."""
    pendentes = db.anuncios_sem_descricao(busca["id"], limite)
    if not pendentes and not (avaliador._extras(busca).get("centro") and db.ceps_sem_coords(busca["id"], 1)):
        return
    ok = 0
    async with _sessao() as sessao:
        for a in pendentes:
            try:
                pagina = (await _baixar(sessao, a["url"])).text
                desc, cep = extrair_descricao(pagina), _extrair_cep(pagina)
                await _geocodificar_cep(busca, cep)
                # '(sem descrição)' marca visita feita — evita refetch eterno
                db.atualizar_descricao(a["id"], busca["id"], desc or "(sem descrição)", cep)
                ok += 1
            except OlxIndisponivel:
                raise
            except Exception as e:                # tenta de novo no próximo ciclo
                logger.debug(f"descrição {a['url'][:60]}: {e}")
            await asyncio.sleep(1.0)              # pausa também após falha (sem rajada)
    # CEPs lidos enquanto o serviço estava no limite: localiza agora e reavalia
    # (em silêncio) os anúncios deles — o que estiver fora do raio sai
    for cep in db.ceps_sem_coords(busca["id"]) if avaliador._extras(busca).get("centro") else []:
        if time.monotonic() < _cep_pausa_ate:
            break
        await _geocodificar_cep(busca, cep)
        if db.coords_local(f"cep:{cep}") is not None:
            db.reavaliar_cep(busca["id"], cep)
        await asyncio.sleep(1.0)
    if ok:
        logger.info(f"[{busca.get('nome')}] {ok} descrições preenchidas")


async def _atualizar_mercado(busca: dict):
    """Mediana do MERCADO: a busca SEM o teto, até 5 páginas (as mais novas),
    só o produto certo e sem defeito; 1x/dia. Com teto, a OLX só devolve até
    teto+15% e a nota não sabe se o preço é pechincha ou só o normal. Amostra
    pequena/dispersa também fica registrada (senão remediria a cada rodada);
    falha não derruba a busca: tenta de novo na próxima."""
    nome = busca.get("nome")
    if busca.get("tipo") == "imovel":
        return await _atualizar_mercado_imovel(busca)
    try:
        precos, vistos = [], set()
        async with _sessao() as sessao:
            for pagina in range(1, 6):
                ads = await _pagina_de_busca(sessao, _montar_url(busca, pagina, teto=False))
                novos = [a for a in ads or [] if (a["list_id"] or a["url"]) not in vistos]
                if not novos:
                    break
                vistos.update(a["list_id"] or a["url"] for a in novos)
                precos += [(a["preco"], avaliador.variante(a["titulo"], a["atributos"])) for a in novos
                           if a["preco"] > 0 and avaliador.triagem(busca, a)[0]
                           and not avaliador.tem_defeito(a, busca.get("tipo"))]
                await asyncio.sleep(1.5)
        if _editada(busca, db._MUDA_MERCADO):
            return                 # editada durante a medição: a mediana é do termo antigo
        resumo = avaliador.resumo_mercado(precos)
        db.salvar_mercado(busca["id"], resumo or {"n": len(precos)})
        logger.info(f"[{nome}] mercado: " + (
            f"mediana {avaliador._brl(resumo['mediana'])} de {resumo['n']} anúncios" + "".join(
                f"; {v}: {avaliador._brl(g['mediana'])} ({g['n']})" for v, g in resumo["variantes"].items())
            if resumo else f"amostra pequena ou dispersa demais ({len(precos)} anúncios), sem referência"))
    except Exception as e:
        logger.warning(f"[{nome}] medição de mercado falhou: {e}")
        _bloqueio(e)


async def _atualizar_mercado_imovel(busca: dict):
    """Referência de preço do imóvel: os PRÓPRIOS anúncios da busca (30 dias), por
    bairro, quartos e tamanho — sem requisição nenhuma. O mercado geral da cidade
    foi testado e não serve: quem procura até R$ 2.000 está nos 20% mais baratos
    dele, então tudo saía "abaixo do mercado" e kitnet real de R$ 600 saía como
    golpe. O que interessa é qual é barato ENTRE os que cabem no filtro."""
    amostra = [c for c in db.anuncios_comparaveis(busca["id"]) if avaliador.triagem(busca, c)[0]]
    if (teto := busca.get("preco_max") or 0):
        # só o que cabe (ou quase) no teto: o coletado vai até +15% no ALUGUEL, e com
        # condomínio passa muito do teto — puxava a mediana e metade virava "barato"
        amostra = [c for c in amostra
                   if avaliador.custo_imovel(c)[0] <= teto * (1 + avaliador.MARGEM_NEGOCIACAO)]
    if _editada(busca, db._MUDA_MERCADO):
        return
    resumo = avaliador.resumo_mercado_imovel(amostra)
    db.salvar_mercado(busca["id"], resumo or {"n": len(amostra)})
    logger.info(f"[{busca.get('nome')}] referência de preço: " + (
        f"{resumo['n']} imóveis do filtro em {len(resumo['amostras'])} grupos (bairro/quartos), "
        f"custo mediano {avaliador._brl(resumo['mediana'])}"
        if resumo else f"amostra pequena ({len(amostra)} imóveis), sem referência"))


# ── LOOP PRINCIPAL ────────────────────────────────────────

ultimo_ciclo = time.monotonic()   # heartbeat lido pelo watchdog do main.py

# Bloqueio da OLX só vira alerta quando as falhas seguem por 1 h sem nenhuma
# leitura boa no meio: o Cloudflare às vezes dá 403 em ondas de ~30 min e o bot
# se recupera sozinho; avisar já na 1ª falha seria alarme falso. Conta da 1ª
# falha, não do último sucesso:
# busca diária com um 403 isolado não pode alarmar.
OLX_ALERTA_APOS = 3600
_olx_falha_desde = None           # 1ª falha da sequência atual (relógio monotônico)
_olx_alertado = False
# 403/429: TODAS as buscas esperam 15 min, senão cada uma bate sozinha no
# bloqueio. Insistir no bloqueio só piora o histórico do IP.
OLX_PAUSA = 900
_olx_pausa_ate = 0.0


def _bloqueio(e: Exception) -> bool:
    """403/429 em QUALQUER leitura (busca, mercado, checagem diária): TODAS as
    buscas esperam OLX_PAUSA. Insistir no bloqueio só piora o histórico do IP."""
    global _olx_pausa_ate
    if isinstance(e, OlxIndisponivel) and re.match(r"(403|429) ", str(e)):
        _olx_pausa_ate = time.monotonic() + OLX_PAUSA
        logger.info(f"bloqueio da OLX: buscas pausadas por {OLX_PAUSA // 60} min")
        return True
    return False


def _editada(antes: dict, campos: set) -> bool:
    """A busca foi editada (ou apagada) enquanto a rodada corria?"""
    atual = db.obter_busca(antes["id"])
    return not atual or any(atual.get(k) != antes.get(k) for k in campos)


async def _olx_respondeu():
    global _olx_falha_desde, _olx_alertado
    _olx_falha_desde = None
    if _olx_alertado:
        _olx_alertado = False
        await asyncio.to_thread(notifier.alertar, "olx_voltou",
                                "✅ <b>OLX voltou</b>: o bot está lendo os anúncios de novo.", 0)


async def _olx_falhou(e: Exception):
    global _olx_falha_desde, _olx_alertado
    agora = time.monotonic()
    if _olx_falha_desde is None:
        _olx_falha_desde = agora
    parado = agora - _olx_falha_desde
    if parado >= OLX_ALERTA_APOS and not _olx_alertado:
        _olx_alertado = await asyncio.to_thread(
            notifier.alertar, "olx",
            f"⚠️ <b>OLX indisponível</b> há {parado / 60:.0f} min: {notifier.esc(str(e))}\n"
            "Bloqueio ou mudança de layout. O bot continua tentando e avisa quando voltar.", 0)


try:   # glibc: devolve ao sistema a memória livre presa no heap fragmentado
    _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
except (OSError, AttributeError):          # macOS, Windows, musl: não tem
    _malloc_trim = None
_ram_base = 0
RAM_MAX = 200          # MB; o normal é ~76


def _vigiar_ram():
    """Depois de cada rodada com busca: gc.collect + malloc_trim (devolve ao
    sistema o que foi liberado) e 1 linha no log quando a RAM fica 5+ MB acima
    da última anotação, com o detalhe que mostra onde ela está."""
    global _ram_base
    gc.collect()          # ciclos de exceção (ver telegram_bot) antes: lixo vivo o trim não solta
    if _malloc_trim:
        _malloc_trim(0)
    ram = db.ram_mb()
    if not ram:
        return
    if ram > RAM_MAX:
        # ponytail: rede de segurança p/ vazamento futuro; a linha "RAM subiu" mostra
        # o evento. Aqui (fim da rodada) nada está em voo: sem envio ao Telegram nem
        # escrita no banco pela metade
        logger.critical(f"RAM em {ram} MB (limite {RAM_MAX}{_detalhe_ram()}): reiniciando o processo")
        os._exit(1)        # Restart=on-failure (systemd) / restart: unless-stopped (compose)
    if _ram_base and ram >= _ram_base + 5:
        logger.info(f"RAM subiu: {ram} MB (+{ram - _ram_base} MB desde a última anotação{_detalhe_ram()})")
    if not _ram_base or ram >= _ram_base + 5 or ram < _ram_base:
        _ram_base = ram


def _detalhe_ram() -> str:
    """Onde está a memória: anônima x páginas de arquivo (bibliotecas) e objetos
    Python vivos — separa vazamento em Python, nativo e fragmentação."""
    try:
        st = {k: int(v.split()[0]) >> 10 for k, v in (l.split(":", 1) for l in
              Path("/proc/self/status").read_text().splitlines() if l.startswith("Rss"))}
        return (f"; anônima {st['RssAnon']} MB, arquivos {st['RssFile']} MB, "
                f"{sys.getallocatedblocks() // 1000} mil objetos Python")
    except (OSError, KeyError, ValueError):
        return ""


# Checagem diária da OLX numa busca ampla e estável: pega mudança de formato
# mesmo com as buscas do usuário quietas (preço que some, descrição que sai
# vazia, ordenação que muda — tudo isso falhava em silêncio). 2 requisições/dia
CANARIO_URL = "https://www.olx.com.br/brasil?q=geladeira&o=1&sf=1"
_canario_proximo = time.monotonic() + 3600        # 1ª: 1 h depois de subir


async def _canario():
    global _canario_proximo
    _canario_proximo = time.monotonic() + 86400
    try:
        async with _sessao() as sessao:
            ads = await _pagina_de_busca(sessao, CANARIO_URL) or []
            problemas = [f"só {len(ads)} anúncios numa busca que traz ~50"] if len(ads) < 40 else []
            for campo, ok in (("preço", lambda a: a["preco"] > 0), ("id", lambda a: a["list_id"]),
                              ("data", lambda a: a["data_postagem"]), ("local", lambda a: a["local"]),
                              ("foto", lambda a: a["imagem"])):
                if ads and sum(1 for a in ads if ok(a)) < 0.8 * len(ads):
                    problemas.append(f"{campo} faltando na maioria")
            datas = [a["data_postagem"] for a in ads if a["data_postagem"]]
            if datas and sum(x < y for x, y in zip(datas, datas[1:])) > 0.2 * len(datas):
                problemas.append("não vem mais em ordem de data (o monitor perderia os novos)")
            if ads:
                await asyncio.sleep(1.5)
                try:
                    desc = extrair_descricao((await _baixar(sessao, ads[0]["url"])).text)
                except OlxIndisponivel:
                    raise
                except Exception:      # 404/5xx na página do anúncio: é defeito a avisar
                    desc = ""
                if len(desc) < 10:
                    problemas.append("a descrição do anúncio não sai mais")
    except Exception as e:             # bloqueio/rede: não é formato; tenta em 1 h
        _canario_proximo = time.monotonic() + 3600
        _bloqueio(e)
        (logger.info if isinstance(e, OlxIndisponivel) else logger.warning)(
            f"checagem diária da OLX adiada 1 h: {e!r}")
        return
    if problemas:
        logger.warning("checagem diária da OLX: " + "; ".join(problemas))
        await asyncio.to_thread(notifier.alertar, "olx_canario",
                                "⚠️ Checagem diária da OLX falhou: " + "; ".join(problemas)
                                + ". O bot precisa de ajuste.", 24)
    else:
        logger.info(f"checagem diária da OLX ok: {len(ads)} anúncios completos, por data, com descrição")


async def ciclo(timeout_busca: float = 900):
    """Uma volta: buscas vencidas, depois a fila de avaliação."""
    global ultimo_ciclo
    buscas = db.buscas_para_processar() if time.monotonic() >= _olx_pausa_ate else []
    for busca in buscas:
        if time.monotonic() < _olx_pausa_ate:      # a anterior levou 403 agora
            break
        # relida: a lista é do início do ciclo, e a busca pode ter sido pausada,
        # apagada ou editada enquanto as anteriores rodavam
        busca = db.obter_busca(busca["id"])
        if not busca or busca["status"] == "pausada":
            continue
        ultimo_ciclo = time.monotonic()           # heartbeat por busca, não por ciclo
        try:
            # 15 min por busca: uma travada não congela o resto p/ sempre
            await asyncio.wait_for(processar_busca(busca), timeout_busca)
        except asyncio.TimeoutError:
            # antes o TimeoutError saía do for: as buscas seguintes eram puladas
            # e esta ficava sem proximo_scan
            logger.error(f"[{busca['nome']}] estourou {timeout_busca:.0f} s — "
                         "próxima tentativa no intervalo")
            db.registrar_erro(busca["id"], busca["intervalo_min"])
    if time.monotonic() >= max(_canario_proximo, _olx_pausa_ate):
        await _canario()
    await avaliador.processar_fila(notifier)
    db.manutencao_diaria()
    if buscas:
        _vigiar_ram()


async def loop_scheduler():
    """Loop principal — executa indefinidamente."""
    global ultimo_ciclo
    logger.info("Scheduler iniciado.")
    while True:
        ultimo_ciclo = time.monotonic()
        try:
            await ciclo()
        except Exception:
            logger.exception("Erro no loop do scheduler")
        await asyncio.sleep(30)


async def processar_busca(busca: dict):
    """Raspa, grava, faz a triagem de identidade e baixa descrições. O STATUS
    não é tocado no início: uma interrupção no meio (timeout, reinício,
    desligamento) deixa a busca exatamente como estava, ainda processável."""
    bid, nome = busca["id"], busca.get("nome", str(busca["id"]))
    fase = busca.get("fase") or "scan"
    # full re-scan diário: renova ultima_vez de TODOS os anúncios (detecção de
    # expirados) e pega o que o monitor não alcança
    if fase == "monitor" and db.full_scan_vencido(bid):
        fase = "scan"
        logger.info(f"[{nome}] full re-scan diário")
    logger.info(f"[{nome}] Iniciando fase: {fase.upper()}")
    _broadcast({"tipo": "busca_status", "busca_id": bid, "status": "rodando"})

    try:
        produtos = await raspar_busca(busca, fase)
        brutos = len(produtos)
        produtos = filtrar_produtos(busca, produtos)
        logger.info(f"[{nome}] {len(produtos)} anúncios coletados"
                    + (f" ({brutos - len(produtos)} filtrados)" if brutos != len(produtos) else ""))

        novos, descartados, quedas = 0, 0, []
        for p in produtos:
            anuncio_id, novo, preco_antigo = db.upsert_anuncio(bid, p)
            if novo:
                novos += 1
                # triagem de identidade JÁ na entrada: ~97% dos resultados da OLX
                # são outro produto, e antes cada um custava 1 visita à página
                ok, motivo = avaliador.triagem(busca, p)
                if ok:
                    db.adicionar_fila(anuncio_id, bid, prioridade=1 if fase == "scan" else 2)
                else:
                    descartados += 1
                    db.salvar_analise(anuncio_id, avaliador.excluido(p["titulo"], motivo))
            elif preco_antigo and 0 < p["preco"] < preco_antigo:
                queda = preco_antigo - p["preco"]
                an = db.analise_de(anuncio_id) or {}
                teto = busca.get("preco_max") or 0
                # >=3% E >=R$50 num anúncio já aprovado; ou cruzou o teto do usuário
                # (estava acima, coletado pela margem de negociação, e agora cabe)
                if (queda >= 0.03 * preco_antigo and queda >= 50
                        and an.get("recomendacao") in ("COMPRAR", "VER")) \
                        or (teto and busca.get("tipo") != "imovel" and preco_antigo > teto >= p["preco"]
                            and an.get("decisao") == "APROVADO"):
                    quedas.append((anuncio_id, p, preco_antigo))

        logger.info(f"[{nome}] {novos} novos ({descartados} descartados na triagem)")
        for anuncio_id, p, antigo in quedas:
            # já avisado com link: a reavaliação (pode virar COMPRAR) não manda de novo
            if await notifier.enviar_queda(busca, p, antigo, anuncio_id):
                db.marcar_notificado(anuncio_id)
                logger.info(f"alerta queda enviado: [{nome}] {p['titulo'][:50]} {antigo:.0f} -> {p['preco']:.0f}")

        # descrição antes da avaliação (novos + backfill), só de quem passou na triagem
        await _preencher_descricoes(busca)
        # só agora a OLX "respondeu": página de anúncio bloqueada (403) sem isto
        # zerava o contador a cada rodada e o aviso de 1 h nunca saía
        await _olx_respondeu()

        if _editada(busca, db._MUDA_SCAN | db._MUDA_MERCADO):
            # a edição já reagendou um scan completo: concluir aqui o desfaria (e
            # mediria o mercado do termo antigo)
            logger.info(f"[{nome}] editada durante a rodada: novo scan já agendado")
            return
        # scan vazio NÃO vira monitor (erro silencioso / busca sem resultado ainda).
        # Conta o que a OLX devolveu, não o que passou nos filtros locais: busca
        # com tudo filtrado refazia o scan completo (25 págs) a cada intervalo
        db.concluir_scan(bid, busca["intervalo_min"], scan_completo=fase == "scan" and brutos > 0)
        _broadcast({"tipo": "busca_concluida", "busca_id": bid, "fase": fase,
                    "novos": novos, "total": len(produtos)})
        if db.mercado_vencido(bid):
            await _atualizar_mercado(busca)

    except Exception as e:
        logger.error(f"[{nome}] Erro no scraping: {e}")
        db.registrar_erro(bid, busca["intervalo_min"])
        if isinstance(e, OlxIndisponivel):
            await _olx_falhou(e)
        # bloqueio pausa tudo e tem o aviso de 1 h; qualquer outra falha (layout de
        # UMA categoria mudou, rede) avisa após 6 h — antes, "layout mudou" de uma
        # busca ficava mudo se as outras lessem normal (zeravam o contador global)
        cidade = busca.get("tipo") == "imovel" and avaliador._extras(busca).get("cidade")
        if isinstance(e, LocalDesconhecido) or (cidade and not busca.get("ultimo_scan")
                                                and not isinstance(e, OlxIndisponivel)):
            # nome de cidade errado: a OLX ora ignora o local, ora devolve erro 5xx.
            # Busca que nunca leu nada avisa já (as outras falhas esperam 6 h)
            await asyncio.to_thread(
                notifier.alertar, f"local:{bid}",
                f"⚠️ <b>{notifier.esc(nome)}</b>: a OLX não reconheceu a cidade "
                f"“{notifier.esc(cidade)}” — confira o nome e o estado "
                f"({notifier.esc(str(e)[:80])})", 24)
        elif not _bloqueio(e) and db.sem_sucesso_ha(bid, 6):
            await asyncio.to_thread(
                notifier.alertar, f"falha:{bid}",
                f"⚠️ <b>{notifier.esc(nome)}</b> está falhando há mais de 6 h: "
                f"{notifier.esc(type(e).__name__)}: {notifier.esc(str(e)[:150])}")
        _broadcast({"tipo": "busca_status", "busca_id": bid, "status": "erro"})
