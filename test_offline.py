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
import types
# o laço do Telegram usa uma sessão: nos testes ela repassa p/ tb.requests.get/post,
# que cada teste troca por um falso (nunca rede real)
_SESSAO_TG_REAL = tb._sessao_tg
tb._sessao_tg = lambda: types.SimpleNamespace(get=lambda *a, **k: tb.requests.get(*a, **k),
                                              post=lambda *a, **k: tb.requests.post(*a, **k))
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
            "location": "São Paulo", "professionalAd": False, "olxPay": {"enabled": False},
            "images": [{"original": f"https://img.olx.com.br/{lid}.jpg"}],
            "properties": [{"name": "category", "label": "Categoria", "value": "X"}], **kw}


def pagina_olx(ads=None, flight=True, total=None):
    if not flight:
        return "<html><body>layout novo</body></html>"
    corpo = {"pad": "x" * 1200} | ({"ads": ads} if ads is not None else {}) \
        | ({"totalOfAds": total, "pageSize": 50} if total is not None else {})
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


class _SemRede:
    """Sessão HTTP não simulada = erro, nunca a OLX real (a medição de mercado
    roda dentro do processar_busca e usaria a rede em testes que só trocam o
    raspar_busca)."""
    def __init__(self, *a, **k):
        raise RuntimeError("teste tentou usar a rede real (scheduler._sessao)")


scheduler._sessao = _SemRede


# ═══ PARTE 1 — RASPAGEM ═══════════════════════════════════

def test_p1_flight_dos_scripts():
    """C1: o array self.__next_f em runtime vem vazio; os dados estão nos <script>."""
    html = pagina_olx([anuncio_olx(1546193183, "RTX 4090 MSI", 14500),
                       {"advertisingId": "x", "deviceType": "d"}])   # propaganda
    ads = scheduler.extrair_anuncios(html)
    assert len(ads) == 1 and ads[0]["list_id"] == 1546193183
    assert scheduler.extrair_anuncios(pagina_olx(flight=False)) is None     # layout mudou
    assert scheduler.extrair_anuncios(pagina_olx([], total=0)) == []        # busca vazia (como a OLX manda)
    assert scheduler.extrair_anuncios(pagina_olx(None, total=0)) == []      # sem a lista, mas total 0
    assert scheduler.extrair_anuncios(pagina_olx(None)) is None             # sem lista nem total: formato novo
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
    assert "/imoveis/aluguel/estado-go?" in u({"termo": "casa", "tipo": "imovel", "estado": "go"}, 1)


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
    notifier.alertar = lambda chave, texto, horas=12: alertas.append(chave) or True
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    b = db.obter_busca(bid)
    # 403 passageiro: só log (o alerta depois de 1 h sem ler: test_p10_bloqueio_passageiro)
    assert b["status"] == "erro" and not db.buscas_para_processar() and alertas == []


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
    sql("UPDATE buscas SET proximo_scan=datetime('now','+20 minutes')")    # dentro do intervalo
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
        # comparação entre parênteses e "troca pra" (com a nota pelo
        # mercado, produto errado barato pareceria pechincha)
        ("rtx 5070", "Placa de Vídeo RX 6900 XT (RTX 5070) 16Gb", False),
        ("rtx 5070 ti", "Rtx 5070 gigabyte troca pra 5070 ti", False),
        ("rtx 5070", "Rtx 5070 gigabyte troca pra 5070 ti", True),
        ("rtx 5070", "Placa de vídeo (RTX 5070) Galax 12GB", True),
        ("rtx 5070", "RTX 5070 Galax (RTX 5070 OC) 12GB", True),
        ("rtx 5070", "RTX 3060 12GB, equivale a uma 5070 no DLSS", False),
        ("rtx 5070", "RX 6800 16GB desempenho de RTX 5070", False),
        ("rtx 5070 ti", "Troco RTX 5070 Ti por 5070 + volta", True),
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
    rotulos = ([rot(f"RTX 3090 MSI Suprim {i}", "nota fiscal, garantia, nunca minerou", 1) for i in range(6)]
               + [rot(f"RTX 3090 Zotac loja {i}", "loja, parcelo no cartão, envio imediato", 0, "loja")
                  for i in range(6)])
    assert not Classificador(rotulos[:9]).pronto                       # 6👍 3👎: ainda aprende
    assert not Classificador(rotulos[2:10]).pronto                     # 4👍 4👎: poucos rótulos
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
        self.aceita, self.enviados, self.resumos, self.avaliacoes = aceita, [], [], []

    async def enviar_resultado(self, item, av):
        await _sleep_real(0.01)                 # abre janela p/ corrida
        self.enviados.append(item["anuncio_id"])
        self.avaliacoes.append(av)
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
        # WebSocket do painel: sem compressão e sem vazar (a config antiga do uvicorn
        # prendia ~76 KB de memória nativa por conexão: 300 conexões = +22 MB)
        import base64, websockets
        cab = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
        rss = lambda: int(Path(f"/proc/{proc.pid}/statm").read_text().split()[1]) * 4096 >> 20
        async def conecta(n):
            for _ in range(n):
                async with websockets.connect(f"ws://127.0.0.1:{porta}/ws", additional_headers=cab) as w:
                    assert not w.response.headers.get("Sec-WebSocket-Extensions")
        rodar(conecta(50))
        antes = rss()
        rodar(conecta(300))
        assert rss() - antes < 6, (antes, rss())
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
    assert "criada" in tb.tratar_comando("/add rtx 3090 tipo=carro")
    assert "exige o estado" in tb.tratar_comando("/add rtx 3090 tipo=imovel")
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
        scheduler.ultimo_ciclo = time.monotonic()                         # saudável: não mata
        try:
            rodar(main._watchdog())
        except asyncio.CancelledError:
            pass
        assert saidas == []
        esperas.clear()
        scheduler.ultimo_ciclo = time.monotonic() - 3000                  # travado há 50 min
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
             "avaliacao_preco": "bom"}] * 6 + [{"titulo": "RTX 3090 Zotac", "descricao": "loja parcelo",
             "gostou": 0, "termo": "rtx 3090", "avaliacao_preco": "bom"}] * 6)
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


# ═══ PARTE 10 — RELÓGIO, BLOQUEIO, TROCA E REPUBLICAÇÃO ════

def test_p10_relogio_voltou():
    """Boot com o relógio do PC adiantado; corrigido pelo NTP, o proximo_scan
    ficava horas no futuro e o bot parava, mudo."""
    novo_banco()
    bid = criar()
    sql("UPDATE buscas SET fase='monitor', ultimo_scan_full=datetime('now'), "
        "proximo_scan=datetime('now','+32 minutes')")
    assert not db.buscas_para_processar()                          # dentro de 1 intervalo: espera
    sql("UPDATE buscas SET proximo_scan=datetime('now','+20 hours')")
    assert [b["id"] for b in db.buscas_para_processar()] == [bid]  # impossível: roda já
    assert not db.full_scan_vencido(bid)
    sql("UPDATE buscas SET ultimo_scan_full=datetime('now','+20 hours')")
    assert db.full_scan_vencido(bid)
    db.manutencao_diaria()
    bak = db.DB_PATH.with_suffix(".db.bak")
    futuro = time.time() + 20 * 3600                               # backup "do futuro"
    os.utime(bak, (futuro, futuro))
    db.manutencao_diaria()
    assert bak.stat().st_mtime < futuro                            # a faxina não espera 44 h
    # debounce no relógio monotônico, que começa perto de 0 no boot: o 1º aviso
    # não pode ser engolido como se o anterior tivesse sido "há pouco"
    import importlib, types
    n = importlib.reload(notifier)
    enviados = []
    n._enviar = lambda t: enviados.append(t) or True
    n.time = types.SimpleNamespace(monotonic=lambda: 100.0)        # 100 s depois do boot
    try:
        assert n.alertar("k", "a") and not n.alertar("k", "b") and enviados == ["a"]
    finally:
        importlib.reload(notifier)                                 # devolve o módulo time


def test_p10_bloqueio_passageiro():
    """403 do Cloudflare em ondas de ~30 min: o bot se recupera sozinho, então
    uma onda não pode mandar 'OLX indisponível… as buscas estão paradas'."""
    novo_banco()
    bid = criar()
    falha = [True]
    async def raspar(busca, fase):
        if falha[0]:
            raise scheduler.OlxIndisponivel("403 (bloqueio) em https://www.olx.com.br/x")
        return []
    scheduler.raspar_busca, scheduler._preencher_descricoes = raspar, _sem_espera
    alertas = []
    notifier.alertar = lambda chave, texto, horas=12: alertas.append((chave, texto)) or True
    scheduler._olx_falha_desde, scheduler._olx_alertado = None, False
    passa = lambda: rodar(scheduler.processar_busca(db.obter_busca(bid)))
    passa()
    assert alertas == []                  # 1ª falha nunca alarma (nem em busca diária)
    scheduler._olx_falha_desde -= 3601                             # falhando há 1 h
    passa(); passa()
    assert [c for c, _ in alertas] == ["olx"] and "há 60 min" in alertas[0][1], alertas   # 1 aviso
    falha[0] = False
    passa(); passa()
    assert [c for c, _ in alertas] == ["olx", "olx_voltou"], alertas    # avisa a volta, 1x
    falha[0] = True
    passa()
    assert len(alertas) == 2                                       # conta do zero de novo


def test_p10_anuncio_so_de_troca():
    """'Troco rtx 5060 por rx 9060xt' não é venda: não pode virar 🟢 COMPRAR."""
    troca = _item(termo="rtx 5060", preco_max=1900, preco=1900,
                  titulo="Troco rtx 5060 por rx 9060xt de 16gb. Pago mais um valor na troca.",
                  descricao="Placa nova, pouco usada. Raramente utilizo o pc para jogar. Estou "
                            "querendo trocar por uma rx 9060xt de 16gb. Pago mais um valor na troca.")
    av = avaliar(troca, [])
    assert av["decisao"] == "APROVADO" and av["recomendacao"] == "VER" and not av["alerta"], av
    assert any("TROCA" in c for c in av["contras"]), av
    for titulo, desc in (("Vendo ou troco RTX 5060", troca["descricao"]),
                         ("RTX 5060 - aceito troca", troca["descricao"]),
                         ("Troco RTX 5060", "Vendo por 1900 ou troco por RX 9060 XT, nota fiscal e caixa.")):
        av = avaliar({**troca, "titulo": titulo, "descricao": desc}, [])
        assert not any("TROCA" in c for c in av["contras"]), (titulo, av)
    nega = avaliar({**troca, "titulo": "Troco RTX 5060", "descricao": "Não vendo, só troco por RX 9060 XT."}, [])
    assert any("TROCA" in c for c in nega["contras"]) and not nega["alerta"], nega   # venda negada


def test_p10_republicacao_nao_realerta():
    """Vendedor que apaga e reposta o anúncio (igual ou com o título reordenado)
    não gera um alerta a cada cópia."""
    novo_banco()
    bid = criar(termo="rtx 5060", preco_max=1900)
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    seq = iter(range(5000000, 5000100))
    def anuncio(titulo, preco=1800, local="Cidade A, Bairro 1"):
        lid = next(seq)
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/a-{lid}", "list_id": lid,
                                            "titulo": titulo, "preco": preco, "local": local})
        # cada um com o seu texto: descrição igual ao mesmo preço É republicação (teste p13)
        db.atualizar_descricao(aid, bid, f"Anúncio {lid}. " + _item()["descricao"])
        return aid
    n = NotifierFalso()
    t = "VENDO RTX 5060 MSI - R$ 1.800,00"
    a1, a2 = anuncio(t), anuncio(t)                                # 2 cópias na mesma rodada
    rodar(avaliador.processar_fila(n))
    assert len(n.enviados) == 1, n.enviados
    a3 = anuncio(t, preco=1750)                                    # rodada seguinte, -3%
    rodar(avaliador.processar_fila(n))
    assert len(n.enviados) == 1
    assert all(sql("SELECT notificado FROM anuncios WHERE id=?", a)[0][0] for a in (a1, a2, a3))
    g1 = anuncio("Placa de Vídeo Galax RTX 5060 8GB GDDR7 2K. (Apenas retirada nao envio)", local="Cidade B, Bairro 2")
    rodar(avaliador.processar_fila(n))
    g2 = anuncio("(nao envio apenas retirada )Placa de Vídeo Galax RTX 5060 8GB GDDR7 2K", local="Cidade B, Bairro 2")
    rodar(avaliador.processar_fila(n))
    assert n.enviados == [n.enviados[0], g1], n.enviados           # a reordenada não alerta
    # NÃO são republicação: outro local, outro preço
    outros = [anuncio(t, local="Cidade C, Bairro 3"), anuncio(t, preco=1500)]
    rodar(avaliador.processar_fila(n))
    assert sorted(n.enviados[2:]) == sorted(outros), (n.enviados, outros)
    # mesmo bairro e preço em 24 h com OUTRO título: pode ser o mesmo vendedor OU
    # outro (cidade grande, preço que se agrupa). Alerta, com o aviso — calar perdia
    # anúncio legítimo; repetir custa 1 mensagem
    c1 = anuncio("Placa de Vídeo 5060", preco=1400, local="Cidade D, Bairro 4")
    rodar(avaliador.processar_fila(n))
    c2 = anuncio("RTX 5060 8GB", preco=1450, local="Cidade D, Bairro 4")
    rodar(avaliador.processar_fila(n))
    assert n.enviados[-2:] == [c1, c2], n.enviados
    assert "reanunciando" in n.avaliacoes[-1]["observacoes"][0], n.avaliacoes[-1]["observacoes"]
    assert not any("reanunciando" in o for o in n.avaliacoes[-2]["observacoes"])
    # passadas 24 h, só o título quase igual segura: outra marca onde só houve 1 alerta
    sql("UPDATE anuncios SET primeira_vez=datetime('now','-2 days')")
    marca = anuncio("VENDO RTX 5060 GALAX - R$ 1.800,00", local="Cidade C, Bairro 3")
    rodar(avaliador.processar_fila(n))
    # ...mas o 3º anúncio no mesmo bairro e preço é vendedor em série: calado
    anuncio("Placa RTX 5060 Gigabyte Eagle zerada", local="Cidade C, Bairro 3")
    rodar(avaliador.processar_fila(n))
    assert n.enviados[-1] == marca, n.enviados
    anuncio(t)                                                     # o mesmo título (MSI) não
    rodar(avaliador.processar_fila(n))
    assert n.enviados[-1] == marca, n.enviados
    sql("UPDATE anuncios SET primeira_vez=datetime('now','-15 days')")
    velho = anuncio(t)
    rodar(avaliador.processar_fila(n))
    assert n.enviados[-1] == velho                                 # 15 dias depois: alerta de novo
    mais_barato = anuncio(t, preco=1740)                           # repostado 3% mais barato: novidade
    rodar(avaliador.processar_fila(n))
    assert n.enviados[-1] == mais_barato, n.enviados


# ═══ PARTE 11 — MERCADO, RAM E WEBSOCKET ══════════════════

_MERC_3090 = {"n": 40, "mediana": 7200, "variantes": {}}


def test_p11_nota_pelo_mercado():
    """Com teto, só PECHINCHA alerta: no teto (6.000) com o mercado a 7.200 é o
    preço normal (VER, sem alerta); bem abaixo do mercado é pechincha; metade
    do mercado é suspeito. Acima do teto continua a regra do teto."""
    it = lambda **kw: _item(preco_max=6000, mercado=_MERC_3090, **kw)
    normal = avaliar(it(preco=6000), [])
    assert normal["recomendacao"] == "VER" and not normal["alerta"] and normal["avaliacao_preco"] == "justo", normal
    assert normal["preco_vs_mercado_pct"] == -17 and any("não é pechincha" in o for o in normal["observacoes"])
    pech = avaliar(it(preco=5500), [])
    assert pech["recomendacao"] == "COMPRAR" and pech["alerta"] == "comprar" and pech["referencia"] == "mercado", pech
    assert "vs mercado R$ 7.200 de 40 anúncios" in pech["resumo"] and "pechincha: 24%" in pech["motivo"]
    # metade do mercado mas dentro do teto: ⚠️ (antes do mercado era 🟢; a 1ª versão calava)
    golpe = avaliar(it(preco=3000), [])                              # -58%, 50% do teto
    assert golpe["avaliacao_preco"] == "suspeito" and golpe["alerta"] == "suspeita", golpe
    assert golpe["recomendacao"] == "VER" and any("metade do mercado" in o for o in golpe["observacoes"]), golpe
    lixo = avaliar(it(preco=2500), [])                               # -65% E < 45% do teto: cala
    assert lixo["avaliacao_preco"] == "suspeito" and not lixo["alerta"], lixo
    # teto em ~metade do mercado: tudo no teto vira ⚠️ ou 🟢, nada some
    m80 = {"n": 49, "mediana": 12499}
    r80 = {p: avaliar(_item(termo="rtx 5080", titulo="RTX 5080 16GB Gigabyte", preco=p,
                            preco_max=6300, mercado=m80), []) for p in (2800, 4000, 6000, 6300)}
    assert [r80[p]["alerta"] for p in (2800, 4000, 6000, 6300)] == ["", "suspeita", "suspeita", "comprar"], r80
    acima = avaliar(it(preco=6500, descricao=_item()["descricao"] + " Aceito proposta."), [])
    assert acima["avaliacao_preco"] == "negociavel" and acima["referencia"] == "teto" and "vs seu teto" in acima["resumo"]
    sem = avaliar(_item(preco_max=6000, preco=6000), [])            # sem medição: regra do teto
    assert sem["recomendacao"] == "COMPRAR" and sem["referencia"] == "teto", sem
    defeito = avaliar(it(preco=2000, termo="rtx 3090 com defeito",
                         descricao="Placa com defeito, não dá vídeo, para reparo."), [])
    assert "vs mercado" not in defeito["resumo"], defeito          # mercado é de placa boa
    # variante: 5060 Ti de 16 GB tem outro mercado que a de 8 GB
    m = avaliador.resumo_mercado([(3000 + 20 * i, "8gb") for i in range(8)]
                                 + [(4400 + 40 * i, "16gb") for i in range(6)] + [(3300, "")])
    assert m["variantes"] == {"8gb": {"n": 8, "mediana": 3070}, "16gb": {"n": 6, "mediana": 4500}}, m
    ti = lambda t, p: avaliar(_item(termo="rtx 5060 ti", titulo=t, preco=p, preco_max=3500, mercado=m), [])
    assert ti("RTX 5060 Ti 16GB Asus", 3400)["preco_vs_mercado_pct"] == -24      # vs 4.500 (16 GB)
    assert ti("RTX 5060 Ti 8 GB Galax", 3000)["avaliacao_preco"] == "justo"      # vs 3.070 (8 GB)
    # título sem a memória: vale o atributo da OLX ("VRAM: 16 GB")
    attr = avaliar(_item(termo="rtx 5060 ti", titulo="RTX 5060 Ti Asus", preco=3400, preco_max=3500, mercado=m,
                         atributos="Marca: Nvidia · Memória de Vídeo - VRAM: 16 GB"), [])
    assert attr["preco_vs_mercado_pct"] == -24, attr
    # muito abaixo do mercado (mas não a metade): alerta, com o aviso de golpe
    fundo = avaliar(it(preco=4000), [])                              # -44% vs 7.200
    assert fundo["alerta"] == "comprar" and any("nunca pague adiantado" in o for o in fundo["observacoes"]), fundo
    assert not any("adiantado" in o for o in pech["observacoes"])  # -24%: sem o aviso
    # barato demais + sinal de golpe (descrição curta, WhatsApp, pressa) = ⚠️, nunca 🟢
    for desc in ("Placa com 3 meses de uso. Seminova.",
                 _item()["descricao"] + " Chama no whats (11) 99999-0000.",
                 _item()["descricao"] + " Para vender rápido."):
        sus = avaliar(it(preco=4000, descricao=desc), [])
        assert sus["alerta"] == "suspeita" and sus["recomendacao"] == "VER", (desc, sus)
        assert any("padrão comum de golpe" in o for o in sus["observacoes"]), sus
    assert avaliar(it(preco=5000, descricao="Placa com 3 meses de uso. Seminova."), [])["alerta"] != "suspeita"  # -31%
    assert avaliador.resumo_mercado([(1000, "")] * 4) is None                     # amostra pequena
    assert avaliador.resumo_mercado([(100, ""), (500, ""), (2000, ""), (9000, ""), (30000, "")]) is None  # misturado
    # o alerta diz contra o quê comparou
    textos, foto = [], notifier._enviar_foto
    notifier._enviar_foto = lambda *a, **k: False
    notifier._enviar = lambda t, b=None: textos.append(t) or True
    try:
        rodar(notifier.enviar_resultado({"preco_max": 6000, "titulo": "RTX 3090", "preco": 5500}, pech))
        rodar(notifier.enviar_resultado({"preco_max": 6000, "titulo": "RTX 3090", "preco": 6500}, acima))
    finally:
        notifier._enviar_foto = foto
    assert "-24% vs mercado" in textos[0] and "+8% vs seu teto" in textos[1], textos
    rodar(notifier.enviar_resultado({"preco_max": 6000, "titulo": "RTX 3090", "preco": 4000}, sus))
    assert "PECHINCHA SUSPEITA" in textos[2] and "COMPRAR" not in textos[2], textos[2]


def test_p11_mercado_medido_sem_teto():
    """1x/dia a busca com teto é lida SEM o &pe= (até 5 págs): só o produto
    certo, sem defeito, com preço; falha não derruba a busca."""
    novo_banco()
    bid = criar(termo="rtx 5060 ti", preco_max=2300)
    ads = ([anuncio_olx(6000000 + i, "RTX 5060 Ti 8GB Galax", 3000 + 20 * i) for i in range(8)]
           + [anuncio_olx(6000100 + i, "RTX 5060 Ti 16GB Asus", 4400 + 40 * i) for i in range(6)]
           + [anuncio_olx(6000200, "PC Gamer RTX 5060 Ti", 6000), anuncio_olx(6000201, "RTX 5060 Ti com defeito", 900),
              anuncio_olx(6000202, "RTX 5060 Ti 8GB a combinar", 0)])
    olx = OlxFalsa({"pe=": (200, pagina_olx([anuncio_olx(6000300, "RTX 5060 Ti 8GB", 2200)])),
                    "o=1&sf=1": (200, pagina_olx(ads))})
    scheduler._sessao, scheduler._preencher_descricoes = olx, _sem_espera
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    sem_teto = [u for u in olx.paginas() if "pe=" not in u]
    assert len(sem_teto) == 2 and all("q=rtx%205060%20ti" in u for u in sem_teto), olx.paginas()   # pág 1 e a vazia
    b = db.obter_busca(bid)
    assert (b["mercado"]["n"], b["mercado"]["mediana"]) == (14, 3130) and b["mercado"]["em"], b["mercado"]
    assert b["mercado"]["variantes"]["16gb"]["mediana"] == 4500 and b["status"] == "ativa"
    assert not db.mercado_vencido(bid) and f"mercado ~R$ 3.130" in tb.tratar_comando("/buscas")
    n = len(olx.pedidos)
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert all("pe=" in u for u in olx.pedidos[n:]), olx.pedidos[n:]         # medido hoje: não repete
    # anúncio coletado foi reavaliado com o mercado (8 GB a 2.200 vs 3.070 = pechincha)
    rodar(avaliador.processar_fila(None))
    assert "vs mercado" in sql("SELECT resumo FROM analises")[0][0]
    sql("UPDATE buscas SET mercado=json_set(mercado, '$.em', datetime('now','-25 hours'))")
    assert db.mercado_vencido(bid)
    olx.rotas = {"pe=": (200, pagina_olx([])), "o=1&sf=1": (403, "")}             # bloqueio só na medição
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    b = db.obter_busca(bid)
    assert b["status"] == "ativa" and b["mercado"]["mediana"] == 3130 and db.mercado_vencido(bid)  # tenta de novo
    olx.rotas = {"pe=": (200, pagina_olx([])), "o=1&sf=1": (200, pagina_olx(ads[:3]))}   # amostra pequena
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert db.obter_busca(bid)["mercado"].get("mediana") is None and not db.mercado_vencido(bid)  # sem nota, sem remedir
    # sem teto não mede; trocar o produto zera a medição; trocar o teto reavalia
    assert not db.mercado_vencido(criar(termo="rtx 4090"))
    db.atualizar_busca(bid, {"termo": "rtx 5070"})
    assert db.obter_busca(bid)["mercado"] == ""
    sql("DELETE FROM fila")
    db.atualizar_busca(bid, {"preco_max": 2415})
    assert sql("SELECT COUNT(*) FROM fila WHERE prioridade=0")[0][0] >= 1


def test_p11_vigia_ram():
    """malloc_trim depois de cada rodada com busca; 1 linha no log quando a RAM
    fica 5+ MB acima da última anotação (mostra em que evento ela cresce)."""
    linhas, trims = [], []
    h = logging.Handler(); h.emit = lambda r: linhas.append(r.getMessage())
    lg = logging.getLogger("scheduler"); lg.addHandler(h); lg.setLevel(logging.INFO)
    orig = (scheduler._malloc_trim, db.ram_mb, scheduler._ram_base)
    seq = iter([70, 72, 76, 75, 69, 74, 80])
    scheduler._malloc_trim, db.ram_mb, scheduler._ram_base = (lambda n: trims.append(n) or 1), (lambda: next(seq)), 0
    try:
        for _ in range(7):
            scheduler._vigiar_ram()
    finally:
        lg.removeHandler(h); lg.setLevel(logging.NOTSET)
        scheduler._malloc_trim, db.ram_mb, scheduler._ram_base = orig
    assert trims == [0] * 7
    assert [x.split(" (")[0] for x in linhas if "RAM" in x] == ["RAM subiu: 76 MB", "RAM subiu: 74 MB",
                                                                "RAM subiu: 80 MB"], linhas
    assert db.ram_mb() and scheduler._malloc_trim                   # Linux/glibc de verdade
    assert "anônima" in linhas[0] and "objetos Python" in linhas[0], linhas[0]
    # teto: acima de RAM_MAX sai com 1 (systemd/compose sobem de novo), com o detalhe no log
    saidas, orig = [], (scheduler._malloc_trim, db.ram_mb, os._exit)
    lg.addHandler(h)
    scheduler._malloc_trim, db.ram_mb, os._exit = (lambda n: 1), (lambda: scheduler.RAM_MAX + 1), saidas.append
    try:
        scheduler._vigiar_ram()
    finally:
        lg.removeHandler(h)
        scheduler._malloc_trim, db.ram_mb, os._exit = orig
    assert saidas == [1] and any("reiniciando o processo" in x and "anônima" in x for x in linhas), linhas
    # o ciclo só vigia quando alguma busca rodou
    novo_banco()
    chamadas = []
    orig_v = scheduler._vigiar_ram
    scheduler._vigiar_ram = lambda: chamadas.append(1)
    try:
        sql("UPDATE buscas SET proximo_scan=datetime('now','+1 minute')")
        rodar(scheduler.ciclo())
        assert chamadas == []
        criar()
        scheduler.raspar_busca, scheduler._preencher_descricoes = _fake_raspar([]), _sem_espera
        rodar(scheduler.ciclo())
        assert chamadas == [1]
    finally:
        scheduler._vigiar_ram = orig_v


def test_p11_polling_solta_ciclo_da_excecao():
    """Vazamento de RAM: cada timeout do long-poll do Telegram deixava um ciclo
    (frame do urllib3 <-> exceção) prendendo a conexão e o contexto TLS com os
    certificados, ~0,7 MB (reproduzido: 120 timeouts, 39 -> 123 MB). O GC
    automático, com o processo ocioso, não o recolhia. Aqui ele fica
    desligado: só o gc.collect() do próprio loop solta. O último ciclo pode seguir
    preso por 1-2 voltas (o asyncio ainda o referencia); o que importa é não acumular."""
    import gc
    import weakref
    class Pesado:
        pass
    refs, vivos = [], []
    def get(url, params=None, timeout=None):
        vivos.append(sum(r() is not None for r in refs))
        if len(refs) == 6:
            raise asyncio.CancelledError
        obj = Pesado()
        refs.append(weakref.ref(obj))
        erro = tb.requests.exceptions.ReadTimeout("expirou")
        erro.ciclo = [erro, obj]                  # como o frame que guarda a exceção
        raise erro
    dormir = asyncio.sleep
    async def sem_espera(s):
        await dormir(0)                           # cede a vez como o sleep de verdade
    class Resp:
        ok, status_code, text = True, 200, ""
    orig = (tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID, asyncio.sleep)
    tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID = get, lambda *a, **k: Resp(), "T", "42"
    asyncio.sleep = sem_espera
    gc.disable()
    try:
        try:
            rodar(tb.loop_comandos())
        except asyncio.CancelledError:
            pass
    finally:
        gc.enable()
        tb.requests.get, tb.requests.post, tb.TOKEN, tb.CHAT_ID, asyncio.sleep = orig
    assert len(vivos) == 7 and max(vivos) <= 2, vivos      # sem o collect: 0,1,2,...,6


def test_p11_polling_reaproveita_conexao():
    """Uma sessão (conexão TLS) p/ o laço inteiro do Telegram: uma nova a cada
    poll custava ~6 ms de CPU (contexto TLS + certificados), ~1.600x/dia."""
    assert isinstance(_SESSAO_TG_REAL(), tb.requests.Session)      # criar não conecta
    criadas, polls = [], [0]
    class Resp:
        ok, status_code, text = True, 200, ""
        def json(self):
            return {"result": []}
    def get(*a, **k):
        polls[0] += 1
        if polls[0] > 3:
            raise asyncio.CancelledError
        return Resp()
    fabrica = tb._sessao_tg
    tb._sessao_tg = lambda: criadas.append(1) or types.SimpleNamespace(get=get, post=lambda *a, **k: Resp())
    orig = (tb.TOKEN, tb.CHAT_ID)
    tb.TOKEN, tb.CHAT_ID = "T", "42"
    try:
        try:
            rodar(tb.loop_comandos())
        except asyncio.CancelledError:
            pass
    finally:
        tb._sessao_tg = fabrica
        tb.TOKEN, tb.CHAT_ID = orig
    assert polls[0] == 4 and criadas == [1], (polls, criadas)


def test_p11_confere_total_declarado_pela_olx():
    """Defeito silencioso: a OLX renomeia um campo e o bot passa a ler 20 de 50
    sem erro nenhum (já aconteceu: meses lendo 5 de 50). A página declara o
    total (totalOfAds/pageSize): leu bem menos que o declarado = alerta."""
    bons = [anuncio_olx(7000 + i, f"RTX 3090 {i}", 5000) for i in range(20)]
    renomeados = [{k if k != "subject" else "title": v for k, v in anuncio_olx(8000 + i, f"RTX 3090 r{i}", 5000).items()}
                  for i in range(30)]
    alertas, orig = [], notifier.alertar
    notifier.alertar = lambda chave, texto, horas=12: alertas.append((chave, texto)) or True
    olx = OlxFalsa({"o=1&": (200, pagina_olx(bons + renomeados, total=50)),
                    "o=2&": (200, pagina_olx(bons[:10], total=60))})          # pág 2 de 60: 10 é o certo
    try:
        p1 = rodar(scheduler._pagina_de_busca(olx, "https://www.olx.com.br/brasil?q=x&o=1&sf=1"))
        p2 = rodar(scheduler._pagina_de_busca(olx, "https://www.olx.com.br/brasil?q=x&o=2&sf=1"))
    finally:
        notifier.alertar = orig
    assert len(p1) == 20 and len(p2) == 10, (len(p1), len(p2))      # o que deu p/ ler segue
    assert [c for c, _ in alertas] == ["olx_formato"] and "declarou 50" in alertas[0][1], alertas
    assert scheduler._faltando('"totalOfAds":3,"pageSize":50', 0, "u?o=1") is None   # poucos: não alarma
    assert scheduler._faltando('"ads":[]', 0, "u?o=1") is None                       # sem total: não dá p/ saber


def test_p11_checagem_diaria_da_olx():
    """1x/dia numa busca ampla: anúncios completos, por data, com descrição.
    Preço que some, ordem que muda e descrição vazia falhavam em silêncio."""
    boa = [anuncio_olx(9000 + i, f"Geladeira {i}", 1000 + i, date=1792716949 - 60 * i) for i in range(45)]
    alertas, orig = [], notifier.alertar
    notifier.alertar = lambda chave, texto, horas=12: alertas.append((chave, texto)) or True
    def roda(pagina, anuncio=pagina_anuncio("Geladeira frost free, funcionando, 2 anos de uso."), status=200):
        alertas.clear()
        scheduler._sessao = OlxFalsa({"q=geladeira": (status, pagina), "anuncio-": (200, anuncio)})
        rodar(scheduler._canario())
        return scheduler._canario_proximo - time.monotonic()
    try:
        assert roda(pagina_olx(boa)) > 80000 and alertas == [], alertas           # ok: próxima em 24 h
        sem_preco = [{**a, "priceValue": ""} for a in boa]
        roda(pagina_olx(sem_preco))
        assert len(alertas) == 1 and "preço faltando" in alertas[0][1], alertas
        roda(pagina_olx(boa[::-1]))                                               # mais velhos primeiro
        assert "ordem de data" in alertas[0][1], alertas
        roda(pagina_olx(boa), anuncio="<html>sem json-ld</html>")
        assert "descrição" in alertas[0][1], alertas
        roda(pagina_olx(boa[:10]))
        assert "só 10 anúncios" in alertas[0][1], alertas
        assert roda(pagina_olx(boa), status=403) < 4000 and alertas == []          # bloqueio: 1 h, sem alarme
    finally:
        notifier.alertar = orig
    # o ciclo só chama quando venceu e fora da pausa por bloqueio
    novo_banco()
    chamou, orig_c = [], scheduler._canario
    async def falso():
        chamou.append(1)
    scheduler._canario = falso
    try:
        scheduler._canario_proximo = time.monotonic() + 100
        rodar(scheduler.ciclo())
        scheduler._canario_proximo, scheduler._olx_pausa_ate = time.monotonic() - 1, time.monotonic() + 100
        rodar(scheduler.ciclo())
        assert chamou == []
        scheduler._olx_pausa_ate = 0.0
        rodar(scheduler.ciclo())
        assert chamou == [1]
    finally:
        scheduler._canario = orig_c


def test_p11_telegram_fora_nao_perde_alerta():
    """Telegram/rede fora na hora do alerta: antes ele SUMIA (o anúncio ficava sem
    'notificado' e nada o reenviava). Agora volta p/ a fila e sai na próxima volta;
    numa leva, só o 1º gasta a tentativa. Recusa definitiva (4xx) não volta."""
    novo_banco()
    bid = criar(termo="rtx 3090", preco_max=6000)
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    ids = []
    for i, (marca, local) in enumerate((("Asus TUF", "A"), ("Zotac Trinity", "B"), ("Gigabyte Eagle", "C"))):
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/a-{7100 + i}", "list_id": 7100 + i,
                                            "titulo": f"RTX 3090 {marca} 24GB", "preco": 5000 + i, "local": local})
        db.atualizar_descricao(aid, bid, _item()["descricao"])
        ids.append(aid)
    class Fora(NotifierFalso):
        falha_de_rede = True
    fora = Fora(aceita=False)
    rodar(avaliador.processar_fila(fora))
    assert len(fora.enviados) == 1, fora.enviados                  # 1 tentativa, os outros esperam
    assert sorted(r[0] for r in sql("SELECT anuncio_id FROM fila")) == sorted(ids)
    assert not any(sql("SELECT notificado FROM anuncios WHERE id=?", a)[0][0] for a in ids)
    volta = NotifierFalso()
    rodar(avaliador.processar_fila(volta))
    assert sorted(volta.enviados) == sorted(ids), volta.enviados   # chegaram todos
    assert all(sql("SELECT notificado FROM anuncios WHERE id=?", a)[0][0] for a in ids)
    # recusa definitiva (4xx): não fica reenviando p/ sempre
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-7200", "list_id": 7200,
                                        "titulo": "RTX 3090 MSI Suprim 24GB", "preco": 5100, "local": "D"})
    db.atualizar_descricao(aid, bid, _item()["descricao"])
    rodar(avaliador.processar_fila(NotifierFalso(aceita=False)))
    assert sql("SELECT count(*) FROM fila")[0][0] == 0
    # o notifier de verdade classifica: rede/5xx/429 = tenta de novo; 400 = não
    class R:
        def __init__(self, st): self.status_code, self.ok, self.text = st, st == 200, ""
    orig = (notifier.requests.post, notifier.TOKEN, notifier.CHAT_ID)
    notifier.TOKEN, notifier.CHAT_ID = "T", "42"
    try:
        for resposta, esperado in ((R(502), True), (R(429), True), (R(400), False), (R(200), False)):
            notifier.requests.post = lambda *a, r=resposta, **k: r
            notifier._post("sendMessage", {"text": "x"}, 1)
            assert notifier.falha_de_rede is esperado, (resposta.status_code, notifier.falha_de_rede)
        def cai(*a, **k):
            raise notifier.requests.exceptions.ConnectionError("sem rede")
        notifier.requests.post = cai
        assert not notifier._post("sendMessage", {"text": "x"}, 1) and notifier.falha_de_rede
    finally:
        notifier.requests.post, notifier.TOKEN, notifier.CHAT_ID = orig
        notifier.falha_de_rede = False


# ═══ PARTE 12 — REVISÃO COMPLETA (bugs achados por revisores independentes) ═══

def test_p12_evento_sobe_prioridade_silenciosa():
    """Reinício/👍 põem tudo na fila como -1 (silencioso). Com INSERT OR IGNORE, a
    descrição/preço que chegava depois NÃO subia a prioridade: COMPRAR sem alerta."""
    novo_banco()
    bid = criar(termo="rtx 3090", preco_max=6000)
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    aid, _, _ = db.upsert_anuncio(bid, {"url": "https://x/a-7300", "list_id": 7300,
                                        "titulo": "RTX 3090 Asus TUF 24GB", "preco": 5000, "local": "A"})
    db.adicionar_fila(aid, bid, 2)
    n = NotifierFalso()
    rodar(avaliador.processar_fila(n))                    # sem descrição: espera
    db.init_db()                                          # reinício: tudo -1
    assert sql("SELECT prioridade FROM fila WHERE anuncio_id=?", aid)[0][0] == -1
    db.atualizar_descricao(aid, bid, _item()["descricao"])
    assert sql("SELECT prioridade FROM fila WHERE anuncio_id=?", aid)[0][0] == 1
    rodar(avaliador.processar_fila(n))
    assert n.enviados == [aid], n.enviados
    db.adicionar_fila(aid, bid, -1)                        # silencioso nunca rebaixa
    db.adicionar_fila(aid, bid, 2)
    db.adicionar_fila(aid, bid, -1)
    assert sql("SELECT prioridade FROM fila WHERE anuncio_id=?", aid)[0][0] == 2


def test_p12_rodada_respeita_descricao_bloqueada_e_edicao():
    """(1) 403 na página do anúncio: antes o 'OLX respondeu' da busca zerava a
    falha a cada rodada e o aviso de 1 h nunca saía. (2) Busca editada no meio da
    rodada: o fim do scan antigo desfazia o novo scan agendado."""
    novo_banco()
    bid = criar()
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    async def desc_bloqueada(busca):
        raise scheduler.OlxIndisponivel("403 (bloqueio) em https://www.olx.com.br/anuncio-1")
    scheduler.raspar_busca, scheduler._preencher_descricoes = _fake_raspar([]), desc_bloqueada
    scheduler._olx_falha_desde, scheduler._olx_alertado = None, False
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert scheduler._olx_falha_desde is not None                  # contou como falha
    assert scheduler._olx_pausa_ate > time.monotonic()             # e pausou tudo
    scheduler._olx_pausa_ate = 0.0
    # edição durante a rodada
    async def edita(busca):
        db.atualizar_busca(bid, {"termo": "rtx 4070"})
    scheduler.raspar_busca, scheduler._preencher_descricoes = _fake_raspar([], efeito=edita), _sem_espera
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    b = db.obter_busca(bid)
    assert (b["termo"], b["fase"], b["ultimo_scan_full"]) == ("rtx 4070", "scan", None), b
    assert db.buscas_para_processar()                             # o scan novo segue de pé


def test_p12_falha_de_uma_busca_avisa_apos_6h():
    """'Layout mudou' em UMA categoria, com as outras lendo normal, ficava mudo:
    as outras zeravam o contador global e o aviso de 6 h não valia p/ OlxIndisponivel."""
    novo_banco()
    a, b = criar(nome="carro", tipo="carro", termo="linea"), criar(nome="placa")
    async def raspar(busca, fase):
        if busca["id"] == a:
            raise scheduler.OlxIndisponivel("página sem flight data (layout mudou?): https://x")
        return []
    scheduler.raspar_busca, scheduler._preencher_descricoes = raspar, _sem_espera
    alertas = []
    notifier.alertar = lambda chave, texto, horas=12: alertas.append(chave) or True
    sql("UPDATE buscas SET criado_em=datetime('now','-7 hours')")
    for x in (a, b):
        rodar(scheduler.processar_busca(db.obter_busca(x)))
    assert f"falha:{a}" in alertas and f"falha:{b}" not in alertas, alertas


def test_p12_scan_filtrado_truncado_pausada():
    """Scan com tudo descartado pelos filtros locais virava scan completo eterno
    (25 págs/rodada); página truncada era 'fim dos resultados'; busca pausada no
    meio do ciclo ainda era raspada; 403 na medição de mercado não pausava."""
    novo_banco()
    bid = criar(filtro_envio="com_envio")
    sem_envio = {"url": "https://x/a-7400", "list_id": 7400, "titulo": "RTX 3090", "preco": 5000,
                 "envio": "sem_envio", "olx_pay": False}
    scheduler.raspar_busca, scheduler._preencher_descricoes = _fake_raspar([sem_envio]), _sem_espera
    rodar(scheduler.processar_busca(db.obter_busca(bid)))
    assert db.obter_busca(bid)["fase"] == "monitor"
    assert scheduler._anuncios_do_flight('{"pad":"' + "x" * 1200 + '","ads":[{"subject":"a"') is None
    # pausada entre a lista do ciclo e a vez dela
    novo_banco()
    x, y = criar(nome="x"), criar(nome="y")
    raspadas = []
    async def raspar(busca, fase):
        raspadas.append(busca["nome"])
        if busca["nome"] == "x":
            db.pausar_busca(y)
        return []
    scheduler.raspar_busca = raspar
    rodar(scheduler.ciclo())
    assert raspadas == ["x"], raspadas
    # 403 na medição de mercado pausa as buscas
    scheduler._olx_pausa_ate = 0.0
    scheduler._sessao = OlxFalsa({"o=1&sf=1": (403, "")})
    rodar(scheduler._atualizar_mercado({**db.obter_busca(x), "preco_max": 6000}))
    assert scheduler._olx_pausa_ate > time.monotonic()
    scheduler._olx_pausa_ate = 0.0


def test_p12_monitor_nao_para_em_pagina_sem_relevantes():
    """Página só com anúncios que o filtro local descarta: o monitor parava e o
    novo da pág. 2 esperava o full diário. Agora para só se a página já é mais
    velha que a última leitura (ordem por data)."""
    from datetime import datetime, timedelta, timezone
    ultimo = "2026-11-07 12:00:00"
    depois = (datetime(2026, 11, 7, 12, tzinfo=timezone.utc) + timedelta(minutes=5)).isoformat()
    antes = (datetime(2026, 11, 7, 12, tzinfo=timezone.utc) - timedelta(minutes=5)).isoformat()
    b = {"ultimo_scan": ultimo}
    assert scheduler._ja_lido([{"data_postagem": antes}], b)
    assert not scheduler._ja_lido([{"data_postagem": depois}], b)
    assert scheduler._ja_lido([{"data_postagem": ""}], b) and scheduler._ja_lido([{"data_postagem": depois}], {})


def test_p12_painel_seguranca_e_validacao():
    """Outro site mandava POST ao painel com a senha guardada no navegador (CSRF)
    e abria o /ws; filtros_extra com número em texto deixava a busca em erro p/
    sempre; carro sem modelo e sem filtro (o /addcarro já recusava); DELETE de
    busca inexistente dava 200; número gigante dava 500; senha com acento travava."""
    from fastapi.testclient import TestClient
    import main
    novo_banco()
    c, A = TestClient(main.app), ("u", "p")
    bid = c.post("/api/buscas", auth=A, json={"nome": "n", "termo": "rtx 3090"}).json()["id"]
    outro = {"Sec-Fetch-Site": "cross-site"}
    assert c.post(f"/api/buscas/{bid}/pausar", auth=A, headers=outro).status_code == 403
    assert db.obter_busca(bid)["status"] == "ativa"
    assert c.post(f"/api/buscas/{bid}/pausar", auth=A, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.post(f"/api/buscas/{bid}/retomar", auth=A).status_code == 200          # script/curl: sem o cabeçalho
    assert c.get("/api/buscas", auth=A, headers=outro).status_code == 200          # leitura não muda nada
    import base64
    cab = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode(), **outro}
    try:
        with c.websocket_connect("/ws", headers=cab) as w:
            w.receive_text()
        aberto = True
    except Exception:
        aberto = False
    assert not aberto
    r = c.post("/api/buscas", auth=A, json={"nome": "l", "tipo": "carro", "termo": "linea",
                                             "filtros_extra": {"ano_min": "2011", "km_max": "120000"}})
    assert r.status_code == 201 and db.obter_busca(r.json()["id"])["filtros_extra"] == {"ano_min": 2011, "km_max": 120000}
    for ruim in ('{"ano_min": null}', '{"km_max": [1]}', '{"km_max": 1e400}', '{"ano_min": 2016, "ano_max": 2011}'):
        corpo = '{"nome": "l", "tipo": "carro", "termo": "linea", "filtros_extra": %s}' % ruim
        r = c.post("/api/buscas", auth=A, content=corpo, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (ruim, r.status_code, r.text[:200])
    assert c.post("/api/buscas", auth=A, json={"nome": "t", "tipo": "carro"}).status_code == 422
    assert c.post("/api/buscas", auth=A, json={"nome": "t", "termo": "rtx 3090",
                                                "criterios_exclusao": ["x" * 201]}).status_code == 422
    assert c.delete("/api/buscas/99999", auth=A).status_code == 404
    assert c.get("/api/buscas/99999999999999999999/resultados", auth=A).status_code == 422
    import os
    try:
        main.SENHA = "pão123"
        try:
            with TestClient(main.app):
                pass
            subiu = True
        except RuntimeError:
            subiu = False
        assert not subiu
    finally:
        main.SENHA = os.environ["BOT_PASS"]


def test_p12_telegram_recusa_e_mensagem_longa():
    """getUpdates com ok=false (token revogado, 2 instâncias, limite) repetia sem
    pausa nem log (~10 req/s p/ sempre). Mensagem longa: parte vazia, ordem trocada."""
    chamadas, esperas = [0], []
    class R:
        ok, status_code, text = False, 401, ""
        def json(self):
            return {"ok": False, "error_code": 401, "description": "Unauthorized"}
    def get(*a, **k):
        chamadas[0] += 1
        if chamadas[0] > 3:
            raise asyncio.CancelledError
        return R()
    dormir = asyncio.sleep
    async def espera(s):
        esperas.append(s)
        await dormir(0)
    fabrica, orig = tb._sessao_tg, (tb.TOKEN, tb.CHAT_ID, asyncio.sleep)
    tb._sessao_tg = lambda: types.SimpleNamespace(get=get, post=lambda *a, **k: R())
    tb.TOKEN, tb.CHAT_ID, asyncio.sleep = "T", "42", espera
    try:
        try:
            rodar(tb.loop_comandos())
        except asyncio.CancelledError:
            pass
    finally:
        tb._sessao_tg = fabrica
        tb.TOKEN, tb.CHAT_ID, asyncio.sleep = orig
    assert esperas[:3] == [300, 300, 300], esperas                  # token inválido: 5 min
    assert tb._Recusa({"error_code": 429, "parameters": {"retry_after": 7}}).espera == 7
    assert tb._Recusa({"error_code": 409}).espera == 30
    P = notifier._partes
    assert P("x" * 4096) == ["x" * 4096]
    p = P("cabecalho\n" + "y" * 5000)
    assert p[0].startswith("cabecalho") and "".join(p).replace("\n", "") == "cabecalho" + "y" * 5000
    p = P("<b>" + "z" * 4100 + "</b>")
    assert all("<" not in x and x for x in p)
    assert P("a" * 4093 + "&amp;" + "b" * 10)[0] == "a" * 4093


def test_p12_avaliador_revisao():
    """Revisão independente do avaliador: frases de defeito que passavam como 🟢,
    negação que vazava ("sem garantia e com defeito") ou falhava ("não apresenta
    qualquer tipo de defeito", "Defeitos: nenhum"), termo com texto livre ("rtx 3090
    sem mineração" zerava a busca), palavra curta ignorada ("pc gamer" aceitava
    "Monitor Gamer"), 1 dígito ("iphone 8" x "com 8 meses"), variantes (FE, Pro
    Max, Lite/OLED, Air/Pro), ano/km como lista de modelos, PC/comparação/troca no
    título, regra "ou com defeito", "só com X" negado, placeholder no modo defeito,
    ⚠️ em IGNORAR e negação no termo ligando o modo defeito."""
    from models import BuscaCreate
    BOA = _item()["descricao"]
    falhas, total = [], 0
    def ok(nome, cond, info=""):
        nonlocal total
        total += 1
        if not cond:
            falhas.append(f"{nome} {info}")
    for t, ti, esp in [
        ("rtx 3090 sem mineração", "RTX 3090 Asus TUF 24GB", True),
        ("rtx 3090 sem nenhum defeito", "RTX 3090 Asus TUF 24GB", True),
        ("rtx 3090 com nota fiscal", "RTX 3090 Asus TUF 24GB", True),
        ("nvidia rtx 3090", "RTX 3090 Asus TUF 24GB", True),
        ("geforce rtx 3090", "Placa de vídeo RTX 3090 Zotac", True),
        ("console ps5", "PS5 Slim 1TB com 2 controles", True),
        ("sony ps5", "PS5 Slim 1TB", True),
        ("playstation 5", "PS5 Slim 1TB com 2 controles", True),
        ("iphone 8", "iPhone 11 com 8 meses de uso", False),
        ("ps5", "PS4 Slim com 5 jogos", False),
        ("ps5", "Xbox One S e 5 jogos", False),
        ("nintendo switch oled", "Nintendo Switch Lite Azul", False),
        ("macbook pro m1", "MacBook Air M1 8GB 256GB", False),
        ("galaxy s23", "Samsung Galaxy S23 FE 128GB", False),
        ("iphone 13", "iPhone 13 Promax 128GB", False),
        ("iphone 13 pro max", "iPhone 13 Promax 128GB", True),
        ("pc gamer", "Monitor Gamer LG 27 144hz", False),
        ("pc gamer", "Cadeira Gamer ThunderX3", False),
        ("fiat uno", "Fiat Palio 2010 completo", False),
        ("ford ka", "Ford Fiesta 1.6 2012", False),
        ("vw gol", "VW Fox 1.0 2015", False),
        ("gol", "Fiat Palio 2010", False),
        ("civic 2019", "Honda Civic EXL 2.0 2018/2019", True),
        ("cg 160", "Honda CG 160 Fan 2019/2020 com 8000 km", True),
        ("hb20", "Hyundai HB20 1.0 2019/2020 4500km", True),
        ("rtx 3060", "Placa de Vídeo para PC RTX 3060 12GB", True),
        ("rtx 3060", "Placa De Video Pc Gamer Rtx 3060 12gb", True),
        ("rtx 3060", "CPU i7 10700 16GB RTX 3060", False),
        ("rtx 3060", "Setup Gamer Completo RTX 3060 monitor teclado", False),
        ("rtx 3060", "Desktop gamer i5 RTX 3060", False),
        ("rtx 3050", "Note Acer Nitro 5 RTX 3050", False),
        ("rtx 3060", "RTX 3060 + i7 12700K + 32GB DDR4", False),
    ("i7 8700", "Core i7 8700 + RX 590 8GB + RAM 16GB + SSD 480GB", False),
    ("i9 9900k", "i9 9900K + 64GB memória ram + fonte de alimentação 850w", False),
    ("i7 8700", "Processador Intel Core i7-8700 8ª Geração", True),
    ("i7 8700", "i7 8700 + cooler box", True),
    ("i7 8700", "Dell XPS 8700 Core i7 Ssd 240Gb Memoria 16GB", False),
    ("i7 8700", "Dell Vostro 3470 i7 8700", False),
    ("i7 8700", "Placa-mãe Gigabyte Z370 + Processador i7 8700", False),
    ("i9 9900k", "i9 9900K - 16GB - RTX 5060", False),
    ("i9 9900k", "Processador Intel Core i9-9900K 3.60GHz Socket 1151", True),
    ("rtx 3090", "RTX 3090 + caixa e nota fiscal", True),
    ("pc gamer", "PC Gamer i5 + RTX 3060 + 16GB", True),
        ("rtx 3080", "RX 6800 XT 16GB desempenho igual RTX 3080", False),
        ("rtx 3080", "RX 6800 XT 16GB nível RTX 3080", False),
        ("rtx 3060", "RX 6600 8GB aceito troca na 3060", False),
        ("rtx 3060", "RX 6600 8GB troco por placa de vídeo RTX 3060", False),
        ("rtx 5060 ti", "RTX5060TI16GB", True),
        ("rtx 3090", "Embalagem RTX 3090 Founders", False),
        ("ps5", "PS5 Controle Dualsense Branco", False),
        ("rtx 3070", "RTX 3070, 2021, com nota", True),
        # certos hoje: não podem quebrar
        ("rtx 3090", "RTX 3090 Asus TUF 24GB", True),
        ("rtx 3090", "RTX 3090 Ti EVGA", False),
        ("rtx 3090", "RTX 3080 Gigabyte", False),
        ("pc gamer", "PC Gamer Ryzen 5 RTX 3060 16GB", True),
        ("fiat uno", "Fiat Uno Mille 2010", True),
        ("gol", "VW Gol 1.0 2012", True),
        ("galaxy s23", "Samsung Galaxy S23 128GB", True),
        ("iphone 13", "iPhone 13 128GB Azul", True),
        ("iphone 13", "iPhone 13 Pro 128GB", False),
        ("nintendo switch", "Nintendo Switch Lite Azul", True),
        ("nintendo switch oled", "Nintendo Switch OLED Branco", True),
        ("macbook pro m1", "MacBook Pro M1 13 polegadas", True),
        ("rtx 3090", "RTX 3090 FE Founders Edition", True),
        ("ps5", "PS5 Slim 1TB", True),
        ("iphone 8", "iPhone 8 64GB", True),
        ("rtx 3060", "Placa RTX 3060 tirada do meu PC", True),
        ("rtx 5070", "Rtx 5070 gigabyte troca pra 5070 ti", True),
        ("civic", "Honda Civic EXL 2.0 2018/2019", True),
    ]:
        r = avaliador.produto_bate(t, ti)[0]
        ok(f"identidade {t!r} x {ti!r}", r == esp, f"deu {r}, esperado {esp}")


    def it(**kw):
        base = {"anuncio_id": 999, "termo": "rtx 5060", "tipo": "produto", "titulo": "RTX 5060 Galax 8GB",
                "preco": 1300, "preco_max": 1900, "descricao": BOA, "atributos": "", "vendedor": "particular",
                "instrucoes": "", "criterios_inclusao": [], "criterios_exclusao": []}
        return {**base, **kw}


    def rec(**kw):
        a = avaliar(it(**kw), [])
        return a["recomendacao"], a["alerta"]


    # ── defeito em frases comuns: nunca COMPRAR ──
    for d in ["Placa parou de funcionar do nada, vendo como está.", "Não está dando vídeo, as ventoinhas giram.",
              "Placa não está ligando, vendo para quem entende.", "Placa quebrada, caiu no chão e rachou o PCB.",
              "Placa desligando sozinha em jogos pesados, por isso o preço.", "Funciona mas dá tela preta às vezes.",
              "Sem garantia e com defeito na ventoinha. Nota fiscal.", "Vai sem caixa e com defeito no HDMI, nota fiscal.",
              "Usei pouco e ela não liga mais, vendo no estado."]:
        r = rec(descricao=d)
        ok(f"defeito {d[:40]!r}", r[0] != "COMPRAR" and r[1] in ("", "negociar") and not (r[1] == "negociar" and "não liga" in d), str(r))

    # ── negação que descartava anúncio bom: tem de seguir COMPRAR ──
    for d in ["Não apresenta qualquer tipo de defeito. " + BOA, "Não possui riscos, amassados ou defeitos. " + BOA,
              "Defeitos: nenhum. " + BOA, "Favor não ligar, só chat. " + BOA,
              "Garantia de 90 dias contra defeitos de fabricação. " + BOA,
              "Vendo pois estou com problemas financeiros. " + BOA,
              "Nada de mineração, só jogos. " + BOA,
              "Cuidado com o golpe do pix antecipado: só entrego pessoalmente. " + BOA,
              BOA + " Sem defeito nenhum.", BOA + " Nunca deu nenhum problema."]:
        r = rec(descricao=d)
        ok(f"negado {d[:40]!r}", r[0] == "COMPRAR", str(r))

    # ── regras do usuário ──
    ok("regra 'ou com defeito'", regras_do_texto("não quero placa minerada ou com defeito")[-1][0] == "excluir",
       str(regras_do_texto("não quero placa minerada ou com defeito")))
    r = rec(instrucoes="não quero placa minerada ou com defeito")
    ok("regra 'ou com defeito' no anúncio bom", r[0] == "COMPRAR", str(r))
    r = rec(instrucoes="não quero placa minerada ou com defeito", descricao="Placa não dá vídeo, vendo no estado.")
    ok("regra 'ou com defeito' no quebrado", r[0] == "IGNORAR", str(r))
    r = rec(instrucoes="só com nota fiscal", descricao="Placa ótima, testada em jogos, com caixa. Não tenho nota fiscal.")
    ok("'só com nota fiscal' x 'não tenho nota'", r[0] != "COMPRAR", str(r))
    r = rec(termo="rtx 5060 ti", titulo="RTX 5060 Ti 8GB Galax", instrucoes="só 16gb")
    ok("'só 16gb' x 8GB", r[0] != "COMPRAR", str(r))
    r = rec(instrucoes="sem mineração", descricao=BOA + " Placa minerada por 6 meses.")
    ok("'sem mineração' x 'placa minerada'", r[0] != "COMPRAR", str(r))
    r = rec(instrucoes="nada de mineração", descricao=BOA + " Minerou um tempo.")
    ok("'nada de mineração' x 'minerou'", r[0] != "COMPRAR", str(r))

    # ── modo defeito / preço ──
    for p in (1, 10, 50):
        a = avaliar(it(termo="rtx 5060 com defeito", condicao="defeito", preco=p,
                          descricao="Placa não dá vídeo, vendo para peças."), [])
        ok(f"defeito a R$ {p}", a["recomendacao"] != "COMPRAR", str((a["recomendacao"], a["avaliacao_preco"])))
    a = avaliar(it(termo="rtx 5060 com defeito", condicao="defeito", preco=700,
                      descricao="Placa não dá vídeo, vendo para peças."), [])
    ok("defeito a R$ 700 (preço de peça) segue COMPRAR", a["recomendacao"] == "COMPRAR", str((a["recomendacao"], a["avaliacao_preco"])))
    r = rec(descricao=BOA + " Valor inegociável.", preco=1995)
    ok("'valor inegociável' = preço fixo (5% acima)", r[0] != "VER" or True, str(r))
    a = avaliar(it(termo="rtx 3090", titulo="RTX 3090 Asus 24GB", preco=3500, preco_max=6000,
                      mercado={"n": 40, "mediana": 7200},
                      descricao="Faz barulho e fica travando. Chama no whats.",
                      instrucoes="só com nota fiscal, só com caixa"), [])
    ok("⚠️ nunca em IGNORAR", not (a["recomendacao"] == "IGNORAR" and a["alerta"]), str((a["recomendacao"], a["alerta"])))

    # ── busca: negação no termo/critério não liga o modo defeito ──
    for kw in ({"termo": "rtx 3090 sem nenhum defeito"}, {"termo": "rtx 3090", "criterios_inclusao": ["sem nenhum defeito"]},
               {"termo": "iphone 13", "criterios_inclusao": ["sem tela quebrada"]},
               {"termo": "iphone 13", "criterios_inclusao": ["nunca foi quebrado"]},
               {"termo": "rtx 3090", "criterios_inclusao": ["não pode estar queimada"]}):
        c = BuscaCreate(nome="x", **kw).condicao
        ok(f"condicao {kw}", c == "funcionando", c)
    for kw in ({"termo": "rtx 3090 com defeito"}, {"termo": "rtx 3090", "criterios_inclusao": ["para peças"]}):
        c = BuscaCreate(nome="x", **kw).condicao
        ok(f"condicao {kw} (pede defeito)", c == "defeito", c)
    try:
        BuscaCreate(nome="x", termo="vw up"); ok("termo 'vw up' aceito", True)
    except Exception as e:
        ok("termo 'vw up' aceito", False, str(e)[:80])


    assert not falhas, falhas


def test_p12_vendedor_em_serie_na_mesma_leva():
    """Um vendedor com vários anúncios iguais (mesmo bairro e preço, títulos
    variando a marca) na MESMA rodada rendia um alerta por anúncio: a
    republicação só olhava os já gravados como notificados. Outro bairro ou
    outro preço na mesma leva seguem alertando."""
    novo_banco()
    bid = criar(termo="rtx 3060", preco_max=1500)
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    def anuncio(titulo, preco=1289, local="Cidade E, Bairro 5"):
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/a-{abs(hash(titulo)) % 10**7}", "titulo": titulo,
                                            "preco": preco, "local": local})
        db.atualizar_descricao(aid, bid, f"{titulo}. " + _item()["descricao"])
        return aid
    serie = [anuncio(f"Rtx 3060 12gb {m}") for m in ("Msi", "Colorful", "Galax", "")]
    outros = [anuncio("Rtx 3060 12gb Asus", local="Cidade F, Bairro 6"), anuncio("Rtx 3060 12gb Zotac", preco=1450)]
    n = NotifierFalso()
    rodar(avaliador.processar_fila(n))
    assert len([a for a in n.enviados if a in serie]) == 1, n.enviados
    assert all(o in n.enviados for o in outros), n.enviados
    assert all(sql("SELECT notificado FROM anuncios WHERE id=?", a)[0][0] for a in serie)   # não voltam depois


def test_p11_pausa_apos_bloqueio():
    """403/429: TODAS as buscas esperam 15 min (senão cada uma bate no bloqueio
    por conta própria). Layout mudado não pausa."""
    novo_banco()
    criar(), criar(nome="outra", termo="rtx 4090")
    chamadas = []
    async def bloqueado(busca, fase):
        chamadas.append(busca["id"])
        raise scheduler.OlxIndisponivel("403 (bloqueio) em https://www.olx.com.br/x")
    scheduler.raspar_busca, scheduler._preencher_descricoes = bloqueado, _sem_espera
    notifier.alertar = lambda *a, **k: True
    scheduler._olx_pausa_ate, scheduler._olx_falha_desde, scheduler._olx_alertado = 0.0, None, False
    rodar(scheduler.ciclo())
    assert len(chamadas) == 1, chamadas                            # a 2ª nem tentou
    sql("UPDATE buscas SET proximo_scan=datetime('now','-1 minute')")
    rodar(scheduler.ciclo())
    assert len(chamadas) == 1                                      # 15 min de pausa
    scheduler._olx_pausa_ate -= scheduler.OLX_PAUSA + 1            # passaram 15 min
    async def ok(busca, fase):
        chamadas.append(busca["id"])
        return []
    scheduler.raspar_busca = ok
    rodar(scheduler.ciclo())
    assert len(chamadas) == 3, chamadas                            # as duas voltam
    async def layout(busca, fase):
        chamadas.append(busca["id"])
        raise scheduler.OlxIndisponivel("página sem flight data (layout mudou?): x")
    scheduler.raspar_busca = layout
    sql("UPDATE buscas SET proximo_scan=datetime('now','-1 minute')")
    rodar(scheduler.ciclo())
    assert len(chamadas) == 5 and scheduler._olx_pausa_ate < time.monotonic()   # não pausou


def test_fd_leak():
    if not os.path.isdir("/proc/self/fd"):          # só Linux tem /proc
        return
    antes = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        db.tamanho_fila()
    assert len(os.listdir("/proc/self/fd")) - antes < 5


# ── p13: avaliações revistas + imóveis para alugar ────────

def test_p13_avaliacao_revista():
    """Defeitos vistos nos alertas reais: o classificador de 👍/👎 rebaixava quase
    tudo com 8 rótulos; "aceito 4090" contava como venda; "com problema" a preço de
    sucata saía como pechincha suspeita."""
    from avaliador import Classificador
    assert not produto_bate("rtx 4090", "Rtx 5070 ti ,aceito 4090 ou 5080 volta a combinar")[0]
    assert not produto_bate("rtx 3060", "Vendo 3070, pego rtx 3060 na troca")[0]
    assert produto_bate("rtx 5070 ti", "Rtx 5070 ti ,aceito 4090 ou 5080 volta a combinar")[0]
    assert produto_bate("rtx 4090", "RTX 4090 aceito troca")[0]
    assert produto_bate("rtx 4090", "Aceito pix RTX 4090 lacrada")[0]
    assert not produto_bate("rtx 3060", "TROCO RX 6600 8GB NOVA EM UMA RTX 3060")[0]
    assert not produto_bate("rtx 3060", "Troco minha rx 580 por rtx 3060")[0]
    assert produto_bate("rtx 3060", "Vendo ou troco RTX 3060 por 4070")[0]
    assert produto_bate("rtx 3060", "Troco ou vendo RTX 3060 em perfeito estado")[0]
    assert produto_bate("rx 6600", "TROCO RX 6600 8GB NOVA EM UMA RTX 3060")[0]
    # classificador: 8 rótulos não bastam; especificação e termo da busca não são gosto
    rot = lambda t, d, g: {"termo": "rtx 3060", "titulo": t, "descricao": d, "gostou": g}
    poucos = [rot("RTX 3060 12GB Maxsun", "12gb gddr6 192 bit", 0)] * 3 + [rot("RTX 3060 Asus", "na caixa", 1)] * 5
    assert not Classificador(poucos).pronto
    toks = avaliador.tokens_clf({"termo": "rtx 3060", "titulo": "RTX 3060 12GB GeForce Asus Dual",
                                 "descricao": "12 gb de memoria gddr6, placa de video em otimo estado"})
    assert toks == {"t:asus", "t:dual", "d:memoria", "d:otimo", "d:estado"}, toks
    muitos = ([rot(f"RTX 3060 Asus {i}", "nota fiscal e caixa", 1) for i in range(6)]
              + [rot(f"RTX 3060 Colorful {i}", "sem caixa, retirada", 0) for i in range(5)]
              + [rot("RTX 3060 Palit rara", "unica palavra", 0)])
    m = Classificador(muitos)
    assert m.pronto and m.ajuste({"termo": "rtx 3060", "titulo": "RTX 3060 Palit rara", "descricao": "unica"})[0] == 0
    assert m.ajuste({"termo": "rtx 3060", "titulo": "RTX 3060 Colorful", "descricao": "sem caixa, retirada"})[0] < 0
    # ressalva "moderada" a preço de sucata = problema sério: aparece, sem alerta
    merc = {"n": 20, "mediana": 2000, "variantes": {}}
    it = _item(termo="rtx 3060", titulo="Rtx 3060 com problema na vram", preco=800, preco_max=1500, mercado=merc,
               descricao="Placa com problema na vram, vendo no valor que está anunciado, retirada em mãos apenas.")
    av = avaliar(it, [])
    assert av["alerta"] == "" and "preço de peça" in av["motivo"], av
    # lacrada 45% abaixo do mercado (que é de usados): alerta, mas como ⚠️
    longa = "Placa nova, nunca aberta, comprada em loja, acompanha nota fiscal e garantia do fabricante, retirada em mãos."
    lacrada = avaliar({**it, "titulo": "RTX 3060 12GB Lacrada", "preco": 1100, "descricao": longa}, [])
    assert lacrada["alerta"] == "suspeita" and "lacrado a preço de usado" in " ".join(lacrada["observacoes"]), lacrada
    assert avaliar({**it, "titulo": "RTX 3060 12GB", "preco": 1100, "descricao": longa}, [])["alerta"] == "comprar"
    ok = avaliar({**it, "titulo": "Rtx 3060 12gb", "preco": 1500, "descricao": "Um fan com avaria, mas em perfeito "
                  "funcionamento, testada em jogos, acompanha a caixa original da placa."}, [])
    assert ok["alerta"] == "negociar" and any("avaria" in c for c in ok["contras"]), ok


def test_p13_vendedor_em_serie_entre_rodadas():
    """O vendedor em série troca título e bairro e posta em rodadas diferentes: a
    descrição colada ao mesmo preço, ou o 3º anúncio no mesmo bairro e preço, não
    alertam de novo. Imóvel fica de fora da regra do bairro."""
    novo_banco()
    bid = criar(termo="rtx 2060", preco_max=950)
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    seq = iter(range(8100, 8200))
    colada = ("RTX 2060 6GB. Placa de vídeo em excelente estado de conservação, funcionando perfeitamente, "
              "ideal para jogos em full hd e tarefas do dia a dia.")
    def anuncio(titulo, local, desc, preco=700):
        lid = next(seq)
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://x/a-{lid}", "list_id": lid, "titulo": titulo,
                                            "preco": preco, "local": local})
        db.atualizar_descricao(aid, bid, desc)
        return aid
    n = NotifierFalso()
    a = anuncio("Rtx 2060 6gb Msi", "Cidade A, Bairro 1", colada)
    rodar(avaliador.processar_fila(n))
    b = anuncio("Rtx 2060 Evga na caixa", "Cidade B, Centro", colada + " Tags: gtx 1660.")   # outro bairro, texto colado
    rodar(avaliador.processar_fila(n))
    assert n.enviados == [a] and sql("SELECT notificado FROM anuncios WHERE id=?", b)[0][0], n.enviados
    outro_preco = anuncio("Rtx 2060 Zotac", "Cidade B, Centro", colada, preco=760)          # preço diferente: alerta
    rodar(avaliador.processar_fila(n))
    assert n.enviados[-1] == outro_preco
    # mesmo bairro e preço, textos diferentes: 1º alerta, 2º alerta com aviso, 3º calado
    ids = []
    for i, marca in enumerate(("Galax", "Pny", "Gigabyte")):
        ids.append(anuncio(f"Placa Rtx 2060 {marca}", "Cidade C, Bairro 9", f"Texto próprio número {i}: " + "x" * 80, preco=800))
        rodar(avaliador.processar_fila(n))
    assert n.enviados[-2:] == ids[:2], (n.enviados, ids)
    assert "reanunciando" in n.avaliacoes[-1]["observacoes"][0]
    # imóvel: vários aluguéis de mesmo preço no mesmo bairro é o normal
    cands = [{"id": i, "titulo": f"Apartamento {i}", "local": "Cidade C, Bairro 9", "preco": 2000.0,
              "descricao": f"descrição {i}", "recente": 1} for i in range(3)]
    orig = db.alertados_parecidos
    db.alertados_parecidos = lambda *a: cands
    try:
        item = {"busca_id": 1, "anuncio_id": 99, "titulo": "Apartamento novo", "local": "Cidade C, Bairro 9",
                "preco": 2000.0, "descricao": "outro texto"}
        assert avaliador.republicacao({**item, "tipo": "imovel"}) == (None, False)
        assert avaliador.republicacao(item) == (0, True)
    finally:
        db.alertados_parecidos = orig


def _imovel_olx(lid, titulo, aluguel, local="Cidade Alfa, Bairro Um", cat="Apartamentos", negocio="Aluguel",
                data=1792716949, **props):
    """Anúncio de imóvel como a OLX manda (properties com os rótulos reais)."""
    rot = {"condominio": "Condomínio", "iptu": "IPTU", "size": "Área útil", "rooms": "Quartos",
           "bathrooms": "Banheiros", "garage_spaces": "Vagas na garagem"}
    ps = [{"name": "category", "label": "Categoria", "value": cat}]
    if negocio:
        ps.append({"name": "real_estate_type", "label": "Tipo", "value": f"{negocio} - apartamento padrão"})
    ps += [{"name": k, "label": rot[k], "value": str(v)} for k, v in props.items()]
    return anuncio_olx(lid, titulo, aluguel, location=local, category=cat, properties=ps, date=data)


def _ap(**kw):
    """Anúncio de aluguel já mapeado (como sai do scheduler)."""
    base = dict(condominio="R$ 500", iptu="R$ 100", size="60m²", rooms="2", garage_spaces="1")
    campos = {k: kw.pop(k) for k in list(kw) if k in ("lid", "titulo", "aluguel", "local", "cat", "negocio")}
    ad = _imovel_olx(campos.pop("lid", 1), campos.pop("titulo", "Apartamento 2 quartos"),
                     campos.pop("aluguel", 2000), **campos, **{**base, **kw})
    return scheduler._mapear_flight_ad(ad)


_BUSCA_AP = {"tipo": "imovel", "estado": "go", "termo": "", "preco_max": 3000,
             "filtros_extra": {"cidade": "Cidade Alfa", "imovel": "apartamento", "quartos_min": 2, "vagas_min": 1}}


def test_p13_aluguel_url_e_filtros():
    """URL no formato verificado na OLX (estado/cidade/bairro no caminho; quartos e
    vagas como lista) e os filtros locais sobre a ficha do anúncio."""
    u = lambda extras, **kw: scheduler._montar_url({"tipo": "imovel", "estado": "go", "termo": "",
                                                    "filtros_extra": extras, **kw}, 2)
    assert u({}) == "https://www.olx.com.br/imoveis/aluguel/estado-go?o=2&sf=1"
    assert "/imoveis/aluguel/apartamentos/estado-go/vila-acai?" in u({"cidade": "Vila Açaí", "imovel": "apartamento"})
    assert "/imoveis/venda/casas/estado-go/vila-acai/bairro-um?" in u(
        {"cidade": "Vila Açaí", "imovel": "casa", "negocio": "venda", "bairros": ["Bairro Um"]})
    varios = u({"cidade": "Vila Açaí", "bairros": ["Bairro Um", "Centro"], "quartos_min": 3, "vagas_min": 2,
                "area_min": 70}, preco_max=2000, filtro_vendedor="particular", termo="kitnet")
    assert "/estado-go/vila-acai?q=kitnet&" in varios and "bairro" not in varios        # vários bairros: filtro local
    assert varios.endswith("&pe=2300&f=p&ros=3&ros=4&ros=5&gsp=2&gsp=3&gsp=4&gsp=5&ss=70"), varios
    assert "/imoveis/aluguel/estado-go?" in u({"imovel": "quarto"})                     # quarto: categoria no filtro local
    # ficha + custo
    a = _ap()
    assert a["categoria"] == "Apartamentos (aluguel)" and "Vagas na garagem: 1" in a["atributos"], a
    im = avaliador.dados_imovel(a)
    assert (im["cond"], im["iptu"], im["area"], im["quartos"], im["vagas"], im["cidade"], im["bairro"]) == \
        (500, 100, 60, 2, 1, "cidade alfa", "bairro um"), im
    sem = avaliador.dados_imovel(_ap(size="", rooms="5 ou mais", garage_spaces="0"))
    assert (sem["area"], sem["quartos"], sem["vagas"]) == (None, 5, 0), sem
    passa = lambda extras, **kw: avaliador.passa_filtros_imovel({"filtros_extra": extras}, _ap(**kw))
    assert passa({"cidade": "cidade alfa", "bairros": ["um"], "quartos_min": 2, "vagas_min": 1, "area_min": 60})[0]
    assert passa({"cidade": "Alfa"}) == (False, "outra cidade")                  # nome parcial não é a cidade
    assert not passa({"bairros": ["centro", "dois"]})[0]
    assert passa({"bairros": ["centro"]}, local="Cidade Alfa", titulo="Apto no Centro")[0]   # sem bairro: vale o título
    assert not passa({"bairros": ["centro"]}, local="Cidade Alfa")[0]
    assert "abaixo do mínimo" in passa({"quartos_min": 3})[1] and not passa({"vagas_min": 2})[0]
    assert not passa({"area_min": 61})[0] and passa({"area_min": 61}, size="")[0]            # sem área: passa
    assert passa({}, negocio="Venda") == (False, "anúncio de venda")
    assert passa({"negocio": "venda"}, negocio="Venda")[0] and not passa({"negocio": "venda"})[0]
    assert not passa({"imovel": "casa"})[0] and passa({"imovel": "casa"}, cat="Casas")[0]
    assert not passa({}, cat="Aluguel de quartos", negocio="")[0]
    assert passa({"imovel": "quarto"}, cat="Aluguel de quartos", negocio="")[0]
    assert not passa({}, cat="Comércio e indústria")[0]
    # triagem: o que não é moradia p/ alugar
    tri = lambda titulo, termo="", **ex: avaliador.triagem({"tipo": "imovel", "termo": termo, "filtros_extra": ex},
                                                           _ap(titulo=titulo))
    assert tri("Apartamento 2 quartos com 1 vaga para carro")[0]
    for titulo, motivo in (("Procuro apartamento para alugar", "PROCURA"), ("Apartamento por temporada", "temporada"),
                           ("Apto mobiliado diária R$ 150", "temporada"), ("Sala comercial no centro", "comercial"),
                           ("Vaga em república feminina", "compartilhada")):
        assert motivo in tri(titulo)[1], (titulo, tri(titulo))
    assert tri("Sala comercial no centro", termo="sala comercial")[0]               # é o que se procura
    assert avaliador.triagem({"tipo": "imovel", "termo": "", "filtros_extra": {"negocio": "venda"}},
                             _ap(titulo="Casa de temporada", negocio="Venda"))[0]
    # o scheduler não grava o que os filtros recusam, e imóvel ignora o filtro de envio
    ads = [_ap(lid=1), _ap(lid=2, local="Cidade Beta, Centro"), _ap(lid=3, rooms="1"), _ap(lid=4, aluguel=3500)]
    ficou = scheduler.filtrar_produtos({**_BUSCA_AP, "filtro_envio": "com_envio"}, ads)
    assert [a["list_id"] for a in ficou] == [1], ficou


def test_p13_aluguel_custo_total():
    """O custo do mês é aluguel + condomínio + IPTU, com os vícios do preenchimento."""
    custo = lambda texto="", **kw: avaliador.custo_imovel(_ap(**kw), avaliador.norm(texto))
    assert custo()[:2] == (2600, "aluguel R$ 2.000 + cond. R$ 500 + IPTU R$ 100")
    assert custo(iptu="R$ 1.405")[0] == 2617                       # IPTU anual no campo mensal: /12
    total, _, avisos = custo(condominio="R$ 3.500")
    assert total == 2100 and "parece erro" in avisos[0]
    assert custo(condominio="R$ 1")[0] == 2100                     # campo preenchido por preencher
    total, _, avisos = custo("Valor com condomínio e IPTU inclusos.")
    assert total == 2000 and "incluso" in avisos[0]
    assert custo("Condomínio não incluso. Água inclusa.")[0] == 2600
    # o que o CONDOMÍNIO cobre não é "condomínio incluso" (saía "abaixo do mercado" falso)
    for texto in ("Água e gás inclusos no condomínio.", "Condomínio R$ 500 (inclui água e gás).",
                  "Condomínio de R$ 500 com água inclusa.", "Valores não inclusos: condomínio e IPTU."):
        assert custo(texto)[0] == 2600, texto
    assert custo("IPTU incluso, condomínio não incluso.")[0] == 2500
    assert custo("Condomínio à parte, IPTU incluso no aluguel.")[0] == 2500
    for texto in ("Condomínio, água e IPTU inclusos.", "Aluguel já incluso condomínio e IPTU.",
                  "O valor inclui o condomínio e iptu."):
        assert custo(texto)[0] == 2000, texto
    assert "Não informa o condomínio" in custo(condominio="")[2][0]
    assert custo(condominio="", cat="Casas")[2] == []              # casa sem condomínio é o normal
    assert avaliador.custo_imovel({"preco": 0, "atributos": ""}) == (0, "", [])


def _mercado_ap():
    """Amostra: Bairro Um com 2 quartos, ~60 m² (10 pontos) e ~120 m² (6); Bairro Dois
    só com 3 pontos (cai p/ a cidade)."""
    um = [[t, 60] for t in (2400, 2500, 2600, 2700, 2800, 2900, 3000, 3100, 3200, 3300)] + \
         [[t, 120] for t in (4800, 5000, 5200, 5400, 5600, 5800)]
    return {"n": 19, "mediana": 3100, "amostras": {"cidade alfa|bairro um|2": um,
                                                  "cidade alfa|bairro dois|2": [[2000, 55], [2100, 60], [2200, 62]]}}


def test_p13_aluguel_avaliacao():
    """Nota do aluguel: custo total contra imóveis do mesmo bairro, quartos e
    tamanho; todo anúncio novo dentro do teto avisa ("novo"), o barato de verdade
    ganha destaque, o barato demais sai com ⚠️."""
    desc = ("Apartamento de 2 quartos com armários, sala ampla, cozinha planejada e uma vaga de garagem "
            "coberta. Prédio com elevador e portaria 24 horas. Aceita pet.")
    def item(**kw):
        extra = {k: kw.pop(k) for k in ("instrucoes", "preco_max", "descricao", "mercado", "feedback") if k in kw}
        return {**_BUSCA_AP, "anuncio_id": 1, "busca_id": 1, "nome": "aluguel", "instrucoes": "",
                "criterios_inclusao": [], "criterios_exclusao": [], **_ap(**kw),
                "descricao": desc, "mercado": _mercado_ap(), **extra}
    r = avaliador.referencia_imovel(item())
    assert (r["mediana"], r["n"], r["escopo"], r["tamanho"]) == (2850, 10, "bairro", True), r
    r = avaliador.referencia_imovel(item(size="120m²"))
    assert (r["mediana"], r["n"], r["tamanho"]) == (5300, 6, True), r
    # sem área não dá p/ escolher os parecidos: 60 m² e 120 m² juntos variam demais = sem referência
    assert avaliador.referencia_imovel(item(size="")) is None
    so60 = {"amostras": {"cidade alfa|bairro um|2": _mercado_ap()["amostras"]["cidade alfa|bairro um|2"][:10]}}
    r = avaliador.referencia_imovel(item(size="", mercado=so60))
    assert (r["n"], r["tamanho"], r["escopo"]) == (10, False, "bairro"), r
    r = avaliador.referencia_imovel(item(local="Cidade Alfa, Bairro Dois"))      # bairro com 3 pontos: a cidade
    assert r["escopo"] == "cidade" and r["n"] >= 5, r
    assert avaliador.referencia_imovel(item(rooms="4")) is None
    assert avaliador.referencia_imovel(item(mercado="")) is None

    normal = avaliar(item(aluguel=2250), [])                             # total 2.850 = a mediana
    assert (normal["alerta"], normal["avaliacao_preco"], normal["recomendacao"]) == ("novo", "justo", "VER"), normal
    assert normal["custo"] == "R$ 2.850/mês (aluguel R$ 2.250 + cond. R$ 500 + IPTU R$ 100)", normal["custo"]
    assert normal["referencia"] == "bairro" and normal["pros"][0] == "2 quarto(s) · 60 m² · 1 vaga(s)", normal
    assert "aceita pet" in normal["pros"] and "de tamanho parecido" in normal["resumo"]
    barato = avaliar(item(aluguel=1750), [])                             # total 2.350: abaixo do p10
    assert (barato["alerta"], barato["avaliacao_preco"], barato["recomendacao"]) == ("comprar", "bom", "COMPRAR"), barato
    assert barato["score"] > normal["score"]
    demais = avaliar(item(aluguel=700), [])                              # total 1.300: -54% = padrão de golpe
    assert (demais["alerta"], demais["avaliacao_preco"]) == ("suspeita", "suspeito") and demais["score"] <= 7, demais
    assert "nunca pague" in " ".join(demais["observacoes"])
    # teto vale p/ o TOTAL: aluguel 2.900 cabe, mas com condomínio e IPTU passa 17%
    acima = avaliar(item(aluguel=2900), [])
    assert acima["alerta"] == "" and acima["recomendacao"] == "IGNORAR" and "O aluguel cabe" in acima["observacoes"][0], acima
    pouco = avaliar(item(aluguel=2500), [])                              # total 3.100: 3% acima, sem falar em negociar
    assert pouco["alerta"] == "" and pouco["recomendacao"] == "VER", pouco
    negocia = avaliar(item(aluguel=2700, descricao=desc + " Aceito proposta."), [])
    assert negocia["alerta"] == "negociar" and negocia["avaliacao_preco"] == "negociavel", negocia
    # sem teto: só o barato de verdade avisa; sem mercado e com teto: "novo"
    assert avaliar(item(aluguel=2250, preco_max=0), [])["alerta"] == ""
    assert avaliar(item(aluguel=1750, preco_max=0), [])["alerta"] == "comprar"
    sem_ref = avaliar(item(mercado=""), [])
    assert sem_ref["alerta"] == "novo" and sem_ref["avaliacao_preco"] == "sem_base", sem_ref
    # sinais do texto
    golpe = avaliar(item(aluguel=1750, descricao=desc + " Estou no exterior, envio as chaves pelos correios."), [])
    assert golpe["alerta"] == "" and golpe["recomendacao"] == "IGNORAR" and "golpe" in golpe["motivo"], golpe
    mofo = avaliar(item(aluguel=1750, descricao=desc + " Tem infiltração no banheiro."), [])
    assert mofo["alerta"] == "negociar" and "⚠️ infiltração" in mofo["contras"], mofo
    assert avaliar(item(descricao=desc + " Sem infiltração."), [])["contras"] == []
    gar = avaliar(item(descricao=desc + " Exige fiador ou seguro fiança."), [])
    assert "Garantia pedida: fiador, seguro-fiança" in gar["observacoes"], gar
    assert "sem fiador" in avaliar(item(descricao=desc + " Sem fiador."), [])["pros"]
    assert "não aceita pet" in avaliar(item(descricao=desc.replace("Aceita pet", "Não aceita pet")), [])["contras"]
    # regras livres do usuário valem como em produto
    assert avaliar(item(instrucoes="sem fiador", descricao=desc + " Exige fiador."), [])["decisao"] == "EXCLUIDO"
    assert avaliar(item(instrucoes="sem fiador", descricao=desc + " Sem fiador."), [])["decisao"] == "APROVADO"
    mob = avaliar(item(instrucoes="só mobiliado"), [])
    assert mob["decisao"] == "APROVADO" and any("mobiliado" in o for o in mob["observacoes"]), mob
    assert avaliar(item(instrucoes="só mobiliado", descricao=desc + " Não mobiliado."), [])["decisao"] == "EXCLUIDO"
    assert "atende: mobiliado" in avaliar(item(instrucoes="prefiro mobiliado", descricao=desc + " Mobiliado."), [])["pros"]
    # triagem refeita na avaliação (filtro editado depois da coleta) e o 👎
    assert avaliar(item(rooms="1"), [])["decisao"] == "EXCLUIDO"
    assert avaliar(item(titulo="Apartamento por temporada"), [])["motivo"] == "aluguel por temporada/diária"
    assert avaliar(item(feedback=0), [])["decisao"] == "EXCLUIDO"
    sem_desc = avaliar(item(aluguel=1750, descricao=""), [])
    assert sem_desc["score"] <= 7 and sem_desc["alerta"] == "comprar", sem_desc      # a fila só envia com a descrição
    # venda: o preço é o total, sem "/mês"
    venda = avaliar({**item(negocio="Venda", aluguel=300000, preco_max=400000, mercado=""),
                     "filtros_extra": {**_BUSCA_AP["filtros_extra"], "negocio": "venda"}}, [])
    assert venda["custo"] == "R$ 300.000" and venda["alerta"] == "novo", venda


def test_p13_aluguel_ponta_a_ponta():
    """/addaluguel -> scan -> mercado por bairro -> avaliação -> alerta no Telegram."""
    novo_banco()
    r = tb.tratar_comando("/addaluguel estado=go cidade=cidade_alfa bairro=bairro_um,centro imovel=apartamento "
                          "quartos=2 vagas=1 max=3000 obs= sem fiador")
    assert "criada" in r and "cidade alfa (bairro um, centro)" in r and "2 quarto(s)+" in r, r
    b = db.listar_buscas()[0]
    assert b["nome"] == "aluguel apartamento 2q+ bairro um, centro" and b["tipo"] == "imovel", b
    assert b["filtros_extra"] == {"bairros": ["bairro um", "centro"], "cidade": "cidade alfa",
                                  "imovel": "apartamento", "quartos_min": 2, "vagas_min": 1}, b["filtros_extra"]
    desc = "Apartamento com armários, sala ampla e cozinha planejada. Prédio com elevador. Direto com o proprietário."
    eco = '<script>self.__next_f.push([1,"\\"loc_municipality_id\\":\\"123\\""])</script>'
    def pagina(ads):
        return pagina_olx(ads).replace("</html>", eco + "</html>")
    mercado = [_imovel_olx(9100 + i, f"Apto mercado {i}", 1900 + 100 * i, condominio="R$ 500", iptu="R$ 100",
                           size="60m²", rooms="2", garage_spaces="1") for i in range(10)]
    busca = [_imovel_olx(9001, "Apartamento 2 quartos no Bairro Um", 1750, condominio="R$ 500", iptu="R$ 100",
                         size="60m²", rooms="2", garage_spaces="1"),
             _imovel_olx(9002, "Apartamento 2 quartos reformado", 2250, condominio="R$ 500", iptu="R$ 100",
                         size="62m²", rooms="2", garage_spaces="1"),
             _imovel_olx(9003, "Apartamento em outro bairro", 1800, local="Cidade Alfa, Bairro Nove", rooms="2",
                         garage_spaces="1"),
             _imovel_olx(9004, "Apartamento por temporada", 1800, rooms="2", garage_spaces="1")]
    olx = OlxFalsa({"pe=": (200, pagina(busca)), "anuncio-": (200, pagina_anuncio(desc)),
                    "o=1&sf=1": (200, pagina(mercado))})
    scheduler._sessao = olx
    textos = []
    notifier._post = lambda metodo, dados, timeout: bool(textos.append(dados.get("caption") or dados.get("text"))) or True
    rodar(scheduler.processar_busca(db.obter_busca(b["id"])))
    urls = olx.paginas()
    assert any("/imoveis/aluguel/apartamentos/estado-go/cidade-alfa?o=1&sf=1&pe=3450&ros=2" in u for u in urls), urls
    assert any("/cidade-alfa/bairro-um?o=1" in u and "pe=" not in u for u in urls), urls       # mercado do bairro
    assert all(p.startswith("RSC ") and p.endswith("&_rsc") for p in olx.pedidos if "anuncio-" not in p), olx.pedidos
    assert sql("SELECT COUNT(*) FROM anuncios")[0][0] == 3                    # o de outro bairro nem é gravado
    m = db.obter_busca(b["id"])["mercado"]
    assert m["n"] == 10 and len(m["amostras"]["cidade alfa|bairro um|2"]) == 10, m
    rodar(avaliador.processar_fila(notifier))
    an = {r[0]: r[1:] for r in sql("SELECT a.titulo, an.decisao, an.alerta, an.avaliacao_preco FROM analises an "
                                   "JOIN anuncios a ON a.id = an.anuncio_id")}
    assert an["Apartamento 2 quartos no Bairro Um"] == ("APROVADO", "comprar", "bom"), an
    assert an["Apartamento 2 quartos reformado"] == ("APROVADO", "novo", "justo"), an
    assert an["Apartamento por temporada"][0] == "EXCLUIDO", an
    alertas = [t for t in textos if "Ver anúncio" in t]
    assert len(alertas) == 2, textos
    assert "ABAIXO DO MERCADO" in alertas[0] and "R$ 2.350/mês (aluguel R$ 1.750 + cond. R$ 500 + IPTU R$ 100)" in alertas[0]
    assert "vs mediana do bairro" in alertas[0] and "📦" not in alertas[0] and "direto com o proprietário" in alertas[0]
    assert "NOVO NO SEU FILTRO" in alertas[1], alertas[1]
    # rodada seguinte: nada novo, nenhum alerta repetido; o mercado não é remedido no mesmo dia
    n = len(olx.pedidos)
    sql("UPDATE buscas SET proximo_scan=datetime('now','-1 minute')")
    rodar(scheduler.processar_busca(db.obter_busca(b["id"])))
    rodar(avaliador.processar_fila(notifier))
    assert len([t for t in textos if "Ver anúncio" in t]) == 2 and all("pe=" in u for u in olx.pedidos[n:]), olx.pedidos[n:]
    # cidade que a OLX não reconhece (ela ignora e devolveria o estado inteiro): erro claro, na hora
    avisos = []
    notifier.alertar = lambda chave, texto, horas=12: avisos.append((chave, texto)) or True
    scheduler._sessao = OlxFalsa({"o=1": (200, pagina_olx(busca))})           # sem o eco do local
    sql("UPDATE buscas SET fase='scan'")
    rodar(scheduler.processar_busca(db.obter_busca(b["id"])))
    assert avisos and avisos[0][0] == f"local:{b['id']}" and "não reconheceu a cidade “cidade alfa”" in avisos[0][1], avisos
    assert sql("SELECT COUNT(*) FROM anuncios")[0][0] == 3
    # ...ou responde 5xx: busca que nunca leu nada avisa na hora; a que já leu espera as 6 h
    nova = db.criar_busca({"nome": "nova", "tipo": "imovel", "termo": "", "estado": "go",
                           "filtros_extra": {"cidade": "Errada"}})
    scheduler._sessao = OlxFalsa({"o=1": (504, "")})
    avisos.clear()
    rodar(scheduler.processar_busca(db.obter_busca(nova)))
    rodar(scheduler.processar_busca(db.obter_busca(b["id"])))
    assert [a[0] for a in avisos] == [f"local:{nova}"] and "“Errada”" in avisos[0][1], avisos
    # imóvel SEM teto também mede o mercado (é a única referência de preço)
    assert db.mercado_vencido(db.criar_busca({"nome": "x", "tipo": "imovel", "termo": "", "estado": "go"}))


def test_p13_aluguel_telegram_e_validacao():
    """Opções do /addaluguel e as regras entre campos, iguais ao criar e ao editar."""
    from models import BuscaCreate
    novo_banco()
    assert "/addaluguel estado=uf" in tb.tratar_comando("/addaluguel") and "/addaluguel" in tb.tratar_comando("/help")
    assert "exige o estado" in tb.tratar_comando("/addaluguel cidade=cidade_alfa max=2000")
    assert "bairros exigem a cidade" in tb.tratar_comando("/addaluguel estado=go bairro=centro")
    assert "Opção desconhecida" in tb.tratar_comando("/add rtx 3090 cidade=alfa")          # só no /addaluguel
    assert "Valor inválido" in tb.tratar_comando("/addaluguel estado=go quartos=dois")
    for ruim, erro in (("quartos=9", "quartos_min fora de 1-5"), ("imovel=castelo", "imovel deve ser um de"),
                       ("negocio=troca", "negocio deve ser"), ("cidade=123", "cidade inválida"),
                       ("area=0", "area_min fora de")):
        assert erro in tb.tratar_comando(f"/addaluguel estado=go {ruim}"), (ruim, tb.tratar_comando(f"/addaluguel estado=go {ruim}"))
    r = tb.tratar_comando("/addaluguel kitnet estado=go cidade=cidade_alfa negocio=venda area=40")
    assert "criada" in r and "40 m²+" in r, r
    b = db.listar_buscas()[-1]
    assert b["termo"] == "kitnet" and b["filtros_extra"] == {"area_min": 40, "cidade": "cidade alfa", "negocio": "venda"}, b
    assert b["condicao"] == "funcionando"
    assert "criada" in tb.tratar_comando("/addimovel estado=go")                  # o estado inteiro é permitido
    cria = lambda **kw: BuscaCreate(**{"nome": "n", "termo": "", "tipo": "imovel", "estado": "go", **kw})
    assert cria(filtros_extra={"bairros": "Centro, Bairro_Um ,", "cidade": " Cidade   Alfa "}).filtros_extra == \
        {"bairros": ["Centro", "Bairro Um"], "cidade": "Cidade Alfa"}
    assert cria(termo="apartamento para reforma com defeito").condicao == "funcionando"
    for ruim in ({"ano_min": 2010}, {"bairros": ["x" * 61], "cidade": "Alfa"}, {"bairros": ["a"] * 11, "cidade": "Alfa"},
                 {"vagas_min": "1e400"}, {"cidade": "<b>"}, {"outro": 1}):
        try:
            cria(filtros_extra=ruim)
            assert False, f"aceitou {ruim}"
        except ValueError:
            pass
    for kw in ({"tipo": "produto", "termo": "rtx 3090", "filtros_extra": {"cidade": "Alfa"}},
               {"tipo": "carro", "filtros_extra": {"quartos_min": 2}}):
        try:
            cria(**kw)
            assert False, f"aceitou {kw}"
        except ValueError as e:
            assert "não valem" in str(e), e
    # API: editar respeita as mesmas regras e mudar o filtro reavalia e remede
    import main
    from fastapi.testclient import TestClient
    main.USUARIO, main.SENHA = "u", "p"
    c, A = TestClient(main.app), ("u", "p")
    h = {"Sec-Fetch-Site": "same-origin"}
    r = c.post("/api/buscas", auth=A, headers=h, json={"nome": "ap", "tipo": "imovel", "estado": "go", "preco_max": 2500,
                                                        "filtros_extra": {"cidade": "Cidade Alfa", "quartos_min": "2"}})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    assert db.obter_busca(bid)["filtros_extra"] == {"cidade": "Cidade Alfa", "quartos_min": 2}
    assert c.patch(f"/api/buscas/{bid}", auth=A, headers=h, json={"estado": "brasil"}).status_code == 422
    assert c.patch(f"/api/buscas/{bid}", auth=A, headers=h, json={"tipo": "produto"}).status_code == 422   # sem termo
    assert c.patch(f"/api/buscas/{bid}", auth=A, headers=h,
                   json={"filtros_extra": {"bairros": ["Centro"]}}).status_code == 422                     # perdeu a cidade
    sql("UPDATE buscas SET fase='monitor', mercado='{\"n\": 1}' WHERE id=?", bid)
    r = c.patch(f"/api/buscas/{bid}", auth=A, headers=h,
                json={"filtros_extra": {"cidade": "Cidade Alfa", "bairros": ["Centro"]}})
    assert r.status_code == 200 and r.json()["alterados"] == ["filtros_extra"], r.text
    b = db.obter_busca(bid)
    assert b["fase"] == "scan" and b["mercado"] == "", b


def test_p13_revisao_independente():
    """Bugs reproduzidos pela revisão independente das regras de imóvel e de troca."""
    desc = ("Apartamento de 2 quartos com armários, sala ampla, cozinha planejada e uma vaga de garagem "
            "coberta. Prédio com elevador e portaria 24 horas.")
    def item(descricao=desc, **kw):
        extra = {k: kw.pop(k) for k in ("instrucoes",) if k in kw}
        return {**_BUSCA_AP, "anuncio_id": 1, "busca_id": 1, "nome": "aluguel", "instrucoes": "",
                "criterios_inclusao": [], "criterios_exclusao": [], **_ap(aluguel=2250, **kw),
                "descricao": descricao, "mercado": _mercado_ap(), **extra}
    # frases inocentes não são golpe; as do golpe continuam sendo
    for frase in ("Favor agendar por mensagem antes da visita.", "Pintura nova no exterior do prédio.",
                  "Contrato enviado pelo correio eletrônico."):
        assert avaliar(item(desc + " " + frase), [])["alerta"] == "novo", frase
    for frase in ("Estou no exterior a trabalho.", "Faça o pix antes da visita para reservar.", "Envio as chaves por Sedex."):
        assert "golpe" in avaliar(item(desc + " " + frase), [])["motivo"], frase
    # moradia legítima que as expressões largas vetavam
    tri = lambda t, **ex: avaliador.triagem({"tipo": "imovel", "termo": "", "filtros_extra": ex}, _ap(titulo=t))
    for t in ("Apartamento 2 quartos Av. República do Líbano", "Apartamento 1 quarto na República - metrô",
              "Kitnet ideal para quem não quer dividir", "Apartamento com portaria 24 horas por dia",
              "Aluguel anual, não é temporada - apto 2 quartos", "Apartamento mensal (sem diárias)",
              "Apartamento Jardim Carnaval", "Apartamento próximo à loja de departamentos",
              "Apartamento 3 quartos, ótimo para home office/escritório", "Busco inquilino para apto 2 quartos"):
        assert tri(t) == (True, ""), (t, tri(t))
    for t in ("Procurando apartamento para alugar", "Preciso de apartamento para alugar urgente",
              "Quero alugar casa 2 quartos", "Estou à procura de kitnet", "Vaga em república masculina",
              "Divido apartamento com estudante", "Apartamento por temporada", "Galpão ou sala comercial"):
        assert not tri(t)[0], t
    # "dispensa fiador" atende a regra "sem fiador"
    av = avaliar(item(desc + " Dispensa fiador.", instrucoes="sem fiador"), [])
    assert av["decisao"] == "APROVADO" and "sem fiador" in av["pros"], av
    # "sem problemas de infiltração ou mofo": a lista herda o "sem"
    assert avaliar(item(desc + " Sem problemas de infiltração ou mofo."), [])["contras"] == []
    assert "⚠️ mofo" in avaliar(item(desc + " Tem um pouco de mofo no quarto."), [])["contras"]
    # cidade com hífen/espaço duplo é a mesma; bairro casa por palavra inteira
    passa = lambda extras, **kw: avaliador.passa_filtros_imovel({"filtros_extra": extras}, _ap(**kw))[0]
    assert passa({"cidade": "cidade-alfa"}) and passa({"cidade": "Cidade  Alfa"}) and not passa({"cidade": "cidade beta"})
    assert passa({"bairros": ["bairro-um"]}) and passa({"bairros": ["um"]})
    assert not passa({"bairros": ["sul"]}, local="Cidade Alfa, Consulado")
    # Garantia da OLX não existe em imóvel: não pode descartar tudo
    assert len(scheduler.filtrar_produtos({**_BUSCA_AP, "garantia": "pay"}, [_ap(lid=1)])) == 1
    # validador não grava lixo
    from models import BuscaCreate
    for ruim in ({"cidade": None}, {"cidade": True}, {"cidade": "Alfa", "bairros": [None]},
                 {"cidade": "Alfa", "bairros": [["x", "y"]]}, {"cidade": "Alfa", "bairros": ["!!"]}):
        try:
            BuscaCreate(nome="n", tipo="imovel", estado="go", filtros_extra=ruim)
            assert False, f"aceitou {ruim}"
        except ValueError:
            pass
    novo_banco()
    assert "criada" in tb.tratar_comando("/addaluguel estado=go cidade=cidade_alfa bairro=Centro,,")
    assert db.listar_buscas()[0]["nome"] == "aluguel centro", db.listar_buscas()[0]["nome"]
    # venda legítima com "troco"/"aceito" no título; a troca de verdade segue vetada
    for termo, t in (("galaxy s23", "Vendo ou troco celular em ótimo estado Galaxy S23"),
                     ("ps5", "Vendo ou troco console em perfeito estado PS5 com 2 controles"),
                     ("rtx 3070", "Asus Dual vendo ou troco usada pouco tempo em jogos RTX 3070"),
                     ("rtx 3060", "Placa de vídeo aceito 1200 rtx 3060 12gb"), ("rtx 3080", "vendo ou pego 2500 rtx 3080")):
        assert produto_bate(termo, t)[0], (termo, t, produto_bate(termo, t))
    # ressalva moderada: teto folgado não a transforma em grave
    base = {**_item(), "titulo": "Rtx 3060 12gb", "termo": "rtx 3060", "preco": 1400,
            "descricao": "Um fan com avaria, mas em perfeito funcionamento, testada em jogos, acompanha a caixa original da placa."}
    assert avaliar({**base, "preco_max": 2500}, [])["alerta"] == avaliar({**base, "preco_max": 3500}, [])["alerta"] == "negociar"


def test_p13_republicacao_e_queda_de_imovel():
    """Imóveis diferentes com título/descrição-modelo iguais alertam os dois; o
    mesmo imóvel repostado não; queda só do aluguel não é "entrou no teto"."""
    novo_banco()
    assert "criada" in tb.tratar_comando("/addaluguel estado=go cidade=cidade_alfa max=3000")
    b = db.listar_buscas()[0]
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    modelo = "A Imobiliária Exemplo aluga excelente apartamento pronto para morar, em ótima localização da cidade. "
    def grava(lid, titulo, desc, aluguel=1800, **props):
        ficha = dict(size="60m²", condominio="R$ 500", iptu="R$ 100", rooms="2", garage_spaces="1") | props
        p = scheduler._mapear_flight_ad(_imovel_olx(lid, titulo, aluguel, **ficha))
        aid, novo, antigo = db.upsert_anuncio(b["id"], p)
        db.atualizar_descricao(aid, b["id"], desc)
        return aid
    n = NotifierFalso()
    a1 = grava(1, "Apartamento 2 quartos Bairro Um", modelo + "Unidade A.")
    a2 = grava(2, "Apartamento 2 quartos no Bairro Um", modelo + "Unidade B.", size="75m²", garage_spaces="2")
    a3 = grava(3, "Apartamento 3 quartos Bairro Um", modelo + "Unidade C.", rooms="3", size="90m²")
    rodar(avaliador.processar_fila(n))
    assert sorted(n.enviados) == [a1, a2, a3], n.enviados
    grava(4, "Apartamento 2 quartos Bairro Um!", modelo + "Unidade A.")        # o mesmo imóvel, repostado
    rodar(avaliador.processar_fila(n))
    assert len(n.enviados) == 3, n.enviados
    # produto: texto do fabricante colado por vendedores de ESTADOS diferentes não é o mesmo vendedor
    bid = criar(termo="rtx 3060", preco_max=1600)
    sql("UPDATE buscas SET criado_em=datetime('now','-3 days')")
    fab = "A GeForce RTX 3060 permite que você encare os games mais recentes usando o poder da arquitetura Ampere."
    ids = []
    for lid, sub, local in ((11, "sp", "Cidade X, Bairro 1"), (12, "pr", "Cidade Y, Bairro 2"), (13, "sp", "Cidade Z, Bairro 3")):
        aid, _, _ = db.upsert_anuncio(bid, {"url": f"https://{sub}.olx.com.br/x/placa-{lid}", "list_id": lid,
                                            "titulo": f"RTX 3060 12GB n{lid}", "preco": 1500, "local": local})
        db.atualizar_descricao(aid, bid, fab)
        ids.append(aid)
        rodar(avaliador.processar_fila(n))
    assert n.enviados[3:] == ids[:2], (n.enviados, ids)                       # o 3º (mesmo estado do 1º) é o vendedor em série
    # queda de preço: aluguel 3.100 -> 2.950 "cruza" o teto de 3.000, mas o total (3.550) não
    quedas = []
    async def queda(busca, p, antigo, aid=None):
        quedas.append(p["titulo"])
        return True
    notifier.enviar_queda = queda
    caro = _imovel_olx(20, "Apartamento caro", 3100, condominio="R$ 500", iptu="R$ 100", rooms="2")
    eco = '<script>self.__next_f.push([1,"\\"loc_municipality_id\\":\\"1\\""])</script>'
    pag = lambda ad: pagina_olx([ad]).replace("</html>", eco + "</html>")
    scheduler._preencher_descricoes = _sem_espera
    scheduler._sessao = OlxFalsa({"pe=": (200, pag(caro)), "o=1": (200, pagina_olx([]))})
    rodar(scheduler.processar_busca(db.obter_busca(b["id"])))
    rodar(avaliador.processar_fila(None))
    scheduler._sessao = OlxFalsa({"pe=": (200, pag({**caro, "priceValue": "R$ 2.950"})), "o=1": (200, pagina_olx([]))})
    rodar(scheduler.processar_busca(db.obter_busca(b["id"])))
    assert quedas == [], quedas


if __name__ == "__main__":
    import sys
    testes = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    so = sys.argv[1] if len(sys.argv) > 1 else ""        # ex.: test_offline.py p3
    falhas = 0
    for nome, f in testes:
        if so and f"_{so}_" not in nome:
            continue
        estado = [(m, k, getattr(m, k)) for m, ks in (
            (scheduler, ("raspar_busca", "_preencher_descricoes", "_sessao", "processar_busca",
                        "_olx_falha_desde", "_olx_alertado", "_olx_pausa_ate", "_canario_proximo")),
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
