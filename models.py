"""
models.py — Schemas Pydantic v2 (validação na fronteira: API web E Telegram)
"""
import json
import re
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

# horário da OLX (Brasil): fixo, p/ não depender do fuso da máquina/container
FUSO = ZoneInfo("America/Sao_Paulo")

UFS = {"ac", "al", "ap", "am", "ba", "ce", "df", "es", "go", "ma", "mt", "ms", "mg", "pa",
       "pb", "pr", "pe", "pi", "rj", "rn", "rs", "ro", "rr", "sc", "sp", "se", "to"}


_NOMES = {"preco_min": "preço mínimo", "preco_max": "preço máximo", "intervalo_min": "intervalo",
          "filtro_envio": "envio", "filtro_vendedor": "vendedor", "filtros_extra": "filtros de carro",
          "criterios_inclusao": "critérios de inclusão", "criterios_exclusao": "critérios de exclusão",
          "instrucoes": "regras"}


def mensagem_erro(err: dict) -> str:
    """Erro de validação do pydantic (em inglês) -> frase em português p/ o usuário."""
    campo = next((str(x) for x in reversed(err.get("loc") or []) if isinstance(x, str) and x != "body"), "")
    nome = _NOMES.get(campo, campo)
    ctx, tipo = err.get("ctx") or {}, err.get("type", "")
    if tipo == "literal_error":
        opcoes = ctx.get("expected", "").replace("'", "").replace(" or ", ", ")
        return f"valor inválido para {nome} (use: {opcoes})"
    if tipo in ("greater_than_equal", "less_than_equal", "greater_than", "less_than"):
        return f"{nome} fora do limite permitido ({ctx.get('ge', ctx.get('le', ctx.get('gt', ctx.get('lt', ''))))})"
    if tipo in ("string_too_long", "too_long"):
        return f"{nome} longo demais"
    if tipo in ("string_too_short", "too_short", "missing"):
        return f"{nome} é obrigatório"
    if tipo in ("int_parsing", "int_type", "int_from_float"):
        return f"{nome} precisa ser um número inteiro"
    if tipo == "value_error":
        return str(err.get("msg", "")).removeprefix("Value error, ")
    return f"valor inválido para {nome}" if nome else "dados inválidos"


class Avaliacao(BaseModel):
    decisao:             Literal["APROVADO", "EXCLUIDO"]
    resumo:              str
    pros:                list[str]     = Field(default_factory=list)
    contras:             list[str]     = Field(default_factory=list)
    observacoes:         list[str]     = Field(default_factory=list)
    score:               int           = Field(ge=1, le=10)
    preco_vs_mercado_pct: Optional[int] = None
    avaliacao_preco:     Literal["bom", "justo", "caro", "suspeito", "sem_base", "negociavel"]
    recomendacao:        Literal["COMPRAR", "VER", "IGNORAR"]
    motivo:              str
    # o que vira alerta no Telegram: "comprar" (🟢) ou "negociar" (🟡: o preço
    # compensa uma ressalva); "" não notifica
    alerta:              Literal["", "comprar", "negociar"] = ""

    @model_validator(mode="after")
    def coerencia_recomendacao(self):
        if self.decisao == "EXCLUIDO":
            self.recomendacao, self.alerta = "IGNORAR", ""
        return self


Envio    = Literal["qualquer", "com_envio", "sem_envio", "frete_gratis"]
Vendedor = Literal["qualquer", "particular", "loja"]
# "pay" = Garantia da OLX; "pay_entrega" = garantia + entrega (pós-filtro)
Garantia = Literal["", "pay", "pay_entrega"]
# intenção explícita: "defeito" = procuro peça quebrada/para peças (defeito conta A FAVOR)
Condicao = Literal["funcionando", "defeito", "qualquer"]
_RE_QUER_DEFEITO = re.compile(r"defeit|quebrad|para pecas|retirada de pecas|sucata|nao liga|"
                              r"nao funciona|sem video|nao da video|para reparo|queimad")


def _sem_acento(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


class _Validacoes(BaseModel):
    """Validadores comuns a criar e editar (None = campo não enviado)."""

    @field_validator("nome", "termo", "instrucoes", mode="before", check_fields=False)
    @classmethod
    def _strip(cls, v):
        # sem caracteres de controle (um NUL num nome corrompia o log inteiro)
        return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", v).strip() if isinstance(v, str) else v

    @field_validator("termo", check_fields=False)
    @classmethod
    def _termo_identifica(cls, v):
        # "F", "T", "Lu" casam com tudo na OLX (1.250 anúncios cada): o termo
        # precisa de uma palavra de 3+ letras ou um modelo com número (ps5, s23)
        if v and not re.search(r"[^\W_]{3,}|(?=\w*\d)[^\W_]{2,}", v):
            raise ValueError("termo genérico demais — use ao menos uma palavra de 3 letras ou o modelo")
        return v

    @field_validator("estado", mode="before", check_fields=False)
    @classmethod
    def _estado(cls, v):
        if v is None:
            return v
        v = str(v).strip().lower() or "brasil"
        if v != "brasil" and v not in UFS:
            raise ValueError("estado deve ser 'brasil' ou uma UF (ex.: go)")
        return v

    @field_validator("criterios_inclusao", "criterios_exclusao", "categorias", mode="before",
                     check_fields=False)
    @classmethod
    def _lista(cls, v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = v.split(",")
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return v

    @field_validator("filtros_extra", mode="before", check_fields=False)
    @classmethod
    def _extras(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            v = json.loads(v or "{}")
        if not isinstance(v, dict) or set(v) - {"ano_min", "ano_max", "km_max", "motor"}:
            raise ValueError("filtros_extra aceita só ano_min, ano_max, km_max, motor")
        for k in ("ano_min", "ano_max"):
            if k in v and not 1950 <= int(v[k]) <= 2100:
                raise ValueError(f"{k} fora de 1950-2100")
        if "km_max" in v and int(v["km_max"]) < 0:
            raise ValueError("km_max negativo")
        if "motor" in v and not re.fullmatch(r"\d\.\d", str(v["motor"])):
            raise ValueError("motor deve ser como 1.8")
        return v


class BuscaCreate(_Validacoes):
    nome:                str = Field(min_length=1, max_length=100)
    tipo:                Literal["produto", "carro", "imovel"] = "produto"
    termo:               str = Field(default="", max_length=100)
    criterios_inclusao:  list[str] = Field(default_factory=list, max_length=20)
    criterios_exclusao:  list[str] = Field(default_factory=list, max_length=20)
    estado:              str = "brasil"
    preco_min:           int = Field(default=0, ge=0, le=100_000_000)
    preco_max:           int = Field(default=0, ge=0, le=100_000_000)
    filtro_envio:        Envio = "qualquer"
    filtro_vendedor:     Vendedor = "qualquer"
    instrucoes:          str = Field(default="", max_length=1000)
    filtros_extra:       dict = Field(default_factory=dict)
    garantia:            Garantia = ""
    condicao:            Condicao = "funcionando"
    categorias:          list[str] = Field(default_factory=list, max_length=30)
    intervalo_min:       int = Field(default=30, ge=5, le=1440)

    @model_validator(mode="after")
    def _coerencia(self):
        # "/add rtx 3090 com defeito" sem condicao= explícita: deduz a intenção
        # (negado não conta: critério "sem defeito" é o contrário de pedir defeito)
        pedido = re.sub(r"\b(sem|nao|nunca|nenhuma?)\s+\w+", " ",
                        _sem_acento(f"{self.termo} {' '.join(self.criterios_inclusao)}"))
        if "condicao" not in self.model_fields_set and _RE_QUER_DEFEITO.search(pedido):
            self.condicao = "defeito"
        if not self.termo and self.tipo != "carro":
            raise ValueError("termo obrigatório (só carro aceita busca sem modelo)")
        if self.preco_min and self.preco_max and self.preco_min > self.preco_max:
            raise ValueError("preco_min maior que preco_max")
        return self


class Feedback(BaseModel):
    gostou: Optional[bool]          # None remove a avaliação


class BuscaUpdate(_Validacoes):
    """Todos os campos editáveis (antes o PATCH descartava termo, estado,
    instruções, vendedor e tipo EM SILÊNCIO)."""
    nome:                Optional[str] = Field(default=None, min_length=1, max_length=100)
    tipo:                Optional[Literal["produto", "carro", "imovel"]] = None
    termo:               Optional[str] = Field(default=None, max_length=100)
    criterios_inclusao:  Optional[list[str]] = Field(default=None, max_length=20)
    criterios_exclusao:  Optional[list[str]] = Field(default=None, max_length=20)
    estado:              Optional[str] = None
    preco_min:           Optional[int] = Field(default=None, ge=0, le=100_000_000)
    preco_max:           Optional[int] = Field(default=None, ge=0, le=100_000_000)
    filtro_envio:        Optional[Envio] = None
    filtro_vendedor:     Optional[Vendedor] = None
    instrucoes:          Optional[str] = Field(default=None, max_length=1000)
    filtros_extra:       Optional[dict] = None
    garantia:            Optional[Garantia] = None
    condicao:            Optional[Condicao] = None
    categorias:          Optional[list[str]] = Field(default=None, max_length=30)
    intervalo_min:       Optional[int] = Field(default=None, ge=5, le=1440)
