"""
main.py — FastAPI + WebSocket + static files
"""
import asyncio
import base64
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

def _carregar_env(arquivo: Path):
    """.env ao lado do main.py vira variável de ambiente (sem sobrescrever as já
    definidas por systemd/Docker). Precisa rodar ANTES de importar notifier e
    telegram_bot, que leem o token na importação. Assim `python main.py` funciona
    igual em Linux, macOS e Windows, sem `source .env`."""
    if arquivo.is_file():
        for linha in arquivo.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                k, v = linha.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_carregar_env(Path(__file__).parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status  # noqa: E402
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

import avaliador
import database as db
import scheduler
import telegram_bot
from models import FUSO, BuscaCreate, BuscaUpdate, Feedback, mensagem_erro

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# log no horário de Brasília mesmo em container/servidor em UTC
logging.Formatter.converter = staticmethod(lambda ts: datetime.fromtimestamp(ts, FUSO).timetuple())
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("main")      # estável também quando roda como __main__

# credenciais vêm do .env (systemd EnvironmentFile / run.sh); sem senha = não sobe
USUARIO = os.getenv("BOT_USER", "admin")
SENHA   = os.getenv("BOT_PASS", "")
FRONTEND_DIR = Path(__file__).parent / "frontend"
security = HTTPBasic()


def _credenciais_ok(usuario: str, senha: str) -> bool:
    ok_user = secrets.compare_digest(usuario.encode(), USUARIO.encode())
    ok_pass = secrets.compare_digest(senha.encode(), SENHA.encode())
    return bool(SENHA) and ok_user and ok_pass


def verificar_auth(creds: HTTPBasicCredentials = Depends(security)):
    if not _credenciais_ok(creds.username, creds.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Basic"})
    return creds.username


# ── WebSocket ─────────────────────────────────────────────

class WSManager:
    def __init__(self):
        self.conexoes: list[WebSocket] = []

    async def conectar(self, ws: WebSocket):
        await ws.accept()
        self.conexoes.append(ws)

    def desconectar(self, ws: WebSocket):
        if ws in self.conexoes:          # o broadcast pode já ter removido
            self.conexoes.remove(ws)

    async def broadcast(self, msg: dict):
        texto = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.conexoes):
            try:
                await ws.send_text(texto)
            except Exception:
                self.desconectar(ws)


ws_manager = WSManager()


async def broadcast(msg: dict):
    await ws_manager.broadcast(msg)


# ── Ciclo de vida ─────────────────────────────────────────

def _task_morreu(t: asyncio.Task):
    # sem isso, uma task que morre com exceção some EM SILÊNCIO
    if not t.cancelled() and t.exception():
        logger.critical(f"TASK {t.get_name()} MORREU: {t.exception()!r}", exc_info=t.exception())


async def _watchdog():
    """Scheduler sem heartbeat há 45 min = travado -> suicídio; systemd revive.
    O heartbeat é por busca (cada uma tem teto de 15 min)."""
    while True:
        await asyncio.sleep(60)
        parado = time.time() - scheduler.ultimo_ciclo
        if parado > 2700:
            logger.critical(f"WATCHDOG: scheduler sem ciclo há {parado:.0f}s — reiniciando o processo")
            os._exit(1)   # Restart=on-failure do systemd assume daqui


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SENHA:
        raise RuntimeError("BOT_PASS vazio no .env — defina uma senha para o painel")
    db.init_db()
    avaliador.set_broadcast(broadcast)
    scheduler.set_broadcast(broadcast)
    # guarda referências: o event loop só mantém weakrefs de tasks
    app.state.tasks = [
        asyncio.create_task(scheduler.loop_scheduler(), name="scheduler"),
        asyncio.create_task(telegram_bot.loop_comandos(), name="telegram"),
        asyncio.create_task(_watchdog(), name="watchdog"),
    ]
    for t in app.state.tasks:
        t.add_done_callback(_task_morreu)
    logger.info("Bot iniciado ✓")
    yield
    for t in app.state.tasks:
        t.cancel()


app = FastAPI(title="Monitor OLX", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def erro_de_validacao(request, exc: RequestValidationError):
    """Mensagem em português no painel (o padrão do pydantic é em inglês)."""
    return JSONResponse(status_code=422, content={"detail": "; ".join(
        dict.fromkeys(mensagem_erro(e) for e in exc.errors()))})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # o navegador reenvia o Basic auth da página no handshake do WS
    try:
        tipo, b64 = ws.headers.get("authorization", "").split(" ", 1)
        usuario, senha = base64.b64decode(b64).decode().split(":", 1)
        autorizado = tipo.lower() == "basic" and _credenciais_ok(usuario, senha)
    except ValueError:
        autorizado = False
    if not autorizado:
        await ws.close(code=1008)
        return
    await ws_manager.conectar(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.desconectar(ws)


# ── REST — Buscas ─────────────────────────────────────────

def _busca_ou_404(bid: int) -> dict:
    busca = db.obter_busca(bid)
    if not busca:
        raise HTTPException(404, "Busca não encontrada")
    return busca


@app.get("/api/buscas")
async def listar_buscas(_=Depends(verificar_auth)):
    return db.listar_buscas()


@app.post("/api/buscas", status_code=201)
async def criar_busca(dados: BuscaCreate, _=Depends(verificar_auth)):
    bid = db.criar_busca(dados.model_dump())
    await broadcast({"tipo": "busca_criada", "busca_id": bid})
    return {"id": bid}


@app.patch("/api/buscas/{bid}")
async def atualizar_busca(bid: int, dados: BuscaUpdate, _=Depends(verificar_auth)):
    atual = _busca_ou_404(bid)
    campos = dados.model_dump(exclude_none=True)
    final = {**atual, **campos}            # coerência vale p/ o resultado da edição
    if final["preco_min"] and final["preco_max"] and final["preco_min"] > final["preco_max"]:
        raise HTTPException(422, "preço mínimo maior que o preço máximo")
    if not final["termo"] and final["tipo"] != "carro":
        raise HTTPException(422, "termo obrigatório (só carro aceita busca sem modelo)")
    mudou = db.atualizar_busca(bid, campos)
    await broadcast({"tipo": "busca_status", "busca_id": bid})
    return {"ok": True, "alterados": sorted(mudou)}


@app.post("/api/buscas/{bid}/pausar")
async def pausar(bid: int, _=Depends(verificar_auth)):
    _busca_ou_404(bid)
    db.pausar_busca(bid)
    await broadcast({"tipo": "busca_status", "busca_id": bid, "status": "pausada"})
    return {"ok": True}


@app.post("/api/buscas/{bid}/retomar")
async def retomar(bid: int, _=Depends(verificar_auth)):
    _busca_ou_404(bid)
    db.retomar_busca(bid)
    await broadcast({"tipo": "busca_status", "busca_id": bid, "status": "ativa"})
    return {"ok": True}


@app.delete("/api/buscas/{bid}")
async def deletar(bid: int, _=Depends(verificar_auth)):
    db.deletar_busca(bid)
    await broadcast({"tipo": "busca_deletada", "busca_id": bid})
    return {"ok": True}


@app.post("/api/buscas/{bid}/scan-agora")
async def scan_agora(bid: int, _=Depends(verificar_auth)):
    """Força scan imediato (não despausa: retome antes)."""
    if _busca_ou_404(bid)["status"] == "pausada":
        raise HTTPException(409, "Busca pausada — retome antes de escanear")
    db.forcar_scan(bid)
    return {"ok": True}


# ── REST — Resultados ─────────────────────────────────────

@app.get("/api/buscas/{bid}/resultados")
async def resultados(bid: int, recomendacao: Optional[str] = None,
                     limite: int = 50, offset: int = 0, _=Depends(verificar_auth)):
    return db.listar_resultados(bid, recomendacao, min(max(limite, 1), 1000), max(offset, 0))


@app.get("/api/buscas/{bid}/categorias")
async def categorias(bid: int, _=Depends(verificar_auth)):
    """Categorias da OLX entre os anúncios ativos (p/ o filtro opcional da busca)."""
    sel = _busca_ou_404(bid).get("categorias") or []
    achadas = db.categorias_da_busca(bid)
    nomes = {c for c, _ in achadas}
    return ([{"nome": c, "n": n, "selecionada": c in sel} for c, n in achadas]
            + [{"nome": c, "n": 0, "selecionada": True} for c in sel if c not in nomes])


@app.post("/api/anuncios/{aid}/feedback")
async def feedback(aid: int, dados: Feedback, _=Depends(verificar_auth)):
    """👍/👎 do painel: alimenta o classificador (mesmo efeito do botão no Telegram)."""
    if not db.registrar_feedback(aid, dados.gostou):
        raise HTTPException(404, "Anúncio não encontrado")
    return {"ok": True}


@app.get("/api/buscas/{bid}/stats")
async def stats(bid: int, _=Depends(verificar_auth)):
    return db.stats_busca(bid)


@app.get("/api/status")
async def status_sistema(_=Depends(verificar_auth)):
    buscas = db.listar_buscas()
    return {
        "fila_pendente":        db.tamanho_fila(),
        "buscas_ativas":        sum(1 for b in buscas if b["status"] != "pausada"),
        "buscas_pausadas":      sum(1 for b in buscas if b["status"] == "pausada"),
        "ultimo_processamento": avaliador.ultimo_processamento,
        "classificador":        telegram_bot.status_classificador().removeprefix("🧠 "),
        "agora":                datetime.now(FUSO).isoformat(timespec="seconds"),
    }


# ── Frontend ──────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index(_=Depends(verificar_auth)):
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":     # ponto de entrada único: run.sh, systemd e Docker
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")),
                access_log=False)
