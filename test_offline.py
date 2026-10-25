"""Regressão offline: um teste por avaria já encontrada (sem rede, banco temporário).
Uso:  ./venv/bin/python test_offline.py          (canário na OLX real: test_live_olx.py)"""
import asyncio
import json
import os
import time
import sqlite3
import tempfile
from pathlib import Path

os.environ.update(TELEGRAM_TOKEN="", TELEGRAM_CHAT_ID="", BOT_USER="u", BOT_PASS="p")

import database as db
import avaliador
import notifier
import scheduler
import telegram_bot as tb
from avaliador import produto_bate, avaliar, regras_do_texto, afirmado, norm

_tmp = tempfile.TemporaryDirectory()
# NUNCA o banco real: antes, rodar só um grupo de testes ("p6") usava data/bot.db
# e o fuzz criou 50 buscas-lixo em produção (o bot real as raspou e alertou)
_BANCO_REAL = db.DB_PATH.resolve()
db.DB_PATH = Path(_tmp.name) / "inicial.db"
db.init_db()

import logging
_erros_de_log = []
logging.Handler.handleError = lambda self, record: _erros_de_log.append(record.getMessage())


def novo_banco() -> Path:
    db.DB_PATH = Path(tempfile.mkdtemp(dir=_tmp.name)) / "t.db"
    db.init_db()
    return db.DB_PATH


def sql(q, *a):
    with db.get_conn() as c:
        return c.execute(q, a).fetchall()


def criar(**kw) -> int:
    from models import BuscaCreate
    return db.criar_busca(BuscaCreate(**{"nome": "b", "termo": "rtx 3090", **kw}).model_dump())


# ── OLX falsa ─────────────────────────────────────────────

def anuncio_olx(lid, titulo, preco, **kw):
    return {"subject": titulo, "url": f"https://go.olx.com.br/x/anuncio-{lid}", "listId": lid,
            "priceValue": f"R$ {preco:,}".replace(",", "."), "date": 1792716949,
            "location": "Goiânia", "professionalAd": False, "olxPay": {"enabled": False},
            "images": [{"original": f"https://img.olx.com.br/{lid}.jpg"}],
            "properties": [{"name": "category", "label": "Categoria", "value": "X"}], **kw}


def pagina_olx(ads=None, flight=True):
    if not flight:
        return "<html><body>layout novo</body></html>"
    corpo = {"pad": "x" * 1200} | ({"ads": ads} if ads is not None else {})
    return (f'<html><script>self.__next_f.push([0])</script>'
            f'<script>self.__next_f.push([1,{json.dumps(json.dumps(corpo))}])</script></html>')


def pagina_anuncio(desc):
    ld = json.dumps({"@type": "Product", "description": desc})
    return f'<html><script type="application/ld+json">{ld}</script></html>'


class OlxFalsa:
    """Substitui o AsyncSession: url -> (status, html)."""
    def __init__(self, rotas):
        self.rotas, self.pedidos = rotas, []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, timeout=None, headers=None):
        rsc = bool(headers and headers.get("RSC"))
        self.pedidos.append(("RSC " if rsc else "HTML ") + url)
        st, corpo = next((v for k, v in self.rotas.items() if k in url), (200, pagina_olx([])))
        tipo = "text/html"
        if rsc and st == 200:          # como a OLX: payload RSC = o flight puro
            flight = scheduler._texto_flight(scheduler._RE_SCRIPT.findall(corpo))
            if flight and not self.ignora_rsc:
                corpo, tipo = flight, "text/x-component"

        class R:
            status_code, text, headers = st, corpo, {"content-type": tipo}
            def raise_for_status(self):
                if st >= 400:
                    raise RuntimeError(f"HTTP {st}")
        return R()

    ignora_rsc = False

    def paginas(self):
        return {u.split(" ", 1)[1] for u in self.pedidos}


def rodar(coro):
    return asyncio.run(coro)


async def _sem_espera(*a, **k):
    pass


_sleep_real = asyncio.sleep
scheduler.asyncio.sleep = _sem_espera      # paginação sem os 1,5 s reais


# ═══ PARTE 1 — RASPAGEM ═══════════════════════════════════

def test_p1_flight_dos_scripts():
    """C1: o array self.__next_f em runtime vem vazio; os dados estão nos <script>."""
    html = pagina_olx([anuncio_olx(1546193183, "RTX 4090 MSI", 14500),
                       {"advertisingId": "x", "deviceType": "d"}])   # propaganda
    ads = scheduler.extrair_anuncios(html)
    assert len(ads) == 1 and ads[0]["list_id"] == 1546193183
    assert scheduler.extrair_anuncios(pagina_olx(flight=False)) is None     # layout mudou
    assert scheduler.extrair_anuncios(pagina_olx(None)) == []               # busca vazia
    assert scheduler._texto_flight(['self.__next_f.push([1,"!quebrado']) == ""


def test_p1_mapeamento_completo():
    """N4: data ISO real (não 'Hoje, 18:35'), imagem, vendedor, atributos, garantia."""
    p = scheduler._mapear_flight_ad(anuncio_olx(
        7, "RTX 4090", 11000, oldPrice="R$ 12.000", professionalAd=True,
        olxDelivery={"enabled": True}, olxPay={"enabled": True},
        properties=[{"name": "brand", "label": "Fabricante", "value": "Gigabyte"},
                    {"name": "category", "label": "Categoria", "value": "Placas"}]))
    assert p["preco"] == 11000 and p["vendedor"] == "loja" and p["envio"] == "com_envio"
    assert p["data_postagem"].startswith("2026-") and p["imagem"].endswith("7.jpg")
    assert "Preço anterior" in p["atributos"] and "Fabricante: Gigabyte" in p["atributos"]
    assert "Categoria" not in p["atributos"] and p["olx_pay"] is True
    assert scheduler._mapear_flight_ad(anuncio_olx(8, "PS4", 1500, category="Consoles de Vídeo Game"))[
        "categoria"] == "Consoles de Vídeo Game"


def test_p1_descricao_jsonld():
    d = scheduler.extrair_descricao(pagina_anuncio("Linha 1<br>Linha &amp; 2"))
    assert d == "Linha 1\nLinha & 2", d
    assert scheduler.extrair_descricao("<html></html>") == ""
    carro = json.dumps({"@type": "Person", "makesOffer": [{"itemOffered": {"description": "Linea 2012"}}]})
    assert scheduler.extrair_descricao(f'<script type="application/ld+json">{carro}</script>') == "Linea 2012"


def test_p1_url():
    u = scheduler._montar_url
    assert "&sf=1" in u({"termo": "x"}, 1)                       # N1: sf=3 era relevância
    assert "sf=3" not in u({"termo": "x", "ordenacao": "sf=3"}, 1)
    assert "olx.com.br/estado-go?" in u({"termo": "x", "estado": "go"}, 1)   # C4
    assert "olx.com.br/brasil?q=x" in u({"termo": "x"}, 1)
    # a OLX exige TODAS as palavras: descritivas zeram a busca (0 vs 76 resultados)
    assert "q=rtx%203090&" in u({"termo": "rtx 3090 com defeito"}, 1)
    assert "q=rtx%203090&" in u({"termo": "placa de vídeo rtx 3090 quebrada"}, 1)
    assert "q=geladeira&" in u({"termo": "geladeira usada"}, 1)
    assert "q=teclado%20mec%C3%A2nico&" in u({"termo": "teclado mecânico"}, 1)
    assert "q=usado&" in u({"termo": "usado"}, 1)                    # nada sobra: mantém
    assert "olx_pay_and_ship" not in u({"termo": "x", "garantia": "pay"}, 1)  # N7: ignorado
    assert "&f=p" in u({"termo": "x", "filtro_vendedor": "particular"}, 1)
    assert "&f=c" in u({"termo": "x", "filtro_vendedor": "loja"}, 1)
    assert "&ps=100&pe=230" in u({"termo": "x", "preco_min": 100, "preco_max": 200}, 1)   # +15% p/ negociar
    c = u({"termo": "punto", "tipo": "carro", "estado": "go"}, 1)
    assert "carros-vans-e-utilitarios/estado-go?q=punto" in c
    assert u({"termo": "", "tipo": "carro", "filtros_extra": {"ano_min": 2010, "ano_max": 2014}},
             1).endswith("?o=1&sf=1&rs=2010&re=2014")
    assert "/imoveis/estado-go?" in u({"termo": "casa", "tipo": "imovel", "estado": "go"}, 1)


def test_p1_filtros():
    f = scheduler.filtrar_produtos
    prods = [{"preco": 50, "envio": "com_envio"}, {"preco": 200, "envio": "com_envio"},
             {"preco": 200, "envio": "sem_envio"}, {"preco": 900, "envio": "com_envio"},
             {"preco": 0, "envio": "com_envio"}]
    assert [p["preco"] for p in f({"preco_min": 100, "preco_max": 500,
                                   "filtro_envio": "com_envio"}, prods)] == [200, 0]
    assert len(f({}, prods)) == 5
    pv = [{"preco": 1, "vendedor": "loja"}, {"preco": 2, "vendedor": "particular"}, {"preco": 3}]
    assert [p["preco"] for p in f({"filtro_vendedor": "particular"}, pv)] == [2, 3]
    pg = [{"preco": 1, "olx_pay": True, "envio": "sem_envio"},
          {"preco": 2, "olx_pay": True, "envio": "com_envio"},
          {"preco": 3, "olx_pay": False, "envio": "com_envio"}]
    assert [p["preco"] for p in f({"garantia": "pay"}, pg)] == [1, 2]            # N7
    assert [p["preco"] for p in f({"garantia": "pay_entrega"}, pg)] == [2]
    b = {"filtros_extra": {"ano_min": 2010, "ano_max": 2014, "km_max": 120000, "motor": "1.8"}}
    c = scheduler._passa_filtros_carro
    assert c(b, {"titulo": "Fiat Linea Essence 1.8 2012", "atributos": "Quilometragem: 98000"})
    assert not c(b, {"titulo": "Linea", "atributos": "Quilometragem: 150000"})   # sem 'km'
    assert not c(b, {"titulo": "Linea 1.8 2012", "atributos": "150.000 km"})
    assert not c(b, {"titulo": "Fiat Linea 1.8 2016"}) and not c(b, {"titulo": "Linea 1.4 2012"})
    assert c(b, {"titulo": "Fiat Linea"})                                       # sem info: passa


def test_p1_paginacao_e_falhas():
    pag1 = [anuncio_olx(i, f"RTX 3090 n{i}", 4000) for i in range(1, 51)]
    olx = OlxFalsa({"o=1&": (200, pagina_olx(pag1)),
                    "o=2&": (200, pagina_olx(pag1[:3] + [anuncio_olx(99, "RTX 3090 x", 4100)])),
                    "o=3&": (200, pagina_olx([]))})
    scheduler._sessao = olx
    r = rodar(scheduler.raspar_busca({"id": 1, "nome": "t", "termo": "rtx 3090"}, "scan"))
    assert len(r) == 51 and len(olx.paginas()) == 3        # dedup + para no fim
    assert all(p.startswith("RSC ") for p in olx.pedidos)    # RSC bastou: nenhum HTML baixado
    olx = OlxFalsa(olx.rotas)
    olx.ignora_rsc = True                                    # servidor ignorou o header RSC
    scheduler._sessao = olx
    r = rodar(scheduler.raspar_busca({"id": 1, "nome": "t", "termo": "rtx 3090"}, "scan"))
    assert len(r) == 51 and sum(p.startswith("HTML ") for p in olx.pedidos) == 3   # reserva HTML
    for rotas in ({"o=1&": (403, "Attention Required | Cloudflare")},
                  {"o=1&": (200, pagina_olx(flight=False))}):
        scheduler._sessao = OlxFalsa(rotas)
        try:
            rodar(scheduler.raspar_busca({"id": 1, "nome": "t", "termo": "x"}, "monitor"))
            raise AssertionError("deveria falhar alto")
        except scheduler.OlxIndisponivel:
            pass


def test_p1_monitor_ate_o_conhecido():
    """Monitor lê páginas até achar uma só com anúncios já conhecidos (eram 2 fixas)."""
    novo_banco()
    bid = criar()
    pags = {n: [anuncio_olx(n * 100 + i, f"RTX 3090 {n}-{i}", 4000) for i in range(50)] for n in range(1, 6)}
    for a in pags[4]:                                       # págs 4+ já estão no banco
        db.upsert_anuncio(bid, scheduler._mapear_flight_ad(a))
    olx = OlxFalsa({f"o={n}&": (200, pagina_olx(ads)) for n, ads in pags.items()})
    scheduler._sessao = olx
    r = rodar(scheduler.raspar_busca({"id": bid, "nome": "t", "termo": "rtx 3090"}, "monitor"))
    assert len(olx.paginas()) == 4 and len(r) == 200, (olx.paginas(), len(r))   # parou na 4
    olx = OlxFalsa({f"o={n}&": (200, pagina_olx(ads)) for n, ads in pags.items()})
    scheduler._sessao = olx
    rodar(scheduler.raspar_busca({"id": bid, "nome": "t", "termo": "rtx 3090"}, "scan"))
    assert len(olx.paginas()) == 6                          # scan lê tudo até acabar


def test_p1_upsert_enriquece():
    """N4: anúncio da era CSS (sem imagem/vendedor, data relativa) é completado."""
    novo_banco()
    bid = criar()
    sem = {"url": "https://x/a-1499805297", "titulo": "RTX 3090", "preco": 100,
           "data_postagem": "Hoje, 18:35"}
    aid, novo, _ = db.upsert_anuncio(bid, sem)
    assert novo
    cheio = {**sem, "list_id": 1499805297, "imagem": "https://i/1.jpg", "vendedor": "particular",
             "atributos": "Condição: Usado", "data_postagem": "2026-10-22T18:35:00"}
    aid2, novo2, antigo = db.upsert_anuncio(bid, cheio)
    a = sql("SELECT * FROM anuncios WHERE id=?", aid)[0]
    assert aid2 == aid and not novo2 and antigo is None
    assert a["imagem"] and a["vendedor"] == "particular" and a["data_postagem"].startswith("2026")
    _, _, antigo = db.upsert_anuncio(bid, {**cheio, "preco": 90})
    assert antigo == 100
    assert sql("SELECT COUNT(*) FROM historico_precos WHERE anuncio_id=?", aid)[0][0] == 2
    assert sql("SELECT prioridade FROM fila WHERE anuncio_id=?", aid)[0][0] == 2
    assert db.hash_anuncio("https://x/a-velho-1499805297") == db.hash_anuncio("q", 1499805297)


# ═══ PARTE 2 — CICLO DE VIDA ══════════════════════════════

def _fake_raspar(produtos=None, efeito=None):
    async def raspar(busca, fase):
        if efeito:
            await efeito(busca)
        return list(produtos or [])
    return raspar


def test_p2_scan_vira_monitor():
    novo_banco()
    bid = criar()
    b = db.obter_busca(bid)
    assert (b["status"], b["fase"]) == ("ativa", "scan") and db.buscas_para_processar()
    scheduler.raspar_busca = _fake_raspar([{"url": "https://x/a-1000001", "list_id": 1000001,
                                            "titulo": "RTX 3090 24GB", "preco": 4000}])
    scheduler._preencher_descricoes = _sem_espera
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    b = db.obter_busca(bid)
    assert (b["status"], b["fase"]) == ("ativa", "monitor") and b["ultimo_scan_full"]
    assert "T" not in b["proximo_scan"] and not db.buscas_para_processar()   # UTC SQL, +30 min
    # 30 min ±5% de variação (as buscas não disparam todas no mesmo segundo)
    assert sql("SELECT proximo_scan BETWEEN datetime('now','+28 minutes') AND datetime('now','+32 minutes') "
               "FROM buscas")[0][0] == 1
    assert not db.full_scan_vencido(bid)


def test_p2_interrupcao_nao_mata_busca():
    """C2: status recebia 'monitor' no início; interrupção = busca morta p/ sempre."""
    novo_banco()
    bid = criar()
    async def trava(busca):
        await _sleep_real(10)
    scheduler.raspar_busca = _fake_raspar(efeito=trava)
    outra = criar(nome="outra", termo="rtx 4090")
    feitas = []
    real = scheduler.processar_busca
    async def processar(busca):
        feitas.append(busca["id"])
        await real(busca)
    scheduler.processar_busca = processar
    try:
        rodar(scheduler.ciclo(timeout_busca=0.05))
    finally:
        scheduler.processar_busca = real
    assert feitas == [outra, bid] or feitas == [bid, outra]       # N13: a 2ª não é pulada
    for b in (db.obter_busca(bid), db.obter_busca(outra)):
        assert b["status"] == "erro" and b["proximo_scan"]         # agendada, não presa
    sql("UPDATE buscas SET proximo_scan=datetime('now','-1 minute')")
    assert len(db.buscas_para_processar()) == 2                    # erro volta a rodar


def test_p2_pausa_durante_scan_e_scan_em_pausada():
    novo_banco()
    bid = criar()
    async def pausa(busca):
        db.pausar_busca(busca["id"])
    scheduler.raspar_busca = _fake_raspar(efeito=pausa)
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert db.obter_busca(bid)["status"] == "pausada"              # A5
    db.forcar_scan(bid)
    assert db.obter_busca(bid)["status"] == "pausada"              # N11
    assert "pausada" in tb.tratar_comando(f"/scan {bid}")
    db.retomar_busca(bid)
    assert db.obter_busca(bid)["status"] == "ativa" and db.buscas_para_processar()


def test_p2_erro_agenda_retentativa():
    novo_banco()
    bid = criar()
    async def falha(busca):
        raise scheduler.OlxIndisponivel("403")
    scheduler.raspar_busca = _fake_raspar(efeito=falha)
    alertas = []
    notifier.alertar = lambda chave, texto, horas=12: alertas.append(chave)
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    b = db.obter_busca(bid)
    assert b["status"] == "erro" and not db.buscas_para_processar() and alertas == ["olx"]


def test_p2_migracao_v1():
    """Banco da era LLM/Xianyu: status 'monitor', JSON duplo, llm_fila, llm_cache."""
    db.DB_PATH = Path(tempfile.mkdtemp(dir=_tmp.name)) / "velho.db"
    c = sqlite3.connect(db.DB_PATH)
    c.executescript("""
    CREATE TABLE buscas (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, tipo TEXT DEFAULT 'produto',
      plataforma TEXT NOT NULL DEFAULT 'olx', termo TEXT, criterios_inclusao TEXT DEFAULT '[]',
      criterios_exclusao TEXT DEFAULT '[]', estado TEXT DEFAULT 'brasil', preco_min INT DEFAULT 0,
      preco_max INT DEFAULT 0, filtro_envio TEXT DEFAULT 'qualquer', ordenacao TEXT DEFAULT 'sf=3',
      garantia TEXT DEFAULT '', intervalo_min INT DEFAULT 30, status TEXT DEFAULT 'pendente',
      fase TEXT DEFAULT 'scan',
      criado_em TEXT, ultimo_scan TEXT, proximo_scan TEXT, filtro_vendedor TEXT DEFAULT 'qualquer',
      instrucoes TEXT DEFAULT '', filtros_extra TEXT DEFAULT '{}', ultimo_scan_full TEXT);
    CREATE TABLE anuncios (id INTEGER PRIMARY KEY, busca_id INT REFERENCES buscas(id) ON DELETE CASCADE,
      url TEXT, hash TEXT, titulo TEXT, preco REAL, local TEXT, data_postagem TEXT, envio TEXT,
      descricao TEXT, imagem TEXT DEFAULT '', vendedor TEXT DEFAULT '', atributos TEXT DEFAULT '',
      primeira_vez TEXT, ultima_vez TEXT, notificado INT DEFAULT 0, UNIQUE(busca_id, hash));
    CREATE TABLE analises (id INTEGER PRIMARY KEY, anuncio_id INT, decisao TEXT, resumo TEXT,
      pros TEXT, contras TEXT, observacoes TEXT, score INT, preco_vs_mercado_pct INT,
      avaliacao_preco TEXT, recomendacao TEXT, motivo TEXT, gerado_em TEXT, UNIQUE(anuncio_id));
    CREATE TABLE llm_fila (id INTEGER PRIMARY KEY, anuncio_id INT, busca_id INT,
      prioridade INT DEFAULT 0, tentativas INT DEFAULT 0, criado_em TEXT, UNIQUE(anuncio_id));
    CREATE TABLE llm_cache (hash TEXT PRIMARY KEY, resultado TEXT, gerado_em TEXT);
    INSERT INTO buscas (id, nome, termo, status, fase, criterios_inclusao, plataforma) VALUES
      (3, 'a', 'rtx 5090', 'monitorando', 'monitor', '"[\\"lacrada\\"]"', 'olx'),
      (4, 'x', 'rtx 5090', 'monitorando', 'scan', '"[]"', 'xianyu'),
      (7, 'b', 'rtx 3090', 'monitor', 'monitor', '[]', 'olx'),
      (8, 'c', 'punto', 'pausada', 'monitor', '[]', 'olx');
    INSERT INTO anuncios (id, busca_id, url, hash, titulo, preco, descricao) VALUES
      (1, 3, 'u1', 'h1', 'RTX 5090', 15000, '(sem descrição)'), (2, 7, 'u2', 'h2', 'RTX 3090', 4000, 'ok');
    INSERT INTO llm_fila (anuncio_id, busca_id, prioridade) VALUES (2, 7, 2);
    """)
    c.commit()
    c.close()
    db.init_db()
    db.init_db()                                                       # idempotente
    buscas = {b["id"]: b for b in db.listar_buscas()}
    assert set(buscas) == {3, 7, 8}                                    # xianyu removido
    assert buscas[7]["status"] == "ativa" and buscas[8]["status"] == "pausada"
    assert buscas[3]["criterios_inclusao"] == ["lacrada"]
    assert "plataforma" not in buscas[3] and "ordenacao" not in buscas[3]
    tabelas = {r[0] for r in sql("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fila" in tabelas and not {"llm_fila", "llm_cache"} & tabelas
    assert dict(sql("SELECT anuncio_id, prioridade FROM fila")) == {1: -1, 2: 2}
    assert sql("SELECT descricao FROM anuncios WHERE id=1")[0][0] == ""
    assert sql("PRAGMA user_version")[0][0] == db.VERSAO_SCHEMA
    # busca criada DEPOIS da migração, num banco cujo DEFAULT de status era 'pendente'
    sql("UPDATE buscas SET proximo_scan=datetime('now','+1 day')")
    nova = criar(termo="rtx 5070")
    assert db.obter_busca(nova)["status"] == "ativa"
    assert [b["id"] for b in db.buscas_para_processar()] == [nova]


def test_p2_manutencao_diaria():
    novo_banco()
    bid = criar()
    ids = {}
    for n, (nome, dias, rec) in enumerate((("lixo_velho", 40, "IGNORAR"), ("lixo_novo", 5, "IGNORAR"),
                                           ("bom_velho", 40, "VER"), ("antigo", 200, "VER"),
                                           ("curtido", 200, "IGNORAR"))):
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/{nome}-{9000000 + n}", "titulo": "RTX 3090",
                                            "preco": 1})
        db.salvar_analise(aid, {"decisao": "APROVADO", "resumo": "", "score": 5, "avaliacao_preco": "justo",
                                "recomendacao": rec, "motivo": ""})
        sql(f"UPDATE anuncios SET ultima_vez=datetime('now','-{dias} days') WHERE id=?", aid)
        ids[nome] = aid
    db.registrar_feedback(ids["curtido"], True)
    linhas = []
    h = logging.Handler(); h.emit = lambda r: linhas.append(r.getMessage())
    lg = logging.getLogger("database")
    lg.addHandler(h); lg.setLevel(logging.INFO)
    try:
        db.manutencao_diaria()
    finally:
        lg.removeHandler(h); lg.setLevel(logging.NOTSET)
    assert any("faxina diária: 2 anúncios velhos apagados" in x for x in linhas), linhas   # aparece no log
    restam = {n for n, i in ids.items() if sql("SELECT 1 FROM anuncios WHERE id=?", i)}
    assert restam == {"lixo_novo", "bom_velho", "curtido"}, restam
    bak = db.DB_PATH.with_suffix(".db.bak")
    assert bak.exists() and sqlite3.connect(bak).execute("SELECT COUNT(*) FROM buscas").fetchone()[0] == 1
    m = bak.stat().st_mtime
    db.manutencao_diaria()
    assert bak.stat().st_mtime == m                                   # 1x por dia


# ═══ PARTE 3 — AVALIADOR POR REGRAS ═══════════════════════

def test_p3_identidade():
    casos = [
        ("rtx 3090", "Rtx 5060 ti 16gb", False), ("rtx 3090", "RTX 5070 12gb Eagle", False),
        ("rtx 3090", "RTX 3090 Ti Founders", False), ("rtx 3090", "RTX 3090 Rog Strix 24GB", True),
        ("rtx 3090", "RTX3090TI", False), ("rtx 3090", "RTX 3090FE", True),
        ("rtx 5060 ti", "RTX 5060 Ti 16GB Msi", True), ("rtx 5060 ti", "RTX 5060 8GB", False),
        ("rtx 5090", "Trimble RTX Da2 GEO", False), ("rtx 3090", "Placa 13090", False),
        ("rx 6600", "RX 6600 XT", False), ("rx 6600", "RX 6600 8GB", True),
        ("i7 12700", "i7 12700K", False), ("iphone 13", "iPhone 13 Pro Max", False),
        ("iphone 13", "iPhone 13 128GB", True), ("iphone 13", "iPhone 14 Pro", False),
        # conjunto x peça
        ("rtx 4090", "Notebook Legion RTX 4090", False), ("rtx 4090", "PC Gamer RTX 4090 i9", False),
        ("rtx 4090", "PC TOP: RTX 4090, 32GB DDR5, 6TB-HD", False),
        ("rtx 4090", "RTX 4090 + PC gamer completo", False),
        ("rtx 4090", "RTX 4090 PCIe 4.0 24GB", True),
        ("rtx 3090", "Placa de vídeo RTX 3090 para PC", True),             # falso negativo antigo
        ("rtx 4090", "RTX 4090 tirada do meu computador, perfeita", True),
        ("notebook rtx 4090", "Notebook Legion RTX 4090", True),
        # acessório / compra
        ("rtx 3090", "Waterblock EK para RTX 3090", False), ("rtx 4090", "Fonte 1000W para RTX 4090", False),
        ("rtx 3090", "RTX 3090 com waterblock instalado", True), ("iphone 13", "Capinha iPhone 13", False),
        ("iphone 13", "iPhone 13 com capinha de brinde", True), ("rtx 3090", "RTX 3090 caixa vazia", False),
        ("rtx 3090", "Compro RTX 3090", False),
        ("rtx 5090", "Asus ROG Strix (RTX 4080/4090/5070TI/5080/5090) - Compra ou Troca", False),
        ("rtx 5090", "Notebook RTX 4090/5090", False), ("rtx 5090", "Busco RTX 5090", False),
        ("rtx 5090", "Kit 9950X3D2+6000CL26+X870E Apex+rtx 5090", False),
        ("rtx 3090", "RTX 3090 24GB/384bit", True), ("rtx 3090", "Vendo ou troco RTX 3090", True),
        # termo sem número
        ("linea", "Chevrolet Chevette L / SL", False), ("linea", "Fiat Linea Essence 1.8", True),
        ("punto", "Biz 125 2020", False), ("fiat linea", "Fiat Palio Attractive", False),
        ("fiat linea", "LINEA ESSENCE 1.8 DUALOGIC", True), ("teclado mecânico", "Teclado Redragon", True),
        ("", "Qualquer carro", True),
        ("rtx 3090 com defeito", "RTX 3090 Asus", True), ("placa de video rtx 3090", "RTX 3090 Asus", True),
        ("iphone 13", "Moto G 13", False),
        ("rtx 3090", "CPU GAMER Ryzen 9 32gb RTX 3090 para jogos", False),
        ("rtx 3090", "Ryzen 7 5700x RTX 3090", False), ("rtx 3090", "RTX 3090 testada em Ryzen 7", True),
        ("ryzen 5 5600", "Ryzen 5 5600 processador", True),
        ("rtx 3090", "Caixa da placa de vídeo RTX 3090 Gainward", False),
        ("rtx 3090", "RTX 3090 com caixa e nota", True),
        ("rtx 3090", "Chip gráfico GA102 RTX 3090", False),
        ("ps4", "Assistência técnica de ps3 e ps4", False), ("ps4", "Conserto de PS4", False),
        ("ps4", "PS4 para conserto", True),
        ("rtx 5060", "Caixas Vazias Placa de Vídeo RTX Série 50 (5060, 5070, 5080)", False),
        ("rtx 5060", "Rx7600 Placa de Vídeo - troco por rtx 5060 + volta", False),
        ("rtx 5060", "RTX 5060 8GB - aceito troca", True), ("rtx 5060", "Troco em RTX 5060 meu PS5", False),
        ("rtx 5070", "EVGA RTX 3080 FTW3 ULTRA (melhor CxB entre RTX 5050 5060 5060Ti 4060 4070 e 5070)", False),
        ("rtx 5070", "RTX 3080 melhor que a 5070", False), ("rtx 5070", "RTX 5070 melhor que a 4070", True),
        ("rtx 5070", "RTX 5070 12GB 2475MHz 6144 cuda", True),
        ("rtx 3090", "GPU NVIDIA Original GA102-300-A1 RTX 3090 | Chip Gráfico Testado", False), ("rtx 5060", "Fontes para RTX 5060", False),
    ]
    erros = [(t, ti, ok) for t, ti, ok in casos if produto_bate(t, ti)[0] != ok]
    assert not erros, erros


def test_p3_negacao_e_instrucoes():
    t = norm("Placa sem defeito. Nunca foi usada em mineração. Tem defeito no cooler")
    assert afirmado(t, "defeit") is True                    # 2ª frase afirma
    assert afirmado(norm("sem defeito nenhum"), "defeit") is False
    assert afirmado(norm("sem riscos. Defeito no HDMI"), "defeit") is True   # frase nova
    assert afirmado(norm("sem riscos, mas com defeito"), "defeit") is True
    assert afirmado(t, "mineraca") is False and afirmado(t, "garanti") is None
    r = regras_do_texto("só aceito com nota fiscal, sem carro batido nem de leilão, prefiro manual")
    assert [(m, rad) for m, rad, _ in r] == [
        ("exigir", ["nota", "fiscal"]), ("excluir", ["batid"]), ("excluir", ["leila"]),
        ("preferir", ["manual"])], r
    assert regras_do_texto("") == [] and regras_do_texto("nenhuma") != []


def _item(**kw):
    return {"anuncio_id": 999, "termo": "rtx 3090", "tipo": "produto", "titulo": "RTX 3090 Asus",
            "preco": 4000, "descricao": "Placa em ótimo estado, funcionando perfeitamente, "
            "testada em jogos, acompanha caixa original e nota fiscal da Kabum.",
            "atributos": "", "vendedor": "particular", "instrucoes": "",
            "criterios_inclusao": [], "criterios_exclusao": [], **kw}


_COMPS = [{"id": i, "titulo": "RTX 3090", "atributos": "", "preco": p}
          for i, p in enumerate([4500, 4800, 5000, 5200, 5500])]


def test_p3_avaliacao():
    bom = avaliar(_item(), _COMPS)
    assert bom["recomendacao"] == "COMPRAR" and bom["avaliacao_preco"] == "bom", bom
    assert bom["preco_vs_mercado_pct"] == -20 and "nota fiscal" in bom["pros"]
    # sem descrição nunca vira COMPRAR (N3: 1/3 dos COMPRAR eram desmentidos depois)
    assert avaliar(_item(descricao=""), _COMPS)["recomendacao"] == "VER"
    assert avaliar(_item(descricao="(sem descrição)", preco=3000), _COMPS)["score"] <= 7
    # defeito afirmado derruba; negado não; procurado pelo usuário não
    d = _item(descricao="Placa com defeito, não dá vídeo, vendo no estado. Nota fiscal.")
    assert avaliar(d, _COMPS)["recomendacao"] == "IGNORAR"
    assert avaliar(_item(descricao=_item()["descricao"] + " Sem defeito nenhum."),
                   _COMPS)["recomendacao"] == "COMPRAR"
    assert avaliar({**d, "termo": "rtx 3090 com defeito"}, _COMPS)["recomendacao"] != "IGNORAR"
    # critérios/instruções do usuário
    assert avaliar(_item(instrucoes="nada de mineração",
                         descricao="Usada em mineração por 6 meses, com nota."), _COMPS)["decisao"] == "EXCLUIDO"
    assert avaliar(_item(instrucoes="nada de mineração",
                         descricao="Nunca foi usada em mineração, nota fiscal, caixa."),
                   _COMPS)["decisao"] == "APROVADO"
    assert avaliar(_item(criterios_exclusao=["lacrada"], descricao="Lacrada na caixa"),
                   _COMPS)["decisao"] == "EXCLUIDO"
    vencida = avaliar(_item(descricao=_item()["descricao"].replace("nota fiscal", "fora da garantia")), _COMPS)
    assert "garantia" not in vencida["pros"], vencida
    falta = avaliar(_item(instrucoes="só com garantia"), _COMPS)
    assert any("garantia" in o for o in falta["observacoes"]) and falta["score"] < bom["score"]
    # preço
    assert avaliar(_item(preco=1000), _COMPS)["avaliacao_preco"] == "suspeito"
    assert avaliar(_item(preco=1000), _COMPS)["recomendacao"] != "COMPRAR"
    assert avaliar(_item(preco=7000), _COMPS)["avaliacao_preco"] == "caro"
    assert avaliar(_item(preco=2700), _COMPS)["avaliacao_preco"] == "suspeito"     # -46%
    assert avaliar(_item(), _COMPS[:4])["avaliacao_preco"] == "sem_base"     # < 5 comparáveis
    assert avaliar(_item(), [{**c, "id": 999} for c in _COMPS])["avaliacao_preco"] == "sem_base"
    # golpe
    g = avaliar(_item(descricao=_item()["descricao"] + " Só aceito pix antecipado."), _COMPS)
    assert g["recomendacao"] != "COMPRAR" and any("golpe" in c for c in g["contras"])
    honesto = avaliar(_item(descricao=_item()["descricao"] + " Só pix ou dinheiro, retirada em mãos."), _COMPS)
    assert honesto["recomendacao"] == "COMPRAR", honesto
    # identidade
    assert avaliar(_item(titulo="RTX 3090 Ti"), _COMPS)["decisao"] == "EXCLUIDO"


def test_p3_carro_mesmo_ano():
    comps = ([{"id": i, "titulo": f"Linea 1.8 {2012}", "atributos": "", "preco": 30000} for i in range(5)]
             + [{"id": 10 + i, "titulo": "Linea 2016", "atributos": "", "preco": 60000} for i in range(9)])
    it = _item(termo="linea", tipo="carro", titulo="Fiat Linea 1.8", atributos="Ano: 2012",
               preco=24000, descricao="Carro revisado, único dono, documento ok, sem batidas, "
               "nunca foi de leilão, manual e chave reserva.")
    av = avaliar(it, comps)
    assert av["preco_vs_mercado_pct"] == -20, av            # vs 2012, não vs a mistura
    assert av["decisao"] == "APROVADO" and not any("leil" in c for c in av["contras"])
    assert avaliar({**it, "atributos": ""}, comps)["avaliacao_preco"] == "sem_base"
    # leilão = ressalva moderada; com preço bom vira VALE NEGOCIAR (nunca COMPRAR)
    batido = avaliar({**it, "descricao": "Carro de leilão, pequena batida na frente."}, comps)
    assert batido["recomendacao"] == "VER" and "⚠️ leilão" in batido["contras"], batido
    monta = avaliar({**it, "descricao": "Monta: Média monta. Aberto a propostas, revisado, único dono."}, comps)
    assert monta["recomendacao"] != "COMPRAR" and any("monta" in c for c in monta["contras"]), monta
    # detalhe ESTÉTICO + preço bom: passa (vira argumento de negociação, não bloqueio)
    leve = avaliar({**it, "descricao": it["descricao"] + " Apenas detalhes leves de queimado no teto."}, comps)
    assert leve["recomendacao"] == "COMPRAR" and any("queimado" in o for o in leve["observacoes"]), leve


def test_p3_procurando_defeito():
    """Quem QUER peça com defeito: defeito conta a favor, peça funcionando cai,
    e o preço compara só com outras quebradas (não vira 'golpe')."""
    from models import BuscaCreate
    assert BuscaCreate(nome="n", termo="rtx 3090 com defeito").condicao == "defeito"   # deduz
    assert BuscaCreate(nome="n", termo="rtx 3090", criterios_inclusao=["para peças"]).condicao == "defeito"
    assert BuscaCreate(nome="n", termo="rtx 3090 com defeito", condicao="qualquer").condicao == "qualquer"
    assert BuscaCreate(nome="n", termo="rtx 3090").condicao == "funcionando"
    quebradas = [{"id": i, "titulo": "RTX 3090 com defeito", "atributos": "",
                  "descricao": "não dá vídeo", "preco": p} for i, p in enumerate([1000, 1100, 1200, 1300, 1400])]
    boas = [{"id": 10 + i, "titulo": "RTX 3090", "atributos": "", "descricao": "perfeita",
             "preco": 4500} for i in range(10)]
    base = _item(termo="rtx 3090", condicao="defeito", preco=900,
                 descricao="Placa liga mas não dá vídeo, vendo no estado para retirada de peças ou reparo. "
                           "Sem garantia, retirar em mãos.")
    q = avaliar(base, quebradas + boas)
    assert q["decisao"] == "APROVADO" and q["recomendacao"] in ("VER", "COMPRAR"), q
    assert q["avaliacao_preco"] == "bom" and q["preco_vs_mercado_pct"] == -25, q   # vs mediana 1200
    assert any("confirma o defeito" in p for p in q["pros"])
    f = avaliar({**base, "preco": 4300, "descricao": _item()["descricao"]}, quebradas + boas)
    assert f["recomendacao"] == "IGNORAR" and any("não menciona defeito" in c for c in f["contras"]), f
    # mesmo anúncio quebrado numa busca normal: IGNORAR
    assert avaliar({**base, "condicao": "funcionando"}, quebradas + boas)["recomendacao"] == "IGNORAR"
    # 'qualquer': informa sem punir
    qq = avaliar({**base, "condicao": "qualquer", "preco": 4500}, quebradas + boas)
    assert qq["recomendacao"] == "VER" and any("Menciona" in o for o in qq["observacoes"]), qq
    # pelo Telegram, de ponta a ponta
    novo_banco()
    r = tb.tratar_comando("/add rtx 3090 com defeito max=2000")
    assert "🔧 defeito" in r, r
    assert "🔧 qualquer" in tb.tratar_comando("/add rtx 3090 condicao=qualquer")
    assert "❌" in tb.tratar_comando("/add rtx 3090 condicao=quebrada")


def test_p3_categoria_e_dispersao():
    """'ps4' traz consoles (~R$1.500) e jogos (~R$120): preço só dentro da
    categoria da OLX; mistura dispersa demais (geladeira) = sem base."""
    consoles = [{"id": i, "titulo": "PS4 Slim", "atributos": "", "categoria": "Consoles", "preco": p}
                for i, p in enumerate([1400, 1450, 1500, 1550, 1600])]
    jogos = [{"id": 10 + i, "titulo": f"Jogo {i} PS4", "atributos": "", "categoria": "Jogos",
              "preco": p} for i, p in enumerate([50, 70, 90, 110, 130, 150, 200, 250])]
    console = _item(termo="ps4", titulo="PS4 Slim 1TB", categoria="Consoles", preco=1200)
    av = avaliar(console, consoles + jogos)
    assert av["preco_vs_mercado_pct"] == -20 and av["avaliacao_preco"] == "bom", av   # vs consoles
    jogo = avaliar(_item(termo="ps4", titulo="FIFA PS4", categoria="Jogos", preco=60), consoles + jogos)
    assert jogo["avaliacao_preco"] == "sem_base" and "variados" in jogo["resumo"], jogo  # jogos: 1,6
    misturado = avaliar({**console, "categoria": ""}, consoles + jogos)
    assert misturado["avaliacao_preco"] == "sem_base", misturado
    # sinais ótimos mas sem base de preço: VER, nunca COMPRAR
    assert misturado["score"] >= 8 and misturado["recomendacao"] == "VER", misturado
    # filtro opcional de categorias da busca
    assert avaliador.triagem({"termo": "ps4", "categorias": ["Consoles"]},
                             {"titulo": "FIFA PS4", "categoria": "Jogos"})[0] is False
    assert avaliador.triagem({"termo": "ps4", "categorias": ["Consoles"]},
                             {"titulo": "PS4 Slim", "categoria": "Consoles"})[0] is True
    assert avaliador.triagem({"termo": "ps4", "categorias": []}, {"titulo": "FIFA PS4", "categoria": "Jogos"})[0]
    assert avaliar({**console, "categorias": ["Jogos"]}, consoles)["decisao"] == "EXCLUIDO"


def test_p3_teto_do_usuario():
    """Busca com teto (5060 por menos de 1900): o teto é a referência — a mediana
    do que está abaixo do teto é enviesada e quase sempre sem base."""
    it = _item(termo="rtx 5060", titulo="RTX 5060 Galax 8GB", preco_max=1900, preco=1750,
               descricao="Placa funcionando, usada em jogos por alguns meses, retirada em mãos no centro.")
    av = avaliar(it, [])
    assert av["recomendacao"] == "COMPRAR" and av["avaliacao_preco"] == "bom", av
    assert "seu teto" in av["resumo"]
    assert avaliar({**it, "preco": 1400}, [])["score"] > av["score"]          # bem abaixo
    golpe = avaliar({**it, "preco": 700}, [])
    assert golpe["avaliacao_preco"] == "suspeito" and golpe["recomendacao"] != "COMPRAR", golpe
    assert avaliar({**it, "preco": 2100}, [])["avaliacao_preco"] == "caro"     # subiu de preço
    assert avaliar({**it, "descricao": ""}, [])["recomendacao"] == "VER"       # sem descrição


def test_p3_classificador():
    """Aprende com 👍/👎: puxa p/ cima o parecido com o curtido, p/ baixo o rejeitado."""
    from avaliador import Classificador
    def rot(titulo, desc, gostou, vendedor="particular"):
        return {"termo": "rtx 3090", "titulo": titulo, "descricao": desc, "atributos": "",
                "vendedor": vendedor, "categoria": "Placas", "preco": 4000, "gostou": gostou}
    rotulos = ([rot(f"RTX 3090 MSI Suprim {i}", "nota fiscal, garantia, nunca minerou", 1) for i in range(4)]
               + [rot(f"RTX 3090 Zotac loja {i}", "loja, parcelo no cartão, envio imediato", 0, "loja")
                  for i in range(4)])
    assert not Classificador(rotulos[:5]).pronto                       # 4👍 1👎: ainda aprende
    m = Classificador(rotulos)
    assert m.pronto
    msi = _item(titulo="RTX 3090 MSI Suprim X", descricao="Com nota fiscal e garantia, nunca minerou, "
                "funcionando perfeitamente em jogos e renderização.")
    loja = _item(titulo="RTX 3090 Zotac", vendedor="loja", descricao="Loja física, parcelo no cartão, "
                 "envio imediato para todo o Brasil com seguro.")
    d_msi, porque = m.ajuste(msi)
    d_loja, porque_loja = m.ajuste(loja)
    assert d_msi > 0 and "curtiu" in porque, (d_msi, porque)
    assert d_loja < 0 and "rejeitou" in porque_loja, (d_loja, porque_loja)
    assert avaliar(loja, _COMPS, m)["score"] < avaliar(loja, _COMPS)["score"]
    assert any("curtiu" in p for p in avaliar(msi, _COMPS, m)["pros"])
    # o 👎 num anúncio específico é a palavra final; 👍 aparece nos prós
    assert avaliar({**msi, "feedback": 0}, _COMPS, m)["recomendacao"] == "IGNORAR"
    assert "você marcou 👍" in avaliar({**msi, "feedback": 1}, _COMPS, m)["pros"]
    # 👍 vence a regra de identidade (falso negativo das regras)
    salvo = avaliar({**msi, "titulo": "Kit RTX 3090 + suporte vertical", "feedback": 1}, _COMPS)
    assert salvo["decisao"] == "APROVADO" and any("mantido pelo seu 👍" in o for o in salvo["observacoes"])


def test_p3_feedback_persistente():
    """Rótulo sobrevive à remoção da busca e reenfileira os ativos (em silêncio)."""
    novo_banco()
    bid = criar()
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-7000001", "titulo": "RTX 3090 MSI",
                                        "preco": 4000, "descricao": "nota"})
    sql("DELETE FROM fila")
    assert db.registrar_feedback(aid, True) and not db.registrar_feedback(999, True)
    assert dict(sql("SELECT anuncio_id, prioridade FROM fila")) == {aid: -1}
    db.registrar_feedback(aid, False)
    assert [r["gostou"] for r in db.feedbacks()] == [0]                 # sobrescreve
    assert db.obter_fila_pendente()[0]["feedback"] == 0
    db.deletar_busca(bid)
    f = db.feedbacks()
    assert len(f) == 1 and f[0]["anuncio_id"] is None and f[0]["titulo"] == "RTX 3090 MSI"
    db.registrar_feedback(aid, None)                                    # anúncio sumiu: no-op
    assert len(db.feedbacks()) == 1


# (descrição, preço, rec esperada, alerta esperado) — busca "rtx 5060" com teto R$1.900
CASOS_RESSALVA = [
    # limpo dentro do teto: COMPRAR
    ("Placa funcionando perfeitamente, usada só em jogos, nunca minerou. Acompanha caixa.", 1750, "COMPRAR", "comprar"),
    # detalhe ESTÉTICO não impede: COMPRAR (vira argumento de negociação)
    ("Funcionando 100%, tem um pequeno risco no backplate, nada que afete. Com caixa.", 1750, "COMPRAR", "comprar"),
    ("Placa top, apenas marcas de uso na carcaça. Testada em jogos pesados.", 1650, "COMPRAR", "comprar"),
    # MODERADO + preço bom: VALE NEGOCIAR (era bloqueado)
    ("Funciona normal mas a ventoinha faz barulho em carga alta, por isso o preço.", 1400, "VER", "negociar"),
    ("Placa já foi consertada (troca de capacitor), funcionando bem há 6 meses.", 1500, "VER", "negociar"),
    ("Vendo no estado, funcionando, sem garantia. Retirada em mãos.", 1700, "VER", "negociar"),
    # GRAVE: sem alerta; só aparece se o preço for de peça
    ("Placa não dá vídeo, liga as ventoinhas. Vendo para reparo.", 1500, "IGNORAR", ""),
    ("Placa não dá vídeo, liga as ventoinhas. Vendo para reparo.", 900, "VER", ""),
    # atenuante: "pequeno defeito" no cooler = moderado, não grave
    ("Tem um pequeno defeito no cooler que faz barulho, de resto perfeita.", 1450, "VER", "negociar"),
    # negação continua valendo
    ("Sem defeitos, sem riscos, nunca foi aberta. Nota fiscal e caixa.", 1800, "COMPRAR", "comprar"),
    # golpe nunca alerta, mesmo barato
    ("Placa nova. Pagamento antecipado via pix, envio pelos correios.", 1500, "IGNORAR", ""),
    # negociação acima do teto (coletado pela margem de 15%)
    ("Placa perfeita, com caixa. Aceito proposta.", 2050, "VER", "negociar"),
    ("Placa perfeita, com caixa. Valor fixo.", 2050, "IGNORAR", ""),
    ("Placa perfeita, com caixa. Não aceito propostas.", 2050, "IGNORAR", ""),
    # pouco acima do teto e o vendedor não disse nada: aparece (VER), sem alerta
    ("Placa perfeita, com caixa, retirada em mãos.", 2050, "VER", ""),
    # 13% acima sem dizer nada: fora (de 8% a 15% só com "aceito proposta")
    ("Placa perfeita, com caixa, retirada em mãos.", 2150, "IGNORAR", ""),
    ("Placa perfeita, com caixa. Aceito proposta.", 2150, "VER", "negociar"),
    # preço suspeito (< 45% do teto) e limpo: nunca COMPRAR
    ("Placa nova lacrada, envio imediato.", 700, "VER", ""),
]


def test_p3_ressalvas_e_negociacao():
    """Detalhe com preço bom NÃO pode sumir: estético passa, moderado vira
    'vale negociar', grave só aparece a preço de peça, golpe nunca alerta."""
    erros = []
    for desc, preco, rec, alerta in CASOS_RESSALVA:
        av = avaliar(_item(termo="rtx 5060", titulo="RTX 5060 Galax 8GB", preco_max=1900, preco=preco,
                           descricao=desc), [])
        if (av["recomendacao"], av["alerta"]) != (rec, alerta):
            erros.append((desc[:55], preco, (av["recomendacao"], av["alerta"]), (rec, alerta), av["motivo"]))
    assert not erros, "\n".join(map(str, erros))
    # ressalva aparece explicada no alerta
    av = avaliar(_item(termo="rtx 5060", titulo="RTX 5060", preco_max=1900, preco=1400,
                       descricao=CASOS_RESSALVA[3][0]), [])
    assert "⚠️ barulho" in av["contras"]
    neg = avaliar(_item(termo="rtx 5060", titulo="RTX 5060", preco_max=1900, preco=2050,
                        descricao="Aceito proposta. Com caixa."), [])
    assert neg["avaliacao_preco"] == "negociavel" and any("ofereça até R$ 1.900" in o for o in neg["observacoes"])


class NotifierFalso:
    def __init__(self, aceita=True):
        self.aceita, self.enviados, self.resumos = aceita, [], []

    async def enviar_resultado(self, item, av):
        await _sleep_real(0.01)                 # abre janela p/ corrida
        self.enviados.append(item["anuncio_id"])
        return self.aceita

    async def enviar_resumo(self, bid, r):
        self.resumos.append(r)
        return True


def _popular(n_lixo=20):
    bid = criar()
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days') WHERE id=?", bid)   # não é recente
    ids = []
    for i, (tit, p) in enumerate([("RTX 3090 Asus", 4000)] + [(f"RTX 3090 n{i}", 5000) for i in range(5)]
                                 + [("Cabo para RTX 3090", 50)] * n_lixo):
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/a-{1000000 + i}", "titulo": tit,
                                            "preco": p, "descricao": _item()["descricao"] if i == 0
                                            else "Funcionando bem."})
        ids.append(aid)
    return bid, ids


def test_p3_fila_notifica_certo():
    novo_banco()
    bid, ids = _popular()
    for aid in ids[:6]:
        db.adicionar_fila(aid, bid, 2)
    for aid in ids[6:]:
        db.adicionar_fila(aid, bid, -1)                       # silenciosa
    n = NotifierFalso(aceita=False)
    rodar(avaliador.processar_fila(n))
    assert db.tamanho_fila() == 0
    assert n.enviados == [ids[0]]                              # só o COMPRAR (os outros 5 são VER sem alerta)
    assert sql("SELECT notificado FROM anuncios WHERE id=?", ids[0])[0][0] == 0   # A2: falhou
    av = sql("SELECT preco_vs_mercado_pct, recomendacao FROM analises WHERE anuncio_id=?", ids[0])[0]
    assert av["preco_vs_mercado_pct"] == -20 and av["recomendacao"] == "COMPRAR"   # N2
    assert sql("SELECT COUNT(*) FROM analises WHERE recomendacao='IGNORAR'")[0][0] == 20
    assert n.resumos and n.resumos[0]["n"] == 6                # resumo ignora as silenciosas
    db.adicionar_fila(ids[0], bid, 1)
    n2 = NotifierFalso(aceita=True)
    rodar(avaliador.processar_fila(n2))
    assert n2.enviados == [ids[0]] and sql("SELECT notificado FROM anuncios WHERE id=?", ids[0])[0][0] == 1
    db.adicionar_fila(ids[0], bid, 1)
    rodar(avaliador.processar_fila(n2))
    assert n2.enviados == [ids[0]]                              # não re-notifica


def test_p3_alerta_negociar_e_teto():
    """Ponta a ponta: acima do teto + 'aceito proposta' -> 🟡 notificado; o preço
    cai p/ dentro do teto -> alerta 'entrou no seu teto'."""
    novo_banco()
    bid = criar(termo="rtx 5060", preco_max=1900)
    p = {"url": "https://x/a-9100001", "list_id": 9100001, "titulo": "RTX 5060 Galax", "preco": 2050}
    aid, _, _ = db.upsert_anuncio(bid, p)
    db.atualizar_descricao(aid, bid, "Placa perfeita com caixa, aceito proposta.")
    n = NotifierFalso()
    alertas = []
    async def enviar_resultado(item, av):
        alertas.append(av["alerta"])
        return True
    n.enviar_resultado = enviar_resultado
    rodar(avaliador.processar_fila(n))
    assert alertas == ["negociar"] and sql("SELECT notificado FROM anuncios WHERE id=?", aid)[0][0] == 1
    assert sql("SELECT alerta FROM analises WHERE anuncio_id=?", aid)[0][0] == "negociar"
    quedas = []
    async def queda(busca, anuncio, antigo, anuncio_id=None):
        quedas.append((antigo, anuncio["preco"]))
        return True
    notifier.enviar_queda = queda
    scheduler._preencher_descricoes = _sem_espera
    sql("UPDATE buscas SET fase='monitor', ultimo_scan_full=datetime('now')")
    scheduler.raspar_busca = _fake_raspar([{**p, "preco": 1880}])        # caiu só 8%, mas cruzou o teto
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert quedas == [(2050, 1880)], quedas


def test_p3_fila_sem_corrida():
    """N10: dois disparos simultâneos da fila não notificam em dobro."""
    novo_banco()
    bid, ids = _popular(n_lixo=0)
    db.adicionar_fila(ids[0], bid, 2)
    n = NotifierFalso()
    async def dois():
        await asyncio.gather(avaliador.processar_fila(n), avaliador.processar_fila(n))
    rodar(dois())
    assert n.enviados == [ids[0]]


def test_p3_triagem_na_entrada():
    """A6: produto errado é descartado ANTES de baixar descrição."""
    novo_banco()
    bid = criar()
    scheduler.raspar_busca = _fake_raspar([
        {"url": "https://x/a-2000001", "list_id": 2000001, "titulo": "RTX 3090 Asus", "preco": 4000},
        {"url": "https://x/a-2000002", "list_id": 2000002, "titulo": "Notebook RTX 3080", "preco": 5000}])
    visitas = []
    async def descricoes(busca, limite=30):
        visitas.extend(a["id"] for a in db.anuncios_sem_descricao(busca["id"]))
    scheduler._preencher_descricoes = descricoes
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert len(visitas) == 1 and db.tamanho_fila() == 1
    assert sql("SELECT recomendacao FROM analises")[0][0] == "IGNORAR"


def test_p3_queda_de_preco():
    novo_banco()
    bid = criar()
    sql("UPDATE buscas SET fase='monitor', ultimo_scan_full=datetime('now')")
    p = {"url": "https://x/a-3000001", "list_id": 3000001, "titulo": "RTX 3090", "preco": 30000}
    aid, _, _ = db.upsert_anuncio(bid, p)
    quedas = []
    async def queda(busca, anuncio, antigo, anuncio_id=None):
        assert anuncio_id == aid                                   # botões 👍/👎 do alerta
        quedas.append(anuncio["preco"])
        return True
    notifier.enviar_queda = queda
    scheduler._preencher_descricoes = _sem_espera
    for novo_preco, analise in ((29950, "VER"), (25000, None), (25000, "VER"), (20000, "IGNORAR")):
        sql("DELETE FROM analises")
        if analise:
            db.salvar_analise(aid, {"decisao": "APROVADO", "resumo": "", "score": 6,
                                    "avaliacao_preco": "justo", "recomendacao": analise, "motivo": ""})
        sql("UPDATE anuncios SET preco=30000")
        scheduler.raspar_busca = _fake_raspar([{**p, "preco": novo_preco}])
        rodar(scheduler.processar_busca(db.obter_busca(bid)))
    # R$50 em 30 mil não alerta; sem análise não alerta; aprovado alerta; IGNORAR não
    assert quedas == [25000], quedas
    # avisado da queda = notificado: a reavaliação não manda um 2º alerta
    assert sql("SELECT notificado FROM anuncios WHERE id=?", aid)[0][0] == 1


# ═══ PARTE 4 — NOTIFICAÇÕES / TELEGRAM ════════════════════

def test_p4_escape_e_token():
    enviados = []
    original = notifier._post
    notifier._post = lambda metodo, dados, timeout: enviados.append((metodo, dados)) or True
    try:
        av = avaliar(_item(titulo="RTX 3090 <3 & cia"), _COMPS)
        ok = rodar(notifier.enviar_resultado(
            {**_item(titulo="RTX 3090 <3 & cia"), "nome": "<b>x", "url": "https://x/?a=1&b=2",
             "imagem": ""}, av))
        texto = enviados[-1][1]["text"]
        assert ok and "&lt;3 &amp; cia" in texto and "&lt;b&gt;x" in texto and "a=1&amp;b=2" in texto
        assert enviados[-1][1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == "fb:999:0"
        rodar(notifier.enviar_queda({"nome": "a&b"}, {"titulo": "<x>", "preco": 90, "imagem": ""}, 100))
        assert "&lt;x&gt;" in enviados[-1][1]["text"]
    finally:
        notifier._post = original
    assert not notifier._enviar_foto("https://i/1.jpg", "x" * 1025)        # legenda longa
    notifier.TOKEN = "123:ABC"
    try:
        assert notifier.sem_token("url: /bot123:ABC/sendMessage") == "url: /bot<TOKEN>/sendMessage"
    finally:
        notifier.TOKEN = ""


def test_p4_alerta_debounce():
    import importlib
    n = importlib.reload(notifier)
    chamadas = []
    n._enviar = lambda t: chamadas.append(t) or True
    assert n.alertar("k", "a") and not n.alertar("k", "b") and n.alertar("outra", "c")
    assert len(chamadas) == 2
    n._enviar = lambda t: chamadas.append(t) and False                       # Telegram fora
    assert not n.alertar("falha", "x") and not n.alertar("falha", "x")       # não martela
    n._ultimo_alerta["falha"] -= 1801                                        # 30 min depois
    assert not n.alertar("falha", "x") and len(chamadas) == 4                # tentou de novo


def test_p4_telegram():
    novo_banco()
    r = tb.tratar_comando("/add rtx 4090 max=8000 min=2000 estado=go envio=com_envio "
                          "vendedor=particular garantia=olx intervalo=15")
    assert "criada" in r and "GO" in r, r
    b = db.listar_buscas()[0]
    assert (b["preco_min"], b["preco_max"], b["estado"], b["filtro_envio"], b["filtro_vendedor"],
            b["garantia"], b["intervalo_min"]) == (2000, 8000, "go", "com_envio", "particular", "pay", 15)
    assert "estado" in tb.tratar_comando("/add x estado=goias")             # validado
    assert "❌" in tb.tratar_comando("/add x vendedor=amigo")
    assert "❌" in tb.tratar_comando("/add x min=500 max=100")
    assert "desconhecida" in tb.tratar_comando("/add rtx ord=barato")       # ordenação removida
    assert "inválido" in tb.tratar_comando("/add rtx max=abc")
    r = tb.tratar_comando("/add teclado mecânico max=300 obs= só switch marrom, com fio")
    b = db.listar_buscas()[0]
    assert b["instrucoes"] == "só switch marrom, com fio" and b["termo"] == "teclado mecânico"
    r = tb.tratar_comando("/addcarro linea ano=2011-2016 km=120000 motor=1.8 estado=go")
    assert "2011–2016" in r and "120.000 km" in r and "motor 1.8" in r, r
    assert db.listar_buscas()[0]["filtros_extra"] == {"ano_max": 2016, "ano_min": 2011,
                                                     "km_max": 120000, "motor": "1.8"}
    assert "carros 15000-35000 2010-2014" in tb.tratar_comando("/addcarro min=15000 max=35000 ano=2010-2014")
    assert "ano=" in tb.tratar_comando("/addcarro") and "min=" in tb.tratar_comando("/add")
    assert "ano=" in tb.tratar_comando("/addcarro estado=go")          # todos-os-carros exige filtro
    db.criar_busca({"nome": "<i>x</i>", "termo": "y"})
    assert "&lt;i&gt;" in tb.tratar_comando("/buscas")
    assert "Nenhum achado" in tb.tratar_comando("/top")
    bid = db.listar_buscas()[0]["id"]
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-5000001", "titulo": None, "preco": None})
    db.salvar_analise(aid, {"decisao": "APROVADO", "resumo": "", "score": 9,
                            "avaliacao_preco": "bom", "recomendacao": "COMPRAR", "motivo": ""})
    assert "9/10" in tb.tratar_comando("/top")                             # título None não quebra
    assert "pausada" in tb.tratar_comando(f"/pausar {bid}")
    assert "retomada" in tb.tratar_comando(f"/retomar {bid}")
    assert "agendado" in tb.tratar_comando(f"/scan {bid}")
    assert "não existe" in tb.tratar_comando("/pausar 999")
    assert "Confirme" in tb.tratar_comando(f"/del {bid}") and db.obter_busca(bid)   # pede confirmação
    assert "apagada" in tb.tratar_comando(f"/del {bid} sim") and not db.obter_busca(bid)
    assert "fila de avaliação" in tb.tratar_comando("/status") and "classificador" in tb.tratar_comando("/status")
    # clique nos botões 👍/👎 de um alerta
    bfb = criar()
    afb, _, _ = db.upsert_anuncio(bfb, {"url": "https://x/a-8000001", "titulo": "RTX 3090", "preco": 1})
    aviso, botoes = tb.tratar_callback(f"fb:{afb}:1")
    assert "👍" in aviso and botoes["inline_keyboard"][0][0]["text"].startswith("✅")
    assert db.feedbacks()[-1]["gostou"] == 1
    assert tb.tratar_callback("fb:999999:0")[1] is None and tb.tratar_callback("lixo")[1] is None
    bid = criar(termo="ps4")
    for i, cat in enumerate(["Consoles", "Consoles", "Jogos"]):
        db.upsert_anuncio(bid, {"url": f"https://x/a-{6000000 + i}", "titulo": "PS4", "preco": 1,
                                "categoria": cat})
    r = tb.tratar_comando(f"/categorias {bid}")
    assert "1. Consoles (2)" in r and "2. Jogos (1)" in r and "TODAS" in r, r
    r = tb.tratar_comando(f"/categorias {bid} 1")
    assert "✅ Consoles" in r and db.obter_busca(bid)["categorias"] == ["Consoles"]
    assert db.tamanho_fila() >= 3                                  # mudou filtro: reavalia
    assert db.obter_busca(bid)["categorias"] == ["Consoles"]
    tb.tratar_comando(f"/categorias {bid} todas")
    assert db.obter_busca(bid)["categorias"] == [] and "inválidos" in tb.tratar_comando(f"/categorias {bid} 9")
    for ruim in ("-1", "0,1", "1,x"):                       # índice negativo escolhia a ERRADA
        assert "inválidos" in tb.tratar_comando(f"/categorias {bid} {ruim}"), ruim
    assert db.obter_busca(bid)["categorias"] == []
    # preço com ponto de milhar, OBS em maiúsculas
    tb.tratar_comando("/add rtx 5060 max=1.900 min=1.000,00 OBS= só com nota")
    b = db.listar_buscas()[0]
    assert (b["preco_max"], b["preco_min"], b["instrucoes"]) == (1900, 1000, "só com nota"), b
    assert "VALE NEGOCIAR" in tb.tratar_comando("/help")
    assert "/help" in tb.tratar_comando("/xyz")


# ═══ PARTE 5 — API / SEGURANÇA / PAINEL ═══════════════════

def test_p5_api():
    from fastapi.testclient import TestClient
    import main
    novo_banco()
    c, A = TestClient(main.app), ("u", "p")
    assert c.get("/").status_code == 401 and c.get("/", auth=("u", "x")).status_code == 401
    assert c.get("/", auth=A).status_code == 200
    assert c.get("/openapi.json").status_code == 404 and c.get("/docs").status_code == 404
    assert c.post("/api/processar-fila", auth=A).status_code in (404, 405)    # N10
    r = c.post("/api/buscas", auth=A, json={"nome": "n", "termo": "rtx 3090",
                                            "criterios_inclusao": ["lacrada"]})
    bid = r.json()["id"]
    assert c.get("/api/buscas", auth=A).json()[0]["criterios_inclusao"] == ["lacrada"]   # N9
    for ruim in ({"intervalo_min": -5}, {"preco_min": -1}, {"estado": "go/../x"},
                 {"garantia": "x"}, {"filtros_extra": {"evil": 1}}):
        assert c.patch(f"/api/buscas/{bid}", auth=A, json=ruim).status_code == 422, ruim
    assert c.post("/api/buscas", auth=A, json={"nome": "n", "termo": ""}).status_code == 422
    sql("UPDATE buscas SET fase='monitor'")
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-4000001", "titulo": "t", "preco": 1})
    sql("DELETE FROM fila")
    r = c.patch(f"/api/buscas/{bid}", auth=A, json={"termo": "rtx 4090", "estado": "GO",
                                                    "instrucoes": "só NF", "filtro_vendedor": "loja",
                                                    "nome": "n"})
    b = db.obter_busca(bid)
    assert (b["termo"], b["estado"], b["instrucoes"], b["filtro_vendedor"]) == \
        ("rtx 4090", "go", "só NF", "loja")                                 # A1
    assert r.json()["alterados"] == ["estado", "filtro_vendedor", "instrucoes", "termo"]
    assert b["fase"] == "scan" and db.tamanho_fila() == 1                   # re-scan + reavalia
    assert c.post(f"/api/buscas/{bid}/pausar", auth=A).status_code == 200
    assert c.post(f"/api/buscas/{bid}/scan-agora", auth=A).status_code == 409
    assert db.obter_busca(bid)["status"] == "pausada"
    assert c.post("/api/buscas/999/pausar", auth=A).status_code == 404
    assert len(c.get(f"/api/buscas/{bid}/resultados?limite=999999", auth=A).json()) <= 1000
    db.upsert_anuncio(bid, {"url": "https://x/a-4000002", "titulo": "t", "preco": 1, "categoria": "Placas"})
    assert c.get(f"/api/buscas/{bid}/categorias", auth=A).json() == [
        {"nome": "Placas", "n": 1, "selecionada": False}]
    assert c.patch(f"/api/buscas/{bid}", auth=A, json={"categorias": ["Placas"]}).json()["alterados"] == ["categorias"]
    assert c.get(f"/api/buscas/{bid}/categorias", auth=A).json()[0]["selecionada"] is True
    assert c.post(f"/api/anuncios/{aid}/feedback", auth=A, json={"gostou": True}).status_code == 200
    assert [r["feedback"] for r in c.get(f"/api/buscas/{bid}/resultados", auth=A).json() if r["id"] == aid] == [1]
    assert c.post("/api/anuncios/999999/feedback", auth=A, json={"gostou": False}).status_code == 404
    st = c.get("/api/status", auth=A).json()
    assert "avaliações" in st["classificador"]
    assert "modelo_carregado" not in st and st["buscas_pausadas"] == 1
    # WS: sem credencial é recusado; com credencial conecta
    from starlette.websockets import WebSocketDisconnect
    from anyio import ClosedResourceError
    try:
        with c.websocket_connect("/ws") as ws:
            ws.receive_text()
        raise AssertionError("WS aceitou sem senha")
    except (WebSocketDisconnect, ClosedResourceError):
        pass
    import base64
    h = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    with c.websocket_connect("/ws", headers=h):
        pass
    m = main.WSManager()
    m.desconectar(object())                                                # não explode


def test_p5_sem_senha_nao_sobe():
    import main
    main.SENHA, original = "", main.SENHA
    try:
        from fastapi.testclient import TestClient
        with TestClient(main.app):
            raise AssertionError("subiu sem BOT_PASS")
    except RuntimeError as e:
        assert "BOT_PASS" in str(e)
    finally:
        main.SENHA = original


def test_p5_painel_escapa_html():
    """XSS: todo dado do servidor interpolado em innerHTML passa por esc()."""
    import re
    js = (Path(__file__).parent / "frontend" / "app.js").read_text()
    # interpolação DIRETA (${a.titulo}, ${a.local || ''}); condições (${a.x ? ...}) não contam
    crus = re.findall(r"\$\{\s*(?:a|b|item|busca|t|r|p|c|o)\.(?:titulo|nome|termo|resumo|motivo|"
                      r"local|envio|url|imagem|estado|data_postagem|tipo|descricao|vendedor)"
                      r"\s*(?:\}|\|\||\?\?)", js)
    assert not crus, crus
    assert "<li>${esc(" in js and not re.search(r"<li>\$\{(?!esc\()", js)     # pros/contras/obs
    assert "innerHTML = msg" not in js and 'href="${urlSegura(' in js
    assert "plataforma" not in js and "xianyu" not in js.lower()


# ═══ PARTE 6 — ROBUSTEZ / PORTABILIDADE ═══════════════════

def test_p6_fuzz_nada_explode():
    """Entrada malformada (OLX mudou formato, texto estranho) nunca derruba nada."""
    import random, string
    novo_banco()
    rnd = random.Random(7)
    alfabeto = string.printable + "áéãç€😀\x00[]{}\"\\"
    lixo = lambda n: "".join(rnd.choice(alfabeto) for _ in range(rnd.randint(0, n)))
    for _ in range(400):
        t = lixo(300)
        scheduler._texto_flight([t]); scheduler._achar_array_json(t, '"ads"')
        scheduler.extrair_anuncios(t); scheduler._anuncios_do_flight(t)
        scheduler.extrair_descricao(t); scheduler._extrair_preco(t)
        ad = {k: rnd.choice([None, "", 0, -1, 1.5, "abc", [], {}, [{}], {"x": 1}, True])
              for k in ("price", "priceValue", "oldPrice", "properties", "date", "images",
                        "location", "olxDelivery", "professionalAd", "listId", "category", "olxPay")}
        ad.update(subject="RTX 3090", url="https://x/a-1234567")
        assert scheduler._mapear_flight_ad(ad)["titulo"] == "RTX 3090"
        item = {"termo": rnd.choice(["", "rtx 3090", "😀", "a"]), "titulo": lixo(60), "descricao": lixo(200),
                "preco": rnd.choice([0, None, -5, 1e12]), "preco_max": rnd.choice([0, None, 1]),
                "instrucoes": lixo(30), "criterios_exclusao": rnd.choice([None, "[", lixo(10)]),
                "categorias": rnd.choice([None, "[]", lixo(5)]), "feedback": rnd.choice([None, 0, 1])}
        assert avaliar(item, [{"id": 1, "titulo": item["titulo"], "preco": 1}])["score"] in range(1, 11)
        assert tb.tratar_comando(f"/{rnd.choice(['add', 'top', 'categorias', 'scan'])} {lixo(20)}")
    # 1 anúncio quebrado não derruba a página inteira
    ok = anuncio_olx(1, "RTX 3090 boa", 4000)
    quebrado = {"subject": "RTX 3090", "url": "https://x/a-2", "properties": -1, "images": {"x": 1}}
    assert len(scheduler.extrair_anuncios(pagina_olx([ok, quebrado, "lixo", None]))) == 2
    assert tb.tratar_callback("fb:99999999999999999999:1")[1] is None      # estourava o SQLite


def test_p6_telegram_ponta_a_ponta():
    """O laço real de long-polling: dono x estranho, mensagem x clique, sem texto."""
    novo_banco()
    bid = criar()
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-9300001", "titulo": "RTX 3090", "preco": 1})
    chamadas, respostas = [], []
    lote = [
        {"update_id": 10, "message": {"chat": {"id": 42}, "text": "/status"}},
        {"update_id": 11, "message": {"chat": {"id": 666}, "text": "/del 1 sim"}},         # estranho
        {"update_id": 12, "message": {"chat": {"id": 42}, "photo": [{}]}},                  # sem texto
        {"update_id": 13, "callback_query": {"id": "cb1", "data": f"fb:{aid}:1",
                                             "message": {"message_id": 7, "chat": {"id": 42}}}},
        {"update_id": 14, "callback_query": {"id": "cb2", "data": f"fb:{aid}:0",
                                             "message": {"message_id": 8, "chat": {"id": 666}}}},
    ]
    class Resp:
        def __init__(self, j): self.j, self.ok, self.status_code, self.text = j, True, 200, ""
        def json(self): return self.j
    def get(url, params=None, timeout=None):
        chamadas.append(("get", params["offset"]))
        if sum(c[0] == "get" for c in chamadas) == 1:
            return Resp({"result": lote})
        raise asyncio.CancelledError                         # encerra o laço no 2º poll
    def post(url, json=None, timeout=None):
        chamadas.append((url.rsplit("/", 1)[1], json))
        return Resp({})
    orig = (tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID, tb._enviar)
    tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID = get, post, "T", "42"
    tb._enviar = lambda texto, *a: respostas.append(texto) or True
    try:
        try:
            rodar(tb.loop_comandos())
        except asyncio.CancelledError:
            pass
    finally:
        tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID, tb._enviar = orig
    assert len(respostas) == 1 and "fila de avaliação" in respostas[0], (respostas, chamadas)  # só o dono
    assert db.obter_busca(bid)                                               # estranho não apagou
    metodos = [c[0] for c in chamadas if c[0] != "get"]
    assert metodos == ["setMyCommands", "answerCallbackQuery", "editMessageReplyMarkup"], metodos
    assert [r["gostou"] for r in db.feedbacks()] == [1]                       # 👎 do estranho ignorado
    assert chamadas[-1] == ("get", 15)                                       # offset avançou


def test_p6_telegram_sem_chat_id_ensina():
    """Instalação nova: sem TELEGRAM_CHAT_ID o bot responde com o chat id."""
    enviados = []
    class Resp:
        ok, status_code, text = True, 200, ""
        def json(self): return {"result": [{"update_id": 1, "message": {"chat": {"id": 555}, "text": "oi"}}]}
    n = [0]
    def get(*a, **k):
        n[0] += 1
        if n[0] > 1:
            raise asyncio.CancelledError
        return Resp()
    orig = (tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID, tb._post)
    tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID = get, lambda *a, **k: Resp(), "T", ""
    tb._post = lambda metodo, dados, timeout: enviados.append(dados) or True
    try:
        try:
            rodar(tb.loop_comandos())
        except asyncio.CancelledError:
            pass
    finally:
        tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID, tb._post = orig
    assert enviados and enviados[0]["chat_id"] == 555 and "TELEGRAM_CHAT_ID=555" in enviados[0]["text"]


def test_p6_mensagem_longa_dividida():
    partes = notifier._partes("\n".join(f"#{i} <b>busca {i}</b> · 🟢0" for i in range(400)))
    assert len(partes) > 1 and all(len(x) <= 4096 for x in partes)
    assert all(x.count("<b>") == x.count("</b>") for x in partes)          # nenhuma tag cortada
    assert notifier._partes("curto") == ["curto"] and len(notifier._partes("x" * 9000)) == 3


def test_p6_alerta_falha_prolongada():
    novo_banco()
    bid = criar()
    async def cai(busca):
        raise ConnectionError("rede fora")
    scheduler.raspar_busca = _fake_raspar(efeito=cai)
    alertas = []
    notifier.alertar = lambda chave, texto, horas=12: alertas.append(chave)
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert alertas == []                                   # 1ª falha: sem alarde
    sql("UPDATE buscas SET criado_em=datetime('now','-7 hours'), ultimo_scan=datetime('now','-7 hours')")
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert alertas == [f"falha:{bid}"]                     # 6h+ sem sucesso: avisa


def test_p6_app_sobe_por_main_py():
    """`python main.py` sobe o painel na PORT do ambiente (ponto de entrada único)."""
    import socket, subprocess, sys, time, urllib.request
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0)); porta = so.getsockname()[1]
    # senha e porta SÓ no .env (como no Windows, sem `source .env`): main.py lê sozinho
    env = {k: v for k, v in os.environ.items() if k not in ("BOT_PASS", "PORT", "HOST", "TELEGRAM_TOKEN")}
    tmp = tempfile.mkdtemp(dir=_tmp.name)
    import shutil
    for f in ("main.py", "database.py", "scheduler.py", "avaliador.py", "notifier.py", "telegram_bot.py", "models.py"):
        shutil.copy(Path(__file__).parent / f, tmp)
    shutil.copytree(Path(__file__).parent / "frontend", Path(tmp) / "frontend")
    (Path(tmp) / ".env").write_text(f"# comentário\nBOT_PASS=p\nPORT={porta}\nHOST=127.0.0.1\nTELEGRAM_TOKEN=\n")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=tmp, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{porta}/", timeout=1)
            except urllib.error.HTTPError as e:
                assert e.code == 401                       # subiu e exige senha
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("main.py não subiu")
        assert (Path(tmp) / "data" / "bot.db").exists()     # banco criado do zero
    finally:
        proc.terminate(); proc.wait(5)


def test_p6_log_no_fuso_de_brasilia():
    """Log e datas no horário de Brasília em qualquer máquina (container é UTC)."""
    __import__("main")                    # configura o logging
    from datetime import datetime, timezone
    from models import FUSO
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    rec.created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    assert logging.Formatter("%(asctime)s").format(rec).startswith("2026-01-01 09:00")   # UTC-3
    ad = scheduler._mapear_flight_ad({**anuncio_olx(1, "x", 1), "date": int(rec.created)})
    assert ad["data_postagem"] == "2026-01-01T09:00:00-03:00"
    assert datetime.fromisoformat(ad["data_postagem"]).tzinfo is not None and FUSO


# ═══ PARTE 7 — CAMINHOS QUE NENHUM TESTE EXECUTAVA (medido com coverage) ═

def test_p7_descricoes_de_verdade():
    """_preencher_descricoes real (antes sempre substituída nos testes)."""
    novo_banco()
    bid = criar()
    ids = {}
    for n, sufixo in enumerate(("ok", "vazia", "fora", "quebrada")):
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/{sufixo}-{9400000 + n}", "titulo": "RTX 3090",
                                            "preco": 1})
        ids[sufixo] = aid
    olx = OlxFalsa({"ok-": (200, pagina_anuncio("Placa perfeita, com nota.")),
                    "vazia-": (200, "<html>sem json-ld</html>"),
                    "fora-": (404, "não existe"),
                    "quebrada-": (200, '<script type="application/ld+json">{quebrado</script>')})
    scheduler._sessao = olx
    sql("DELETE FROM fila")
    rodar(scheduler._preencher_descricoes(db.obter_busca(bid)))
    desc = {k: sql("SELECT descricao FROM anuncios WHERE id=?", v)[0][0] for k, v in ids.items()}
    assert desc == {"ok": "Placa perfeita, com nota.", "vazia": "(sem descrição)", "fora": "",
                    "quebrada": "(sem descrição)"}, desc            # 404: tenta de novo depois
    assert db.tamanho_fila() == 3                                    # as visitadas vão p/ reavaliação
    scheduler._sessao = OlxFalsa({"x/": (403, "bloqueado")})
    sql("UPDATE anuncios SET descricao=''")
    try:
        rodar(scheduler._preencher_descricoes(db.obter_busca(bid)))
        raise AssertionError("bloqueio não pode ser engolido")
    except scheduler.OlxIndisponivel:
        pass


def test_p7_broadcast_e_full_scan_diario():
    novo_banco()
    bid = criar()
    sql("UPDATE buscas SET fase='monitor', ultimo_scan_full=datetime('now','-2 days')")
    msgs, fases = [], []
    async def bc(m):
        msgs.append(m["tipo"])
    async def raspar(busca, fase):
        fases.append(fase)
        return []
    scheduler.raspar_busca, scheduler._preencher_descricoes = raspar, _sem_espera
    async def roda():
        scheduler.set_broadcast(bc)
        await scheduler.processar_busca(db.obter_busca(bid))
        await _sleep_real(0.01)                    # deixa as tasks de broadcast rodarem
    try:
        rodar(roda())
    finally:
        scheduler.set_broadcast(None)
    assert fases == ["scan"] and msgs == ["busca_status", "busca_concluida"], (fases, msgs)


def test_p7_loop_do_scheduler_sobrevive():
    chamadas = []
    async def ciclo():
        chamadas.append(1)
        if len(chamadas) == 1:
            raise RuntimeError("falha num ciclo")
        raise asyncio.CancelledError
    orig = scheduler.ciclo
    scheduler.ciclo = ciclo
    try:
        rodar(scheduler.loop_scheduler())
    except asyncio.CancelledError:
        pass
    finally:
        scheduler.ciclo = orig
    assert len(chamadas) == 2                      # 1ª falha não matou o loop


def test_p7_filtros_todos_os_ramos():
    f = scheduler.filtrar_produtos
    prods = [{"preco": 1, "envio": "com_envio"}, {"preco": 2, "envio": "sem_envio"}]
    assert [p["preco"] for p in f({"filtro_envio": "com_envio"}, prods)] == [1]
    assert [p["preco"] for p in f({"filtro_envio": "sem_envio"}, prods)] == [2]
    assert [p["preco"] for p in f({"filtro_envio": "frete_gratis"}, prods)] == [1]
    assert [p["preco"] for p in f({"filtro_vendedor": "loja"},
                                  [{"preco": 1, "vendedor": "loja"}, {"preco": 2, "vendedor": "particular"}])] == [1]
    assert f({"filtros_extra": "{json quebrado"}, prods) == prods          # extras ilegíveis: ignora
    assert "rs=" not in scheduler._montar_url({"termo": "x", "tipo": "carro", "filtros_extra": "{quebrado"}, 1)
    c = scheduler._passa_filtros_carro
    assert not c({"filtros_extra": {"ano_max": 2010}}, {"titulo": "Linea 2014"})
    assert c({"filtros_extra": {"km_max": 1000}}, {"titulo": "Linea", "atributos": "1.2.3 km"})
    assert c({"filtros_extra": {"motor": "1.8"}}, {"titulo": "Linea 1.8"})


def test_p7_json_ruim_nos_parsers():
    assert scheduler._texto_flight(["self.__next_f.push(nao-json) self.__next_f.push([1,\"ok\"])"]) == "ok"
    assert scheduler._achar_array_json('"ads":[{quebrado}]', '"ads"') is None
    assert scheduler._achar_array_json('"ads": sem colchete', '"ads"') is None
    assert scheduler._achar_array_json('"ads":[1, [2', '"ads"') is None     # não fecha


def test_p7_sinais_que_nunca_rodavam():
    base = _item(descricao="Funciona perfeitamente, usada em mineração por 4 meses. Nota fiscal e caixa.")
    m = avaliar(base, _COMPS)
    assert "usada em mineração" in m["contras"] and m["score"] < avaliar(_item(), _COMPS)["score"], m
    assert "usada em mineração" not in avaliar({**base, "termo": "rtx 3090 mineração"}, _COMPS)["contras"]
    c = avaliar(_item(descricao=_item()["descricao"] + " Chama no whats 62 99999-1234."), _COMPS)
    assert any("fora da OLX" in o for o in c["observacoes"])
    assert avaliador._radical("placas") == "plac" and avaliador._radical("bom") == "bom"
    assert "sem garantia" in avaliar(_item(descricao="Sem garantia, mas funcionando bem e com nota."),
                                     _COMPS)["contras"]


def test_p7_fila_com_anuncio_que_falha():
    """Anúncio cuja avaliação explode: não trava a fila, ganha tentativa, desiste após 3."""
    novo_banco()
    bid = criar()
    ruim, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-9500001", "titulo": "RTX 3090 X", "preco": 1})
    bom, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-9500002", "titulo": "RTX 3090 Y", "preco": 1})
    db.adicionar_fila(ruim, bid, 2); db.adicionar_fila(bom, bid, 1)
    orig = avaliador.avaliar
    def avaliar_que_falha(item, *a, **k):
        if item["anuncio_id"] == ruim:
            raise RuntimeError("explodiu")
        return orig(item, *a, **k)
    avaliador.avaliar = avaliar_que_falha
    try:
        for _ in range(4):
            rodar(avaliador.processar_fila(None))
    finally:
        avaliador.avaliar = orig
    assert sql("SELECT COUNT(*) FROM analises WHERE anuncio_id=?", bom)[0][0] == 1
    assert sql("SELECT tentativas FROM fila WHERE anuncio_id=?", ruim)[0][0] == 3
    assert db.tamanho_fila() == 0                   # desistiu: não fica girando p/ sempre


def test_p7_banco_caminhos_restantes():
    novo_banco()
    bid = criar()
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-9600001", "titulo": "RTX 3090", "preco": 4000})
    db.salvar_analise(aid, {"decisao": "APROVADO", "resumo": "", "score": 8, "avaliacao_preco": "bom",
                            "recomendacao": "COMPRAR", "motivo": ""})
    st = db.stats_busca(bid)
    assert st["total"] == 1 and st["qtd_comprar"] == 1 and st["preco_medio"] == 4000
    assert st["distribuicao_scores"] == [{"score": 8, "quantidade": 1}] and len(st["historico_precos"]) == 1
    assert [r["id"] for r in db.listar_resultados(bid, "COMPRAR")] == [aid]
    assert db.listar_resultados(bid, "VER") == []
    assert db.busca_recente(bid)
    sql("UPDATE buscas SET criado_em=datetime('now','-2 days')")
    assert not db.busca_recente(bid) and not db.busca_recente(999)
    assert db.atualizar_busca(999, {"nome": "x"}) == set()
    assert db.atualizar_busca(bid, {"nome": "b"}) == set()                 # nada mudou
    assert db.conhecidos(bid, []) == set()
    db.registrar_feedback(aid, True); db.registrar_feedback(aid, None)
    assert db.feedbacks() == []
    assert db._row_to_dict(None) == {}
    sql("UPDATE buscas SET criterios_inclusao='nao-json' WHERE id=?", bid)
    assert db.obter_busca(bid)["criterios_inclusao"] == "nao-json"        # não explode


def test_p7_telegram_http_de_verdade():
    """_post real: sucesso, recusa (log com motivo) e exceção (sem vazar o token)."""
    chamadas = []
    class R:
        def __init__(self, ok): self.ok, self.status_code, self.text = ok, (200 if ok else 400), "Bad Request: can't parse"
    respostas = iter([R(True), R(False), ConnectionError("https://api.telegram.org/bot123:SEGREDO/sendMessage")])
    def post(url, json=None, timeout=None):
        chamadas.append((url, json))
        r = next(respostas)
        if isinstance(r, Exception):
            raise r
        return r
    orig = (notifier.requests.post, notifier.TOKEN, notifier.CHAT_ID)
    notifier.requests.post, notifier.TOKEN, notifier.CHAT_ID = post, "123:SEGREDO", "42"
    logs = []
    h = logging.Handler(); h.emit = lambda rec: logs.append(rec.getMessage())
    logging.getLogger("notifier").addHandler(h)
    try:
        assert notifier._enviar("oi") is True
        assert chamadas[0][1]["chat_id"] == "42" and chamadas[0][1]["parse_mode"] == "HTML"
        assert notifier._enviar_foto("https://i/1.jpg", "legenda", notifier.teclado(5)) is False
        assert chamadas[1][1]["reply_markup"]["inline_keyboard"] and "can't parse" in logs[-1]
        assert notifier._enviar("oi") is False
        assert "SEGREDO" not in logs[-1] and "<TOKEN>" in logs[-1]
        notifier.CHAT_ID = ""
        assert notifier._post("sendMessage", {"text": "x"}, 1) is False   # sem destino: não chama
        assert len(chamadas) == 3
    finally:
        notifier.requests.post, notifier.TOKEN, notifier.CHAT_ID = orig
        logging.getLogger("notifier").removeHandler(h)
    # foto que dá certo não manda o texto de novo; resumo usa o envio comum
    enviados = []
    notifier._post = lambda m, d, t: enviados.append(m) or True
    assert rodar(notifier._enviar_com_foto("legenda", "https://i/1.jpg", 1)) and enviados == ["sendPhoto"]
    assert rodar(notifier.enviar_resumo(1, {"nome": "x", "n": 5, "comprar": 1, "ver": 1})) and enviados[-1] == "sendMessage"


def test_p7_telegram_ramos_restantes():
    novo_banco()
    assert "Nenhuma busca" in tb.tratar_comando("/buscas") and "Monitor OLX" in tb.tratar_comando("/start")
    assert "termo" in tb.tratar_comando("/add tipo=produto min=10").lower() or \
        "/add" in tb.tratar_comando("/add tipo=produto min=10")
    assert "criada" in tb.tratar_comando("/add rtx 3090 tipo=imovel")
    assert "inválido para tipo (use: produto, carro, imovel)" in tb.tratar_comando("/add x tipo=barco")
    assert "inválido para vendedor" in tb.tratar_comando("/add x vendedor=amigo")
    assert "Input should" not in tb.tratar_comando("/add x intervalo=abc") + tb.tratar_comando("/add x min=-5")
    assert "não existe" in tb.tratar_comando("/categorias 999")
    assert "Uso:" in tb.tratar_comando("/categorias") and "Uso:" in tb.tratar_comando("/top abc")
    # sem token: o laço nem começa
    orig = (tb.TOKEN, tb.requests.get, tb.requests.post, tb._enviar)
    tb.TOKEN = ""
    assert rodar(tb.loop_comandos()) is None
    # setMyCommands falhando, comando que explode, rede caindo e resposta estranha: o laço segue
    tb.TOKEN, tb.CHAT_ID = "T", "42"
    n, respostas = [0], []
    class R:
        ok, status_code, text = False, 400, "erro"
        def __init__(self, j=None): self.j = j
        def json(self):
            if self.j is None:
                raise ValueError("não é JSON")
            return self.j
    def get(*a, **k):
        n[0] += 1
        if n[0] == 1:
            raise tb.requests.exceptions.ConnectionError("caiu")
        if n[0] == 2:
            return R()                                                  # resposta não-JSON
        if n[0] == 3:
            return R({"result": [{"update_id": 5, "message": {"chat": {"id": 42}, "text": "/status"}},
                                 {"update_id": 6, "callback_query": {"id": "c", "data": "fb:1:1",
                                                                     "message": {"message_id": 1, "chat": {"id": 42}}}}]})
        raise asyncio.CancelledError
    def post(url, json=None, timeout=None):
        if url.endswith("setMyCommands"):
            raise tb.requests.exceptions.ConnectionError("sem rede no boot")
        return R()                                                      # answerCallbackQuery recusado
    tb.requests.get, tb.requests.post = get, post
    tb._enviar = lambda texto, *a: respostas.append(texto) or True
    orig_trata = tb.tratar_comando
    tb.tratar_comando = lambda t: (_ for _ in ()).throw(RuntimeError("bug no comando"))
    try:
        try:
            rodar(tb.loop_comandos())
        except asyncio.CancelledError:
            pass
    finally:
        tb.TOKEN, tb.requests.get, tb.requests.post, tb._enviar = orig
        tb.tratar_comando = orig_trata
    assert n[0] == 4 and respostas and "erro interno" in respostas[0], (n, respostas)


def test_p7_api_rotas_restantes():
    from fastapi.testclient import TestClient
    import main
    novo_banco()
    c, A = TestClient(main.app), ("u", "p")
    bid = c.post("/api/buscas", auth=A, json={"nome": "n", "termo": "rtx 3090", "preco_min": 100}).json()["id"]
    assert c.patch(f"/api/buscas/{bid}", auth=A, json={"preco_max": 50}).status_code == 422   # max < min salvo
    assert c.patch(f"/api/buscas/{bid}", auth=A, json={"termo": ""}).status_code == 422
    r = c.patch(f"/api/buscas/{bid}", auth=A, json={"intervalo_min": 1, "filtro_vendedor": "amigo"})
    assert r.status_code == 422 and "intervalo fora do limite" in r.json()["detail"] \
        and "inválido para vendedor" in r.json()["detail"], r.json()
    assert c.patch(f"/api/buscas/{bid}", auth=A, json={"termo": "", "tipo": "carro"}).status_code == 200
    assert c.post(f"/api/buscas/{bid}/pausar", auth=A).status_code == 200
    assert c.post(f"/api/buscas/{bid}/retomar", auth=A).json() == {"ok": True}
    assert db.obter_busca(bid)["status"] == "ativa"
    assert c.post(f"/api/buscas/{bid}/scan-agora", auth=A).json() == {"ok": True}
    assert c.get(f"/api/buscas/{bid}/stats", auth=A).json()["total"] == 0
    assert c.get(f"/api/buscas/{bid}/resultados?recomendacao=COMPRAR", auth=A).json() == []
    assert c.post("/api/buscas/999/retomar", auth=A).status_code == 404
    assert c.delete(f"/api/buscas/{bid}", auth=A).json() == {"ok": True} and not db.obter_busca(bid)
    assert c.get("/static/app.js").status_code == 200                      # estáticos sem senha


def test_p7_app_sobe_e_desce_com_as_tarefas():
    """lifespan de verdade: as 3 tarefas sobem, a que morre é registrada, e o
    encerramento cancela todas."""
    from fastapi.testclient import TestClient
    import main
    novo_banco()
    vivas = []
    async def eterna(nome):
        vivas.append(nome)
        try:
            await _sleep_real(60)
        except asyncio.CancelledError:
            vivas.remove(nome); raise
    orig = (scheduler.loop_scheduler, main.telegram_bot.loop_comandos, main._watchdog)
    scheduler.loop_scheduler = lambda: eterna("scheduler")
    main.telegram_bot.loop_comandos = lambda: eterna("telegram")
    main._watchdog = lambda: eterna("watchdog")
    try:
        with TestClient(main.app) as c:
            assert c.get("/", auth=("u", "p")).status_code == 200
            assert sorted(vivas) == ["scheduler", "telegram", "watchdog"]
        assert vivas == []                                                 # encerrou tudo
    finally:
        scheduler.loop_scheduler, main.telegram_bot.loop_comandos, main._watchdog = orig
    logs = []
    h = logging.Handler(); h.emit = lambda rec: logs.append(rec.getMessage())
    logging.getLogger("main").addHandler(h)
    async def morre():
        raise RuntimeError("bug na tarefa")
    async def checa():
        t = asyncio.create_task(morre(), name="x")
        t.add_done_callback(main._task_morreu)
        await _sleep_real(0.01)
    try:
        rodar(checa())
    finally:
        logging.getLogger("main").removeHandler(h)
    assert any("TASK x MORREU" in m for m in logs), logs


def test_p7_watchdog_reinicia_quem_travou():
    import main
    saidas, esperas = [], []
    async def sleep(seg):
        esperas.append(seg)
        if len(esperas) > 2:
            raise asyncio.CancelledError
    orig = (main.asyncio.sleep, main.os._exit, scheduler.ultimo_ciclo)
    main.asyncio.sleep, main.os._exit = sleep, lambda code: saidas.append(code)
    try:
        scheduler.ultimo_ciclo = time.time()                              # saudável: não mata
        try:
            rodar(main._watchdog())
        except asyncio.CancelledError:
            pass
        assert saidas == []
        esperas.clear()
        scheduler.ultimo_ciclo = time.time() - 3000                       # travado há 50 min
        try:
            rodar(main._watchdog())
        except asyncio.CancelledError:
            pass
        assert saidas and saidas[0] == 1
    finally:
        main.asyncio.sleep, main.os._exit, scheduler.ultimo_ciclo = orig


def test_p7_ws_broadcast_derruba_conexao_morta():
    import main
    m = main.WSManager()
    class Viva:
        def __init__(self): self.msgs = []
        async def send_text(self, t): self.msgs.append(t)
    class Morta:
        async def send_text(self, t): raise RuntimeError("cliente sumiu")
    viva, morta = Viva(), Morta()
    m.conexoes = [viva, morta]
    rodar(m.broadcast({"tipo": "x"}))
    assert m.conexoes == [viva] and json.loads(viva.msgs[0]) == {"tipo": "x"}


def test_p7_validacoes_dos_modelos():
    from models import BuscaCreate
    from pydantic import ValidationError
    ok = BuscaCreate(nome="n", termo="rtx 3090", criterios_inclusao="a, b", filtros_extra='{"ano_min": 2010}')
    assert ok.criterios_inclusao == ["a", "b"] and ok.filtros_extra == {"ano_min": 2010}
    assert BuscaCreate(nome="n", termo="rtx 3090", criterios_inclusao='["c"]').criterios_inclusao == ["c"]
    assert BuscaCreate(nome="n", termo="rtx 3090", filtros_extra="").filtros_extra == {}
    for ruim in ({"filtros_extra": {"ano_min": 1800}}, {"filtros_extra": {"km_max": -1}},
                 {"filtros_extra": {"motor": "18"}}, {"filtros_extra": {"x": 1}}, {"estado": "zz"}):
        try:
            BuscaCreate(nome="n", termo="rtx 3090", **ruim)
            raise AssertionError(ruim)
        except ValidationError:
            pass


def test_p7_ultimos_ramos():
    # avaliador: broadcast de cada avaliação chega ao painel
    novo_banco()
    bid = criar()
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-9700001", "titulo": "RTX 3090", "preco": 1})
    db.adicionar_fila(aid, bid, 2)
    msgs = []
    async def bc(m):
        msgs.append(m["tipo"])
    async def roda():
        avaliador.set_broadcast(bc)
        await avaliador.processar_fila(None)
        await _sleep_real(0.01)
    try:
        rodar(roda())
    finally:
        avaliador.set_broadcast(None)
    assert msgs == ["avaliado", "fila"], msgs
    # identidade: termo que É o acessório; problema/golpe que o usuário procura
    assert produto_bate("fonte 850w", "Fonte Corsair 850W Gold")[0]
    assert avaliador.problemas(norm("tem uns riscos"), "produto", ["riscos"]) == {}
    assert avaliador._achados(norm("pix antecipado"), avaliador.GOLPE, ["pix antecipado"]) == ([], [])
    # classificador pronto mas sem evidência p/ o anúncio: ajuste zero
    rot = [{"titulo": f"a{i}", "gostou": i % 2, "termo": "t"} for i in range(6)]
    assert avaliador.Classificador(rot).ajuste({"titulo": "zzz"}) == (0, "")
    # JSON-LD: lista no topo e 1º bloco sem descrição
    duas = (f'<script type="application/ld+json">{json.dumps({"@type": "Org", "name": "x"})}</script>'
            f'<script type="application/ld+json">{json.dumps([{"description": "segunda"}])}</script>')
    assert scheduler.extrair_descricao(duas) == "segunda"
    # filtros de carro pelo caminho real (filtrar_produtos), ano mínimo e km ilegível
    b = {"filtros_extra": {"ano_min": 2012, "km_max": 1000}}
    assert scheduler.filtrar_produtos(b, [{"preco": 1, "titulo": "Linea 2010"}]) == []
    assert scheduler.filtrar_produtos(b, [{"preco": 1, "titulo": "Linea ... km 2013"}])
    assert "inválido" in tb.tratar_comando("/addcarro ano=abc")
    # 1 anúncio cujo mapeamento explode (formato totalmente novo) é pulado com aviso
    orig = scheduler._mapear_flight_ad
    scheduler._mapear_flight_ad = lambda ad: (_ for _ in ()).throw(KeyError("novo")) if ad.get("listId") == 2 else orig(ad)
    try:
        assert [a["list_id"] for a in scheduler.extrair_anuncios(
            pagina_olx([anuncio_olx(1, "a", 1), anuncio_olx(2, "b", 1), anuncio_olx(3, "c", 1)]))] == [1, 3]
    finally:
        scheduler._mapear_flight_ad = orig


def test_p7_paginacao_limites_e_rsc_corrompido():
    # monitor: no máximo 10 páginas mesmo que tudo seja novo
    pags = {n: [anuncio_olx(n * 100 + i, f"x{n}{i}", 1) for i in range(3)] for n in range(1, 30)}
    olx = OlxFalsa({f"o={n}&": (200, pagina_olx(a)) for n, a in pags.items()})
    scheduler._sessao = olx
    novo_banco()
    r = rodar(scheduler.raspar_busca({"id": criar(), "nome": "t", "termo": "x"}, "monitor"))
    assert len(olx.paginas()) == 10 and len(r) == 30
    olx = OlxFalsa({f"o={n}&": (200, pagina_olx(a)) for n, a in pags.items()})
    scheduler._sessao = olx
    rodar(scheduler.raspar_busca({"id": 1, "nome": "t", "termo": "x"}, "scan"))
    assert len(olx.paginas()) == 25                       # scan: teto de 25
    # RSC chega como x-component mas corrompido: cai p/ o HTML, que está bom
    class RscQuebrado(OlxFalsa):
        async def get(self, url, timeout=None, headers=None):
            r = await super().get(url, timeout, headers)
            if headers and headers.get("RSC"):
                r.text, r.headers = "x" * 50, {"content-type": "text/x-component"}
            return r
    olx = RscQuebrado({"o=1&": (200, pagina_olx([anuncio_olx(1, "RTX", 1)]))})
    scheduler._sessao = olx
    r = rodar(scheduler.raspar_busca({"id": 1, "nome": "t", "termo": "x"}, "monitor"))
    assert len(r) == 1 and any(p.startswith("HTML ") for p in olx.pedidos)


def test_p7_mensagens_de_erro_em_portugues():
    from models import BuscaCreate, mensagem_erro
    from pydantic import ValidationError
    def msgs(**d):
        try:
            BuscaCreate(**d)
        except ValidationError as e:
            return "; ".join(mensagem_erro(x) for x in e.errors())
        return ""
    assert msgs(nome="n", termo="a" * 200) == "termo longo demais"
    assert msgs(termo="rtx 3090") == "nome é obrigatório"
    assert msgs(nome="n", termo="rtx 3090", intervalo_min="abc") == "intervalo precisa ser um número inteiro"
    assert msgs(nome="n", termo="rtx 3090", criterios_inclusao=5) == "valor inválido para critérios de inclusão"
    assert mensagem_erro({"type": "x"}) == "dados inválidos"
    from fastapi.testclient import TestClient
    import main
    novo_banco()
    c = TestClient(main.app)
    bid = c.post("/api/buscas", auth=("u", "p"), json={"nome": "n", "termo": "rtx 3090"}).json()["id"]
    r = c.patch(f"/api/buscas/{bid}", auth=("u", "p"), json={"estado": None, "categorias": None,
                                                             "filtros_extra": None, "nome": "novo"})
    assert r.status_code == 200 and r.json()["alterados"] == ["nome"]       # null = não mexe


def test_p8_revisao_scheduler():
    """Defeitos achados lendo o código linha a linha (nenhum teste os previa)."""
    assert scheduler._extrair_preco(1500.5) == 1500.5 and scheduler._extrair_preco(1500) == 1500
    assert scheduler._extrair_preco(True) == 0 and scheduler._extrair_preco("R$ 1.234,56") == 1234.56
    # "ads": null seguido de outra lista não vira lista de anúncios
    assert scheduler._achar_array_json('{"ads": null, "x": [{"subject": "a"}]}', '"ads"') is None
    assert scheduler._achar_array_json('{"ads" : [1, 2]}', '"ads"') == [1, 2]
    # monitor com filtro local (envio): descartados não impedem de parar cedo
    novo_banco()
    bid = criar(filtro_envio="com_envio")
    pag1 = [anuncio_olx(i, f"RTX 3090 {i}", 4000, olxDelivery={"enabled": i % 2 == 0}) for i in range(1, 11)]
    for a in pag1:
        p = scheduler._mapear_flight_ad(a)
        if p["envio"] == "com_envio":
            db.upsert_anuncio(bid, p)                          # só os que passam no filtro são gravados
    olx = OlxFalsa({"o=1&": (200, pagina_olx(pag1)),
                    **{f"o={n}&": (200, pagina_olx([anuncio_olx(n * 100 + i, "x", 1) for i in range(5)]))
                       for n in range(2, 12)}})
    scheduler._sessao = olx
    rodar(scheduler.raspar_busca({**db.obter_busca(bid), "nome": "t"}, "monitor"))
    assert len(olx.paginas()) == 1, olx.paginas()             # antes: 10 páginas a cada ciclo
    # título editado pelo vendedor -> reavalia
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-9800001", "titulo": "RTX 3080", "preco": 1})
    sql("DELETE FROM fila")
    db.upsert_anuncio(bid, {"url": "https://x/a-9800001", "titulo": "RTX 3090 (título corrigido)", "preco": 1})
    assert sql("SELECT prioridade FROM fila WHERE anuncio_id=?", aid)[0][0] == 2
    # descrição: pausa também depois de falha (sem rajada contra a OLX)
    esperas = []
    async def espera(seg):
        esperas.append(seg)
    orig = scheduler.asyncio.sleep
    scheduler.asyncio.sleep = espera
    scheduler._sessao = OlxFalsa({"x/": (500, "erro")})
    try:
        rodar(scheduler._preencher_descricoes(db.obter_busca(bid)))
    finally:
        scheduler.asyncio.sleep = orig
    assert esperas and len(esperas) == len(db.anuncios_sem_descricao(bid)), esperas


def test_p8_revisao_avaliador():
    """Defeitos achados lendo o avaliador linha a linha."""
    casos = [  # (termo, título, deve passar?)
        ("rtx 3090", "RTX 3090, 24GB, Zotac Trinity", True),          # vírgula + memória != lista
        ("rtx 3090", "RTX 3090,24gb", True),
        ("rtx 5060", "RTX 5060, 5070 e 5080 em estoque", False),     # lista de verdade
        ("rtx 3090", "Vendo placa que é uma RTX 3090", True),         # "que" solto != comparação
        ("rtx 5070", "RTX 3080 melhor que a 5070", False), ("rtx 5070", "RTX 4070 equivalente a 5070", False),
        ("brastemp 11kg", "Máquina de lavar Brastemp 11kg", True),   # máquina de lavar != PC
        ("rtx 3090", "Máquina gamer RTX 3090", False),
        ("jbl flip 5", "Caixa de som JBL Flip 5", True), ("masterliquid 240", "Cooler Master MasterLiquid 240", True),
        ("galaxy s23", "Galaxy S23+ 256GB", False), ("galaxy s23", "Galaxy S23 256GB", True),
        ("galaxy s23+", "Galaxy S23+ 256GB", True),
    ]
    erros = [(t, ti, ok) for t, ti, ok in casos if produto_bate(t, ti)[0] != ok]
    assert not erros, erros
    # contexto/atenuante por palavra
    assert avaliador.problemas(norm("capotou, motor fundido"), "carro", []) == {"motor fundido": 3}
    assert avaliador.problemas(norm("nao liga, levei na assistencia"), "produto", []) == {"não liga": 3}
    assert avaliador.problemas(norm("defeito na caixa de cambio"), "carro", [])["defeito"] == 3
    assert avaliador.problemas(norm("defeito na saida lateral hdmi"), "produto", [])["defeito"] == 3
    assert avaliador.problemas(norm("queimado no teto e capo"), "carro", [])["queimado"] == 1
    # "prefiro SEM defeito" / critério "sem defeito" NÃO é procurar defeito
    d = _item(descricao="Placa com defeito, não dá vídeo.")
    assert avaliar({**d, "instrucoes": "prefiro sem defeito"}, _COMPS)["recomendacao"] == "IGNORAR"
    assert avaliar({**d, "criterios_inclusao": ["sem defeito"]}, _COMPS)["recomendacao"] == "IGNORAR"
    from models import BuscaCreate
    assert BuscaCreate(nome="n", termo="rtx 3090", criterios_inclusao=["sem defeito"]).condicao == "funcionando"
    assert BuscaCreate(nome="n", termo="rtx 3090 com defeito").condicao == "defeito"
    m = avaliar({**_item(descricao="Usada em mineração, nota e caixa."), "instrucoes": "prefiro nunca minerada"}, _COMPS)
    assert "usada em mineração" in m["contras"], m
    # explicação do classificador usa palavras do anúncio
    rot = ([{"titulo": "RTX 3090 MSI Suprim", "descricao": "nota fiscal", "gostou": 1, "termo": "rtx 3090",
             "avaliacao_preco": "bom"}] * 4 + [{"titulo": "RTX 3090 Zotac", "descricao": "loja parcelo",
             "gostou": 0, "termo": "rtx 3090", "avaliacao_preco": "bom"}] * 4)
    delta, porque = avaliador.Classificador(rot).ajuste({"titulo": "RTX 3090 MSI Suprim", "termo": "rtx 3090",
                                                         "descricao": "nota fiscal", "avaliacao_preco": "bom"})
    assert delta > 0 and "bom" not in porque and "rtx 3090" not in porque, porque


def test_p8_termo_e_texto_seguros():
    from models import BuscaCreate
    from pydantic import ValidationError
    for ruim in ("F", "T", "Lu", "a b", "[", "😀", "?!"):
        try:
            BuscaCreate(nome="n", termo=ruim)
            raise AssertionError(f"aceitou termo genérico {ruim!r}")
        except ValidationError:
            pass
    for bom in ("ps5", "s23", "rtx 3090", "geladeira", "iphone 13", "x1"):
        assert BuscaCreate(nome="n", termo=bom).termo == bom
    assert BuscaCreate(nome="a\x00b\x07", termo="rtx\x00 3090").nome == "ab"
    assert BuscaCreate(nome="carros", termo="", tipo="carro", preco_max=30000).termo == ""
    assert "genérico demais" in tb.tratar_comando("/add F")
    # fuzz de comandos: não pode criar busca a partir de lixo
    novo_banco()
    for lixo in ("F", "[", "Lu", "😀 ?", "$6,/+"):
        tb.tratar_comando(f"/add {lixo}")
    assert db.listar_buscas() == []


def test_p9_buscas_de_outras_categorias():
    """Achados rodando buscas reais fora de GPU (civic, cg 160, ps5, imóveis)."""
    casos = [  # (termo, título, deve passar?)
        ("ps5", "NINTENDO SWITCH OLED 256GB", False), ("ps5", "Vendo dois ps4 slim e fat", False),
        ("ps5", "PS5 C/ Mídia Física 1TB", True), ("ps5", "Playstation 5 slim", True),
        ("ps5", "PS4 + 5 jogos", False), ("ps5", "PS5 Pro 2TB", False),
        ("iphone 8", "iPhone 8 64gb", True), ("iphone 8", "iPhone 11 com 8gb", False),
        ("memoria ddr4 8gb", "Memória DDR4 8GB 3200", True), ("memoria ddr4 8gb", "Memória DDR4 16GB", False),
        ("macbook air m1", "MacBook Air M2", False), ("linea 1.9", "Fiat Linea 1.9 16v", True),
        ("cg 160", "CG 160cc Start - 2021 Impecável", True), ("i7 12700", "i7 12700K", False),
        ("geladeira 400 litros", "Geladeira Brastemp 400L", True),
        ("civic", "Lanterna Honda Civic 1996", False), ("civic", "Friso Direito Teto Honda New Civic", False),
        ("civic", "Honda Civic Sedan EX 2.0 Flex 2019", True),
        ("bicicleta aro 29", "Roda aro 29 bicicleta", False), ("notebook dell", "Tela notebook dell 15.6", False),
        ("ps5", "joystick dualsense ps5", False),
        ("lancha", "Tinta anticrustante WEG para barcos, lanchas e veleiros", False),
        ("iphone 13", "Película p/ iPhone 13", False), ("lancha", "Vendo lancha para pesca", True),
        ("notebook dell", "Notebook Dell Vostro 15 3515 Ryzen 5 8GB", True),
        ("pc gamer", "PC Gamer Ryzen 5 RTX 3060", True), ("rtx 3060", "PC Gamer Ryzen 5 RTX 3060", False),
        ("ryzen 5 5600", "PC Gamer Ryzen 5 5600 RTX 3060", False),
    ]
    erros = [(t, ti, ok) for t, ti, ok in casos if produto_bate(t, ti)[0] != ok]
    assert not erros, erros
    # título que nem cita o modelo: vale o campo Modelo da OLX (só nesse caso)
    assert avaliador.identidade("cg 160", {"titulo": "Venda", "atributos": "Modelo: CG 160 Titan · Ano: 2016"})[0]
    assert not avaliador.identidade("cg 160", {"titulo": "Cg 150", "atributos": "Modelo: CG 150 Titan"})[0]
    assert not avaliador.identidade("cg 160", {"titulo": "Banco cg 160", "atributos": "Modelo: CG 160"})[0]
    assert avaliador.identidade("notebook dell", {"titulo": "Dell Latitude i5", "categoria": "Notebooks"})[0]
    assert not avaliador.identidade("ps5", {"titulo": "God of War Ragnarok", "categoria": "Jogos de Vídeo Game",
                                            "atributos": "Marca do Console: Sony · Modelo: Playstation 5"})[0]
    assert not avaliador.identidade("notebook dell", {"titulo": "Dell Latitude i5", "categoria": "Monitores"})[0]
    # moto/caminhão (Ano: da OLX) compara com o mesmo ano; produto ignora ano no título
    comps = [{"id": i, "titulo": "Honda CG 160", "atributos": f"Ano: {2017 if i < 6 else 2025}",
              "preco": 11000 if i < 6 else 20000, "categoria": "Motos"} for i in range(12)]
    assert avaliador.referencia_preco({"anuncio_id": 0, "atributos": "Ano: 2017", "categoria": "Motos"}, comps) == (11000, 5)
    assert avaliador.referencia_preco({"anuncio_id": 0, "titulo": "RTX 3090 de 2023", "atributos": "",
                                       "categoria": "Motos"}, comps)[0] == 20000
    # imóvel: venda e aluguel em categorias separadas
    ad = anuncio_olx(1, "Apto 2 quartos", 1500, properties=[
        {"name": "category", "label": "Categoria", "value": "Apartamentos"},
        {"name": "real_estate_type", "label": "Tipo", "value": "Aluguel - apartamento padrão"}], category="Apartamentos")
    assert scheduler._mapear_flight_ad(ad)["categoria"] == "Apartamentos (aluguel)"
    assert scheduler._mapear_flight_ad(anuncio_olx(2, "Apto", 9, category="Apartamentos"))["categoria"] == "Apartamentos"


def test_p9_alertas_contidos():
    """Busca ampla: no máximo MAX_ALERTAS por passada (os melhores), alerta só
    com a descrição baixada e, no 1º scan, dica de /categorias se misturou."""
    novo_banco()
    bid = criar(termo="rtx 5060", preco_max=2000)
    ids = []
    for i in range(8):
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/a-{3000000 + i}", "list_id": 3000000 + i,
                                            "titulo": f"RTX 5060 n{i}", "preco": 1500 + 10 * i,
                                            "categoria": "Placas" if i % 2 else "Componentes"})
        if i < 7:
            db.atualizar_descricao(aid, bid, _item()["descricao"])
        else:
            db.adicionar_fila(aid, bid, 1)                 # descrição ainda não baixada
        ids.append(aid)
    n = NotifierFalso()
    rodar(avaliador.processar_fila(n))
    assert len(n.enviados) == avaliador.MAX_ALERTAS and ids[7] not in n.enviados, n.enviados
    r = n.resumos[0]
    assert r["sem_alerta"] == 2 and r["cats"] == {"Componentes": 4, "Placas": 4}, r
    texto = []
    notifier._enviar = lambda t: texto.append(t) or True
    rodar(notifier.enviar_resumo(bid, r))
    assert "2 oportunidade(s) sem alerta" in texto[0] and f"/categorias {bid}" in texto[0]
    db.atualizar_descricao(ids[7], bid, _item()["descricao"])      # chegou: agora alerta
    rodar(avaliador.processar_fila(n))
    assert n.enviados[-1] == ids[7]
    sql("UPDATE buscas SET categorias='[\"Placas\"]'")              # já filtrada: sem dica
    db.atualizar_descricao(ids[6], bid, _item()["descricao"] + " ")
    for aid in ids[:6]:
        db.adicionar_fila(aid, bid, 1)
    n2 = NotifierFalso()
    rodar(avaliador.processar_fila(n2))
    assert n2.resumos and all(not r["cats"] for r in n2.resumos), n2.resumos


def test_p9_list_e_apelido_de_buscas():
    novo_banco()
    criar()
    assert tb.tratar_comando("/list") == tb.tratar_comando("/buscas") != "Comando desconhecido — /help"


def test_p9_regra_nova_reavalia_o_banco():
    """Veredito antigo (regra velha aprovou uma 5070 na busca da 3090) é
    refeito no próximo início, sem notificar."""
    novo_banco()
    bid = criar()
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-4000001", "list_id": 4000001,
                                        "titulo": "ASUS 5070 PRIME 12GB IGUAL 4070S 3080 3090 4060", "preco": 3000})
    db.salvar_analise(aid, {**avaliar(_item(), []), "decisao": "APROVADO"})
    sql("UPDATE anuncios SET ultima_vez=datetime('now','-40 days')")     # expirado também
    db.init_db()
    n = NotifierFalso()
    rodar(avaliador.processar_fila(n))
    assert sql("SELECT decisao FROM analises WHERE anuncio_id=?", aid)[0][0] == "EXCLUIDO"
    assert not n.enviados and not n.resumos


def test_fd_leak():
    if not os.path.isdir("/proc/self/fd"):          # só Linux tem /proc
        return
    antes = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        db.tamanho_fila()
    assert len(os.listdir("/proc/self/fd")) - antes < 5


if __name__ == "__main__":
    import sys
    testes = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    so = sys.argv[1] if len(sys.argv) > 1 else ""        # ex.: test_offline.py p3
    falhas = 0
    for nome, f in testes:
        if so and f"_{so}_" not in nome:
            continue
        estado = [(m, k, getattr(m, k)) for m, ks in (
            (scheduler, ("raspar_busca", "_preencher_descricoes", "_sessao", "processar_busca")),
            (notifier, ("alertar", "enviar_queda", "_post", "_enviar", "TOKEN")),
            (tb, ("_enviar", "_post", "TOKEN", "CHAT_ID"))) for k in ks]
        try:
            _erros_de_log.clear()
            assert db.DB_PATH.resolve() != _BANCO_REAL, "teste apontando p/ o banco REAL"
            f()
            assert not _erros_de_log, f"log quebrou: {_erros_de_log[:2]}"
            print(f"  OK    {nome}")
        except Exception as e:
            falhas += 1
            import traceback
            print(f"  FALHA {nome}: {e!r}")
            traceback.print_exc()
        finally:
            for m, k, v in estado:
                setattr(m, k, v)
    print(f"=> {'TUDO OK' if not falhas else f'{falhas} FALHA(S)'}")
    raise SystemExit(1 if falhas else 0)
