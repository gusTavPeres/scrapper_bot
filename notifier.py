"""
notifier.py — Notificações Telegram (parse_mode HTML)

Todo texto vindo de anúncio passa por esc(): um título com '<' ou '&' fazia o
Telegram recusar a mensagem e o alerta sumia sem rastro.
"""
import asyncio
import html
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_REC_EMOJI = {"COMPRAR": "🟢", "VER": "🟡", "IGNORAR": "🔴"}
_AVP_EMOJI = {
    "bom":      "💚 Bom preço",
    "justo":    "🟡 Preço justo",
    "caro":     "🔴 Caro",
    "suspeito": "⚠️ Preço suspeito",
    "sem_base": "📊 Sem referência",
    "negociavel": "🤝 Pede acima do teto, mas aceita proposta",
}


def esc(texto) -> str:
    return html.escape(str(texto if texto is not None else ""), quote=True)


def sem_token(texto) -> str:
    """Exceções do requests trazem a URL com o token — nunca no log."""
    texto = str(texto)
    return texto.replace(TOKEN, "<TOKEN>") if TOKEN else texto


def _brl(v) -> str:
    return "R$ " + f"{v or 0:,.0f}".replace(",", ".")


def _post(metodo: str, dados: dict, timeout: int) -> bool:
    """`dados` pode trazer chat_id próprio (resposta a quem ainda não é o dono)."""
    if not TOKEN or not (CHAT_ID or dados.get("chat_id")):
        logger.warning("Telegram não configurado.")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{metodo}",
                          json={"chat_id": CHAT_ID, "parse_mode": "HTML", **dados},
                          timeout=timeout)
        if not r.ok:
            logger.error(f"Telegram {metodo} recusou ({r.status_code}): {r.text[:200]}")
        return r.ok
    except Exception as e:
        logger.error(f"Telegram {metodo}: {sem_token(e)}")
        return False


def teclado(anuncio_id, marcado=None) -> dict | None:
    """Botões 👍/👎 que alimentam o classificador (callback 'fb:<id>:<0|1>')."""
    if not anuncio_id:
        return None
    return {"inline_keyboard": [[
        {"text": ("✅ " if marcado is True else "") + "👍 bom achado", "callback_data": f"fb:{anuncio_id}:1"},
        {"text": ("✅ " if marcado is False else "") + "👎 não quero", "callback_data": f"fb:{anuncio_id}:0"},
    ]]}


def _partes(texto: str, limite: int = 4096) -> list[str]:
    """Divide em linhas inteiras (cortar no meio de uma tag HTML faz o Telegram
    recusar a mensagem toda)."""
    partes, atual = [], ""
    for linha in texto.split("\n"):
        while len(linha) > limite:              # linha sozinha maior que o limite
            partes.append(linha[:limite]); linha = linha[limite:]
        if len(atual) + len(linha) + 1 > limite:
            partes.append(atual); atual = ""
        atual = f"{atual}\n{linha}" if atual else linha
    return partes + [atual] if atual or not partes else partes


def _enviar(texto: str, botoes: dict | None = None) -> bool:
    partes = _partes(texto)
    ok = True
    for i, parte in enumerate(partes):        # botões só na última parte
        extra = {"reply_markup": botoes} if botoes and i == len(partes) - 1 else {}
        ok = _post("sendMessage", {"text": parte, "disable_web_page_preview": True, **extra}, 10) and ok
    return ok


def _enviar_foto(foto_url: str, legenda: str, botoes: dict | None = None) -> bool:
    """sendPhoto; legenda > 1024 não cabe (cortar quebraria o HTML) -> False
    e o chamador manda texto."""
    if not foto_url or len(legenda) > 1024:
        return False
    extra = {"reply_markup": botoes} if botoes else {}
    return _post("sendPhoto", {"photo": foto_url, "caption": legenda, **extra}, 15)


async def _enviar_com_foto(texto: str, foto_url: str, anuncio_id=None) -> bool:
    botoes = teclado(anuncio_id)
    if await asyncio.to_thread(_enviar_foto, foto_url, texto, botoes):
        return True
    return await asyncio.to_thread(_enviar, texto, botoes)


async def enviar_resultado(anuncio: dict, avaliacao: dict) -> bool:
    """True só se o Telegram aceitou — o chamador marca 'notificado' com isso."""
    rec  = avaliacao.get("recomendacao", "VER")
    pct  = avaliacao.get("preco_vs_mercado_pct")
    ref  = "seu teto" if anuncio.get("preco_max") else "mercado"
    pct_txt = f" ({pct:+d}% vs {ref})" if pct is not None else ""
    cabecalho = ("🟡 <b>VALE NEGOCIAR</b> — o preço compensa as ressalvas"
                 if avaliacao.get("alerta") == "negociar" else f"{_REC_EMOJI.get(rec, '⚪')} <b>{rec}</b>")
    linhas = [
        f"{cabecalho} — Score {avaliacao.get('score', 0)}/10",
        f"🔎 <b>{esc(anuncio.get('nome') or anuncio.get('termo'))}</b>",
        "",
        f"📝 {esc(anuncio.get('titulo'))}",
        f"💰 {_brl(anuncio.get('preco'))}{pct_txt}",
        f"📍 {esc(anuncio.get('local') or 'N/A')} · 📦 {esc((anuncio.get('envio') or 'N/A').replace('_', ' '))}",
        f"💲 {_AVP_EMOJI.get(avaliacao.get('avaliacao_preco'), '')}",
    ]
    for titulo, itens, n in (("✅ <b>A favor:</b>", avaliacao.get("pros"), 4),
                             ("❌ <b>Ressalvas:</b>", avaliacao.get("contras"), 4),
                             ("⚠️ <b>Observações:</b>", avaliacao.get("observacoes"), 3)):
        if itens:
            linhas += ["", titulo] + [f"  • {esc(i)}" for i in itens[:n]]
    linhas += ["", f"🔗 <a href=\"{esc(anuncio.get('url'))}\">Ver anúncio</a>"]
    return await _enviar_com_foto("\n".join(linhas), anuncio.get("imagem"), anuncio.get("anuncio_id"))


async def enviar_queda(busca: dict, anuncio: dict, preco_antigo: float, anuncio_id=None) -> bool:
    """Alerta de queda de preço num anúncio já monitorado."""
    novo = anuncio.get("preco") or 0
    pct = 100 * (1 - novo / preco_antigo) if preco_antigo else 0
    teto = busca.get("preco_max") or 0
    titulo = (f"💸 <b>ENTROU NO SEU TETO</b> (caiu {pct:.0f}%)" if teto and preco_antigo > teto >= novo
              else f"💸 <b>PREÇO CAIU {pct:.0f}%</b>")
    texto = (f"{titulo} — {esc(busca.get('nome'))}\n\n"
             f"📝 {esc(anuncio.get('titulo'))}\n"
             f"💰 <s>{_brl(preco_antigo)}</s> → <b>{_brl(novo)}</b>\n"
             f"📍 {esc(anuncio.get('local') or 'N/A')}\n\n"
             f"🔗 <a href=\"{esc(anuncio.get('url'))}\">Ver anúncio</a>")
    return await _enviar_com_foto(texto, anuncio.get("imagem"), anuncio_id)


async def enviar_resumo(busca_id: int, r: dict) -> bool:
    texto = (f"📊 <b>{esc(r['nome'])}</b> — {r['n']} anúncios avaliados\n"
             f"🟢 COMPRAR: {r['comprar']} · 🟡 VER: {r['ver']} · "
             f"🔴 ignorados: {r['n'] - r['comprar'] - r['ver']}\n")
    if r.get("sem_alerta"):
        texto += f"🔕 Mais {r['sem_alerta']} oportunidade(s) sem alerta (limite por rodada)\n"
    if r.get("cats"):
        cats = sorted(r["cats"].items(), key=lambda x: -x[1])
        texto += (f"🗂 Vieram de {len(cats)} categorias: "
                  + ", ".join(f"{esc(c)} ({n})" for c, n in cats[:4])
                  + f"\nPara ficar só com as que interessam: /categorias {busca_id}\n")
    return await asyncio.to_thread(_enviar, texto + f"Detalhes: /top {busca_id}")


_ultimo_alerta: dict[str, float] = {}


def alertar(chave: str, texto: str, horas: float = 12) -> bool:
    """Alerta operacional com debounce por chave (bloqueio da OLX etc.)."""
    agora = time.time()
    if agora - _ultimo_alerta.get(chave, 0) < horas * 3600:
        return False
    ok = _enviar(texto)
    # falhou (Telegram/rede fora): tenta de novo em 30 min, não só daqui a `horas`
    _ultimo_alerta[chave] = agora if ok else agora - horas * 3600 + 1800
    return ok
