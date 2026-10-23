"""
avaliador.py — Avaliação de anúncios por REGRAS (sem LLM).

Substitui o Qwen-35B via Ollama (24 GB de RAM, ~33 s por anúncio). Em
produção ~97% das decisões já vinham da checagem mecânica de identidade; o
resto também é determinístico:
  1. identidade: é o produto buscado? (modelo, sufixo, PC/notebook, acessório)
  2. exclusões do usuário (critérios + "sem X"/"nada de X" nas instruções)
  3. sinais no texto: defeito, golpe, procedência (nota, garantia, caixa)
  4. preço vs MEDIANA dos anúncios comparáveis (mesmo produto; carro: mesmo ano)
Texto negado não conta: "sem defeito", "nunca foi usada em mineração".
Limite: instruções livres viram palavras-chave ("só com X" exige, "prefiro X"
pontua); números ("acima de 2015") ficam p/ as opções ano=/km=/min=/max=.
"""
import asyncio
import json
import logging
import re
import statistics
import unicodedata
from datetime import datetime

import database as db
from models import FUSO, Avaliacao

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


# ── TEXTO ─────────────────────────────────────────────────

def norm(texto: str) -> str:
    """minúsculo e sem acento: 'Leilão' -> 'leilao'."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


_NEGADORES = {"nao", "nunca", "sem", "nem", "zero", "jamais", "nenhum", "nenhuma",
              "isento", "isenta", "livre"}


_FRONTEIRA = re.compile(r"[.!?;,\n]|\b(?:mas|porem|contudo|entretanto)\b")


def ocorrencias(texto: str, chave: str) -> list[tuple[bool, str]]:
    """[(negada, oração em volta)] de cada ocorrência de `chave` (texto normalizado).
    Negação vale dentro da mesma oração, até 4 palavras antes: "nunca foi usada
    em mineração" (negada) · "sem riscos, mas com defeito" (afirmada)."""
    out = []
    for m in re.finditer(r"(?<![a-z0-9])" + re.escape(chave), texto):
        antes = _FRONTEIRA.split(texto[max(0, m.start() - 60):m.start()])[-1]
        depois = _FRONTEIRA.split(texto[m.start():m.start() + 80])[0]
        out.append((bool(_NEGADORES & set(re.findall(r"[a-z]+", antes)[-4:])), antes + depois))
    return out


def afirmado(texto: str, chave: str):
    """True se `chave` aparece afirmada; False se SÓ aparece negada ('sem
    defeito', 'nunca foi batido'); None se ausente."""
    oc = ocorrencias(texto, chave)
    return any(not negada for negada, _ in oc) if oc else None


def _radical(p: str) -> str:
    """'batidos' -> 'batid', 'leilao' -> 'leila': casa singular/plural/gênero."""
    if len(p) > 4 and p.endswith("s"):
        p = p[:-1]
    return p[:-1] if len(p) > 4 and p[-1] in "aeo" else p


def _todos_afirmados(texto: str, radicais: list[str]) -> bool:
    return all(afirmado(texto, r) for r in radicais)


# ── 1. IDENTIDADE ─────────────────────────────────────────
# A produção provou que o LLM errava ~50% na comparação de número de modelo
# (busca "rtx 3090" -> aprovava RTX 5070/5060 Ti). É checagem de regex.

_SUFIXOS = {"ti", "super", "xt", "xtx", "gre", "pro", "max", "plus", "ultra", "mini", "lite"}
_UNIDADES = {"gb", "tb", "mb", "mhz", "ghz", "hz", "w", "v", "fe", "oc", "mm", "pol"}
# "CPU gamer" no Brasil = o PC inteiro; processador antes do produto = montagem
_CONJUNTOS = ("notebook", "laptop", "computador", "pc", "kit", "workstation", "setup completo",
              "cpu gamer", "cpu completa", "maquina gamer", "maquina para", "maquina completa",
              "servidor", "gabinete", "ryzen", "xeon")       # "máquina de lavar" é produto
# "placa RTX 3090 para PC" / "tirada do meu computador" = é a peça, não o PC
_PREPOSICOES = {"para", "pra", "p", "de", "do", "da", "no", "na", "meu", "minha", "seu",
                "em", "num", "dum", "ex"}
# antes do produto no título = é o item principal: "Waterblock EK para RTX 3090"
_ACESSORIOS = ("capa", "capinha", "pelicula", "case", "cabo", "suporte", "waterblock",
               "water block", "backplate", "cooler", "adaptador", "carregador", "fonte",
               "riser", "bracket", "dock", "controle", "bateria", "carcaca", "caixa", "chip")
# marca sozinha não identifica: "fiat linea" não pode aceitar "Fiat Palio"
_MARCAS = {"fiat", "chevrolet", "volkswagen", "ford", "toyota", "honda", "hyundai",
           "renault", "nissan", "peugeot", "citroen", "jeep", "mitsubishi", "chery",
           "suzuki", "volvo", "audi", "mercedes", "dodge", "nvidia", "geforce", "radeon",
           "apple", "samsung", "xiaomi", "motorola", "asus", "gigabyte", "galax", "zotac",
           "intel", "sony", "microsoft", "nintendo", "lenovo", "dell", "acer"}
# descrevem o estado/categoria, não identificam o produto ("rtx 3090 com defeito")
_DESCRITIVAS = {"defeito", "defeituosa", "defeituoso", "quebrada", "quebrado", "usado", "usada",
                "novo", "nova", "lacrado", "lacrada", "seminovo", "seminova", "placa", "video",
                "original", "barato", "barata", "funcionando", "pecas", "reparo", "gamer"}
# homônimos: o acessório É o produto ("caixa de som JBL", "Cooler Master ...")
_NAO_ACESSORIO = ("caixa de som", "cooler master")
_SO_ACESSORIO = ("caixa vazia", "caixas vazias", "so a caixa", "somente a caixa",
                 "apenas a caixa", "so caixa", "caixa e manual", "chip grafico", "chip de video",
                 "so o chip", "somente o chip")


def _sufixos(ti: str, num: str, inicio: int) -> frozenset:
    """Sufixos de modelo logo após o número: '3090ti', '3090 ti', '13 pro max'."""
    resto = ti[inicio + len(num):]
    out = set()
    colado = re.match(r"([a-z]+\d*[a-z]*)", resto)
    if colado and colado.group(1) not in _UNIDADES:
        out.add(colado.group(1))
    for tok in re.findall(r"[a-z0-9]+", resto)[(1 if colado else 0):][:3]:
        if tok not in _SUFIXOS:
            break
        out.add(tok)
    return frozenset(out)


_CONECTIVOS = {"com", "de", "do", "da", "dos", "das", "para", "pra", "sem", "e", "o", "a",
               "em", "no", "na", "p", "c"}


def termo_de_busca(termo: str) -> str:
    """Só as palavras de IDENTIDADE vão p/ a OLX. Ela busca em título E descrição;
    palavras genéricas ('defeito', 'usada') normalmente não restringem (2026-10-22
    23h: 'rtx 3090 com defeito' = 'rtx 3090' = 76), mas numa medição 1h antes
    restringiram a 0 — comportamento instável da OLX. Tirá-las é inofensivo nos
    dois casos; o estado do produto quem julga é o avaliador, com a descrição."""
    ficam = [p for p in termo.split()
             if norm(p).strip(".,/") not in _CONECTIVOS | _DESCRITIVAS
             and not norm(p).startswith(("defeit", "quebrad"))]
    return " ".join(ficam) or termo


def produto_bate(termo: str, titulo: str) -> tuple[bool, str]:
    """(ok, motivo). Regras no TÍTULO (vendedor sempre põe o modelo lá)."""
    t, ti = (re.sub(r"(\d)\+", r"\1 plus", norm(x)) for x in (termo, titulo))
    if re.search(r"\b(compro|procuro|busco)\b|\bcompra (ou|e) troca\b", ti):
        return False, "anúncio de quem quer COMPRAR, não vender"
    if re.search(r"assistencia tecnica|\bcon[cs]ertos?\s+de\b|\bcon[cs]ertos\b|\bmanutencao (de|em)\b|"
                 r"\bconsertamos\b|\bservicos? de\b", ti):
        return False, "anúncio de SERVIÇO, não de produto"
    if not t.strip():
        return True, ""                       # carro sem modelo: categoria toda
    posicoes = []
    for num in re.findall(r"\d{2,}", t):
        ocorr = [m.start() for m in re.finditer(rf"(?<!\d){num}(?!\d)", ti)]
        if not ocorr:
            return False, f"título não menciona '{num}' (modelo diferente)"
        m_t = re.search(rf"(?<!\d){num}(?!\d)", t)
        quer = _sufixos(t, num, m_t.start())
        tem = [_sufixos(ti, num, i) for i in ocorr]
        # "RTX 4080/4090/5090", "(5060, 5070, 5080)": vários modelos no título
        # (vizinho = número de 3+ dígitos que não é memória: "RTX 3090, 24GB" é 1 modelo)
        if all(re.search(r"\d{3,}[a-z]*\s*[/,]\s*$", ti[:i])
               or re.match(r"[a-z]*\s*[/,]\s*(?:rtx|gtx|rx)?\s*\d{3,}(?!\d|\s*(?:gb|tb|mb|mhz|w)\b)",
                           ti[i + len(num):])
               for i in ocorr):
            return False, f"título lista vários modelos ({num} é só um deles)"
        # "RTX 3080 (melhor que a 5070)": o buscado é só termo de comparação
        if all(re.search(r"(\b(melhor|pior|mais forte|mais rapida?|superior|inferior)\s+(que|a)\b|"
                         r"\b(vs|versus|supera|bate)\b|\b(igual|equivalente|similar)\s+(a|uma?)\b|"
                         r"\bnivel de\b)(\s+\w+){0,2}\s*$", ti[max(0, i - 30):i]) for i in ocorr):
            return False, f"o {num} só aparece como comparação, não é o que está à venda"
        # "RX 7600 - troco por rtx 5060": o buscado é o que o vendedor QUER
        if all(re.search(r"\b(troco|troca|trocar)\s+(por|em|numa?)\b(\s+\w+){0,2}\s*$", ti[max(0, i - 40):i])
               for i in ocorr):
            return False, f"o {num} é o que o vendedor quer na TROCA, não o que vende"
        if quer not in tem:
            achado = " ".join(sorted(tem[0])) or "sem sufixo"
            return False, f"{num} {achado} ≠ {num} {' '.join(sorted(quer)) or 'sem sufixo'}"
        posicoes.append(ocorr[0])
    palavras = [p for p in re.findall(r"[a-z]{4,}", t) if p not in _DESCRITIVAS]
    if palavras and not any(p in ti for p in palavras):
        return False, f"título não menciona nada da busca ({', '.join(palavras[:3])})"
    especificas = [p for p in palavras if p not in _MARCAS]
    if especificas and not posicoes and not any(p in ti for p in especificas):
        return False, f"só a marca bate; título não menciona {', '.join(especificas[:3])}"
    # "RTX 3080 (melhor CxB entre 5050 5060 4060 4070 e 5070)": lista de modelos
    nums_termo = set(re.findall(r"\d{2,}", t))
    if nums_termo and len(set(re.findall(r"(?<!\d)\d{4}(?!\d)", ti)) - nums_termo) >= 3:
        return False, "título lista vários modelos"
    posicoes += [ti.find(p) for p in re.findall(r"[a-z0-9]{2,}", t) if p in ti]
    pos_produto = min(posicoes) if posicoes else 0

    for frase in _SO_ACESSORIO:
        if frase in ti and frase not in t:
            return False, f"anúncio de '{frase}', não do produto"
    for pal in _CONJUNTOS:
        if pal in t:
            continue
        for m in re.finditer(rf"(?<![a-z0-9]){pal}(?![a-z0-9])", ti):
            antes = re.findall(r"[a-z]+", ti[max(0, m.start() - 20):m.start()])[-2:]
            if m.start() < pos_produto or not (_PREPOSICOES & set(antes)):
                return False, f"anúncio é de {pal}, não da peça avulsa buscada"
    for pal in _ACESSORIOS:
        if pal in t:
            continue
        m = re.search(rf"(?<![a-z0-9]){pal}(e?s)?(?![a-z0-9])", ti)     # plural: "fontes"
        if m and m.start() < pos_produto and not any(ti.startswith(h, m.start()) for h in _NAO_ACESSORIO):
            return False, f"anúncio de acessório ({pal}), não do produto"
    return True, ""


def triagem(busca: dict, anuncio: dict) -> tuple[bool, str]:
    """Filtro barato da ENTRADA (antes de baixar descrição): identidade +
    categorias da OLX escolhidas pelo usuário (vazio = todas)."""
    ok, motivo = produto_bate(busca.get("termo") or "", anuncio.get("titulo") or "")
    if not ok:
        return False, f"Checagem de modelo: {motivo}"
    cats, cat = _lista(busca.get("categorias")), anuncio.get("categoria") or ""
    if cats and cat and cat not in cats:
        return False, f"Categoria '{cat}' fora do filtro da busca"
    return True, ""


# ── 2/3. SINAIS NO TEXTO ──────────────────────────────────
# (chave normalizada, rótulo). Sinal que o próprio usuário procura (busca
# "rtx 3090 com defeito", critério "para peças") é ignorado.

# (chave, rótulo, gravidade): 3 = não funciona/inviável · 2 = funciona com
# problema ou risco · 1 = detalhe estético (argumento de negociação, não impede)
PROBLEMAS = [
    ("nao liga", "não liga", 3), ("nao funciona", "não funciona", 3),
    ("nao da video", "não dá vídeo", 3), ("sem video", "sem vídeo", 3), ("tela azul", "tela azul", 3),
    ("artefato", "artefatos na imagem", 3), ("sucata", "sucata", 3),
    ("retirada de pecas", "para peças", 3), ("para pecas", "para peças", 3),
    ("precisa de reparo", "precisa de reparo", 3), ("para reparo", "para reparo", 3),
    ("queimad", "queimado", 3), ("defeit", "defeito", 3),
    ("com problema", "com problema", 2), ("superaquec", "superaquecimento", 2),
    ("esquentando muito", "superaquecimento", 2), ("travando", "travando", 2),
    ("reballing", "reballing", 2), ("ja foi consertad", "já consertado", 2),
    ("ja foi reparad", "já reparado", 2), ("oxidad", "oxidação", 2), ("chiado", "chiado", 2),
    ("coil whine", "coil whine", 2), ("barulh", "barulho", 2), ("bios modificad", "BIOS modificada", 2),
    ("tela quebrad", "tela quebrada", 2), ("trincad", "trincado", 2),
    ("vendo no estado", "vendido no estado", 2), ("no estado em que", "vendido no estado", 2),
    ("risco", "riscos", 1), ("riscad", "riscos", 1), ("arranh", "arranhões", 1),
    ("marcas de uso", "marcas de uso", 1), ("sinais de uso", "marcas de uso", 1),
    ("pequeno detalhe", "detalhes", 1), ("pequenos detalhes", "detalhes", 1),
    ("alguns detalhes", "detalhes", 1), ("detalhe estetico", "detalhes", 1),
    ("detalhes esteticos", "detalhes", 1), ("detalhes de pintura", "detalhes de pintura", 1),
    ("amassad", "amassado", 1), ("lascad", "lascado", 1), ("desbotad", "desbotado", 1),
    ("descascad", "descascado", 1), ("ralad", "ralado", 1),
]
PROBLEMAS_CARRO = [
    ("motor fundido", "motor fundido", 3), ("precisa de motor", "precisa de motor", 3),
    ("grande monta", "sinistro (grande monta)", 3), ("media monta", "sinistro (média monta)", 3),
    ("enchente", "enchente", 3), ("alagad", "alagamento", 3), ("remarcad", "chassi remarcado", 3),
    ("sem documento", "sem documento", 3), ("sinistr", "sinistro", 3),
    ("pequena monta", "sinistro (pequena monta)", 2), ("leilao", "leilão", 2), ("batid", "batido", 2),
    ("recuperado de financiamento", "recuperado de financiamento", 2), ("retomad", "retomado", 2),
    ("alienad", "alienado", 2), ("documento atrasado", "documento atrasado", 2),
    ("ipva atrasado", "IPVA atrasado", 2), ("multa", "multas", 2),
]
# na mesma oração, rebaixam a gravidade: "queimado no teto e capô" é pintura
# por PALAVRA: "capo" não pode casar "capotou", nem "leve" casar "levei"; "caixa"
# e "lateral" saíram ("caixa de câmbio", "saída lateral HDMI" não são estéticos)
_CONTEXTO_ESTETICO = re.compile(r"\b(pintura|estetic\w*|lataria|teto|capo|verniz|carcaca|adesivos?)\b")
_ATENUANTES = re.compile(r"\b(leves?|levemente|pequen\w*|minim\w*|superficia\w*|pouco|quase nao)\b")
NEGOCIACAO = ("aceito proposta", "aceito oferta", "aceito contraproposta", "negociavel",
              "aberto a proposta", "aberto a negociacao", "estudo proposta", "faco desconto",
              "chorinho", "da pra negociar", "pode negociar", "negocio o valor", "aceito negociar")
# o vendedor recusou negociar ("não aceito proposta" = NEGOCIACAO negada, também conta)
PRECO_FIXO = ("valor fixo", "preco fixo", "sem negociacao", "nao negocio", "nao baixo",
              "sem desconto", "nao faco desconto")
MARGEM_NEGOCIACAO = 0.15     # busca com teto coleta até 15% acima (se aceitar proposta)
MARGEM_SILENCIOSA = 0.08     # sem dizer nada sobre negociar, só até 8% acima aparece


def _pede(procurado: list[str], chave: str) -> bool:
    """O usuário PEDE isso (termo/preferências) — afirmado, não negado: "prefiro
    sem defeito" não é pedir defeito (antes virava modo 'procurando defeito')."""
    return any(afirmado(f, chave) for f in procurado)


def problemas(texto: str, tipo: str, procurado: list[str]) -> dict[str, int]:
    """{rótulo: gravidade} dos problemas AFIRMADOS no texto. O contexto da
    oração rebaixa ('pequeno defeito', 'queimado no teto'). Sinal que o próprio
    usuário pede ('rtx 3090 com defeito', critério 'para peças') é ignorado."""
    out: dict[str, int] = {}
    for chave, rotulo, grav in PROBLEMAS + (PROBLEMAS_CARRO if tipo == "carro" else []):
        if _pede(procurado, chave):
            continue
        for negada, oracao in ocorrencias(texto, chave):
            if negada:
                continue
            g = 1 if _CONTEXTO_ESTETICO.search(oracao) else grav
            if _ATENUANTES.search(oracao):
                g = max(1, g - 1)
            out[rotulo] = max(out.get(rotulo, 0), g)
    return out


# "só pix" NÃO entra: é como vendedor honesto diz que não aceita cartão
GOLPE = [("pix antecipado", "pix antecipado"), ("deposito antecipado", "depósito antecipado"),
         ("pagamento antecipado", "pagamento antecipado"), ("adiantamento", "adiantamento"),
         ("sinal para reservar", "sinal antecipado"), ("taxa de envio antecipada", "taxa antecipada")]
POSITIVOS = [
    ("nota fiscal", "nota fiscal"), ("com nf", "nota fiscal"), ("com nota", "nota fiscal"),
    ("garantia", "garantia"), ("na caixa", "com caixa"), ("com caixa", "com caixa"),
    ("caixa original", "com caixa"), ("lacrad", "lacrado"), ("unico dono", "único dono"),
    ("revisoes em dia", "revisões em dia"), ("revisad", "revisado"),
    ("laudo cautelar", "laudo cautelar"), ("chave reserva", "chave reserva"),
]
_RE_CONTATO = re.compile(r"whats|\bzap\b|\bwpp\b|telegram|\(?\b\d{2}\)?\s*9\s?\d{4}[-\s]?\d{4}\b")


def _achados(texto: str, sinais, procurado: list[str]) -> tuple[list[str], list[str]]:
    """(rótulos afirmados, rótulos só negados), sem repetir rótulo. Se o
    usuário PEDE um sinal da lista, a lista inteira vira neutra."""
    if any(_pede(procurado, chave) for chave, _ in sinais):
        return [], []
    sim, nao = [], []
    for chave, rotulo in sinais:
        r = afirmado(texto, chave)
        if r and rotulo not in sim:
            sim.append(rotulo)
        elif r is False and rotulo not in nao:
            nao.append(rotulo)
    return sim, [r for r in nao if r not in sim]


# ── REGRAS DO USUÁRIO ─────────────────────────────────────

_PREFIXOS = [   # (prefixo, modo) — os mais longos primeiro
    ("nao quero", "excluir"), ("nao aceito", "excluir"), ("nada de", "excluir"),
    ("exceto", "excluir"), ("evitar", "excluir"), ("evite", "excluir"), ("sem", "excluir"),
    ("nem", "excluir"), ("nao", "excluir"),
    ("so aceito com", "exigir"), ("so aceito", "exigir"), ("somente com", "exigir"),
    ("apenas com", "exigir"), ("so com", "exigir"), ("tem que ter", "exigir"),
    ("precisa ter", "exigir"), ("obrigatorio", "exigir"), ("exijo", "exigir"),
    ("somente", "exigir"), ("apenas", "exigir"), ("so", "exigir"),
    ("de preferencia", "preferir"), ("preferencia por", "preferir"),
    ("prefiro", "preferir"), ("quero", "preferir"), ("com", "preferir"),
]
_VAZIAS = {"de", "do", "da", "dos", "das", "o", "a", "os", "as", "um", "uma", "com", "que",
           "seja", "ser", "for", "ter", "tenha", "tem", "em", "no", "na", "por", "pra",
           "para", "carro", "produto", "item", "anuncio", "aceito", "quero", "coisa", "nada"}


def regras_do_texto(texto: str) -> list[tuple[str, list[str], str]]:
    """Instruções livres -> [(modo, radicais, frase)]. Ex.: 'só com nota fiscal,
    sem carro batido nem de leilão, prefiro manual' ->
    exigir[nota, fiscal] · excluir[batid] · excluir[leila] · preferir[manual].
    'e'/'ou'/'nem' herdam o modo da cláusula; vírgula recomeça em 'preferir'."""
    regras = []
    for clausula in re.split(r"[,;.\n]+", norm(texto)):
        modo = "preferir"
        for pedaco in re.split(r"\s+(?:e|ou|nem)\s+", clausula.strip()):
            pedaco = pedaco.strip()
            for pre, m in _PREFIXOS:
                if pedaco == pre or pedaco.startswith(pre + " "):
                    modo, pedaco = m, pedaco[len(pre):].strip()
                    break
            radicais = [_radical(p) for p in re.findall(r"[a-z0-9]+", pedaco)
                        if p not in _VAZIAS and len(p) > 1]
            if radicais:
                regras.append((modo, radicais, pedaco))
    return regras


def _regras_busca(item: dict) -> list[tuple[str, list[str], str]]:
    regras = regras_do_texto(item.get("instrucoes") or "")
    for modo, campo in (("excluir", "criterios_exclusao"), ("preferir", "criterios_inclusao")):
        for frase in map(norm, _lista(item.get(campo))):
            if (rad := [_radical(p) for p in re.findall(r"[a-z0-9]+", frase) if p not in _VAZIAS]):
                regras.append((modo, rad, frase))
    return regras


def _lista(v) -> list[str]:
    """Lista de textos a partir de lista, JSON ou texto separado por vírgula."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = v.split(",")
    if not isinstance(v, (list, tuple)):
        v = [v] if v not in (None, "") else []
    return [str(x).strip() for x in v if str(x).strip()]


# ── 4. PREÇO ──────────────────────────────────────────────

def ano_do_anuncio(d: dict):
    m = re.search(r"\bano[^:·]{0,10}:\s*((?:19|20)\d{2})", norm(d.get("atributos") or ""))
    if m:
        return int(m.group(1))
    anos = re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", d.get("titulo") or "")
    return int(anos[-1]) if anos else None


def tem_defeito(d: dict, tipo: str) -> bool:
    """Algum problema moderado/grave no título/atributos/descrição (cacheado no dict)."""
    if "_defeito" not in d:
        texto = norm(f"{d.get('titulo')}.\n{d.get('atributos') or ''}.\n{d.get('descricao') or ''}")
        d["_defeito"] = any(g >= 2 for g in problemas(texto, tipo, []).values())
    return d["_defeito"]


def referencia_preco(item: dict, comparaveis: list[dict]) -> tuple[float | None, int]:
    """(mediana, n) dos comparáveis (identidade já filtrada), sem o próprio
    anúncio. Carro: só o mesmo ano (±1) — 2008 e 2016 não se comparam.
    Busca de peça COM DEFEITO: só os comparáveis com defeito (comparar placa
    quebrada com funcionando faria toda quebrada parecer "golpe").
    Menos de 5 comparáveis = sem base."""
    comps = [c for c in comparaveis if c["id"] != item.get("anuncio_id")]
    if item.get("categoria"):     # console com console, jogo com jogo (taxonomia da OLX)
        comps = [c for c in comps if c.get("categoria") in (item["categoria"], "", None)]
    if item.get("condicao") == "defeito":
        comps = [c for c in comps if tem_defeito(c, item.get("tipo"))]
    if item.get("tipo") == "carro":
        ano = ano_do_anuncio(item)
        if not ano:
            return None, 0
        comps = [c for c in comps if (a := ano_do_anuncio(c)) and abs(a - ano) <= 1]
    if len(comps) < 5:
        return None, len(comps)
    precos = [c["preco"] for c in comps]
    q1, _, q3 = statistics.quantiles(precos, n=4)
    mediana = statistics.median(precos)
    # preços dispersos demais = produtos diferentes sob o mesmo nome ("geladeira",
    # "ps4" com jogos): mediana sem sentido. Calibrado em 2026-10-22 com dados reais:
    # homogêneos (iPhone 11, RTX, carro/ano, consoles) 0,13-0,35; geladeira 1,18;
    # jogos 1,64; "ps4" misturado 2,2-4,3
    if (q3 - q1) / mediana > DISPERSAO_MAX:
        return None, -len(comps)
    return mediana, len(comps)


DISPERSAO_MAX = 0.6


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.0f}".replace(",", ".")


# ── CLASSIFICADOR (aprende com o 👍/👎 do usuário) ─────────
# Naive Bayes binário em Python puro: os rótulos são poucos (dezenas), treina
# em ms a cada passada da fila. Não decide identidade (isso é regra); ajusta o
# score em até ±2 pelo que o usuário mostrou que gosta: marca, vendedor,
# categoria, faixa de preço, palavras da descrição.

_STOP_CLF = _VAZIAS | _CONECTIVOS | {"que", "nao", "sim", "mais", "muito", "bem", "esta",
                                     "tem", "ate", "foi", "sao", "meu", "minha", "vendo", "venda"}
MIN_ROTULOS, MIN_POR_CLASSE = 6, 2


def tokens_clf(d: dict) -> set[str]:
    def palavras(t):
        return {p for p in re.findall(r"[a-z0-9]+", norm(t)) if len(p) > 1 and p not in _STOP_CLF}
    toks = {f"t:{p}" for p in palavras(d.get("titulo"))}
    toks |= {f"d:{p}" for p in palavras(f"{d.get('atributos') or ''} {d.get('descricao') or ''}")}
    for campo, pre in (("vendedor", "v"), ("categoria", "c"), ("avaliacao_preco", "p"), ("termo", "b")):
        if d.get(campo):
            toks.add(f"{pre}:{norm(d[campo])}")
    return toks


class Classificador:
    def __init__(self, rotulos: list[dict]):
        self.n = {0: 0, 1: 0}
        self.cont = {0: {}, 1: {}}
        for r in rotulos:
            c = int(r["gostou"])
            self.n[c] += 1
            for t in tokens_clf(r):
                self.cont[c][t] = self.cont[c].get(t, 0) + 1
        self.pronto = (self.n[0] + self.n[1] >= MIN_ROTULOS
                       and min(self.n.values()) >= MIN_POR_CLASSE)

    def log_odds(self, d: dict) -> tuple[float, list[tuple[float, str]]]:
        """Soma das evidências por token (priores iguais: só o conteúdo conta)."""
        import math
        contrib = []
        for t in tokens_clf(d):
            c1, c0 = self.cont[1].get(t, 0), self.cont[0].get(t, 0)
            if c1 + c0 == 0:
                continue
            contrib.append((math.log((c1 + 1) / (self.n[1] + 2)) - math.log((c0 + 1) / (self.n[0] + 2)), t))
        return sum(v for v, _ in contrib), contrib

    def ajuste(self, d: dict) -> tuple[int, str]:
        if not self.pronto:
            return 0, ""
        lo, contrib = self.log_odds(d)
        delta = 2 if lo > 5 else 1 if lo > 2 else -2 if lo < -5 else -1 if lo < -2 else 0
        if not delta:
            return 0, ""
        do_texto = [(v, t) for v, t in contrib if t[:2] in ("t:", "d:")] or contrib
        fortes = sorted(do_texto, key=lambda x: -x[0] if delta > 0 else x[0])[:3]
        termos = ", ".join(t.split(":", 1)[1] for v, t in fortes if (v > 0) == (delta > 0))
        return delta, (f"parecido com o que você curtiu ({termos})" if delta > 0
                       else f"parecido com o que você rejeitou ({termos})")


# ── AVALIAÇÃO ─────────────────────────────────────────────

def excluido(titulo: str, motivo: str) -> dict:
    return Avaliacao(decisao="EXCLUIDO", resumo=(titulo or "")[:80], score=1,
                     avaliacao_preco="sem_base", recomendacao="IGNORAR",
                     motivo=motivo).model_dump()


def avaliar(item: dict, comparaveis: list[dict], modelo: Classificador | None = None) -> dict:
    """Avaliação completa de um anúncio. Função pura (só lê `item`, os
    comparáveis e o modelo) — testável sem banco nem rede."""
    titulo = item.get("titulo") or ""
    if item.get("feedback") == 0:          # o 👎/👍 do usuário é a palavra final
        return excluido(titulo, "Você marcou 👎 neste anúncio")
    ok, motivo = triagem(item, item)
    if not ok and item.get("feedback") != 1:
        return excluido(titulo, motivo)

    desc = (item.get("descricao") or "").strip()
    sem_desc = desc in ("", "(sem descrição)")
    texto = norm(f"{titulo}.\n{item.get('atributos') or ''}.\n"
                 f"vendedor {item.get('vendedor') or '?'}.\n{'' if sem_desc else desc}")
    regras = _regras_busca(item)
    # o que o próprio usuário pede não é defeito ("rtx 3090 com defeito")
    procurado = [norm(item.get("termo"))] + [f for m, _, f in regras if m != "excluir"]

    for modo, radicais, frase in regras:
        if modo == "excluir" and _todos_afirmados(texto, radicais):
            return excluido(titulo, f"Critério do usuário: '{frase}'")

    score, pros, contras, obs, motivos = 6, [], [], [], []
    condicao = item.get("condicao") or "funcionando"
    # termo/preferências pedindo defeito ("rtx 3090 com defeito") valem como modo defeito
    if condicao == "funcionando" and any(_pede(procurado, ch) for ch, _, g in PROBLEMAS if g >= 2):
        condicao = "defeito"

    probs = problemas(texto, item.get("tipo"), procurado if condicao == "funcionando" else [])
    graves = [r for r, g in probs.items() if g == 3]
    moderados = [r for r, g in probs.items() if g == 2]
    leves = [r for r, g in probs.items() if g == 1]
    gmax = 0
    if condicao == "funcionando":
        gmax = max(probs.values(), default=0)
        if graves:
            score -= min(4, 3 * len(graves))
            contras += [f"⛔ {r}" for r in graves]
            motivos.append(f"problema grave: {', '.join(graves)}")
        if moderados:
            score -= min(3, 2 * len(moderados))
            contras += [f"⚠️ {r}" for r in moderados]
            motivos.append(f"ressalva: {', '.join(moderados)}")
        if leves:                    # estético: argumento de negociação, NÃO desconta
            obs.append(f"Detalhes leves ({', '.join(leves)}) — use como argumento p/ negociar")
    elif condicao == "defeito":
        if graves or moderados:      # é exatamente o que se procura
            score += 1
            pros.append(f"confirma o defeito procurado: {', '.join(graves + moderados)}")
        else:
            contras.append("não menciona defeito — fora do que você procura")
            motivos.append("não confirma o defeito procurado")
    elif probs:
        obs.append(f"Menciona: {', '.join(probs)}")
    if any(afirmado(texto, c) for c in ("mineracao", "minerad", "minerou", "mining")) \
            and not _pede(procurado, "minera"):
        score -= 1
        contras.append("usada em mineração")
    golpe, _ = _achados(texto, GOLPE, procurado)
    if golpe:
        score -= 3
        contras.append(f"padrão de golpe: {', '.join(golpe)}")
        motivos.append("padrão de golpe")
    if _RE_CONTATO.search(texto):
        obs.append("Pede contato fora da OLX — negocie pelo chat, nunca pague adiantado")
    negocia = any(afirmado(texto, c) for c in NEGOCIACAO)
    fixo = (any(afirmado(texto, c) for c in PRECO_FIXO)
            or any(afirmado(texto, c) is False for c in NEGOCIACAO))
    if negocia:
        pros.append("aceita negociação")

    # "fora da garantia" / "garantia vencida" = SEM garantia (vira ressalva, não pró)
    texto_pos = re.sub(r"fora d[ae] garantia|garantia (ja )?(vencid|expirad|acab|terminou)\w*",
                       " sem garantia ", texto)
    positivos, negados = _achados(texto_pos, POSITIVOS, [])
    if positivos:
        score += min(2, len(positivos))
        pros += positivos
    contras += [f"sem {n}" for n in negados]

    bonus = 0
    for modo, radicais, frase in regras:
        if modo == "excluir":
            continue
        if _todos_afirmados(texto, radicais):
            bonus += 1
            pros.append(f"atende: {frase}")
        elif modo == "exigir":
            score -= 1
            obs.append(f"Não informa: {frase} — pergunte ao vendedor")
    score += min(2, bonus)

    if sem_desc:
        obs.append("Sem descrição — confirme estado e procedência")
    elif len(desc) < 80:
        obs.append("Descrição curta — pergunte o estado e a procedência")

    # ── preço ──
    preco = item.get("preco") or 0
    teto = item.get("preco_max") or 0
    ref, n = referencia_preco(item, comparaveis)
    pct, avp, forte = None, "sem_base", False
    barato_ok = condicao == "defeito" or bool(graves)     # quebrado barato é coerente
    if teto and preco > 0:
        # busca COM teto: o teto é a referência do próprio usuário. A mediana aqui
        # seria enviesada (só se coleta o que está abaixo do teto) e quase sempre
        # sem os 5 comparáveis -> nada viraria COMPRAR
        ref, pct = teto, round((preco - teto) / teto * 100)
        if preco > teto:
            na_margem = preco <= teto * (1 + MARGEM_NEGOCIACAO)
            if na_margem and negocia:          # aceita proposta: alerta "vale negociar"
                avp = "negociavel"
                obs.append(f"Pede {pct}% acima do seu teto, mas aceita proposta — "
                           f"ofereça até {_brl(teto)}")
                motivos.append("negociável até o seu teto")
            elif preco <= teto * (1 + MARGEM_SILENCIOSA) and not fixo:   # não disse nada: aparece, sem alerta
                score, avp = score - 1, "caro"
                obs.append(f"Pede {pct}% acima do seu teto — na OLX costuma dar p/ negociar")
                motivos.append(f"{pct}% acima do seu teto (sem preço fixo)")
            else:                              # "valor fixo" ou longe do teto
                score, avp = score - 2, "caro"
                motivos.append(f"{pct}% acima do seu teto de {_brl(teto)}" + (" — preço fixo" if fixo else ""))
        elif preco < 0.45 * teto and not barato_ok:
            score, avp = score - 2, "suspeito"
            obs.append(f"Preço {abs(pct)}% abaixo do seu teto — confira se não é golpe ou outro produto")
            motivos.append("preço suspeito")
        else:
            forte = preco <= 0.8 * teto
            score, avp = score + (3 if forte else 2), "bom"
            motivos.append(f"dentro do seu teto de {_brl(teto)}")
    elif ref and preco > 0:
        pct = round((preco - ref) / ref * 100)
        if pct < -40 and not barato_ok:
            score, avp = score - 2, "suspeito"
            obs.append(f"Preço {abs(pct)}% abaixo da mediana — típico de golpe")
            motivos.append("preço suspeito")
        elif pct <= -8:
            forte = pct <= -20
            score, avp = score + (2 if forte else 1), "bom"
            motivos.append(f"{abs(pct)}% abaixo da mediana")
        elif pct <= 15:
            avp = "justo"
        else:
            score, avp = score - (1 if pct <= 35 else 2), "caro"
            motivos.append(f"{pct}% acima da mediana")

    if modelo:
        delta, porque = modelo.ajuste({**item, "avaliacao_preco": avp})
        if delta:
            score += delta
            (pros if delta > 0 else contras).append(porque)
            motivos.append(porque)
    if item.get("feedback") == 1:
        pros.insert(0, "você marcou 👍")
        if not ok:
            obs.append(f"As regras descartariam ({motivo}) — mantido pelo seu 👍")

    # ── tetos de score: o que impede COMPRAR ──
    if gmax == 3:
        # grave: só aparece (VER, sem alerta) se o preço for de peça/reparo
        muito_barato = (teto and preco and preco <= 0.55 * teto) or (pct is not None and pct <= -45)
        score = min(score, 6 if muito_barato else 4)
        if muito_barato:
            pros.append("preço de peça/reparo — só compensa se você for consertar")
    elif gmax == 2 or sem_desc:
        score = min(score, 7)        # ressalva: no máximo VER (e alerta "vale negociar")
    if condicao == "defeito" and any("não menciona defeito" in c for c in contras):
        score = min(score, 4)        # quem caça peça quebrada não quer a funcionando
    if golpe or (teto and avp == "caro" and (fixo or preco > teto * (1 + MARGEM_SILENCIOSA))):
        score = min(score, 4)        # golpe, ou acima do teto com preço fixo
    elif teto and avp == "caro":
        score = min(score, 6)        # pouco acima do teto, sem preço fixo: VER, sem alerta
    score = max(1, min(10, score))
    rec = "COMPRAR" if score >= 8 else "VER" if score >= 5 else "IGNORAR"
    # COMPRAR = produto certo + sem problema relevante + preço COMPROVADAMENTE bom
    if rec == "COMPRAR" and avp != "bom":
        rec = "VER"
    # VALE NEGOCIAR = o preço compensa uma ressalva (problema moderado, detalhes,
    # sem descrição, pede um pouco acima do teto mas aceita proposta). Grave e
    # golpe nunca alertam
    alerta = ("comprar" if rec == "COMPRAR"
              else "negociar" if rec == "VER" and avp in ("bom", "negociavel") and gmax < 3 and not golpe
              else "")
    base = (f" ({pct:+d}% vs seu teto)" if teto and pct is not None
            else f" ({pct:+d}% vs mediana {_brl(ref)} de {n} anúncios)" if pct is not None
            else f" (sem base de preço: {-n} anúncios com preços variados demais)" if n < 0
            else f" (sem base de preço: {n} comparáveis)")
    return Avaliacao(
        decisao="APROVADO", resumo=f"{titulo[:80]} — {_brl(preco)}{base}",
        pros=pros, contras=contras, observacoes=obs, score=score,
        preco_vs_mercado_pct=pct, avaliacao_preco=avp, recomendacao=rec, alerta=alerta,
        motivo="; ".join(motivos) or "produto confere, sem sinais relevantes",
    ).model_dump()


# ── FILA ──────────────────────────────────────────────────

ultimo_processamento: str | None = None
_lock = asyncio.Lock()   # web e scheduler disparavam a fila em paralelo -> notificação dupla


async def processar_fila(notifier=None):
    """Avalia TODA a fila (regras: ~1 ms por anúncio). Prioridade -1 =
    reavaliação silenciosa: salva, mas não notifica nem entra no resumo."""
    global ultimo_processamento
    if _lock.locked():
        return
    async with _lock:
        itens = db.obter_fila_pendente()
        if not itens:
            return
        comparaveis: dict[int, list] = {}
        por_busca: dict[int, dict] = {}
        modelo = Classificador(db.feedbacks())
        for i in range(0, len(itens), 200):          # lotes: não trava o event loop
            lote, salvar = itens[i:i + 200], []
            for item in lote:
                bid = item["busca_id"]
                try:
                    if bid not in comparaveis:
                        comparaveis[bid] = [c for c in db.anuncios_comparaveis(bid)
                                            if produto_bate(item.get("termo") or "", c["titulo"])[0]]
                    salvar.append((item, avaliar(item, comparaveis[bid], modelo)))
                except Exception:
                    logger.exception(f"falha avaliando anúncio {item['anuncio_id']}")
                    db.incrementar_tentativas(item["fila_id"])
            db.salvar_avaliacoes([(it["fila_id"], it["anuncio_id"], av) for it, av in salvar])

            for item, av in salvar:
                if item["prioridade"] < 0:
                    continue
                r = por_busca.setdefault(item["busca_id"], {"nome": item.get("nome", "?"),
                                                            "n": 0, "comprar": 0, "ver": 0})
                r["n"] += 1
                r["comprar"] += av["recomendacao"] == "COMPRAR"
                r["ver"] += av["recomendacao"] == "VER"
                # notificado: a reavaliação (descrição nova, preço mudou) não re-notifica
                if av["alerta"] and notifier and item["ativo"] and not item["notificado"]:
                    if await notifier.enviar_resultado(item, av):
                        db.marcar_notificado(item["anuncio_id"])
                        logger.info(f"alerta {av['alerta']} enviado: [{item.get('nome')}] "
                                    f"{(item.get('titulo') or '')[:50]} R$ {item.get('preco') or 0:.0f}")
                _broadcast({"tipo": "avaliado", "anuncio_id": item["anuncio_id"],
                            "busca_id": item["busca_id"], "recomendacao": av["recomendacao"],
                            "score": av["score"]})
            await asyncio.sleep(0)

        ultimo_processamento = datetime.now(FUSO).isoformat(timespec="seconds")
        logger.info(f"Avaliados {len(itens)} anúncios")
        _broadcast({"tipo": "fila", "tamanho": db.tamanho_fila(), "status": "concluido"})

        if notifier:
            for bid, r in por_busca.items():
                # lote 100% ignorado só interessa no 1º scan de busca nova
                if r["n"] >= 5 and (r["comprar"] + r["ver"] > 0 or db.busca_recente(bid)):
                    await notifier.enviar_resumo(bid, r)
