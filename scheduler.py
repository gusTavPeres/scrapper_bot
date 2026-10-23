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
import html
import json
import logging
import re
import time
from datetime import datetime
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


# ── EXTRAÇÃO ──────────────────────────────────────────────

_RE_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S)
_RE_LDJSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)


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
    """Acha `chave` no texto e parseia o array JSON que a segue (bracket-match
    respeitando strings/escapes). Retorna list ou None."""
    # só se o VALOR da chave for uma lista ("ads": null seguido de outra lista
    # qualquer não pode virar a lista de anúncios)
    m = re.search(re.escape(chave) + r"\s*:\s*\[", texto)
    if not m:
        return None
    start = m.end() - 1
    depth, in_str, esc = 0, False, False
    for pos in range(start, len(texto)):
        ch = texto[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(texto[start:pos + 1])
                    except Exception:
                        return None
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
    for p in lista("properties")[:8]:
        if isinstance(p, dict) and p.get("label") and p.get("value") \
                and p.get("name") != "category":
            atributos.append(f"{p['label']}: {p['value']}")
    try:
        data = (datetime.fromtimestamp(int(ad["date"]), FUSO).isoformat(timespec="seconds")
                if ad.get("date") else "")
    except Exception:
        data = ""
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
        "categoria":      texto("category") or texto("categoryName"),
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
        return []
    produtos = []
    for ad in _achar_array_json(texto, '"ads"') or []:
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


async def _pagina_de_busca(sessao, url: str) -> list[dict] | None:
    """Payload RSC (3,4x menor) e, se não vier no formato esperado, o HTML."""
    r = await _baixar(sessao, url, headers={"RSC": "1"})
    if "x-component" in (r.headers.get("content-type") or ""):
        ads = _anuncios_do_flight(r.text)
        if ads is not None:
            return ads
    return extrair_anuncios((await _baixar(sessao, url)).text)


def _sessao():
    return AsyncSession(impersonate="chrome")


# ── URL E FILTROS ─────────────────────────────────────────

def _montar_url(busca: dict, pagina: int) -> str:
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
    if preco_max > 0: params += f"&pe={round(preco_max * (1 + avaliador.MARGEM_NEGOCIACAO))}"
    # semântica REAL da OLX (verificada): f=p -> particular, f=c -> loja/comercial
    if vendedor == "particular": params += "&f=p"
    elif vendedor == "loja":     params += "&f=c"

    # carros/imóveis NÃO aparecem na busca genérica por texto — só na categoria
    # (verificado: /go?q=punto = 0 anúncios; categoria carros = 50)
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
        base = f"https://www.olx.com.br/imoveis{uf}"
    else:
        # /go?q= devolve 0 resultados + 80 recomendações (verificado 2026-10-22)
        base = f"https://www.olx.com.br{uf or '/brasil'}"
    q = f"q={termo}&" if termo else ""     # carro sem modelo = categoria inteira
    return f"{base}?{q}o={pagina}{params}"


def filtrar_produtos(busca: dict, produtos: list[dict]) -> list[dict]:
    """Aplica faixa de preço, envio, vendedor e garantia da busca. A URL da OLX
    já filtra preço/vendedor (o pós-filtro é a rede de segurança); envio e
    garantia não existem na URL (o parâmetro olx_pay_and_ship é ignorado pela OLX — verificado)."""
    pmin = busca.get("preco_min") or 0
    pmax = busca.get("preco_max") or 0
    fe = busca.get("filtro_envio") or "qualquer"
    garantia = (busca.get("garantia") or "").lower()
    out = []
    for p in produtos:
        preco = p.get("preco") or 0
        if preco:                       # preço 0 = "a combinar" -> deixa o avaliador julgar
            if pmin and preco < pmin:
                continue
            if pmax and preco > pmax * (1 + avaliador.MARGEM_NEGOCIACAO):
                continue
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


# ── RASPAGEM ──────────────────────────────────────────────

async def raspar_busca(busca: dict, fase: str) -> list[dict]:
    """SCAN: todas as páginas (até 25). MONITOR: as mais novas por data, até a
    primeira página sem NENHUM anúncio novo p/ o banco (antes eram 2 fixas: mais
    de 100 anúncios novos entre dois scans = os excedentes só no full diário)."""
    todos, vistos = [], set()
    # ponytail: 25 págs (~1250 anúncios) cobre qualquer busca útil; suba se precisar
    max_pags = 25 if fase == "scan" else 10
    async with _sessao() as sessao:
        for pagina in range(1, max_pags + 1):
            url = _montar_url(busca, pagina)
            logger.info(f"  [{busca['nome']}] pág {pagina}: {url}")
            ads = None
            for _ in range(2):                    # resposta truncada ocasional: 1 retry
                ads = await _pagina_de_busca(sessao, url)
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
            # alcançou o que já tinha visto (descartado pelos filtros locais nunca é
            # gravado: se contasse como "novo", o monitor nunca pararia cedo)
            relevantes = filtrar_produtos(busca, novos)
            if fase == "monitor" and len(db.conhecidos(busca["id"], relevantes)) == len(relevantes):
                break
            _broadcast({"tipo": "scraping", "busca_id": busca["id"],
                        "pagina": pagina, "encontrados": len(todos)})
            await asyncio.sleep(1.5)
    return todos


async def _preencher_descricoes(busca: dict, limite: int = 60):
    """Baixa a descrição dos anúncios ativos que ainda não têm (os critérios
    costumam viver nela)."""
    pendentes = db.anuncios_sem_descricao(busca["id"], limite)
    if not pendentes:
        return
    ok = 0
    async with _sessao() as sessao:
        for a in pendentes:
            try:
                desc = extrair_descricao((await _baixar(sessao, a["url"])).text)
                # '(sem descrição)' marca visita feita — evita refetch eterno
                db.atualizar_descricao(a["id"], busca["id"], desc or "(sem descrição)")
                ok += 1
            except OlxIndisponivel:
                raise
            except Exception as e:                # tenta de novo no próximo ciclo
                logger.debug(f"descrição {a['url'][:60]}: {e}")
            await asyncio.sleep(1.0)              # pausa também após falha (sem rajada)
    if ok:
        logger.info(f"[{busca.get('nome')}] {ok} descrições preenchidas")


# ── LOOP PRINCIPAL ────────────────────────────────────────

ultimo_ciclo = time.time()   # heartbeat lido pelo watchdog do main.py


async def ciclo(timeout_busca: float = 900):
    """Uma volta: buscas vencidas, depois a fila de avaliação."""
    global ultimo_ciclo
    for busca in db.buscas_para_processar():
        ultimo_ciclo = time.time()           # heartbeat por busca, não por ciclo
        try:
            # 15 min por busca: uma travada não congela o resto p/ sempre
            await asyncio.wait_for(processar_busca(busca), timeout_busca)
        except asyncio.TimeoutError:
            # antes o TimeoutError saía do for: as buscas seguintes eram puladas
            # e esta ficava sem proximo_scan
            logger.error(f"[{busca['nome']}] estourou {timeout_busca:.0f} s — "
                         "próxima tentativa no intervalo")
            db.registrar_erro(busca["id"], busca["intervalo_min"])
    await avaliador.processar_fila(notifier)
    db.manutencao_diaria()


async def loop_scheduler():
    """Loop principal — executa indefinidamente."""
    global ultimo_ciclo
    logger.info("Scheduler iniciado.")
    while True:
        ultimo_ciclo = time.time()
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
                        or (teto and preco_antigo > teto >= p["preco"]
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

        # scan vazio NÃO vira monitor (erro silencioso / busca sem resultado ainda)
        db.concluir_scan(bid, busca["intervalo_min"], scan_completo=fase == "scan" and bool(produtos))
        _broadcast({"tipo": "busca_concluida", "busca_id": bid, "fase": fase,
                    "novos": novos, "total": len(produtos)})

    except Exception as e:
        logger.error(f"[{nome}] Erro no scraping: {e}")
        db.registrar_erro(bid, busca["intervalo_min"])
        if isinstance(e, OlxIndisponivel):
            await asyncio.to_thread(
                notifier.alertar, "olx",
                f"⚠️ <b>OLX indisponível</b> para o bot: {notifier.esc(str(e))}\n"
                "Bloqueio ou mudança de layout — as buscas estão paradas.")
        elif db.sem_sucesso_ha(bid, 6):       # rede/servidor falhando há horas: não ficar mudo
            await asyncio.to_thread(
                notifier.alertar, f"falha:{bid}",
                f"⚠️ <b>{notifier.esc(nome)}</b> está falhando há mais de 6 h: "
                f"{notifier.esc(type(e).__name__)}: {notifier.esc(str(e)[:150])}")
        _broadcast({"tipo": "busca_status", "busca_id": bid, "status": "erro"})
