"""
database.py — Camada de dados SQLite

Regra permanente: TODO timestamp comparado em SQL é gerado EM SQL
(datetime('now'), UTC com espaço). Hora local do Python em formato ISO ('T')
comparada com isso fez o bot rodar só ~3h/dia por meses ('T' > ' ').

Estado de uma busca = duas colunas independentes:
  status  'ativa' | 'pausada' | 'erro'   (só o usuário pausa; só o scan seta erro)
  fase    'scan'  | 'monitor'            (scan = varre tudo; monitor = só as novidades)
Antes o status recebia o nome da fase ('monitor') no início do scan; uma
interrupção no meio deixava a busca fora de buscas_para_processar PARA SEMPRE.
"""
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "bot.db"
VERSAO_SCHEMA = 1


@contextmanager
def get_conn():
    """Conexão com commit no sucesso e close SEMPRE.
    (`with sqlite3.connect()` NÃO fecha — vazava 1 fd por chamada até
    estourar o limite de 1024 e derrubar o scheduler.)"""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS buscas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            nome            TEXT NOT NULL,
            tipo            TEXT NOT NULL DEFAULT 'produto',  -- produto | carro | imovel
            termo           TEXT NOT NULL,
            criterios_inclusao  TEXT DEFAULT '[]',
            criterios_exclusao  TEXT DEFAULT '[]',
            estado          TEXT DEFAULT 'brasil',
            preco_min       INTEGER DEFAULT 0,
            preco_max       INTEGER DEFAULT 0,
            filtro_envio    TEXT DEFAULT 'qualquer',
            filtro_vendedor TEXT DEFAULT 'qualquer',
            garantia        TEXT DEFAULT '',
            instrucoes      TEXT DEFAULT '',
            filtros_extra   TEXT DEFAULT '{}',
            condicao        TEXT DEFAULT 'funcionando',  -- funcionando | defeito | qualquer
            categorias      TEXT DEFAULT '[]',           -- filtro opcional (categorias da OLX)
            intervalo_min   INTEGER DEFAULT 30,
            status          TEXT DEFAULT 'ativa',
            fase            TEXT DEFAULT 'scan',
            criado_em       TEXT DEFAULT (datetime('now')),
            ultimo_scan     TEXT,
            proximo_scan    TEXT,
            ultimo_scan_full TEXT
        );

        CREATE TABLE IF NOT EXISTS anuncios (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            busca_id        INTEGER NOT NULL REFERENCES buscas(id) ON DELETE CASCADE,
            url             TEXT NOT NULL,
            hash            TEXT NOT NULL,
            titulo          TEXT,
            preco           REAL,
            local           TEXT,
            data_postagem   TEXT,
            envio           TEXT,
            descricao       TEXT,
            imagem          TEXT DEFAULT '',
            vendedor        TEXT DEFAULT '',
            atributos       TEXT DEFAULT '',
            categoria       TEXT DEFAULT '',                 -- taxonomia da própria OLX
            primeira_vez    TEXT DEFAULT (datetime('now')),
            ultima_vez      TEXT DEFAULT (datetime('now')),
            notificado      INTEGER DEFAULT 0,
            UNIQUE(busca_id, hash)
        );

        CREATE TABLE IF NOT EXISTS analises (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            anuncio_id          INTEGER NOT NULL REFERENCES anuncios(id) ON DELETE CASCADE,
            decisao             TEXT,
            resumo              TEXT,
            pros                TEXT DEFAULT '[]',
            contras             TEXT DEFAULT '[]',
            observacoes         TEXT DEFAULT '[]',
            score               INTEGER,
            preco_vs_mercado_pct INTEGER,
            avaliacao_preco     TEXT,
            recomendacao        TEXT,
            motivo              TEXT,
            alerta              TEXT DEFAULT '',
            gerado_em           TEXT DEFAULT (datetime('now')),
            UNIQUE(anuncio_id)
        );

        CREATE TABLE IF NOT EXISTS historico_precos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            anuncio_id  INTEGER NOT NULL REFERENCES anuncios(id) ON DELETE CASCADE,
            preco       REAL,
            registrado_em TEXT DEFAULT (datetime('now'))
        );
        """)
        if "alerta" not in {r[1] for r in conn.execute("PRAGMA table_info(analises)")}:
            conn.execute("ALTER TABLE analises ADD COLUMN alerta TEXT DEFAULT ''")
        versao = conn.execute("PRAGMA user_version").fetchone()[0]
        if versao < 1:
            _migrar_v1(conn)
        conn.executescript("""
        -- prioridade: 2 monitor/preço mudou, 1 scan/descrição nova, 0 edição,
        -- -1 reavaliação silenciosa (não notifica)
        CREATE TABLE IF NOT EXISTS fila (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            anuncio_id  INTEGER NOT NULL REFERENCES anuncios(id) ON DELETE CASCADE,
            busca_id    INTEGER NOT NULL,
            prioridade  INTEGER DEFAULT 0,
            tentativas  INTEGER DEFAULT 0,
            criado_em   TEXT DEFAULT (datetime('now')),
            UNIQUE(anuncio_id)
        );
        -- 👍/👎 do usuário. Guarda CÓPIA do texto: o aprendizado sobrevive à
        -- remoção da busca/anúncio (anuncio_id vira NULL)
        CREATE TABLE IF NOT EXISTS feedback (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            anuncio_id      INTEGER UNIQUE REFERENCES anuncios(id) ON DELETE SET NULL,
            termo           TEXT, titulo TEXT, descricao TEXT, atributos TEXT,
            vendedor        TEXT, categoria TEXT, preco REAL, avaliacao_preco TEXT,
            gostou          INTEGER NOT NULL,
            criado_em       TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_anuncios_busca ON anuncios(busca_id);
        CREATE INDEX IF NOT EXISTS idx_fila_prioridade ON fila(prioridade DESC, criado_em);
        """)
        conn.execute(f"PRAGMA user_version={VERSAO_SCHEMA}")
        # regra nova vale p/ o que já está no banco: todo início (= todo deploy)
        # reavalia tudo em silêncio (~1 ms cada; a faxina diária mantém o banco
        # pequeno). Antes ficava o veredito velho, inclusive no histórico do painel
        conn.execute("""INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade)
                        SELECT id, busca_id, -1 FROM anuncios""")


def _migrar_v1(conn):
    """Banco da era LLM/Xianyu -> avaliador por regras (2026-10-22)."""
    tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(buscas)")}
    if "llm_fila" in tabelas:
        conn.execute("ALTER TABLE llm_fila RENAME TO fila")
    conn.execute("DROP TABLE IF EXISTS llm_cache")
    conn.execute("DROP INDEX IF EXISTS idx_anuncios_hash")       # redundante c/ o UNIQUE
    conn.execute("DROP INDEX IF EXISTS idx_analises_anuncio")    # idem
    if "plataforma" in cols:                                      # Xianyu removido
        conn.execute("DELETE FROM buscas WHERE plataforma='xianyu'")
        conn.execute("ALTER TABLE buscas DROP COLUMN plataforma")
    if "ordenacao" in cols:                                       # sf= nunca funcionou
        conn.execute("ALTER TABLE buscas DROP COLUMN ordenacao")
    if "condicao" not in cols:
        conn.execute("ALTER TABLE buscas ADD COLUMN condicao TEXT DEFAULT 'funcionando'")
    if "categorias" not in cols:
        conn.execute("ALTER TABLE buscas ADD COLUMN categorias TEXT DEFAULT '[]'")
    if "categoria" not in {r[1] for r in conn.execute("PRAGMA table_info(anuncios)")}:
        conn.execute("ALTER TABLE anuncios ADD COLUMN categoria TEXT DEFAULT ''")
    conn.execute("UPDATE buscas SET status='ativa' "
                 "WHERE status IS NULL OR status NOT IN ('pausada', 'erro')")
    # buscas criadas pela web tinham critérios com JSON duplo ('"[]"')
    for r in conn.execute("SELECT id, criterios_inclusao, criterios_exclusao FROM buscas").fetchall():
        for col in ("criterios_inclusao", "criterios_exclusao"):
            v = r[col]
            while isinstance(v, str) and v.startswith('"'):
                v = json.loads(v)
            if v != r[col]:
                conn.execute(f"UPDATE buscas SET {col}=? WHERE id=?", (v, r["id"]))
    # visitas da era do seletor CSS que não acharam descrição: tenta de novo
    conn.execute("""UPDATE anuncios SET descricao='' WHERE descricao='(sem descrição)'
                    AND id NOT IN (SELECT anuncio_id FROM analises WHERE recomendacao='IGNORAR')""")
    if "fila" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        # avaliador novo: reavalia tudo SEM notificar (o que já estava na fila notifica)
        conn.execute("""INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade)
                        SELECT id, busca_id, -1 FROM anuncios""")


def manutencao_diaria():
    """1x/dia: apaga o que não serve mais e faz backup consistente (VACUUM INTO
    em data/bot.db.bak). Descartado não visto há 30 dias e qualquer anúncio não
    visto há 180 saem — antes o banco só crescia (8,8 mil anúncios, 97% lixo).
    Anúncio com 👍/👎 fica: o classificador guarda cópia, mas o painel mostra."""
    bak = DB_PATH.with_suffix(".db.bak")
    if bak.exists() and time.time() - bak.stat().st_mtime < 86400:
        return
    with get_conn() as conn:
        apagados = conn.execute("""
            DELETE FROM anuncios WHERE id NOT IN (SELECT anuncio_id FROM feedback
                                                  WHERE anuncio_id IS NOT NULL)
              AND (ultima_vez < datetime('now','-180 days')
                   OR (ultima_vez < datetime('now','-30 days')
                       AND id IN (SELECT anuncio_id FROM analises WHERE recomendacao='IGNORAR')))
        """).rowcount
    tmp = DB_PATH.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    with get_conn() as conn:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    tmp.replace(bak)
    try:     # RAM 1x/dia no log: tendência de vazamento visível sem ferramenta (só Linux)
        ram = f"; RAM {int(open('/proc/self/statm').read().split()[1]) * os.sysconf('SC_PAGE_SIZE') >> 20} MB"
    except (OSError, ValueError, AttributeError):
        ram = ""
    logger.info(f"faxina diária: {apagados} anúncios velhos apagados; backup {bak.name} "
                f"({bak.stat().st_size // 1024} KB){ram}")


def _proximo(intervalo_min: int) -> str:
    """'+N seconds' com ±5% de variação: as buscas não disparam sempre juntas
    no mesmo segundo (padrão fixo é assinatura de robô)."""
    seg = int(intervalo_min) * 60
    return f"+{seg + random.randint(-seg // 20, seg // 20)} seconds"


# ── BUSCAS ────────────────────────────────────────────────

_CAMPOS_BUSCA = ("nome", "tipo", "termo", "criterios_inclusao", "criterios_exclusao",
                 "estado", "preco_min", "preco_max", "filtro_envio", "filtro_vendedor",
                 "garantia", "instrucoes", "filtros_extra", "condicao", "categorias",
                 "intervalo_min")
# mudou o QUE se raspa -> volta p/ fase scan; mudou COMO se julga -> reavalia
_MUDA_SCAN = {"tipo", "termo", "estado", "preco_min", "preco_max", "filtro_envio",
              "filtro_vendedor", "garantia", "filtros_extra"}
_MUDA_AVALIACAO = {"tipo", "termo", "criterios_inclusao", "criterios_exclusao",
                   "instrucoes", "filtros_extra", "condicao", "categorias"}


def _serializar(campos: dict) -> dict:
    return {k: (json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (list, dict)) else v)
            for k, v in campos.items() if k in _CAMPOS_BUSCA}


def criar_busca(dados: dict) -> int:
    """`dados` já validado por models.BuscaCreate (listas/dicts ainda nativos)."""
    # status/fase EXPLÍCITOS: o banco de produção nasceu com DEFAULT 'pendente'
    # (schema antigo) e busca nova ficava fora de buscas_para_processar
    campos = {**_serializar(dados), "status": "ativa", "fase": "scan"}
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO buscas ({', '.join(campos)}) VALUES ({', '.join(':' + k for k in campos)})",
            campos)
        return cur.lastrowid


def atualizar_busca(busca_id: int, dados: dict) -> set:
    """Aplica só os campos que MUDARAM; retorna o conjunto deles."""
    atual = obter_busca(busca_id)
    if not atual:
        return set()
    novos = _serializar(dados)
    mudou = {k for k, v in novos.items() if _serializar({k: atual.get(k)}).get(k) != v}
    if not mudou:
        return mudou
    with get_conn() as conn:
        sets = ", ".join(f"{k}=:{k}" for k in mudou)
        conn.execute(f"UPDATE buscas SET {sets} WHERE id=:id",
                     {**{k: novos[k] for k in mudou}, "id": busca_id})
        if mudou & _MUDA_SCAN:
            conn.execute("UPDATE buscas SET fase='scan', proximo_scan=datetime('now') "
                         "WHERE id=?", (busca_id,))
        if mudou & _MUDA_AVALIACAO:
            conn.execute("INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade) "
                         "SELECT id, busca_id, 0 FROM anuncios WHERE busca_id=?", (busca_id,))
    return mudou


def listar_buscas() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT b.*,
                   (SELECT COUNT(*) FROM anuncios a WHERE a.busca_id=b.id) AS total_anuncios,
                   (SELECT COUNT(*) FROM anuncios a JOIN analises an ON an.anuncio_id=a.id
                     WHERE a.busca_id=b.id AND an.recomendacao='COMPRAR'
                       AND a.ultima_vez >= datetime('now','-2 days')) AS total_comprar,
                   (SELECT COUNT(*) FROM anuncios a JOIN analises an ON an.anuncio_id=a.id
                     WHERE a.busca_id=b.id AND an.recomendacao='VER'
                       AND a.ultima_vez >= datetime('now','-2 days')) AS total_ver,
                   (SELECT COUNT(*) FROM fila f WHERE f.busca_id=b.id) AS fila_pendente
            FROM buscas b
            ORDER BY b.id DESC
        """).fetchall()
        return [_row_to_dict(r) for r in rows]


def obter_busca(busca_id: int) -> Optional[dict]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM buscas WHERE id=?", (busca_id,)).fetchone()
        return _row_to_dict(r) if r else None


def deletar_busca(busca_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM buscas WHERE id=?", (busca_id,))


def pausar_busca(busca_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE buscas SET status='pausada' WHERE id=?", (busca_id,))


def retomar_busca(busca_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE buscas SET status='ativa', proximo_scan=datetime('now') "
                     "WHERE id=?", (busca_id,))


def forcar_scan(busca_id: int):
    """Agenda scan imediato. NÃO mexe no status: /scan numa busca pausada
    antes a despausava para sempre."""
    with get_conn() as conn:
        conn.execute("UPDATE buscas SET proximo_scan=datetime('now') WHERE id=?", (busca_id,))


def buscas_para_processar() -> list[dict]:
    """Buscas não pausadas cujo proximo_scan já passou ('erro' também: uma
    falha de rede não pode matar a busca)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM buscas
            WHERE status IN ('ativa', 'erro')
              AND (proximo_scan IS NULL OR proximo_scan <= datetime('now'))
            ORDER BY proximo_scan ASC
        """).fetchall()
        return [_row_to_dict(r) for r in rows]


def sem_sucesso_ha(busca_id: int, horas: int = 6) -> bool:
    """Busca com mais de `horas` sem nenhum scan bem-sucedido (falha persistente)."""
    with get_conn() as conn:
        r = conn.execute(f"""SELECT COALESCE(ultimo_scan, criado_em) < datetime('now', '-{int(horas)} hours')
                             FROM buscas WHERE id=?""", (busca_id,)).fetchone()
        return bool(r and r[0])


def full_scan_vencido(busca_id: int) -> bool:
    """True se a busca não faz um scan COMPLETO há 24h. O monitor só lê as
    novidades; sem o full diário, ultima_vez dos anúncios profundos apodrece e
    não dá pra saber o que expirou."""
    with get_conn() as conn:
        r = conn.execute(
            "SELECT ultimo_scan_full IS NULL OR ultimo_scan_full <= datetime('now','-1 day') "
            "FROM buscas WHERE id=?", (busca_id,)).fetchone()
        return bool(r and r[0])


def concluir_scan(busca_id: int, intervalo_min: int, scan_completo: bool):
    """Fim de scan com sucesso. Pausa feita DURANTE o scan é preservada."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE buscas SET
              status = CASE WHEN status='pausada' THEN status ELSE 'ativa' END,
              fase = CASE WHEN :completo THEN 'monitor' ELSE fase END,
              ultimo_scan_full = CASE WHEN :completo THEN datetime('now') ELSE ultimo_scan_full END,
              ultimo_scan = datetime('now'),
              proximo_scan = datetime('now', :intervalo)
            WHERE id = :id
        """, {"completo": scan_completo, "intervalo": _proximo(intervalo_min), "id": busca_id})


def registrar_erro(busca_id: int, intervalo_min: int):
    """Falha: marca erro e agenda a próxima tentativa (senão retry a cada 30 s)."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE buscas SET
              status = CASE WHEN status='pausada' THEN status ELSE 'erro' END,
              proximo_scan = datetime('now', ?)
            WHERE id = ?
        """, (_proximo(intervalo_min), busca_id))


# ── ANÚNCIOS ──────────────────────────────────────────────

_RE_ID_URL = re.compile(r"-(\d{6,})/?$")


def hash_anuncio(url: str, list_id=None) -> str:
    """Chave: listId da OLX (estável mesmo se o slug da URL mudar) com
    fallback p/ o id embutido no fim da URL, senão a URL inteira."""
    if not list_id:
        m = _RE_ID_URL.search(url or "")
        list_id = m.group(1) if m else None
    chave = str(list_id) if list_id else (url or "")
    return hashlib.md5(chave.encode()).hexdigest()


def upsert_anuncio(busca_id: int, dados: dict) -> tuple[int, bool, float | None]:
    """Insere ou atualiza anúncio. Retorna (id, novo, preco_antigo).
    preco_antigo != None quando o preço MUDOU (p/ alerta de queda); a mudança
    registra histórico e re-enfileira p/ reavaliação. Anúncio já visto tem os
    metadados atualizados (os da era do fallback CSS não tinham imagem,
    vendedor nem atributos, e a data era relativa: 'Hoje, 18:35')."""
    h = hash_anuncio(dados.get("url", ""), dados.get("list_id"))
    preco = dados.get("preco") or 0
    with get_conn() as conn:
        existente = conn.execute(
            "SELECT id, preco, titulo FROM anuncios WHERE busca_id=? AND hash=?", (busca_id, h)
        ).fetchone()

        if existente:
            aid = existente["id"]
            conn.execute("""
                UPDATE anuncios SET ultima_vez=datetime('now'),
                  url=COALESCE(NULLIF(:url,''), url),
                  titulo=COALESCE(NULLIF(:titulo,''), titulo),
                  local=COALESCE(NULLIF(:local,''), local),
                  data_postagem=COALESCE(NULLIF(:data_postagem,''), data_postagem),
                  envio=COALESCE(NULLIF(:envio,''), envio),
                  imagem=COALESCE(NULLIF(:imagem,''), imagem),
                  vendedor=COALESCE(NULLIF(:vendedor,''), vendedor),
                  atributos=COALESCE(NULLIF(:atributos,''), atributos),
                  categoria=COALESCE(NULLIF(:categoria,''), categoria)
                WHERE id=:id
            """, {k: dados.get(k, "") for k in ("url", "titulo", "local", "data_postagem", "envio",
                                                 "imagem", "vendedor", "atributos", "categoria")}
               | {"id": aid})
            if dados.get("titulo") and dados["titulo"] != existente["titulo"]:
                # vendedor editou o título (ex.: corrigiu o modelo): reavalia
                conn.execute("INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade) "
                             "VALUES (?, ?, 2)", (aid, busca_id))
            if preco > 0 and existente["preco"] != preco:
                conn.execute("INSERT INTO historico_precos (anuncio_id, preco) VALUES (?, ?)",
                             (aid, preco))
                conn.execute("UPDATE anuncios SET preco=? WHERE id=?", (preco, aid))
                conn.execute("INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade) "
                             "VALUES (?, ?, 2)", (aid, busca_id))
                return aid, False, existente["preco"]
            return aid, False, None

        cur = conn.execute("""
            INSERT INTO anuncios
              (busca_id, url, hash, titulo, preco, local, data_postagem, envio,
               descricao, imagem, vendedor, atributos, categoria)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (busca_id, dados.get("url", ""), h, dados.get("titulo", ""), preco,
              dados.get("local", ""), dados.get("data_postagem", ""), dados.get("envio", ""),
              dados.get("descricao", ""), dados.get("imagem", ""), dados.get("vendedor", ""),
              dados.get("atributos", ""), dados.get("categoria", "")))
        conn.execute("INSERT INTO historico_precos (anuncio_id, preco) VALUES (?, ?)",
                     (cur.lastrowid, preco))
        return cur.lastrowid, True, None


def conhecidos(busca_id: int, anuncios: list[dict]) -> set[str]:
    """Hashes dos `anuncios` que a busca já tem no banco."""
    hashes = [hash_anuncio(a.get("url", ""), a.get("list_id")) for a in anuncios]
    if not hashes:
        return set()
    with get_conn() as conn:
        return {r[0] for r in conn.execute(
            f"SELECT hash FROM anuncios WHERE busca_id=? AND hash IN ({','.join('?' * len(hashes))})",
            [busca_id, *hashes])}


def anuncios_sem_descricao(busca_id: int, limite: int = 30) -> list[dict]:
    """Anúncios ATIVOS da busca ainda sem descrição. IGNORADO fica de fora:
    buscar descrição de produto errado era 1 visita desperdiçada por anúncio."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT a.id, a.url FROM anuncios a
            LEFT JOIN analises an ON an.anuncio_id = a.id
            WHERE a.busca_id=? AND (a.descricao IS NULL OR a.descricao='')
              AND (an.recomendacao IS NULL OR an.recomendacao != 'IGNORAR')
              AND a.ultima_vez >= datetime('now','-2 days')
            ORDER BY a.primeira_vez DESC LIMIT ?
        """, (busca_id, limite)).fetchall()
        return [dict(r) for r in rows]


def atualizar_descricao(anuncio_id: int, busca_id: int, descricao: str):
    """Grava a descrição e re-enfileira: a avaliação só-título é inferior."""
    with get_conn() as conn:
        conn.execute("UPDATE anuncios SET descricao=? WHERE id=?", (descricao, anuncio_id))
        conn.execute("""INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade)
                        VALUES (?, ?, 1)""", (anuncio_id, busca_id))


def adicionar_fila(anuncio_id: int, busca_id: int, prioridade: int = 0):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade)
            VALUES (?, ?, ?)
        """, (anuncio_id, busca_id, prioridade))


def obter_fila_pendente(limite: int = 5000) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT f.id as fila_id, f.anuncio_id, f.busca_id, f.tentativas, f.prioridade,
                   a.titulo, a.preco, a.local, a.envio, a.descricao, a.url,
                   a.imagem, a.vendedor, a.atributos, a.categoria, a.notificado,
                   (a.ultima_vez >= datetime('now','-2 days')) AS ativo,
                   (SELECT gostou FROM feedback fb WHERE fb.anuncio_id = a.id) AS feedback,
                   b.termo, b.tipo, b.criterios_inclusao, b.criterios_exclusao,
                   b.nome, b.instrucoes, b.condicao, b.categorias, b.preco_max
            FROM fila f
            JOIN anuncios a ON a.id = f.anuncio_id
            JOIN buscas   b ON b.id = f.busca_id
            WHERE f.tentativas < 3
            ORDER BY f.prioridade DESC, f.criado_em ASC
            LIMIT ?
        """, (limite,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def incrementar_tentativas(fila_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE fila SET tentativas=tentativas+1 WHERE id=?", (fila_id,))


def tamanho_fila() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM fila WHERE tentativas < 3").fetchone()[0]


def categorias_da_busca(busca_id: int) -> list[tuple[str, int]]:
    """Categorias da OLX entre os anúncios ativos da busca, mais comuns primeiro."""
    with get_conn() as conn:
        return [tuple(r) for r in conn.execute("""
            SELECT categoria, COUNT(*) FROM anuncios
            WHERE busca_id=? AND categoria != '' AND ultima_vez >= datetime('now','-2 days')
            GROUP BY 1 ORDER BY 2 DESC, 1
        """, (busca_id,))]


def anuncios_comparaveis(busca_id: int) -> list[dict]:
    """Base do preço de referência: anúncios da busca vistos nos últimos 30 dias.
    O avaliador filtra os de identidade errada (a média antiga incluía cabos,
    notebooks e outros modelos: R$3.691 vs R$12.017 reais numa busca de 4090)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, titulo, atributos, descricao, preco, categoria FROM anuncios
            WHERE busca_id=? AND preco > 0 AND ultima_vez >= datetime('now','-30 days')
        """, (busca_id,)).fetchall()
        return [dict(r) for r in rows]


# ── ANÁLISES ──────────────────────────────────────────────

_SQL_ANALISE = """
    INSERT OR REPLACE INTO analises
      (anuncio_id, decisao, resumo, pros, contras, observacoes,
       score, preco_vs_mercado_pct, avaliacao_preco, recomendacao, motivo, alerta)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _params_analise(anuncio_id: int, av: dict) -> tuple:
    return (anuncio_id, av.get("decisao"), av.get("resumo"),
            json.dumps(av.get("pros", []), ensure_ascii=False),
            json.dumps(av.get("contras", []), ensure_ascii=False),
            json.dumps(av.get("observacoes", []), ensure_ascii=False),
            av.get("score"), av.get("preco_vs_mercado_pct"), av.get("avaliacao_preco"),
            av.get("recomendacao"), av.get("motivo"), av.get("alerta") or "")


def salvar_analise(anuncio_id: int, av: dict):
    with get_conn() as conn:
        conn.execute(_SQL_ANALISE, _params_analise(anuncio_id, av))


def salvar_avaliacoes(lote: list[tuple[int, int, dict]]):
    """[(fila_id, anuncio_id, avaliacao)] numa transação só (1 commit por
    anúncio tornava a reavaliação de ~9 mil anúncios lenta)."""
    with get_conn() as conn:
        for fila_id, anuncio_id, av in lote:
            conn.execute(_SQL_ANALISE, _params_analise(anuncio_id, av))
            conn.execute("DELETE FROM fila WHERE id=?", (fila_id,))


def busca_recente(busca_id: int) -> bool:
    """Criada há menos de 24h — o dono ainda espera o resultado do 1º scan."""
    with get_conn() as conn:
        r = conn.execute("SELECT criado_em >= datetime('now','-1 day') FROM buscas "
                         "WHERE id=?", (busca_id,)).fetchone()
        return bool(r and r[0])


def analise_de(anuncio_id: int) -> dict | None:
    """decisao/recomendacao/alerta salvos do anúncio (None se ainda não avaliado)."""
    with get_conn() as conn:
        r = conn.execute("SELECT decisao, recomendacao, alerta FROM analises WHERE anuncio_id=?",
                         (anuncio_id,)).fetchone()
        return dict(r) if r else None


def marcar_notificado(anuncio_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE anuncios SET notificado=1 WHERE id=?", (anuncio_id,))


def listar_resultados(busca_id: int, recomendacao: str = None,
                      limite: int = 50, offset: int = 0) -> list[dict]:
    with get_conn() as conn:
        filtro = "AND an.recomendacao=?" if recomendacao else ""
        params = [busca_id]
        if recomendacao:
            params.append(recomendacao)
        params += [limite, offset]

        rows = conn.execute(f"""
            SELECT a.*, an.decisao, an.resumo, an.pros, an.contras,
                   an.observacoes, an.score, an.preco_vs_mercado_pct,
                   an.avaliacao_preco, an.recomendacao, an.motivo, an.gerado_em, an.alerta,
                   (a.ultima_vez >= datetime('now','-2 days')) AS ativo,
                   (SELECT gostou FROM feedback fb WHERE fb.anuncio_id = a.id) AS feedback
            FROM anuncios a
            LEFT JOIN analises an ON an.anuncio_id = a.id
            WHERE a.busca_id=? {filtro}
            ORDER BY ativo DESC, an.score DESC NULLS LAST, a.primeira_vez DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def stats_busca(busca_id: int) -> dict:
    with get_conn() as conn:
        stats = conn.execute("""
            SELECT
                COUNT(a.id) as total,
                AVG(an.score) as score_medio,
                MIN(a.preco) as preco_min,
                MAX(a.preco) as preco_max,
                AVG(a.preco) as preco_medio,
                COUNT(CASE WHEN an.recomendacao='COMPRAR' THEN 1 END) as qtd_comprar,
                COUNT(CASE WHEN an.recomendacao='VER' THEN 1 END) as qtd_ver,
                COUNT(CASE WHEN an.recomendacao='IGNORAR' THEN 1 END) as qtd_ignorar
            FROM anuncios a
            LEFT JOIN analises an ON an.anuncio_id = a.id
            WHERE a.busca_id=? AND (an.decisao IS NULL OR an.decisao='APROVADO')
        """, (busca_id,)).fetchone()

        historico = conn.execute("""
            SELECT DATE(hp.registrado_em) as dia,
                   AVG(hp.preco) as preco_medio,
                   MIN(hp.preco) as preco_min,
                   COUNT(*) as quantidade
            FROM historico_precos hp
            JOIN anuncios a ON a.id = hp.anuncio_id
            JOIN analises an ON an.anuncio_id = a.id AND an.decisao='APROVADO'
            WHERE a.busca_id=?
            GROUP BY DATE(hp.registrado_em)
            ORDER BY dia DESC
            LIMIT 30
        """, (busca_id,)).fetchall()

        scores = conn.execute("""
            SELECT an.score, COUNT(*) as quantidade
            FROM analises an
            JOIN anuncios a ON a.id = an.anuncio_id
            WHERE a.busca_id=? AND an.score IS NOT NULL
            GROUP BY an.score
            ORDER BY an.score
        """, (busca_id,)).fetchall()

        return {
            **_row_to_dict(stats),
            "historico_precos": [_row_to_dict(r) for r in historico],
            "distribuicao_scores": [_row_to_dict(r) for r in scores],
        }


def top_analises(busca_id: int = None, n: int = 5) -> list[dict]:
    """Top-N por score (p/ o /top do Telegram). Só anúncios ATIVOS: com o full
    scan diário, quem não é visto há 2 dias provavelmente expirou."""
    with get_conn() as conn:
        filtro = "AND a.busca_id=?" if busca_id else ""
        params = ([busca_id] if busca_id else []) + [n]
        rows = conn.execute(f"""
            SELECT a.titulo, a.preco, a.url, a.vendedor, an.score, an.recomendacao,
                   an.avaliacao_preco, b.nome
            FROM analises an
            JOIN anuncios a ON a.id = an.anuncio_id
            JOIN buscas b ON b.id = a.busca_id
            WHERE a.ultima_vez >= datetime('now', '-2 days')
              AND an.decisao = 'APROVADO' {filtro}
            ORDER BY an.score DESC, a.preco ASC LIMIT ?
        """, params).fetchall()
        return [dict(r) for r in rows]


# ── FEEDBACK (classificador) ──────────────────────────────

def registrar_feedback(anuncio_id: int, gostou) -> bool:
    """gostou: True/False grava (sobrescreve); None remove. False se o anúncio não existe.
    Reenfileira os anúncios ATIVOS em silêncio: o modelo mudou."""
    with get_conn() as conn:
        a = conn.execute("""
            SELECT a.id, b.termo, a.titulo, a.descricao, a.atributos, a.vendedor,
                   a.categoria, a.preco, an.avaliacao_preco
            FROM anuncios a JOIN buscas b ON b.id = a.busca_id
            LEFT JOIN analises an ON an.anuncio_id = a.id WHERE a.id=?
        """, (anuncio_id,)).fetchone()
        if not a:
            return False
        if gostou is None:
            conn.execute("DELETE FROM feedback WHERE anuncio_id=?", (anuncio_id,))
        else:
            conn.execute("""
                INSERT INTO feedback (anuncio_id, termo, titulo, descricao, atributos, vendedor,
                                      categoria, preco, avaliacao_preco, gostou)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(anuncio_id) DO UPDATE SET gostou=excluded.gostou,
                    criado_em=datetime('now')
            """, (*tuple(a), int(bool(gostou))))
        conn.execute("""INSERT OR IGNORE INTO fila (anuncio_id, busca_id, prioridade)
                        SELECT id, busca_id, -1 FROM anuncios
                        WHERE ultima_vez >= datetime('now','-2 days')""")
        return True


def feedbacks() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM feedback")]


# ── HELPERS ───────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for k in ("pros", "contras", "observacoes", "criterios_inclusao", "criterios_exclusao",
              "filtros_extra", "categorias"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d
