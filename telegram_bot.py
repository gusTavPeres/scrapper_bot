"""
telegram_bot.py — comandos via Telegram (long-polling getUpdates).
Só aceita comandos do CHAT_ID configurado no .env. Nunca configurar webhook
neste bot: mataria o getUpdates.
"""
import asyncio
import logging
import os
import re

import requests
from pydantic import ValidationError

import database as db
from models import BuscaCreate, mensagem_erro
import avaliador
from notifier import _enviar, _post, esc, sem_token, teclado

logger = logging.getLogger(__name__)

TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_OPCOES_COMUNS = (
    "<code>min=500 max=2000</code> — faixa de preço\n"
    "<code>estado=go</code> — UF ou 'brasil'\n"
    "<code>envio=com_envio|sem_envio</code>\n"
    "<code>vendedor=particular|loja</code>\n"
    "<code>garantia=olx|entrega</code> — só com Garantia da OLX (+ entrega)\n"
    "<code>condicao=funcionando|defeito|qualquer</code> — 'defeito' = procuro peça\n"
    "   quebrada/para peças (defeito conta a favor). Termo com 'defeito' já ativa.\n"
    "<code>intervalo=30</code> — minutos entre scans\n"
    "<code>obs=</code> regras em texto livre, viram palavras-chave:\n"
    "   'sem X' / 'nada de X' exclui · 'só com X' exige · 'prefiro X' pontua\n"
    "   (tudo depois de obs= vira regra; deixe por último)\n"
)

_AJUDA_ADD = (
    "➕ <b>/add &lt;termo&gt; [opções]</b> — busca geral na OLX\n\n" + _OPCOES_COMUNS +
    "\nEx.: <code>/add rtx 4090 max=8000 vendedor=particular "
    "obs= só com nota fiscal, nada de mineração</code>"
)

_AJUDA_ADDCARRO = (
    "🚗 <b>/addcarro [modelo] [opções]</b> — busca na categoria de CARROS\n"
    "Sem modelo = TODOS os carros que baterem nos filtros.\n\n"
    "<b>Específicos de carro:</b>\n"
    "<code>ano=2010-2015</code> — faixa de ano\n"
    "<code>km=120000</code> — quilometragem máxima\n"
    "<code>motor=1.8</code> — litragem\n\n" + _OPCOES_COMUNS +
    "\nEx.: <code>/addcarro min=15000 max=35000 ano=2009-2014 motor=1.8 "
    "estado=go obs= sem leilão nem batido, prefiro manual</code>\n"
    "Ex.: <code>/addcarro linea ano=2011-2016 estado=go</code>"
)

_HELP = (
    "🤖 <b>Monitor OLX — comandos</b>\n\n"
    "/add &lt;termo&gt; [opções] — nova busca (envie /add vazio p/ ver opções)\n"
    "/addcarro [modelo] [opções] — busca de carros (ano/km/motor)\n"
    "/buscas — lista as buscas\n"
    "/top [id] — melhores achados ativos\n"
    "/status — fila e buscas\n"
    "/categorias &lt;id&gt; — categorias da OLX achadas; filtre com /categorias &lt;id&gt; 1,3\n"
    "/scan &lt;id&gt; · /pausar &lt;id&gt; · /retomar &lt;id&gt; · /del &lt;id&gt;\n\n"
    "Chegam sozinhos: 🟢 COMPRAR, 🟡 VALE NEGOCIAR (o preço compensa uma ressalva) e "
    "💸 queda de preço. Os botões 👍/👎 dos alertas ensinam o bot."
)

_ENVIO = {"gratis": "frete_gratis", "frete": "frete_gratis", "frete_gratis": "frete_gratis",
          "com_envio": "com_envio", "sem_envio": "sem_envio", "qualquer": "qualquer"}
_GARANTIA = {"olx": "pay", "sim": "pay", "entrega": "pay_entrega", "nao": "", "não": ""}
_PRECO_TXT = {"bom": "preço bom", "justo": "preço justo", "caro": "caro", "suspeito": "preço suspeito",
              "sem_base": "sem referência", "negociavel": "negociável"}


def _inteiro(v: str) -> int:
    """'1.900', '1900', '1.900,00' -> 1900 (ponto de milhar é o jeito brasileiro)."""
    return int(float(v.replace(".", "").replace(",", ".")))


def _parse_add(args: list[str], tipo: str = "produto"):
    """Separa termo de opções chave=valor. Retorna (dados, erro).
    `obs=` é GULOSO: tudo depois dele vira instrução livre."""
    texto = " ".join(args)
    obs = ""
    if (m := re.search(r"(?i)\bobs=", texto)):          # OBS= / Obs= também
        obs = texto[m.end():].strip()
        texto = texto[:m.start()]

    dados, termo, extras = {"tipo": tipo, "instrucoes": obs}, [], {}
    for tok in texto.split():
        if "=" not in tok:
            termo.append(tok)
            continue
        k, v = tok.split("=", 1)
        k, v = k.lower(), v.lower().strip()
        try:
            if k in ("min", "preco_min"):
                dados["preco_min"] = _inteiro(v)
            elif k in ("max", "preco_max"):
                dados["preco_max"] = _inteiro(v)
            elif k == "estado":
                dados["estado"] = v
            elif k == "envio":
                dados["filtro_envio"] = _ENVIO[v]
            elif k == "vendedor":
                dados["filtro_vendedor"] = v
            elif k == "garantia":
                dados["garantia"] = _GARANTIA[v]
            elif k in ("condicao", "condição"):
                dados["condicao"] = v
            elif k in ("tipo", "categoria"):
                dados["tipo"] = v
            elif k == "intervalo":
                dados["intervalo_min"] = max(5, min(1440, int(v)))
            elif k == "ano" and tipo == "carro":
                m = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", v) or re.fullmatch(r"(\d{4})", v)
                if not m:
                    raise KeyError
                extras["ano_min"] = int(m.group(1))
                extras["ano_max"] = int(m.group(m.lastindex))
            elif k == "km" and tipo == "carro":
                extras["km_max"] = _inteiro(v)
            elif k == "motor" and tipo == "carro":
                extras["motor"] = v
            else:
                return None, f"Opção desconhecida: <code>{esc(k)}=</code> — veja /help"
        except (ValueError, KeyError):
            return None, f"Valor inválido em <code>{esc(tok)}</code> — veja /help"
    dados["termo"] = " ".join(termo).strip()
    dados["filtros_extra"] = extras
    return dados, None


def _criar(dados: dict) -> tuple[int | None, str]:
    """Valida com o MESMO schema da API web (antes o Telegram gravava direto)."""
    try:
        busca = BuscaCreate(**dados)
    except ValidationError as e:
        erros = "; ".join(dict.fromkeys(mensagem_erro(err) for err in e.errors()))
        return None, f"❌ {esc(erros)}"
    return db.criar_busca(busca.model_dump()), ""


def status_classificador() -> str:
    rot = db.feedbacks()
    bons = sum(r["gostou"] for r in rot)
    m = avaliador.Classificador(rot)
    estado = "ativo" if m.pronto else f"aprendendo (age a partir de {avaliador.MIN_ROTULOS}, ≥{avaliador.MIN_POR_CLASSE} de cada)"
    return f"🧠 classificador: {len(rot)} avaliações (👍{bons} 👎{len(rot) - bons}) — {estado}"


def tratar_callback(dados: str) -> tuple[str, dict | None]:
    """Clique em 👍/👎 de um alerta -> (texto do aviso, teclado atualizado)."""
    m = re.fullmatch(r"fb:(\d{1,12}):([01])", dados or "")
    if not m:
        return "Botão desconhecido", None
    aid, gostou = int(m.group(1)), m.group(2) == "1"
    if not db.registrar_feedback(aid, gostou):
        return "Esse anúncio não está mais no banco", None
    return (("👍 Anotado: vou priorizar parecidos" if gostou else "👎 Anotado: vou evitar parecidos"),
            teclado(aid, gostou))


def tratar_comando(texto: str) -> str:
    """Interpreta um comando e retorna a resposta (testável offline)."""
    partes = texto.strip().split()
    cmd = partes[0].lstrip("/").split("@")[0].lower() if partes else ""

    if cmd in ("start", "help", "ajuda"):
        return _HELP

    if cmd == "status":
        buscas = db.listar_buscas()
        por = {s: sum(1 for b in buscas if b["status"] == s) for s in ("ativa", "pausada", "erro")}
        return (f"📊 fila de avaliação: <b>{db.tamanho_fila()}</b>\n"
                f"buscas: {por['ativa']} ativas · {por['pausada']} pausadas · {por['erro']} com erro\n"
                + status_classificador())

    if cmd == "buscas":
        buscas = db.listar_buscas()
        if not buscas:
            return "Nenhuma busca criada. Use /add &lt;termo&gt;."
        return "\n".join(
            f"#{b['id']} <b>{esc(b['nome'])}</b> "
            f"{b['status'] if b['status'] != 'ativa' else b['fase']} · "
            f"🟢{b['total_comprar']} 🟡{b['total_ver']} ({b['total_anuncios']} anúncios)"
            for b in buscas)

    if cmd in ("add", "addcarro"):
        tipo = "carro" if cmd == "addcarro" else "produto"
        if len(partes) == 1:                       # sem argumentos: mostra as opções
            return _AJUDA_ADDCARRO if tipo == "carro" else _AJUDA_ADD
        dados, erro = _parse_add(list(partes[1:]), tipo=tipo)
        if erro:
            return erro
        extras = dados["filtros_extra"]
        if not dados["termo"] and dados["tipo"] != "carro":
            return _AJUDA_ADD
        if not dados["termo"] and not (extras or dados.get("preco_min") or dados.get("preco_max")):
            return _AJUDA_ADDCARRO           # todos-os-carros exige ao menos 1 filtro
        if not dados["termo"]:
            faixa = f"{dados.get('preco_min', 0) or 0}-{dados.get('preco_max', 0) or '∞'}"
            ano = f" {extras['ano_min']}-{extras['ano_max']}" if extras.get("ano_min") else ""
            dados["nome"] = f"carros {faixa}{ano}"
        else:
            dados["nome"] = dados["termo"]
        bid, erro = _criar(dados)
        if erro:
            return erro
        det = []
        if extras.get("ano_min"):
            det.append(f"📅 {extras['ano_min']}–{extras['ano_max']}")
        if extras.get("km_max"):
            det.append(f"🛣 até {extras['km_max']:,} km".replace(",", "."))
        if extras.get("motor"):
            det.append(f"⚙ motor {extras['motor']}")
        if dados.get("instrucoes"):
            det.append(f"📝 “{esc(dados['instrucoes'][:40])}”")
        if dados.get("preco_min") or dados.get("preco_max"):
            det.append(f"💰 {dados.get('preco_min', 0) or 0}–{dados.get('preco_max', 0) or '∞'}")
        if dados.get("estado"):
            det.append(f"📍 {esc(dados['estado'].upper())}")
        for campo, emoji in (("filtro_envio", "📦"), ("filtro_vendedor", "👤"), ("garantia", "🛡")):
            if dados.get(campo):
                det.append(f"{emoji} {esc(dados[campo])}")
        det.append(f"🔧 {db.obter_busca(bid)['condicao']}")
        det.append(dados["tipo"])
        return (f"✅ Busca <b>#{bid}</b> criada: “{esc(dados['nome'])}”"
                f"\n{' · '.join(det)}\nScan começa em ~30 s.")

    if cmd == "top":
        bid = None
        if len(partes) > 1:
            try:
                bid = int(partes[1])
            except ValueError:
                return "Uso: /top [id da busca]"
        tops = db.top_analises(bid)
        if not tops:
            return "Nenhum achado ativo (anúncios vistos nos últimos 2 dias)."
        linhas = ["🏆 <b>Melhores achados ativos</b>\n"]
        for t in tops:
            preco = f"{t['preco'] or 0:,.0f}".replace(",", ".")
            linhas.append(f"{t['score']}/10 · <b>R$ {preco}</b> "
                          f"({_PRECO_TXT.get(t['avaliacao_preco'], t['avaliacao_preco'])}) — "
                          f"<a href=\"{esc(t['url'])}\">{esc((t['titulo'] or '?')[:45])}</a>")
        return "\n".join(linhas)

    if cmd == "categorias":
        try:
            bid = int(partes[1])
        except (IndexError, ValueError):
            return "Uso: /categorias &lt;id&gt; [números|todas]"
        busca = db.obter_busca(bid)
        if not busca:
            return f"Busca #{bid} não existe."
        cats = db.categorias_da_busca(bid)
        if not cats:
            return "Nenhum anúncio ativo ainda — rode um scan primeiro (/scan)."
        if len(partes) > 2:
            escolha = " ".join(partes[2:]).lower()
            if escolha in ("todas", "0"):
                sel = []
            else:
                nums = [n for n in re.split(r"[,\s]+", escolha) if n]
                if not all(n.isdigit() and 1 <= int(n) <= len(cats) for n in nums):
                    # (índice negativo do Python escolhia a categoria ERRADA em silêncio)
                    return f"Números inválidos (1 a {len(cats)}) — veja a lista com /categorias {bid}"
                sel = [cats[int(n) - 1][0] for n in nums]
            db.atualizar_busca(bid, {"categorias": sel})
            busca = db.obter_busca(bid)
        sel = busca.get("categorias") or []
        linhas = [f"🗂 <b>{esc(busca['nome'])}</b> — "
                  + ("filtrando: só as marcadas ✅" if sel else "aceitando TODAS")]
        linhas += [f"{i}. {'✅ ' if c in sel else ''}{esc(c)} ({n})" for i, (c, n) in enumerate(cats, 1)]
        linhas.append(f"\nFiltrar: <code>/categorias {bid} 1,2</code> · liberar: "
                      f"<code>/categorias {bid} todas</code>")
        return "\n".join(linhas)

    if cmd in ("pausar", "retomar", "del", "scan"):
        try:
            bid = int(partes[1])
        except (IndexError, ValueError):
            return f"Uso: /{cmd} &lt;id&gt;  (veja os ids com /buscas)"
        busca = db.obter_busca(bid)
        if not busca:
            return f"Busca #{bid} não existe."
        nome = esc(busca["nome"])
        if cmd == "pausar":
            db.pausar_busca(bid)
            return f"⏸ Busca #{bid} ({nome}) pausada."
        if cmd == "retomar":
            db.retomar_busca(bid)
            return f"▶ Busca #{bid} ({nome}) retomada."
        if cmd == "scan":
            if busca["status"] == "pausada":
                return f"Busca #{bid} está pausada — use /retomar {bid} primeiro."
            db.forcar_scan(bid)
            return f"🔄 Scan de #{bid} ({nome}) agendado — começa em ~30 s."
        if len(partes) < 3 or partes[2].lower() not in ("sim", "s", "confirmo"):
            return (f"Apagar #{bid} ({nome}) e TODO o histórico dela? Não dá p/ desfazer.\n"
                    f"Confirme com <code>/del {bid} sim</code>")
        db.deletar_busca(bid)
        return f"🗑 Busca #{bid} ({nome}) apagada."

    return "Comando desconhecido — /help"


async def loop_comandos():
    """Long-polling do getUpdates; responde só ao CHAT_ID do dono."""
    if not TOKEN:
        logger.warning("telegram: sem TELEGRAM_TOKEN — notificações e comandos desativados")
        return
    if not CHAT_ID:
        logger.warning("telegram: sem TELEGRAM_CHAT_ID — mande qualquer mensagem ao bot "
                       "que ele responde com o seu chat id")
    api = f"https://api.telegram.org/bot{TOKEN}"
    try:   # menu nativo: digitar "/" no chat mostra os comandos com descrição
        await asyncio.to_thread(requests.post, f"{api}/setMyCommands", json={"commands": [
            {"command": "add", "description": "nova busca — envie vazio p/ ver as opções"},
            {"command": "addcarro", "description": "busca de carros (ano= km= motor=); sem modelo = todos"},
            {"command": "buscas", "description": "listar buscas e contadores"},
            {"command": "top", "description": "melhores achados ativos"},
            {"command": "status", "description": "fila e buscas"},
            {"command": "categorias", "description": "categorias da OLX na busca: /categorias <id> [1,2|todas]"},
            {"command": "scan", "description": "scan imediato: /scan <id>"},
            {"command": "pausar", "description": "pausar busca: /pausar <id>"},
            {"command": "retomar", "description": "retomar busca: /retomar <id>"},
            {"command": "del", "description": "apagar busca: /del <id> sim"},
            {"command": "help", "description": "ajuda completa"},
        ]}, timeout=10)
    except Exception as e:
        logger.warning(f"telegram setMyCommands: {sem_token(e)}")
    offset = 0
    logger.info("telegram: escutando comandos (long-polling)")
    while True:
        try:
            r = await asyncio.to_thread(
                requests.get, f"{api}/getUpdates",
                params={"timeout": 50, "offset": offset}, timeout=60)
            for up in r.json().get("result", []):
                offset = up["update_id"] + 1
                cq = up.get("callback_query")
                if cq:
                    if str((cq.get("message") or {}).get("chat", {}).get("id")) != str(CHAT_ID):
                        continue
                    aviso, botoes = tratar_callback(cq.get("data", ""))
                    logger.info(f"telegram: botão {cq.get('data')!r} -> {aviso}")
                    chamadas = [("answerCallbackQuery", {"callback_query_id": cq["id"], "text": aviso})]
                    if botoes:
                        chamadas.append(("editMessageReplyMarkup", {
                            "chat_id": CHAT_ID, "message_id": cq["message"]["message_id"],
                            "reply_markup": botoes}))
                    for metodo, corpo in chamadas:
                        rr = await asyncio.to_thread(requests.post, f"{api}/{metodo}",
                                                     json=corpo, timeout=10)
                        if not rr.ok:
                            logger.warning(f"telegram {metodo}: {rr.status_code} {rr.text[:150]}")
                    continue
                msg = up.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if not CHAT_ID and chat:      # instalação nova: ensina o chat id
                    await asyncio.to_thread(_post, "sendMessage", {"chat_id": chat, "text":
                        f"Seu chat id é <code>{chat}</code>. Coloque <code>TELEGRAM_CHAT_ID={chat}</code> "
                        "no .env e reinicie o bot."}, 10)
                    continue
                if str(chat) != str(CHAT_ID):
                    continue  # ignora estranhos
                texto = msg.get("text", "")
                if texto.startswith("/"):
                    logger.info(f"telegram: comando {texto!r}")
                    try:
                        resposta = tratar_comando(texto)
                    except Exception as e:
                        logger.exception("telegram: falha no comando")
                        resposta = f"❌ erro interno: {esc(e)}"
                    await asyncio.to_thread(_enviar, resposta)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            # long-poll expirado ou conexão resetada — rotina de rede, sem alarde
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"telegram polling: {sem_token(e)}")
            await asyncio.sleep(5)
