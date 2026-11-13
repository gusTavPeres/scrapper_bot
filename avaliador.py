"""
avaliador.py — Avaliação de anúncios por REGRAS (sem LLM).

Substitui o Qwen-35B via Ollama (24 GB de RAM, ~33 s por anúncio). Em
produção ~97% das decisões já vinham da checagem mecânica de identidade; o
resto também é determinístico:
  1. identidade: é o produto buscado? (modelo, sufixo, PC/notebook, acessório)
  2. exclusões do usuário (critérios + "sem X"/"nada de X" nas instruções)
  3. sinais no texto: defeito, golpe, procedência (nota, garantia, caixa)
  4. preço vs MEDIANA dos anúncios comparáveis (mesmo produto; veículo: mesmo ano)
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
              "isento", "isenta", "livre", "contra", "nada", "dispensa", "dispensamos"}   # "garantia contra defeitos", "nada de mineração"
# enchimento entre o negador e a chave: "não apresenta qualquer tipo de defeito"
_ENCHIMENTO = re.compile(r"\b(?:qualquer tipo de|nenhum tipo de|tipo de|qualquer)\b")


_FRONTEIRA = re.compile(r"[.!?;,\n]|\b(?:mas|porem|contudo|entretanto)\b")


def _negado(trecho: str) -> bool:
    return bool(_NEGADORES & set(re.findall(r"[a-z]+", _ENCHIMENTO.sub(" ", trecho))[-4:]))


def ocorrencias(texto: str, chave: str) -> list[tuple[bool, str]]:
    """[(negada, oração em volta)] de cada ocorrência de `chave` (texto normalizado).
    Negação vale dentro da mesma oração, até 4 palavras antes: "nunca foi usada
    em mineração" (negada) · "sem riscos, mas com defeito" (afirmada). Chave
    terminada em "$" exige fim de palavra ("nao liga$" não casa "não ligar")."""
    fim = r"(?![a-z])" if chave.endswith("$") else ""
    out = []
    for m in re.finditer(r"(?<![a-z0-9])" + re.escape(chave.rstrip("$")) + fim, texto):
        janela = texto[max(0, m.start() - 60):m.start()]
        antes = _FRONTEIRA.split(janela)[-1]
        depois = _FRONTEIRA.split(texto[m.start():m.start() + 80])[0]
        # "sem garantia e com defeito": o "e com" afirma de novo
        negada = _negado(re.split(r"\b(?:e|mas)\s+com\b", antes)[-1])
        if not negada and re.search(r"\b(?:ou|nem)\s*$", antes):
            # "não possui riscos, amassados ou defeitos": a lista herda a negação
            itens = re.split(r"[.!?;\n]|\b(?:mas|porem|contudo|entretanto)\b", janela)[-1].split(",")
            if len(itens) > 1 and all(len(re.findall(r"[a-z]+", x)) <= 3 for x in itens[1:]):
                negada = _negado(itens[0])
        if not negada and re.match(r"[a-z]*\s*:\s*(?:nenhum|nenhuma|nao|zero)\b", texto[m.end():]):
            negada = True                       # "Defeitos: nenhum"
        out.append((negada, antes + depois))
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


_MINERACAO = ("mineraca", "minerad", "minerou", "minerand", "mining")


def _afirmado_regra(texto: str, radical: str):
    """Como afirmado(), mas "mineração" da regra casa "minerada"/"minerou" do anúncio."""
    if radical.startswith("minera"):
        r = [afirmado(texto, c) for c in _MINERACAO]
        return True if any(r) else False if False in r else None
    return afirmado(texto, radical)


def _todos_afirmados(texto: str, radicais: list[str]) -> bool:
    return all(_afirmado_regra(texto, r) for r in radicais)


# ── 1. IDENTIDADE ─────────────────────────────────────────
# A produção provou que o LLM errava ~50% na comparação de número de modelo
# (busca "rtx 3090" -> aprovava RTX 5070/5060 Ti). É checagem de regex.

_SUFIXOS = {"ti", "super", "xt", "xtx", "gre", "pro", "max", "plus", "ultra", "mini", "lite"}
_UNIDADES = {"gb", "tb", "mb", "mhz", "ghz", "hz", "w", "v", "fe", "oc", "mm", "pol", "polegadas",
             "cc", "cil", "cv", "hp", "km", "kg", "ml", "l", "litros", "cm", "mah", "mp",
             "rpm", "fps", "bit", "bits", "btu", "btus", "kw", "kva"}
# "CPU gamer" no Brasil = o PC inteiro; processador antes do produto = montagem
_CONJUNTOS = ("notebook", "laptop", "computador", "pc", "kit", "workstation", "setup", "desktop",
              "cpu", "note", "maquina gamer", "maquina para", "maquina completa",
              "servidor", "gabinete", "ryzen", "xeon", "i3", "i5", "i7", "i9")  # "máquina de lavar" é produto
_PROCESSADORES = ("ryzen", "xeon", "i3", "i5", "i7", "i9", "cpu")
# "placa RTX 3090 para PC" / "tirada do meu computador" = é a peça, não o PC
_PREPOSICOES = {"para", "pra", "p", "de", "do", "da", "no", "na", "meu", "minha", "seu",
                "em", "num", "dum", "ex"}
# antes do produto no título = é o item principal: "Waterblock EK para RTX 3090"
_ACESSORIOS = ("capa", "capinha", "pelicula", "case", "cabo", "suporte", "waterblock",
               "water block", "backplate", "cooler", "adaptador", "carregador", "fonte",
               "riser", "bracket", "dock", "controle", "bateria", "carcaca", "caixa", "chip",
               "joystick", "dualsense", "headset", "skin", "tela", "teclado", "mouse", "placa mae",
               # peças de carro/moto/bike: "Lanterna Honda Civic", "Roda aro 29 bicicleta"
               "farol", "farois", "lanterna", "friso", "dobradica", "porta", "parachoque",
               "para-choque", "para choque", "retrovisor", "roda", "pneu", "motor", "cambio",
               "volante", "banco", "grade", "paralama", "capo", "tampa", "macaneta", "amortecedor",
               "radiador", "alternador", "embreagem", "modulo", "central", "sensor", "bomba",
               "pastilha", "disco", "calota", "spoiler", "aerofolio", "moldura", "tapete",
               "vidro", "lente", "chave", "painel", "escapamento", "ponteira", "carburador",
               "tanque", "carenagem", "guidao", "bauleto", "cilindro", "pedal", "selim", "quadro",
               "embalagem")
# marca sozinha não identifica: "fiat linea" não pode aceitar "Fiat Palio"
_MARCAS = {"fiat", "chevrolet", "volkswagen", "ford", "toyota", "honda", "hyundai",
           "renault", "nissan", "peugeot", "citroen", "jeep", "mitsubishi", "chery",
           "suzuki", "volvo", "audi", "mercedes", "dodge", "nvidia", "geforce", "radeon",
           "apple", "samsung", "xiaomi", "motorola", "asus", "gigabyte", "galax", "zotac",
           "intel", "sony", "microsoft", "nintendo", "lenovo", "dell", "acer", "vw", "gm", "lg", "jbl"}
# descrevem o estado/categoria, não identificam o produto ("rtx 3090 com defeito")
_DESCRITIVAS = {"defeito", "defeituosa", "defeituoso", "quebrada", "quebrado", "usado", "usada",
                "novo", "nova", "lacrado", "lacrada", "seminovo", "seminova", "placa", "video",
                "original", "barato", "barata", "funcionando", "pecas", "reparo", "gamer", "console"}
# variantes sem número que se excluem: "switch oled" x "switch lite", "macbook pro" x "air"
_VARIANTES = ({"lite", "oled"}, {"air", "pro"})
# ano (1950-2039) não é modelo: "Civic 2018/2019", "RTX 3070, 2021" (a série 20 da RTX é 2050+)
_e_ano = lambda n: len(n) == 4 and 1950 <= int(n) <= 2039
# homônimos: o acessório É o produto ("caixa de som JBL", "Cooler Master ...")
_NAO_ACESSORIO = ("caixa de som", "cooler master")
_SO_ACESSORIO = ("caixa vazia", "caixas vazias", "so a caixa", "somente a caixa",
                 "apenas a caixa", "so caixa", "caixa e manual", "chip grafico", "chip de video",
                 "so o chip", "somente o chip")


def _sufixos(ti: str, num: str, inicio: int, fe: bool = False) -> frozenset:
    """Sufixos de modelo logo após o número: '3090ti', '3090 ti', '13 pro max'.
    `fe`: "FE" é variante (Galaxy S23 FE), não "Founders Edition" de placa."""
    resto = ti[inicio + len(num):]
    sufixos, unidades = (_SUFIXOS | {"fe"}, _UNIDADES - {"fe"}) if fe else (_SUFIXOS, _UNIDADES)
    out = set()
    colado = re.match(r"([a-z]+\d*[a-z]*)", resto)
    if colado and colado.group(1) not in unidades:
        out.add(colado.group(1))
    for tok in re.findall(r"[a-z0-9]+", resto)[(1 if colado else 0):][:3]:
        if tok not in sufixos:
            break
        out.add(tok)
    return frozenset(out)


def _unidade_apos(texto: str, fim: int) -> str:
    """Unidade logo após um número: '8gb', '8 gb' -> 'gb' ('' se não houver)."""
    m = re.match(r"\s?([a-z]+)", texto[fim:])
    return m.group(1) if m and m.group(1) in _UNIDADES else ""


_CONECTIVOS = {"com", "de", "do", "da", "dos", "das", "para", "pra", "sem", "e", "o", "a",
               "em", "no", "na", "p", "c"}


def termo_de_busca(termo: str) -> str:
    """Só as palavras de IDENTIDADE vão p/ a OLX. Ela busca em título E descrição;
    palavras genéricas ('defeito', 'usada') normalmente não restringem, mas às vezes
    zeram o resultado — comportamento instável da OLX. Tirá-las é inofensivo nos
    dois casos; o estado do produto quem julga é o avaliador, com a descrição."""
    ficam = [p for p in termo.split()
             if norm(p).strip(".,/") not in _CONECTIVOS | _DESCRITIVAS
             and not norm(p).startswith(("defeit", "quebrad"))]
    return " ".join(ficam) or termo


# "troco por 5060", e com o que ele TEM no meio: "Troco RX 6600 8gb nova em uma RTX 3060"
_RE_TROCA = re.compile(r"\b(troco|troca|trocar)\s+(por|em|numa?|pra|para|na|no)\b(\s+\w+){0,4}\s*$")
# só sem "vendo" no título: "Vendo ou troco celular em ótimo estado Galaxy S23" é venda
_RE_TROCA_MEIO = re.compile(r"\b(troco|troca|trocar)\b(\s+\w+){1,5}?\s+(por|em\s+uma?|numa?|pra|para)\b(\s+\w+){0,4}\s*$")
# "Rtx 5070 ti, aceito 4090 ou 5080": o que ele ACEITA na troca, não o que vende
# (só enchimento de placa entre o verbo e o número: "aceito pix RTX 4090" é venda;
# número só em lista "4090 ou": "aceito 1200 rtx 3060" é o PREÇO que ele aceita)
_RE_ACEITA = re.compile(r"\b(?:aceito|aceita|pego)\s+(?:(?:uma?|sua|seu|placa|rtx|gtx|rx)\s+|"
                        r"\d{3,4}[a-z]*\s*(?:ou|e|,|/)\s*){0,6}$")
_RE_COMPARA = re.compile(r"(\b(melhor|pior|mais forte|mais rapida?|superior|inferior)\s+(que|a)\b|"
                         r"\b(vs|versus|supera|bate)\b|=|"
                         r"\b(igual|equivalente|equivale|similar|tipo|nivel)\b(\s+(a|ao|de|da|uma?)\b)?|"
                         r"\b(nivel|desempenho) de\b)(\s+\w+){0,2}\s*$")


def _so_comparacao(ti: str, i: int) -> bool:
    """O modelo na posição i é só referência de desempenho: depois de palavra
    de comparação, ou sozinho entre parênteses depois de OUTRA placa ("RX 6900
    XT (RTX 5070)"; "Placa (RTX 5070)", sem outra placa antes, é a própria)."""
    if _RE_COMPARA.search(ti[max(0, i - 30):i]):
        return True
    m = re.search(r"\(\s*(?:nvidia\s+|geforce\s+)?(?:rtx|gtx|rx)?\s*$", ti[:i])
    return bool(m and re.search(r"\b(?:rx|rtx|gtx|arc)\s*[a-z]?\d{3,4}", ti[:m.start()]))


def produto_bate(termo: str, titulo: str) -> tuple[bool, str]:
    """(ok, motivo). Regras no TÍTULO (vendedor sempre põe o modelo lá)."""
    t, ti = (re.sub(r"(\d)\+", r"\1 plus", norm(x)) for x in (termo, titulo))
    # "13 promax" = "13 pro max"; "playstation 5" = "ps5"; "rtx5060ti16gb": memória colada no sufixo
    t, ti = (re.sub(r"([a-z])(\d+(?:gb|tb)\b)", r"\1 \2",
                    re.sub(r"\bplaystation\s*(\d)", r"ps\1", re.sub(r"\bpro\s?max\b", "pro max", x)))
             for x in (t, ti))
    # "rtx 3090 sem mineração", "... com nota fiscal": o produto é o que vem antes
    t = re.split(r"\b(?:com|sem|nao|nunca|nada de)\b", t)[0].strip() or t
    fe = not re.search(r"\b(?:rtx|gtx)\b", t)
    if re.search(r"\b(compro|procuro|busco)\b|\bcompra (ou|e) troca\b", ti):
        return False, "anúncio de quem quer COMPRAR, não vender"
    if re.search(r"assistencia tecnica|\bcon[cs]ertos?\s+de\b|\bcon[cs]ertos\b|\bmanutencao (de|em)\b|"
                 r"\bconsertamos\b|\bservicos? de\b", ti):
        return False, "anúncio de SERVIÇO, não de produto"
    if not t.strip():
        return True, ""                       # carro sem modelo: categoria toda
    posicoes = []
    # decimais ("1.9", "2.0") não identificam; 1 dígito ("ps5", "iphone 8") só
    # colado/junto de palavra: "PS4 + 5 jogos" não é PS5
    for num in re.findall(r"(?<![\d.,])\d+(?![.,]?\d)", t):
        m_t = re.search(rf"(?<!\d){num}(?!\d)", t)
        un = _unidade_apos(t, m_t.end())       # "8gb" na busca = memória, não modelo
        # 1 dígito depois de "com"/"e" é quantidade: "iPhone 11 com 8 meses", "PS4 e 5 jogos"
        ocorr = [m.start() for m in re.finditer(rf"(?<!\d){num}(?!\d)", ti)
                 if len(num) > 1 or (_unidade_apos(ti, m.end()) == un
                                     and (un or re.search(r"[a-z][\s-]?$", ti[:m.start()]))
                                     and (re.findall(r"[a-z]+", ti[:m.start()])[-1:] or [""])[0]
                                     not in _CONECTIVOS | {"ou", "mais"})]
        if not ocorr:
            return False, f"título não menciona '{num}' (modelo diferente)"
        quer = _sufixos(t, num, m_t.start(), fe)
        # "RTX 4080/4090/5090", "(5060, 5070, 5080)": vários modelos no título
        # (vizinho = número de 3+ dígitos que não é memória nem ano: "RTX 3090, 24GB"
        # e "Civic 2018/2019" são 1 modelo)
        def _vizinho(i):
            a = re.search(r"(\d{3,})[a-z]*\s*[/,]\s*$", ti[:i])
            d = re.match(r"[a-z]*\s*[/,]\s*(?:rtx|gtx|rx)?\s*(\d{3,})(?!\d|\s*(?:gb|tb|mb|mhz|w|km)\b)",
                         ti[i + len(num):])
            return bool((a and not _e_ano(a.group(1))) or (d and not _e_ano(d.group(1))))
        if not _e_ano(num) and all(_vizinho(i) for i in ocorr):
            return False, f"título lista vários modelos ({num} é só um deles)"
        # menção que é só comparação ("RTX 3080 (melhor que a 5070)", "RX 6900 XT
        # (RTX 5070)") ou o que o vendedor QUER na troca ("RX 7600 troco por rtx
        # 5060", "Rtx 5070 troca pra 5070 ti") não conta: o sufixo (Ti/Super) é
        # checado só nas menções livres
        troca = [i for i in ocorr if _RE_TROCA.search(ti[max(0, i - 40):i])
                 or _RE_ACEITA.search(ti[max(0, i - 40):i])
                 or (not re.search(r"\bvend", ti) and _RE_TROCA_MEIO.search(ti[max(0, i - 50):i]))]
        comparacao = [i for i in ocorr if _so_comparacao(ti, i)]
        livres = [i for i in ocorr if i not in troca and i not in comparacao]
        if not livres:
            return False, (f"o {num} é o que o vendedor quer na TROCA, não o que vende" if troca
                           else f"o {num} só aparece como comparação, não é o que está à venda")
        tem = [_sufixos(ti, num, i, fe) for i in livres]
        if quer not in tem:
            achado = " ".join(sorted(tem[0])) or "sem sufixo"
            return False, f"{num} {achado} ≠ {num} {' '.join(sorted(quer)) or 'sem sufixo'}"
        posicoes.append(livres[0])
    palavras = [p for p in re.findall(r"[a-z]{4,}", t) if p not in _DESCRITIVAS]
    especificas = [p for p in palavras if p not in _MARCAS]
    # com o modelo (número) já batido, marca no termo não precisa estar no título
    # ("nvidia rtx 3090" x "RTX 3090 Asus TUF")
    checar = especificas if posicoes else palavras
    if checar and not any(p in ti for p in checar):
        return False, f"título não menciona nada da busca ({', '.join(checar[:3])})"
    if especificas and not posicoes and not any(p in ti for p in especificas):
        return False, f"só a marca bate; título não menciona {', '.join(especificas[:3])}"
    if not re.search(r"\d", t):
        # sem número, palavra curta do termo identifica: "pc gamer" x "Monitor Gamer",
        # "fiat uno" x "Fiat Palio", "gol" x "Palio" (antes só valiam as de 4+ letras)
        for p in re.findall(r"\b[a-z]{2,3}\b", t):
            if p not in _CONECTIVOS | _MARCAS | _UNIDADES | _DESCRITIVAS \
                    and not re.search(rf"(?<![a-z0-9]){p}(?![a-z0-9])", ti):
                return False, f"título não menciona '{p}'"
    for grupo in _VARIANTES:
        q, a = grupo & set(re.findall(r"[a-z]+", t)), grupo & set(re.findall(r"[a-z]+", ti))
        if q and a and not q & a:
            return False, f"variante diferente: {', '.join(sorted(a))} ≠ {', '.join(sorted(q))}"
    # "RTX 3080 (melhor CxB entre 5050 5060 4060 4070 e 5070)": lista de modelos
    # (ano e quilometragem não contam: "CG 160 2019/2020 com 8000 km")
    nums_termo = set(re.findall(r"\d+", t))
    outros = {n for n in re.findall(r"(?<!\d)(\d{4})(?!\d)(?!\s?km)", ti) if not _e_ano(n)} - nums_termo
    if nums_termo and len(outros) >= 3:
        return False, "título lista vários modelos"
    posicoes += [ti.find(p) for p in re.findall(r"[a-z0-9]{2,}", t) if p in ti]
    pos_produto = min(posicoes) if posicoes else 0
    # "Tinta para barcos, lanchas", "Película p/ iPhone 13": é item PARA o produto
    # ("Placa de vídeo para PC RTX 3060" é a peça: "para PC" diz onde ela vai)
    if re.search(r"\b(?:para|pra|p)\b\W+(?:[a-z0-9]+\W+){0,2}$", ti[:pos_produto]) \
            and not re.search(r"\b(?:para|pra|p)\s+(?:pc|computador|desktop)\b", ti[:pos_produto]):
        return False, "anúncio de item PARA o produto, não do produto"

    for frase in _SO_ACESSORIO:
        if frase in ti and frase not in t:
            return False, f"anúncio de '{frase}', não do produto"
    # busca de máquina inteira ("notebook dell", "pc gamer"): o processador
    # descreve o próprio produto, não é montagem
    sistema = re.search(r"(?<![a-z0-9])(notebook|laptop|computador|pc|workstation|servidor)(?![a-z0-9])", t)
    # "i7 8700 + RTX 3060 + 16GB + SSD": peças somadas com "+" = PC montado (2+ "+"
    # com outra peça depois; "RTX 3090 + caixa" ou "iPhone 13 + capinha" não)
    pecas = r"(rtx|gtx|rx|arc|ryzen|xeon|core|i[3579]|ram|ssd|nvme|fonte|placa mae|gabinete|monitor|\d+\s?gb)"
    if not sistema and len(re.findall(rf"[+|–-]\s*(?:intel\s+|amd\s+|nvidia\s+)?{pecas}\b", ti)) >= 2:
        return False, "anúncio é de pc montado (peças somadas), não da peça avulsa buscada"
    # processador avulso x o que vem junto: "placa-mãe Z370 + i7 8700" é kit; "Dell
    # XPS 8700 Core i7" e "Dell Vostro 3470 i7 8700" são máquinas de marca
    if re.search(r"\b(?:core\s+)?i[3579]\b", t) and not sistema:
        if re.search(r"\bplaca[\s-]*mae\b", ti) and "placa mae" not in t:
            return False, "anúncio é de kit (placa-mãe + processador), não do processador avulso"
        if re.search(r"\b(?:dell|hp|lenovo|acer|asus|samsung|positivo|vostro|xps|optiplex|inspiron|"
                     r"latitude|thinkpad|ideapad|elitedesk|prodesk|aspire|vivobook|zenbook)\b", ti):
            return False, "anúncio é de máquina de marca, não do processador avulso"
    for pal in _CONJUNTOS:
        if re.search(rf"(?<![a-z0-9]){pal}(?![a-z0-9])", t) or (sistema and pal in _PROCESSADORES):
            continue
        for m in re.finditer(rf"(?<![a-z0-9]){pal}(?![a-z0-9])", ti):
            if re.search(r"\bplaca\s+(?:de\s+)?video\b", ti[:m.start()]):
                continue           # "Placa de Video PC Gamer RTX 3060": é a placa
            antes = re.findall(r"[a-z]+", ti[max(0, m.start() - 20):m.start()])[-2:]
            if m.start() < pos_produto or not (_PREPOSICOES & set(antes)):
                return False, f"anúncio é de {pal}, não da peça avulsa buscada"
    # "PS5 Controle Dualsense": acessório colado no nome do produto = é o acessório
    # ("PS5 com controle", "PS5 2 controles", "PS5 controle extra" seguem sendo o console)
    ultimo = (re.findall(r"[a-z0-9]+", t) or [""])[-1]
    if ultimo and (m := re.search(rf"(?<![a-z0-9]){re.escape(ultimo)}\s+([a-z]+)(?:\s+([a-z]+))?", ti)) \
            and m.group(1) in _ACESSORIOS and m.group(1) not in t \
            and m.group(2) not in ("extra", "extras", "adicional", "reserva", "brinde", "de"):
        return False, f"anúncio de acessório ({m.group(1)}), não do produto"
    for pal in _ACESSORIOS:
        if pal in t:
            continue
        m = re.search(rf"(?<![a-z0-9]){pal}(e?s)?(?![a-z0-9])", ti)     # plural: "fontes"
        if m and m.start() < pos_produto and not any(ti.startswith(h, m.start()) for h in _NAO_ACESSORIO):
            return False, f"anúncio de acessório ({pal}), não do produto"
    return True, ""


def identidade(termo: str, anuncio: dict) -> tuple[bool, str]:
    """produto_bate no título; se o título nem cita o produto ("Venda", "Moto
    top"), vale o campo Modelo da OLX — só em VEÍCULO (tem "Ano:"): em jogo e
    acessório o Modelo é a plataforma ("God of War" -> Modelo: Playstation 5).
    Só a marca no título + o resto na categoria também vale: "Dell Latitude i5"
    em Notebooks."""
    titulo, cat = anuncio.get("titulo") or "", anuncio.get("categoria") or ""
    atrib = anuncio.get("atributos") or ""
    ok, motivo = produto_bate(termo, titulo)
    if not ok and "não menciona" in motivo:
        m = re.search(r"(?:^|· )Modelo: ([^·]+)", atrib)
        if m and re.search(r"(?:^|· )Ano: ", atrib) and produto_bate(termo, m.group(1))[0]:
            return True, ""
        if motivo.startswith("só a marca") and cat and produto_bate(termo, f"{titulo} · {cat}")[0]:
            return True, ""
    return ok, motivo


def triagem(busca: dict, anuncio: dict) -> tuple[bool, str]:
    """Filtro barato da ENTRADA (antes de baixar descrição): identidade +
    categorias da OLX escolhidas pelo usuário (vazio = todas)."""
    if busca.get("tipo") == "imovel":
        ok, motivo = triagem_imovel(busca, anuncio)
        if not ok:
            return False, motivo
    else:
        ok, motivo = identidade(busca.get("termo") or "", anuncio)
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
    ("nao liga$", "não liga", 3), ("nao funciona$", "não funciona", 3),   # "favor não ligar" não é defeito
    ("nao funcionou", "não funciona", 3), ("parou de funcionar", "parou de funcionar", 3),
    ("nao esta funcionando", "não funciona", 3), ("nao esta ligando", "não liga", 3),
    ("nao esta dando video", "não dá vídeo", 3), ("tela preta", "tela preta", 3),
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
    ("quebrad", "quebrado", 2), ("rachad", "rachado", 2), ("rachou", "rachado", 2),
    ("desligando sozinh", "desliga sozinha", 2), ("avaria", "avaria", 2), ("vendo como esta", "vendido no estado", 2),
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
# "pouco" só atenua colado no problema ("um pouco barulhenta"): "usei pouco e ela não
# liga mais" rebaixava placa morta p/ "vale negociar"
_ATENUANTES = re.compile(r"\b(leves?|levemente|pequen\w*|minim\w*|superficia\w*|quase nao)\b")
# "vendo pois estou com problemas financeiros" não é problema do produto
_PROBLEMA_PESSOAL = re.compile(r"problemas? (?:financeir|pessoa|de saude|familiar|de dinheiro)")
NEGOCIACAO = ("aceito proposta", "aceito oferta", "aceito contraproposta", "negociavel",
              "aberto a proposta", "aberto a negociacao", "estudo proposta", "faco desconto",
              "chorinho", "da pra negociar", "pode negociar", "negocio o valor", "aceito negociar")
# o vendedor recusou negociar ("não aceito proposta" = NEGOCIACAO negada, também conta)
PRECO_FIXO = ("valor fixo", "preco fixo", "sem negociacao", "nao negocio", "nao baixo",
              "sem desconto", "nao faco desconto", "inegociavel")
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
            if negada or (chave == "com problema" and _PROBLEMA_PESSOAL.search(oracao)):
                continue
            g = 1 if _CONTEXTO_ESTETICO.search(oracao) else grav
            if _ATENUANTES.search(oracao) or re.search(r"\bpouco\s+" + re.escape(chave.rstrip("$")), oracao):
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
_PRESSA = ("vender rapido", "venda rapida", "vendo rapido", "preciso vender", "urgente")
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
        for j, pedaco in enumerate(re.split(r"\s+(?:e|ou|nem)\s+", clausula.strip())):
            pedaco = pedaco.strip()
            herda = j > 0 and pedaco.startswith("com ")
            if herda:
                # "não quero minerada ou com defeito": o "com" do meio herda o modo
                # (antes virava "preferir defeito" e ligava o modo defeito)
                pedaco = pedaco[4:]
            for pre, m in ([] if herda else _PREFIXOS):
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

def ano_do_anuncio(d: dict, so_atributo: bool = False):
    m = re.search(r"\bano[^:·]{0,10}:\s*((?:19|20)\d{2})", norm(d.get("atributos") or ""))
    if m:
        return int(m.group(1))
    if so_atributo:              # produto: ano no título é "comprada em 2023", não modelo
        return None
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
    anúncio. Veículo (carro, ou qualquer um com "Ano:" da OLX): só o mesmo ano (±1) — 2008 e 2016 não se comparam.
    Busca de peça COM DEFEITO: só os comparáveis com defeito (comparar placa
    quebrada com funcionando faria toda quebrada parecer "golpe").
    Menos de 5 comparáveis = sem base."""
    comps = [c for c in comparaveis if c["id"] != item.get("anuncio_id")]
    if item.get("categoria"):     # console com console, jogo com jogo (taxonomia da OLX)
        comps = [c for c in comps if c.get("categoria") in (item["categoria"], "", None)]
    if item.get("condicao") == "defeito":
        comps = [c for c in comps if tem_defeito(c, item.get("tipo"))]
    # carro, e todo anúncio com "Ano:" da OLX (moto 2017 x 2025 não se comparam)
    ano = ano_do_anuncio(item, so_atributo=item.get("tipo") != "carro")
    if ano or item.get("tipo") == "carro":
        if not ano:
            return None, 0
        comps = [c for c in comps if (a := ano_do_anuncio(c)) and abs(a - ano) <= 1]
    if len(comps) < 5:
        return None, len(comps)
    precos = [c["preco"] for c in comps]
    q1, _, q3 = statistics.quantiles(precos, n=4)
    mediana = statistics.median(precos)
    # preços dispersos demais = produtos diferentes sob o mesmo nome ("geladeira",
    # "ps4" com jogos): mediana sem sentido. Calibrado com dados reais:
    # homogêneos (iPhone 11, RTX, carro/ano, consoles) 0,13-0,35; geladeira 1,18;
    # jogos 1,64; "ps4" misturado 2,2-4,3
    if (q3 - q1) / mediana > DISPERSAO_MAX:
        return None, -len(comps)
    return mediana, len(comps)


DISPERSAO_MAX = 0.6


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.0f}".replace(",", ".")


# ── MERCADO (busca com teto) ──────────────────────────────
# Com teto, a OLX só devolve até teto+15%: a mediana dos coletados é a do que
# cabe no bolso, não a do mercado. 1x/dia o scheduler lê a busca SEM o teto e
# guarda a mediana; dentro do teto a nota diz se é PECHINCHA (caber no teto não
# basta: o preço normal do mercado não é oportunidade) ou se é barato demais
# p/ ser verdade (metade do mercado = golpe ou outro produto).
MERCADO_PECHINCHA = -20      # % vs mediana do mercado
MERCADO_SUSPEITO = -50


def variante(titulo: str, atributos: str = "") -> str:
    """Memória/armazenamento ('16gb', '256gb') do título ou, se ele não diz, dos
    atributos da OLX ("VRAM: 8 GB"): a mesma placa com 8 e com 16 GB tem
    mercados diferentes."""
    for texto in (titulo, atributos):
        if m := re.search(r"(?<![\d.,])(\d{1,4})\s?(gb|tb)\b", norm(texto or "")):
            return m.group(1) + m.group(2)
    return ""


def resumo_mercado(precos: list[tuple[float, str]]) -> dict | None:
    """[(preço, variante)] -> {"n", "mediana", "variantes": {v: {"n", "mediana"}}}.
    Grupo com menos de 5 anúncios ou disperso demais (produtos misturados, a
    mesma regra do referencia_preco) fica de fora."""
    def grupo(vs):
        if len(vs) < 5:
            return None
        q1, _, q3 = statistics.quantiles(vs, n=4)
        med = statistics.median(vs)
        return None if (q3 - q1) / med > DISPERSAO_MAX else {"n": len(vs), "mediana": round(med)}
    geral = grupo([p for p, _ in precos])
    if not geral:
        return None
    variantes = {v: g for v in sorted({v for _, v in precos if v})
                 if (g := grupo([p for p, w in precos if w == v]))}
    return {**geral, "variantes": variantes}


def referencia_mercado(item: dict) -> tuple[float | None, int]:
    """(mediana, n) do mercado p/ este anúncio: a da mesma variante se houver."""
    m = item.get("mercado")
    if isinstance(m, str):
        try:
            m = json.loads(m) if m else None
        except ValueError:
            m = None
    if not isinstance(m, dict) or not m.get("mediana"):
        return None, 0
    g = (m.get("variantes") or {}).get(variante(item.get("titulo"), item.get("atributos"))) or m
    return g["mediana"], g["n"]


# ── CLASSIFICADOR (aprende com o 👍/👎 do usuário) ─────────
# Naive Bayes binário em Python puro: os rótulos são poucos (dezenas), treina
# em ms a cada passada da fila. Não decide identidade (isso é regra); ajusta o
# score em até ±2 pelo que o usuário mostrou que gosta: marca, vendedor,
# categoria, faixa de preço, palavras da descrição.

_STOP_CLF = _VAZIAS | _CONECTIVOS | {"que", "nao", "sim", "mais", "muito", "bem", "esta",
                                     "tem", "ate", "foi", "sao", "meu", "minha", "vendo", "venda"}
# Com 8 rótulos (3 👎) ele rebaixava quase todo alerta: "parecido com o que você
# rejeitou (gb, 12, memoria)" — especificação do produto virava gosto. Agora: mais
# rótulos p/ ligar, palavra só conta vista em 2+ rótulos, número/unidade/termo
# da busca não contam, e o 👎 num anúncio não condena a busca inteira (sem "b:")
MIN_ROTULOS, MIN_POR_CLASSE, MIN_APOIO = 12, 4, 2
_LINHAS = {"nvidia", "geforce", "radeon", "intel", "amd", "placa", "video", "processador", "gddr6"}


def tokens_clf(d: dict) -> set[str]:
    do_termo = set(re.findall(r"[a-z0-9]+", norm(d.get("termo"))))

    def palavras(t):
        return {p for p in re.findall(r"[a-z0-9]+", norm(t))
                if len(p) > 2 and p not in _STOP_CLF | _UNIDADES | _LINHAS | do_termo
                and not any(c.isdigit() for c in p)}
    toks = {f"t:{p}" for p in palavras(d.get("titulo"))}
    toks |= {f"d:{p}" for p in palavras(f"{d.get('atributos') or ''} {d.get('descricao') or ''}")}
    for campo, pre in (("vendedor", "v"), ("categoria", "c"), ("avaliacao_preco", "p")):
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
            if c1 + c0 < MIN_APOIO:
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


# ── IMÓVEL (aluguel e venda) ──────────────────────────────
# Imóvel não tem "modelo": o que identifica é ONDE (cidade/bairro), o tamanho
# (quartos, área, vagas) e o CUSTO TOTAL do mês — aluguel + condomínio + IPTU. O
# anúncio de R$ 1.800 com R$ 900 de condomínio custa mais que o de R$ 2.300 sem.
# O preço se compara com a mediana do mesmo bairro e nº de quartos (dados reais de
# uma capital: dentro do bairro os totais variam 0,3-0,55 da mediana; na cidade
# inteira 0,6-0,7 — por isso a cidade é referência só aproximada).
DISPERSAO_CIDADE = 0.75
# expressões estreitas: "Av. República", "portaria 24 horas por dia", "próximo à loja X" e
# "ótimo para home office/escritório" são moradia. Negada não veta ("não é temporada")
_RE_PROCURA = re.compile(r"\b(procuro|procura-se|procurando|a procura de|preciso (?:de|alugar)|busco|"
                         r"quero alugar|compro)\b(?!.*\b(?:inquilin|morador))")
_RE_TEMPORADA = re.compile(r"\b(temporada|diarias?|por noite|airbnb|reveillon)\b")
_RE_COMERCIAL = re.compile(r"\b(sala comercial|ponto comercial|imovel comercial|galpao|consultorio)\b")
_RE_DIVIDIR = re.compile(r"\b(divido|dividir (?:ap|apto|apartamento|casa|aluguel|quarto)|vaga em republica|"
                         r"quarto em republica|republica (?:feminina|masculina|estudantil|de estudantes)|"
                         r"quarto compartilhado|pensionato|pensao|quarto individual|para (?:1|uma) pessoa|"
                         r"alug\w*(?:-se)? (?:de |um |uma )?(?:quartos?|suites?)(?! e\b))\b")
_RE_COLETIVO = re.compile(r"\b(?:cozinha|banheiro)s?(?: e \w+)? (?:\w+ )?compartilhad|\bquartos individuais\b|"
                          r"\bareas comuns compartilhad")
_CATEGORIA_IMOVEL = {"apartamento": ("apartamentos",), "casa": ("casas",),
                     "quarto": ("aluguel de quartos",), "qualquer": ("apartamentos", "casas")}
POSITIVOS_IMOVEL = [
    ("semimobiliad", "semimobiliado"), ("semi mobiliad", "semimobiliado"),
    ("semi-mobiliad", "semimobiliado"), ("mobiliad", "mobiliado"),
    ("aceita pet", "aceita pet"), ("aceita anima", "aceita pet"), ("pet friendly", "aceita pet"),
    ("direto com o proprietario", "direto com o proprietário"),
    ("direto com proprietario", "direto com o proprietário"),
    ("armarios", "armários"), ("planejad", "armários"), ("ar condicionado", "ar-condicionado"),
    ("portaria 24", "portaria 24 h"), ("elevador", "elevador"), ("varanda", "varanda"),
    ("sacada", "varanda"), ("piscina", "piscina"), ("academia", "academia"),
    ("reformad", "reformado"), ("suite", "suíte"),
]
_NEGADOS_IMOVEL = {"mobiliado": "não mobiliado", "aceita pet": "não aceita pet", "elevador": "sem elevador"}
PROBLEMAS_IMOVEL = [("infiltra", "infiltração"), ("mofo", "mofo"), ("vazamento", "vazamento"),
                    ("precisa de reforma", "precisa de reforma"),
                    ("precisando de reforma", "precisa de reforma"), ("precisa de reparo", "precisa de reparo")]
# "pagamento antecipado" NÃO entra: aluguel antecipado (sem fiador) é contrato comum
# frases inteiras: "agendar antes da visita" e "pintura no exterior do prédio" são inocentes
GOLPE_IMOVEL = [("pix antecipado", "pix antecipado"), ("taxa de reserva", "taxa de reserva"),
                ("taxa de visita", "taxa de visita"), ("sinal para reservar", "sinal antecipado")] + [
    (f"{p} antes da visita", "pagamento antes da visita") for p in ("pagamento", "pagar", "deposito", "pix", "sinal")] + [
    (f"{v} no exterior", "proprietário no exterior") for v in ("moro", "mora", "estou", "esta", "trabalho", "trabalha")] + [
    ("fora do pais", "proprietário no exterior"), ("chaves pelo correio", "chaves pelo correio"),
    ("chaves pelos correios", "chaves pelo correio"), ("chave pelo correio", "chaves pelo correio"),
    ("envio as chaves", "chaves pelo correio"), ("envio a chave", "chaves pelo correio")]
GARANTIAS_IMOVEL = [("fiador", "fiador"), ("caucao", "caução"), ("seguro fianca", "seguro-fiança"),
                    ("titulo de capitalizacao", "título de capitalização")]


def _extras(d: dict) -> dict:
    e = d.get("filtros_extra") or {}
    if isinstance(e, str):
        try:
            e = json.loads(e)
        except ValueError:
            e = {}
    return e if isinstance(e, dict) else {}


def _palavras(t) -> str:
    return re.sub(r"[^a-z0-9]+", " ", norm(t)).strip()


def _numero(txt: str):
    """'R$ 1.050' -> 1050 · '61m²' -> 61 · '5 ou mais' -> 5 · '' -> None."""
    m = re.search(r"\d[\d.]*", txt or "")
    return int(m.group().replace(".", "") or 0) if m else None


def dados_imovel(d: dict) -> dict:
    """Ficha do imóvel pelos atributos da OLX. None = o anúncio não informa."""
    atrib = d.get("atributos") or ""

    def campo(rotulo):
        m = re.search(rf"(?:^|· ){rotulo}: ([^·]+)", atrib)
        return _numero(m.group(1)) if m else None
    cidade, _, bairro = (d.get("local") or "").partition(", ")
    return {"cond": campo("Condomínio") or 0, "iptu": campo("IPTU") or 0,
            "area": campo("Área útil") or None, "quartos": campo("Quartos"),
            "vagas": campo("Vagas na garagem"), "cidade": norm(cidade).strip(), "bairro": norm(bairro).strip()}


_TAXAS = r"(?:condominio|iptu|agua|gas|taxas?|luz|energia|internet)"


def _incluso(texto: str, nome: str) -> bool:
    """O anúncio diz que `nome` (condomínio/IPTU) já está no aluguel: "condomínio e
    IPTU inclusos", "incluso condomínio", "valor inclui o condomínio". O contrário
    não conta: "condomínio (inclui água)" e "água inclusa no condomínio" falam do
    que o CONDOMÍNIO cobre, e "condomínio não incluso" nega."""
    depois = rf"\b{nome}\b(?:,?\s+(?:e|ja|esta|estao|sao|{_TAXAS}))*\s+(?:inclus[oa]s?|incluid[oa]s?)\b"
    antes = rf"\b(?:inclui|incluindo|inclus[oa]s?|incluid[oa]s?)\s+(?:os?\s+)?(?:{_TAXAS},?\s+(?:e\s+)?)*{nome}\b"
    return any(not _negado(texto[max(0, m.start() - 30):m.start()])
               for m in re.finditer(f"{depois}|{antes}", texto))


def custo_imovel(d: dict, texto: str = "") -> tuple[float, str, list[str]]:
    """(total do mês, detalhamento, avisos): aluguel + condomínio + IPTU.
    IPTU acima de 30% do aluguel é o valor ANUAL digitado no campo mensal (na
    amostra real aparece IPTU de metade do aluguel) -> divide por 12. Condomínio
    maior que 1,5x o aluguel é erro de digitação: fica de fora, com aviso; abaixo de
    R$ 30 é campo preenchido por preencher."""
    im, aluguel = dados_imovel(d), d.get("preco") or 0
    if not aluguel:
        return 0, "", []
    cond, iptu, avisos, partes = im["cond"], im["iptu"], [], [f"aluguel {_brl(aluguel)}"]
    if cond < 30:
        cond = 0                               # "R$ 1" só p/ preencher o campo
    if cond > 1.5 * aluguel:
        avisos.append(f"Condomínio informado ({_brl(cond)}) parece erro — confirme o valor")
        cond = 0
    elif cond and _incluso(texto, "condominio"):
        avisos.append("O anúncio diz que o condomínio já está incluso no aluguel")
        cond = 0
    elif not cond and "apartamento" in norm(d.get("categoria")) and not _incluso(texto, "condominio"):
        avisos.append("Não informa o condomínio — o total pode ser maior, pergunte")
    if iptu > 0.3 * aluguel:
        iptu = round(iptu / 12)
    if iptu and _incluso(texto, "iptu"):
        iptu = 0
    if cond:
        partes.append(f"cond. {_brl(cond)}")
    if iptu:
        partes.append(f"IPTU {_brl(iptu)}")
    return aluguel + cond + iptu, " + ".join(partes), avisos


def chave_local(estado: str, a: dict) -> str:
    """'go|cidade|bairro' do anúncio ('' sem bairro): chave do cache de coordenadas."""
    im = dados_imovel(a)
    return f"{norm(estado)}|{im['cidade']}|{im['bairro']}" if im["cidade"] and im["bairro"] else ""


# ── o texto diz outro lugar que o cadastro ────────────────
# O local do anúncio vem do CEP que o anunciante digitou. Título e descrição às
# vezes dizem outra coisa: imóvel de outra cidade cadastrado na capital, ou
# "Setor A" no título com o cadastro no Setor B. Os nomes vêm do IBGE (municípios
# do estado e localidades da cidade), carregados 1x pelo scheduler.
_PREFIXO_LUGAR = ("setor ", "jardim ", "vila ", "parque ", "residencial ", "conjunto ", "bairro ")
# o nome vem depois de rua/prédio/"perto de": é referência, não o endereço
_ANTES_REFERENCIA = re.compile(
    r"\b(?:condominio|cond|edificio|ed|ville|av|avenida|rua|alameda|rodovia|br|prox|proximo|proxima|perto|"
    r"lado|frente|sentido|saida|acesso|divisa|shopping|faculdade|colegio|escola|hospital|terminal|praca|pca|"
    r"regiao)\b[\s.]*(?:(?:a|ao|aos|as|de|do|da|com|para|pra|o)\s+)?$")
_ANTES_LUGAR = re.compile(r"(?:\b(?:no|na|em|bairro)\s+|[-,/|:(]\s*)$")
_ANTES_ENDERECO = re.compile(r"\b(?:localizad[oa]|situad[oa]|fica|endereco|bairro)\b[^.\n]{0,40}$")
_nomes_cache: dict[str, tuple[float, object]] = {}


def _regex_nomes(chave: str, carregar):
    """Regex de uma lista de nomes (5+ letras), refeito a cada 5 min. None se vazia."""
    import time
    agora = time.monotonic()
    if chave not in _nomes_cache or agora - _nomes_cache[chave][0] > 300:
        nomes = sorted({n for n in carregar() if len(n) >= 5}, key=len, reverse=True)
        rx = re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, nomes)) + r")(?![a-z0-9])") if nomes else None
        _nomes_cache[chave] = (agora, rx)
    return _nomes_cache[chave][1]


def _outras_cidades(uf: str, cidade: str) -> list[str]:
    """Municípios do estado, menos a própria cidade e os que são pedaço de nome de
    bairro dela ("Goiás" x "Jardim Goiás", "Jaraguá" x "Vila Jaraguá"). Sem a
    lista de bairros carregada não dá p/ separar: devolve vazio."""
    bairros = db.nomes_locais(f"{uf}|{cidade}|")
    return [n for n in db.nomes_locais(f"mun:{uf}|")
            if n != cidade and not any(n in b for b in bairros)] if bairros else []


def _mesmo_bairro(x: str, y: str) -> bool:
    """"Vila Nova" x "Setor Leste Vila Nova", "Centro" x "Central", "Osvaldo" x "Oswaldo"."""
    import difflib
    return (x in y or y in x or (len(x) >= 5 and x[:5] == y[:5])
            or difflib.SequenceMatcher(None, x, y).ratio() >= 0.8)


def local_citado(busca: dict, a: dict):
    """('cidade'|'bairro', nome) quando o texto do anúncio diz que o imóvel fica em
    OUTRO lugar que o do cadastro; None se confere ou não dá p/ saber.
    Cidade: no título, depois de "em"/"-"/","; em qualquer parte, seguida da UF
    ("Cidade Vizinha - UF"). Bairro: no título (nome com cara de bairro ou depois de
    "no"/"-"), ou na descrição depois de "localizado/situado/bairro". Não conta o
    que vem depois de rua, prédio ou "próximo a", nem quando o texto também cita o
    bairro do cadastro (aí é só vizinhança)."""
    if "_local_citado" in a:
        return a["_local_citado"]
    uf, im = norm(busca.get("estado")), dados_imovel(a)
    titulo, desc = norm(a.get("titulo")), norm(a.get("descricao"))
    achado = None
    if im["cidade"] and uf:
        pre_b = f"{uf}|{im['cidade']}|"
        rx_mun = _regex_nomes(f"mun:{uf}|~{im['cidade']}", lambda: _outras_cidades(uf, im["cidade"]))
        for texto, so_com_uf in ((titulo, False), (desc, True)) if rx_mun else ():
            for m in rx_mun.finditer(texto):
                antes = texto[max(0, m.start() - 30):m.start()]
                if m.group(1) == im["cidade"] or _ANTES_REFERENCIA.search(antes) or antes.rstrip(" -,/").endswith(im["cidade"]):
                    continue
                com_uf = re.match(rf"\s*[-/,(]?\s*{uf}\b", texto[m.end():m.end() + 8])
                if com_uf or (not so_com_uf and _ANTES_LUGAR.search(antes)):
                    achado = ("cidade", m.group(1))
                    break
            if achado:
                break
        rx_b = _regex_nomes(pre_b, lambda: db.nomes_locais(pre_b))
        meu = _palavras(im["bairro"])
        nucleo = lambda n: next((n[len(p):] for p in _PREFIXO_LUGAR if n.startswith(p)), n)
        if not achado and rx_b and meu and nucleo(meu) not in _palavras(titulo + " " + desc):
            for texto, na_descricao in ((titulo, False), (desc, True)):
                for m in rx_b.finditer(texto):
                    nome, antes = m.group(1), texto[max(0, m.start() - 45):m.start()]
                    if _mesmo_bairro(nucleo(_palavras(nome)), nucleo(meu)) or _ANTES_REFERENCIA.search(antes) \
                            or re.match(r"\s+(?:residence|residencial|park|tower|home|life|flat|club)\b", texto[m.end():]):
                        continue           # "Centro Residence" é o prédio, não o bairro
                    if (_ANTES_ENDERECO.search(antes) if na_descricao
                            else nome.startswith(_PREFIXO_LUGAR) or _ANTES_LUGAR.search(antes)):
                        achado = ("bairro", nome)
                        break
                if achado:
                    break
    a["_local_citado"] = achado
    return achado


def _km(p, q) -> float:
    import math
    x = math.radians(q[1] - p[1]) * math.cos(math.radians((p[0] + q[0]) / 2))
    return 6371 * math.hypot(x, math.radians(q[0] - p[0]))


def distancia_km(busca: dict, a: dict):
    """Do anúncio ao ponto de referência da busca: pelo CEP (precisão de rua) quando
    a página do anúncio já foi lida, senão pelo centro do bairro (1-2 km de erro).
    None = sem ponto de referência ou local que o mapa não conhece."""
    centro, chave = _extras(busca).get("centro"), chave_local(busca.get("estado") or "", a)
    if not centro:
        return None
    # o CEP (da página do anúncio) dá a rua; sem ele, o centro do bairro
    c = db.coords_local(f"cep:{a['cep']}") if a.get("cep") else None
    if (not c or c[0] is None) and a.get("cep"):
        # CEP ainda não localizado (o serviço limita as consultas): os primeiros 5
        # dígitos são a mesma vizinhança — vale o centro dos CEPs vizinhos já achados
        c = db.coords_vizinhas(a["cep"])
    if not c or c[0] is None:
        c = db.coords_local(chave) if chave else None
    d = _km(centro, c) if c and c[0] is not None else None
    # o texto cita OUTRO bairro: não dá p/ saber qual vale — conta o mais distante
    if (cit := local_citado(busca, a)) and cit[0] == "bairro":
        im = dados_imovel(a)
        c2 = db.coords_local(f"{norm(busca.get('estado'))}|{im['cidade']}|{cit[1]}")
        if c2 and c2[0] is not None:
            d = max(d or 0, _km(centro, c2))
    return d


def passa_filtros_imovel(busca: dict, a: dict) -> tuple[bool, str]:
    """Filtros determinísticos da busca de imóvel sobre a ficha da OLX: negócio,
    tipo, cidade, bairros, quartos, vagas, área. Quartos/vagas/área ausentes no
    anúncio passam (a avaliação avisa); bairro ausente só passa se o título cita
    um dos bairros pedidos."""
    ex, im, cat = _extras(busca), dados_imovel(a), a.get("categoria") or ""
    outro = "aluguel" if ex.get("negocio") == "venda" else "venda"
    if f"({outro})" in cat:
        return False, f"anúncio de {outro}"
    if cat and not norm(cat).startswith(_CATEGORIA_IMOVEL.get(ex.get("imovel") or "qualquer",
                                                              _CATEGORIA_IMOVEL["qualquer"])):
        return False, f"'{cat}' não é o tipo de imóvel procurado"
    # "cidade-alfa", "Cidade  Alfa" e "cidade alfa" são a mesma (a URL também as iguala)
    if ex.get("cidade") and im["cidade"] and _palavras(im["cidade"]) != _palavras(ex["cidade"]):
        return False, "outra cidade"
    bairros, onde = [_palavras(b) for b in ex.get("bairros") or []], _palavras(im["bairro"] or a.get("titulo"))
    # por palavra inteira: "sul" não casa "Consulado"
    if bairros and not any(b and f" {b} " in f" {onde} " for b in bairros):
        return False, "bairro fora da sua lista"
    for chave, campo, rotulo in (("quartos_min", "quartos", "quarto(s)"), ("vagas_min", "vagas", "vaga(s)"),
                                 ("area_min", "area", "m²")):
        if ex.get(chave) and im[campo] is not None and im[campo] < ex[chave]:
            return False, f"{im[campo]} {rotulo}: abaixo do mínimo de {ex[chave]}"
    if ex.get("quartos_max") and im["quartos"] is not None and im["quartos"] > ex["quartos_max"]:
        return False, f"{im['quartos']} quarto(s): acima do máximo de {ex['quartos_max']}"
    # bairro que o mapa não conhece passa (a avaliação avisa): calar perderia anúncio
    if ex.get("raio_km") and (d := distancia_km(busca, a)) is not None and d > ex["raio_km"]:
        return False, f"a {d:.0f} km do ponto de referência (raio de {ex['raio_km']} km)"
    return True, ""


def triagem_imovel(busca: dict, a: dict) -> tuple[bool, str]:
    """Filtros + o que não é moradia p/ alugar: quem PROCURA imóvel, temporada/
    diária, ponto comercial, vaga em república. Palavra que está no termo da
    busca não veta ("sala comercial")."""
    ok, motivo = passa_filtros_imovel(busca, a)
    if not ok:
        return False, motivo
    ex, ti, termo = _extras(busca), norm(a.get("titulo")), norm(busca.get("termo"))
    veta = lambda rx: (m := rx.search(ti)) and m.group(0).strip() not in termo \
        and afirmado(ti, m.group(1)) is not False
    if (cit := local_citado(busca, a)) and cit[0] == "cidade":
        return False, f"o texto diz que fica em {cit[1].title()}, não em {dados_imovel(a)['cidade'].title()}"
    if veta(_RE_PROCURA):
        return False, "anúncio de quem PROCURA imóvel"
    if ex.get("negocio") != "venda" and veta(_RE_TEMPORADA):
        return False, "aluguel por temporada/diária"
    if veta(_RE_COMERCIAL):
        return False, "imóvel comercial"
    if ex.get("imovel") != "quarto":
        if veta(_RE_DIVIDIR) or re.match(r"\W*(?:quartos?|suites?)\b(?! e sala)", ti):
            return False, "quarto/vaga compartilhada"
        # a descrição (quando já lida) entrega o quarto em moradia coletiva
        if _RE_COLETIVO.search(norm(a.get("descricao"))):
            return False, "quarto com cozinha/banheiro compartilhado"
    return True, ""


def resumo_mercado_imovel(anuncios: list[dict]) -> dict | None:
    """Amostra de referência: {'cidade|bairro|quartos': [[custo total, área], ...]}.
    Guarda os pontos, não só a mediana: a referência de cada anúncio sai dos
    imóveis de TAMANHO parecido (ver referencia_imovel)."""
    amostras: dict[str, list] = {}
    for a in anuncios:
        im, total = dados_imovel(a), custo_imovel(a)[0]
        if total >= 300 and im["quartos"] is not None and im["cidade"]:      # "R$ 100" é preço de enfeite
            amostras.setdefault(f"{im['cidade']}|{im['bairro']}|{im['quartos']}", []).append(
                [round(total), im["area"] or 0])
    todos = [t for vs in amostras.values() for t, _ in vs]
    if len(todos) < 5:
        return None
    return {"n": len(todos), "mediana": round(statistics.median(todos)),
            "amostras": dict(sorted(amostras.items()))}


def referencia_imovel(item: dict) -> dict | None:
    """Com o que comparar o custo deste imóvel: mesmo nº de quartos, no bairro
    (ou, sem amostra, na cidade), e de tamanho parecido (área de -25% a +33%)
    quando há 5 assim. Medido numa capital: contra a mediana do bairro INTEIRO, quase
    tudo abaixo de um teto modesto saía "abaixo do mercado" — o bairro mistura
    60 m² simples com 120 m² mobiliado. Devolve mediana, 1º quartil (q1), 10º
    percentil (p10, só com 10+ pontos), n e o escopo."""
    m, im = item.get("mercado"), dados_imovel(item)
    if isinstance(m, str):
        try:
            m = json.loads(m) if m else None
        except ValueError:
            m = None
    am = m.get("amostras") if isinstance(m, dict) else None
    if not isinstance(am, dict) or im["quartos"] is None:
        return None
    q, area = im["quartos"], im["area"]
    cidade = [x for k, v in am.items() if k.startswith(f"{im['cidade']}|") and k.endswith(f"|{q}") for x in v]
    for grupo, escopo, limite in ((am.get(f"{im['cidade']}|{im['bairro']}|{q}") or [], "bairro", DISPERSAO_MAX),
                                  (cidade, "cidade", DISPERSAO_CIDADE)):
        parecidos = [t for t, a in grupo if area and a and 0.75 * area <= a <= 1.33 * area]
        vs = parecidos if len(parecidos) >= 5 else [t for t, _ in grupo]
        if len(vs) < 5:
            continue
        q1, med, q3 = statistics.quantiles(vs, n=4)
        if (q3 - q1) / med <= limite:
            return {"mediana": round(med), "q1": q1, "q3": q3, "n": len(vs), "escopo": escopo,
                    "p10": statistics.quantiles(vs, n=10)[0] if len(vs) >= 10 else None, "min": min(vs),
                    "tamanho": vs is parecidos}
    return None


# o que rebaixa a moradia (não é defeito, é o tipo de imóvel) e quem ela aceita
PRECARIO_IMOVEL = [("barracao", "barracão"), ("nao e sozinha no lote", "divide o lote"),
                   ("nao e sozinho no lote", "divide o lote"), ("mesmo lote", "divide o lote"),
                   ("lote compartilhado", "divide o lote"), ("casa de fundo", "casa de fundos"),
                   ("casa dos fundo", "casa de fundos"), ("barracao de fundo", "casa de fundos"),
                   ("casa nos fundo", "casa de fundos")]
_KITNET = ("kitnet", "kitinete", "quitinete", "kitchenette", "kit net")
RESTRICOES_IMOVEL = [
    ("para mocas", "só para moças"), ("para mulheres", "só para mulheres"), ("somente mulheres", "só para mulheres"),
    ("apenas mulheres", "só para mulheres"), ("so mulheres", "só para mulheres"), ("para rapazes", "só para rapazes"),
    ("casal sem filhos", "só casal sem filhos"), ("sem criancas", "não aceita crianças"),
    ("nao aceita crianca", "não aceita crianças"), ("nao aceitamos crianca", "não aceita crianças"),
    ("uma pessoa apenas", "só para 1 pessoa"), ("apenas uma pessoa", "só para 1 pessoa"),
    ("apenas 1 pessoa", "só para 1 pessoa"), ("somente uma pessoa", "só para 1 pessoa"),
    ("somente 1 pessoa", "só para 1 pessoa"), ("para uma pessoa", "só para 1 pessoa"), ("para 1 pessoa", "só para 1 pessoa")]
NOVO_IMOVEL = ("reformad", "recem reformad", "primeira locacao", "nunca habitad", "nunca morou",
               "apartamento novo", "casa nova", "imovel novo", "predio novo")


def custo_m2_imovel(item: dict, total: float):
    """(R$/m² deste imóvel, 1º quartil, 3º quartil) entre os do filtro com o mesmo nº
    de quartos na cidade; None sem área ou com menos de 8 pontos."""
    m, im = item.get("mercado"), dados_imovel(item)
    am = m.get("amostras") if isinstance(m, dict) else None
    if not isinstance(am, dict) or not im["area"] or not total or im["quartos"] is None:
        return None
    vs = [t / a for k, v in am.items() if k.startswith(f"{im['cidade']}|") and k.endswith(f"|{im['quartos']}")
          for t, a in v if a]
    if len(vs) < 8:
        return None
    q1, _, q3 = statistics.quantiles(vs, n=4)
    return total / im["area"], q1, q3


def avaliar_imovel(item: dict, texto: str, regras: list, modelo, descartado: str | None) -> dict:
    """Aluguel/venda de imóvel. A nota mede o quanto a moradia VALE dentro do
    filtro — não o quanto é barata (a 1ª versão premiava só o preço e o topo da
    lista virou barracão, kitnet e bairro distante):
      base 5 · cabe no teto +1 · perto do ponto de referência até +2 · entre os mais
      baratos dos comparáveis +1 (entre os mais caros -1) · bom custo por m² +1
      (ruim -1) · novo/reformado +1 · mobiliado ou com armários +1 · barracão, lote
      dividido, kitnet em busca de 2+ quartos -1 cada · restrição de morador -2 ·
      infiltração/mofo -2 · golpe -3.
    Com teto, TODO anúncio novo que cabe no filtro avisa; nota 8+ sai como 🟢."""
    titulo, ex, im = item.get("titulo") or "", _extras(item), dados_imovel(item)
    venda = ex.get("negocio") == "venda"
    desc = (item.get("descricao") or "").strip()
    sem_desc = desc in ("", "(sem descrição)")
    preco, teto = item.get("preco") or 0, item.get("preco_max") or 0
    total, detalhe, obs = (preco, "", []) if venda else custo_imovel(item, texto)
    score, pros, contras, motivos = 5, [], [], []

    ficha = [f"{im[c]} {r}" for c, r in (("quartos", "quarto(s)"), ("area", "m²"), ("vagas", "vaga(s)"))
             if im[c] is not None]
    if ficha:
        pros.append(" · ".join(ficha))
    faltam = [r for ch, c, r in (("quartos_min", "quartos", "os quartos"), ("vagas_min", "vagas", "as vagas"),
                                 ("area_min", "area", "a área")) if ex.get(ch) and im[c] is None]
    if faltam:
        obs.append(f"Não informa {', '.join(faltam)} — confirme antes de visitar")

    # ── onde ──
    sem_distancia = False
    if ex.get("centro"):
        d, raio = distancia_km(item, item), ex.get("raio_km") or 0
        if d is not None:
            pros.append(f"a ~{d:.1f} km do seu ponto de referência".replace(".", ","))
            perto = 2 if d <= (0.4 * raio if raio else 2) else 1 if d <= (0.7 * raio if raio else 4) else 0
            if perto:
                score += perto
                motivos.append("bem perto" if perto == 2 else "perto")
        elif raio:
            # raio é para valer: sem conseguir medir, aparece no painel mas não avisa
            sem_distancia = True
            obs.append("Não foi possível medir a distância — confira no mapa")
            motivos.append("distância não confirmada")

    if (cit := local_citado(item, item)) and cit[0] == "bairro":
        obs.append(f"O cadastro diz {(item.get('local') or '').partition(', ')[2]}, mas o texto cita "
                   f"{cit[1].title()} — confira o endereço (a distância conta o mais longe dos dois)")

    # ── o imóvel ──
    # "sem problemas de infiltração ou mofo": o 2º item da lista herda o "sem"
    probs = [r for ch, r in dict(PROBLEMAS_IMOVEL).items() if afirmado(texto, ch) and not re.search(
        rf"\b(?:sem|nao|nenhum\w*)\b[^.;!\n]{{0,40}}(?:\bou|\bnem|,)\s+(?:de\s+)?{ch}", texto)]
    probs = list(dict.fromkeys(probs))
    if probs:
        score -= 2
        contras += [f"⚠️ {r}" for r in probs]
        motivos.append(f"ressalva: {', '.join(probs)}")
    precario = list(dict.fromkeys(r for ch, r in PRECARIO_IMOVEL if afirmado(texto, ch)))
    if (ex.get("quartos_min") or 0) >= 2 and any(afirmado(texto, k) for k in _KITNET):
        precario.append("kitnet")              # "kitnet de 2 quartos": cômodos pequenos
    if precario:
        score -= min(2, len(precario))
        contras += precario
        motivos.append(", ".join(precario))
    restricoes = list(dict.fromkeys(r for ch, r in RESTRICOES_IMOVEL if afirmado(texto, ch)))
    if restricoes:
        score -= 2
        contras += [f"🚫 {r}" for r in restricoes]
        motivos.append("restrição de morador")
    golpe, _ = _achados(re.sub(r"[^.!?\n]*\b(?:cuidado|golpe|desconfie|atencao)\b[^.!?\n]*", " ", texto),
                        GOLPE_IMOVEL, [])
    if golpe:
        score -= 3
        contras.append(f"padrão de golpe: {', '.join(golpe)}")
        motivos.append("padrão de golpe")
    positivos, negados = _achados(texto, POSITIVOS_IMOVEL, [])
    if "semimobiliado" in positivos and "mobiliado" in positivos:
        positivos.remove("mobiliado")
    if any(afirmado(texto, k) for k in NOVO_IMOVEL):
        score += 1
        pros.append("novo ou reformado")
        positivos = [p for p in positivos if p != "reformado"]
    if {"mobiliado", "semimobiliado", "armários"} & set(positivos):
        score += 1
    pros += positivos[:5]        # piscina, elevador etc. informam; só mobília/armários pontuam
    contras += [_NEGADOS_IMOVEL[n] for n in negados if n in _NEGADOS_IMOVEL]
    garantias = [r for ch, r in GARANTIAS_IMOVEL if afirmado(texto, ch)]
    if afirmado(texto, "fiador") is False:
        pros.append("sem fiador")
    if garantias:
        obs.append(f"Garantia pedida: {', '.join(garantias)}")
    negocia = any(afirmado(texto, c) for c in NEGOCIACAO)

    bonus = 0
    for modo, radicais, frase in regras:
        if modo == "excluir":
            continue
        if _todos_afirmados(texto, radicais):
            bonus += 1
            pros.append(f"atende: {frase}")
        elif modo == "exigir":
            score -= 1
            obs.append(f"Não informa: {frase} — pergunte")
    score += min(2, bonus)
    if sem_desc:
        obs.append("Sem descrição — confirme o estado e as condições")

    # ── preço: teto sobre o TOTAL; posição entre os comparáveis do filtro; R$/m² ──
    r = referencia_imovel(item) or {}
    ref, n, escopo = r.get("mediana"), r.get("n", 0), r.get("escopo", "")
    pct = round((total - ref) / ref * 100) if ref and total else None
    avp, suspeita, onde = "sem_base", False, {"bairro": "do bairro", "cidade": "da cidade"}.get(escopo, "")
    # barato demais = bem abaixo do PISO dos comparáveis (60% do 10º percentil), não
    # um % da mediana: kitnet de 650 num bairro de flats de 2.600 é real, e saía ⚠️
    piso = 0.6 * (r.get("p10") or r.get("min") or 0)
    acima_do_teto = False
    if not total:
        obs.append("Preço a combinar")
    elif teto and total > teto:
        acima_do_teto = True
        acima = round((total - teto) / teto * 100)
        if preco <= teto:
            obs.append(f"O aluguel cabe no seu teto, mas com condomínio/IPTU o total passa {acima}%")
        if total <= teto * (1 + MARGEM_NEGOCIACAO) and negocia:
            avp = "negociavel"
            obs.append(f"Total {acima}% acima do seu teto, mas aceita proposta")
            motivos.append("negociável até o seu teto")
        elif total <= teto * (1 + MARGEM_SILENCIOSA):
            avp = "caro"
            motivos.append(f"total {acima}% acima do seu teto")
        else:
            score, avp = min(score - 2, 4), "caro"
            motivos.append(f"total {acima}% acima do seu teto de {_brl(teto)}")
    else:
        if teto:
            score += 1
        if pct is not None:
            # ...ou 70% abaixo da mediana (R$ 250 por um flat: preço de enfeite ou isca)
            if (total < piso and pct < -40) or pct <= -70:
                avp, suspeita = "suspeito", True        # avisa com ⚠️ (nota no máx. 7): pode ser real
                obs.append(f"{-pct}% abaixo da mediana {onde} ({_brl(ref)}): aluguel barato demais é o golpe "
                           "mais comum — nunca pague nada antes de visitar o imóvel")
                motivos.append("preço suspeito")
            elif total <= r["q1"] and pct <= -5:
                score, avp = score + 1, "bom"
                motivos.append(f"{-pct}% abaixo da mediana {onde}")
            elif total <= r["q3"]:
                avp = "justo"
                motivos.append(f"preço normal {onde} ({pct:+d}%)")
            else:
                score, avp = score - 1, "caro"
                motivos.append(f"{pct}% acima da mediana {onde}")
        if not venda and (m2 := custo_m2_imovel(item, total)):
            if m2[0] <= m2[1]:
                score += 1
                pros.append(f"bom custo por m² ({_brl(m2[0])}/m²)")
            elif m2[0] >= m2[2]:
                score -= 1
                contras.append(f"caro por m² ({_brl(m2[0])}/m²)")

    if modelo:
        delta, porque = modelo.ajuste({**item, "avaliacao_preco": avp})
        if delta:
            score += delta
            (pros if delta > 0 else contras).append(porque)
            motivos.append(porque)
    if item.get("feedback") == 1:
        pros.insert(0, "você marcou 👍")
        if descartado:
            obs.append(f"As regras descartariam ({descartado}) — mantido pelo seu 👍")
    if probs or sem_desc or suspeita or restricoes:
        score = min(score, 7)
    if precario and not (probs or restricoes or golpe or acima_do_teto):
        score = max(score, 5)        # barracão no orçamento desce na lista, mas não some
    if sem_distancia:
        score = min(score, 6)
    if golpe:
        score = min(score, 4)
    score = max(1, min(10, score))
    rec = "COMPRAR" if score >= 8 and not acima_do_teto else "VER" if score >= 5 else "IGNORAR"
    cabe = bool(total) and (not teto or total <= teto)
    # restrição de morador (só moças, só 1 pessoa, sem crianças): aparece, não avisa
    alerta = ("" if golpe or rec == "IGNORAR" or sem_distancia or restricoes
              else "suspeita" if suspeita
              else "negociar" if avp == "negociavel"
              else "comprar" if rec == "COMPRAR" and (teto or avp == "bom")
              else "novo" if teto and cabe
              else "")
    base = (f" ({pct:+d}% vs mediana {onde} {_brl(ref)}, {n} imóveis"
            + (" de tamanho parecido)" if r.get("tamanho") else ")") if pct is not None
            else " (sem referência de preço)")
    custo = f"{_brl(total)}" + ("" if venda else "/mês") + (f" ({detalhe})" if " + " in detalhe else "")
    return Avaliacao(
        decisao="APROVADO", resumo=f"{titulo[:70]} — {custo}{base}",
        pros=pros, contras=contras, observacoes=obs, score=score,
        preco_vs_mercado_pct=pct, avaliacao_preco=avp, recomendacao=rec, alerta=alerta,
        referencia=escopo if pct is not None else "", custo=custo,
        motivo="; ".join(motivos) or "confere com o seu filtro",
    ).model_dump()


# ── AVALIAÇÃO ─────────────────────────────────────────────

def excluido(titulo: str, motivo: str) -> dict:
    return Avaliacao(decisao="EXCLUIDO", resumo=(titulo or "")[:80], score=1,
                     avaliacao_preco="sem_base", recomendacao="IGNORAR",
                     motivo=motivo).model_dump()


def _veto_das_regras(item: dict, texto: str, regras: list) -> dict | None:
    """Exclusão pelas regras do usuário ("sem X" afirmado, "só com X" negado)."""
    titulo = item.get("titulo") or ""
    for modo, radicais, frase in regras:
        if modo == "excluir" and _todos_afirmados(texto, radicais):
            return excluido(titulo, f"Critério do usuário: '{frase}'")
        if modo == "exigir":
            # "só com nota fiscal" + "não tenho nota fiscal" / "só 16gb" + placa de 8GB
            mem = [r for r in radicais if re.fullmatch(r"\d+(?:gb|tb)", r)]
            v = variante(titulo, item.get("atributos"))
            if (mem and v and v not in mem) or any(_afirmado_regra(texto, r) is False for r in radicais):
                return excluido(titulo, f"Critério do usuário: '{frase}' (o anúncio diz que não)")
    return None


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

    if (veto := _veto_das_regras(item, texto, regras)):
        return veto
    if item.get("tipo") == "imovel":
        return avaliar_imovel(item, texto, regras, modelo, None if ok else motivo)

    score, pros, contras, obs, motivos = 6, [], [], [], []
    # "Troco rtx 5060 por rx 9060xt": quem anuncia quer OUTRA coisa, não dinheiro.
    # "Vendo ou troco"/"aceito troca" é venda.
    so_troca = bool(re.match(r"\W*(troco|troca|permuto|permuta)\b", norm(titulo))) and not any(
        afirmado(texto, k) for k in ("vendo", "venda", "vender", "vende-se", "vende se"))
    if so_troca:
        contras.append("anúncio de TROCA: não fala em venda — pergunte se vende")
        motivos.append("só troca")
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
    golpe, _ = _achados(re.sub(r"[^.!?\n]*\b(?:cuidado|golpe|desconfie|atencao)\b[^.!?\n]*", " ", texto),
                        GOLPE, procurado)
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
    pct, avp, forte, suspeita = None, "sem_base", False, False
    # quebrado barato é coerente; "com problema na vram" a -60% também (não é golpe)
    barato_ok = condicao == "defeito" or bool(graves or moderados)
    ref_m, n_m = referencia_mercado(item) if condicao != "defeito" else (None, 0)
    vs_mercado = bool(teto and 0 < preco <= teto and ref_m)
    if vs_mercado:
        # dentro do teto, com a mediana do MERCADO (sem o teto): a nota mede a pechincha
        ref, n, pct = ref_m, n_m, round((preco - ref_m) / ref_m * 100)
        metade = pct < MERCADO_SUSPEITO and not barato_ok
        if metade and preco < 0.45 * teto:
            # metade do mercado E abaixo de 45% do teto (a regra de sempre): sem alerta
            score, avp = score - 2, "suspeito"
            obs.append(f"Preço {-pct}% abaixo do mercado ({_brl(ref_m)}) — confira se não é golpe ou outro produto")
            motivos.append("preço suspeito")
        elif pct <= MERCADO_PECHINCHA:
            forte = pct <= -30
            score, avp = score + (3 if forte else 2), "bom"
            motivos.append(f"pechincha: {-pct}% abaixo do mercado")
            if pct <= -40:            # perto do que golpe costuma pedir
                # barato demais + sinal (contato fora da OLX, pressa, descrição curta) =
                # PECHINCHA SUSPEITA. Metade do mercado mas dentro do teto também é ⚠️:
                # o usuário aceita esse preço, então avisa — com a ressalva
                sinais = [r for r, ok in (
                    ("contato fora da OLX", bool(_RE_CONTATO.search(texto))),
                    ("pressa p/ vender", any(afirmado(texto, k) for k in _PRESSA)),
                    ("descrição curta", sem_desc or len(desc) < 80),
                    # produto novo bem abaixo do preço do USADO: isca clássica
                    ("lacrado a preço de usado", bool(afirmado(texto, "lacrad")))) if ok]
                suspeita = metade or bool(sinais)
                obs.append(f"{-pct}% abaixo do mercado" + (
                    f" e {', '.join(sinais)}: padrão comum de golpe" if sinais
                    else ": menos da metade do mercado é o padrão comum de golpe" if metade
                    else ": ótimo se for real")
                    + " — veja pessoalmente ou use a garantia da OLX, nunca pague adiantado")
        elif pct <= 15:
            avp = "justo"
            obs.append(f"Cabe no seu teto, mas é o preço normal do mercado ({_brl(ref_m)}): não é pechincha")
            motivos.append(f"preço de mercado ({pct:+d}%)")
        else:
            score, avp = score - 1, "caro"
            motivos.append(f"{pct}% acima do mercado")
    elif teto and preco > 0:
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
        elif preco < 0.45 * teto and (not barato_ok or preco < 0.15 * teto):   # R$ 1 de placeholder
            score, avp = score - 2, "suspeito"
            obs.append(f"Preço {abs(pct)}% abaixo do seu teto — confira se não é golpe ou outro produto")
            motivos.append("preço suspeito")
        else:
            forte = preco <= 0.8 * teto
            score, avp = score + (3 if forte else 2), "bom"
            motivos.append(f"dentro do seu teto de {_brl(teto)}")
    elif ref and preco > 0:
        pct = round((preco - ref) / ref * 100)
        if pct < -40 and (not barato_ok or pct < -85):
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

    if gmax == 2 and vs_mercado and pct < MERCADO_SUSPEITO:       # só com mercado medido: teto folgado não é sinal
        # ressalva "moderada" a preço de sucata: o preço diz que o problema é sério.
        # Antes saía como PECHINCHA SUSPEITA ("Rtx 3060 com problema na vram", -60%)
        gmax = 3
        motivos.append("preço de peça com problema declarado")
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
    if so_troca:
        score = min(score, 6)        # VER, sem alerta: aparece no /top p/ perguntar se vende
    if suspeita:
        score, avp = min(score, 7), "suspeito"   # alerta, mas como ⚠️, nunca 🟢
    score = max(1, min(10, score))
    rec = "COMPRAR" if score >= 8 else "VER" if score >= 5 else "IGNORAR"
    # COMPRAR = produto certo + sem problema relevante + preço COMPROVADAMENTE bom
    if rec == "COMPRAR" and avp != "bom":
        rec = "VER"
    # VALE NEGOCIAR = o preço compensa uma ressalva (problema moderado, detalhes,
    # sem descrição, pede um pouco acima do teto mas aceita proposta). Grave e
    # golpe nunca alertam
    alerta = ("suspeita" if suspeita and not golpe and gmax < 3 and not so_troca and rec != "IGNORAR"
              else "comprar" if rec == "COMPRAR"
              else "negociar" if rec == "VER" and avp in ("bom", "negociavel") and gmax < 3
              and not golpe and not so_troca
              else "")
    base = (f" ({pct:+d}% vs mercado {_brl(ref)} de {n} anúncios)" if vs_mercado
            else f" ({pct:+d}% vs seu teto)" if teto and pct is not None
            else f" ({pct:+d}% vs mediana {_brl(ref)} de {n} anúncios)" if pct is not None
            else f" (sem base de preço: {-n} anúncios com preços variados demais)" if n < 0
            else f" (sem base de preço: {n} comparáveis)")
    return Avaliacao(
        decisao="APROVADO", resumo=f"{titulo[:80]} — {_brl(preco)}{base}",
        pros=pros, contras=contras, observacoes=obs, score=score,
        preco_vs_mercado_pct=pct, avaliacao_preco=avp, recomendacao=rec, alerta=alerta,
        referencia="mercado" if vs_mercado else "teto" if teto and pct is not None else "",
        motivo="; ".join(motivos) or "produto confere, sem sinais relevantes",
    ).model_dump()


# ── FILA ──────────────────────────────────────────────────

def _inicio(desc) -> str:
    """Começo da descrição, normalizado ('' se curta demais p/ identificar)."""
    t = re.sub(r"[^a-z0-9]+", " ", norm(desc)).strip()
    return t[:60] if len(t) >= 60 else ""


def republicacao(item: dict) -> tuple[int | None, bool]:
    """(id do anúncio já alertado de que este parece cópia, certeza), em 14 dias e
    na mesma faixa de preço. CERTA (sem alerta):
      · mesmo local e título quase igual (80% das palavras): o vendedor apaga e
        posta de novo p/ subir na lista (3 reposts em 1 h eram 3 alertas);
      · descrição que começa igual, ao mesmo preço, em QUALQUER bairro: o vendedor
        em série troca título e bairro, mas cola o mesmo texto;
      · 3º anúncio no mesmo bairro e preço: vendedor em série (um deles rendeu 6
        alertas em 2 dias, 2 por rodada). Não vale p/ imóvel: vários aluguéis de
        mesmo preço no mesmo bairro é o normal.
    FRACA (alerta com aviso): só o mesmo bairro e preço em 24 h, com outro título —
    pode ser OUTRO vendedor. Título curto ("RTX 5060") não identifica ninguém."""
    palavras = lambda t: set(re.findall(r"[a-z0-9]+", norm(t)))
    alvo, ini, preco = palavras(item.get("titulo")), _inicio(item.get("descricao")), item.get("preco") or 0
    cands = db.alertados_parecidos(item["busca_id"], item["anuncio_id"], preco)
    imovel, uf = item.get("tipo") == "imovel", lambda u: re.sub(r"^https?://", "", u or "")[:3]
    if imovel:
        # título de aluguel é quase sempre igual ("Apartamento 2 quartos no Bairro X") e
        # imobiliária abre todo anúncio com o mesmo parágrafo: só é o MESMO imóvel se a
        # ficha (área, quartos, vagas, condomínio) também for igual
        cands = [c for c in cands if dados_imovel(c) == dados_imovel(item)]
    for c in cands:
        # mesmo estado (subdomínio da URL): texto do fabricante colado por vendedores
        # de estados diferentes não é o mesmo vendedor
        if ini and c["preco"] == preco and _inicio(c["descricao"]) == ini and uf(c["url"]) == uf(item.get("url")):
            return c["id"], True
    mesmos = [c for c in cands if item.get("local") and c["local"] == item["local"]]
    for c in mesmos:
        outro = palavras(c["titulo"])
        if len(alvo) >= 4 and len(alvo & outro) >= 0.8 * len(alvo | outro):
            return c["id"], True
    if imovel:
        return None, False
    if len(mesmos) >= 2:
        return mesmos[0]["id"], True
    return next((c["id"] for c in mesmos if c["recente"]), None), False


def _estoque(item: dict) -> bool:
    """Imóvel já anunciado 2+ dias ANTES de a busca existir: é o estoque do 1º scan
    (centenas cabem num filtro de aluguel). Não alerta — fica no /top e no painel;
    as descrições chegam 60 por rodada e cada leva renderia mais 5 alertas, por
    dias. Sem data, vale como novo."""
    if item.get("tipo") != "imovel":
        return False
    try:
        from datetime import timedelta, timezone
        criada = datetime.fromisoformat(item["criado_em"]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(item["data_postagem"]) < criada - timedelta(days=2)
    except (KeyError, TypeError, ValueError):
        return False


ultimo_processamento: str | None = None
# ponytail: teto fixo por busca e passada; o 1º scan de uma busca ampla (ou um
# bug, como o fuzz que gravou 50 buscas-lixo e mandou 170 alertas) não vira
# enxurrada. O resto fica no /top e no resumo.
MAX_ALERTAS = 5
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
        alertas: list[tuple[dict, dict]] = []
        modelo = Classificador(db.feedbacks())
        for i in range(0, len(itens), 200):          # lotes: não trava o event loop
            lote, salvar = itens[i:i + 200], []
            for item in lote:
                bid = item["busca_id"]
                try:
                    if bid not in comparaveis:
                        # imóvel compara com a amostra do mercado, não com os coletados
                        comparaveis[bid] = [] if item.get("tipo") == "imovel" else [
                            c for c in db.anuncios_comparaveis(bid) if triagem(item, c)[0]]
                    salvar.append((item, avaliar(item, comparaveis[bid], modelo)))
                except Exception:
                    logger.exception(f"falha avaliando anúncio {item['anuncio_id']}")
                    db.incrementar_tentativas(item["fila_id"])
            db.salvar_avaliacoes([(it["fila_id"], it["anuncio_id"], av) for it, av in salvar])

            for item, av in salvar:
                if item["prioridade"] < 0:
                    continue
                r = por_busca.setdefault(item["busca_id"], {
                    "nome": item.get("nome", "?"), "n": 0, "comprar": 0, "ver": 0,
                    "alertas": 0, "sem_alerta": 0, "cats": {}, "filtrada": bool(_lista(item.get("categorias"))),
                    "imovel": item.get("tipo") == "imovel"})
                r["n"] += 1
                r["comprar"] += av["recomendacao"] == "COMPRAR"
                r["ver"] += av["recomendacao"] == "VER"
                if av["decisao"] == "APROVADO" and item.get("categoria"):
                    r["cats"][item["categoria"]] = r["cats"].get(item["categoria"], 0) + 1
                # notificado: a reavaliação (preço mudou) não re-notifica. Descrição
                # ainda não baixada: o alerta sai quando ela chegar (re-enfileira)
                if av["alerta"] and notifier and item["ativo"] and not item["notificado"] \
                        and item.get("descricao") and not _estoque(item):
                    alertas.append((item, av))
                _broadcast({"tipo": "avaliado", "anuncio_id": item["anuncio_id"],
                            "busca_id": item["busca_id"], "recomendacao": av["recomendacao"],
                            "score": av["score"]})
            await asyncio.sleep(0)

        adiados, enviados_agora = 0, {}
        # mesmo bairro + mesmo preço + mesmo termo, na MESMA leva: um vendedor com 7
        # anúncios iguais ("Rtx 3060 12gb Msi/Colorful/..." a 1.289, descrição copiada
        # = loja disfarçada ou golpe em série) rendeu 5 alertas numa rodada. A
        # republicação só via os já gravados; aqui vale o que acabou de sair
        chave = lambda it: (it.get("termo"), it.get("local"), round(it.get("preco") or 0))
        for item, av in sorted(alertas, key=lambda x: -x[1]["score"]):     # os melhores primeiro
            r = por_busca[item["busca_id"]]
            orig, certa = republicacao(item)
            if not certa and chave(item) in enviados_agora and item.get("local") \
                    and item.get("tipo") != "imovel":
                orig, certa = enviados_agora[chave(item)], True
            if orig and not certa:
                av = {**av, "observacoes": ["Mesmo bairro e preço de um anúncio enviado há menos de "
                                            "24 h: pode ser o mesmo vendedor reanunciando"] + av["observacoes"]}
            if certa:                                # o usuário já recebeu este anúncio
                db.marcar_notificado(item["anuncio_id"])
                logger.info(f"sem alerta: republicação do anúncio {orig} — "
                            f"{(item.get('titulo') or '')[:50]}")
            elif r["alertas"] >= MAX_ALERTAS:
                r["sem_alerta"] += 1
            elif not adiados and await notifier.enviar_resultado(item, av):
                r["alertas"] += 1
                enviados_agora.setdefault(chave(item), item["anuncio_id"])
                db.marcar_notificado(item["anuncio_id"])
                logger.info(f"alerta {av['alerta']} enviado: [{item.get('nome')}] "
                            f"{(item.get('titulo') or '')[:50]} R$ {item.get('preco') or 0:.0f}")
            elif adiados or getattr(notifier, "falha_de_rede", False):
                # Telegram/rede fora: antes o alerta SUMIA (ninguém reenviava). Volta p/
                # a fila e sai na próxima volta; os desta leva esperam junto (sem gastar
                # um timeout em cada). Recusa definitiva (4xx) não volta: só o log
                adiados += 1
                db.adicionar_fila(item["anuncio_id"], item["busca_id"], max(item["prioridade"], 1))
        if adiados:
            logger.warning(f"Telegram fora do ar: {adiados} alerta(s) adiado(s) p/ a próxima volta")

        ultimo_processamento = datetime.now(FUSO).isoformat(timespec="seconds")
        logger.info(f"Avaliados {len(itens)} anúncios")
        _broadcast({"tipo": "fila", "tamanho": db.tamanho_fila(), "status": "concluido"})

        if notifier:
            for bid, r in por_busca.items():
                recente = db.busca_recente(bid)
                # 1º scan com resultado misturado ("civic": carros E peças): mostra
                # as categorias e o /categorias, que resolve sem regra nova
                if not (recente and not r["filtrada"] and len(r["cats"]) >= 2):
                    r["cats"] = {}
                # lote 100% ignorado só interessa no 1º scan de busca nova
                if (r["n"] >= 5 and (r["comprar"] + r["ver"] > 0 or recente)) or r["sem_alerta"]:
                    await notifier.enviar_resumo(bid, r)
