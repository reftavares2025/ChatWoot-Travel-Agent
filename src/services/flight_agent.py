"""Captur Viagens — venda self-service de passagem aérea (LangGraph).

Grafo de estados: qualifica origem/destino/datas -> busca voos (SerpAPI/Google
Flights) -> apresenta ofertas -> captura dados dos passageiros -> reserva ->
gera boleto -> aguarda confirmação de pagamento (webhook) -> emite bilhete.

Entra pelo mesmo roteador do bot (src/bot/router.py, opção "6" do menu) e usa
os mesmos serviços já em produção (EvolutionService para WhatsApp,
ChatwootService para deixar a equipe acompanhar a conversa).

Emissão de bilhete e geração de boleto são Protocols: troque as classes Mock*
por integrações reais (fornecedor de emissão / API de boleto do banco) antes
de ativar `settings.VENDA_PASSAGEM_ATIVA`. Enquanto não houver integração
real, a feature fica desligada por padrão — nunca mandamos boleto ou reserva
falsa pra um cliente de verdade.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal, Optional, Protocol, TypedDict

from pydantic import BaseModel, Field
from langgraph.graph import END, START, StateGraph

from config.settings import settings
from src.services.evolution import EvolutionService
from src.services.chatwoot import ChatwootService
from src.bot.state import update_estado_lead

TELEFONE_PAULA = "5515997495563"
TELEFONE_FLAVIA = "5515997607519"

# ───────────────────────── Modelos ─────────────────────────


class Slots(BaseModel):
    origem: Optional[str] = Field(None, description="IATA 3 letras")
    destino: Optional[str] = Field(None, description="IATA 3 letras")
    data_ida: Optional[str] = Field(None, description="AAAA-MM-DD")
    data_volta: Optional[str] = Field(None, description="AAAA-MM-DD")
    somente_ida: Optional[bool] = None
    adultos: Optional[int] = None
    hospedagem: Optional[str] = None
    orcamento: Optional[str] = None


class Oferta(BaseModel):
    id: str
    rotulo: str
    descricao: str
    preco_centavos: int = Field(gt=0)
    expira_em: str  # ISO UTC — janela nossa de re-cotação, não garantia da cia
    fonte: str


class Passageiro(BaseModel):
    nome_completo: Optional[str] = None
    cpf: Optional[str] = None
    nascimento: Optional[str] = Field(None, description="AAAA-MM-DD")
    email: Optional[str] = None
    telefone: Optional[str] = None


class Dados(BaseModel):
    passageiros: list[Passageiro] = []


class Intencao(BaseModel):
    tipo: Literal["ideal", "premium", "sim", "recusa", "duvida", "outro"]


class Estado(TypedDict, total=False):
    telefone: str
    etapa: str
    mensagem_in: str
    evento: Optional[dict]
    respostas: list[str]
    slots: dict
    ofertas: list[dict]
    oferta_escolhida: Optional[dict]
    passageiros: list[dict]
    reserva: Optional[dict]
    cobranca: Optional[dict]
    bilhete: Optional[dict]
    erros: int
    rebuscar: bool
    escalar_motivo: Optional[str]


# ───────────────────────── Portas (providers) ─────────────────────────


class BuscaVoos(Protocol):
    def buscar(self, slots: dict) -> list[dict]:
        """Retorna dicts compatíveis com Oferta."""


class Emissor(Protocol):
    def reservar(self, oferta: dict, passageiros: list[dict]) -> dict:
        """-> {"reserva_id", "preco_centavos", "expira_em"(ISO UTC)}. Preço = verdade da API."""

    def emitir(self, reserva_id: str) -> dict:
        """-> {"bilhete", "voucher_url"}. Só chamado após pagamento confirmado."""


class Cobranca(Protocol):
    def gerar_boleto(self, *, referencia: str, valor_centavos: int, pagador: dict,
                     vencimento: date) -> dict:
        """-> {"id", "linha_digitavel", "pdf_url", "vencimento"}."""


@dataclass
class Deps:
    llm: object
    busca: BuscaVoos
    emissor: Emissor
    cobranca: Cobranca
    notificar: Callable[[str, dict], None]  # avisa a diretoria (WhatsApp) — motivo, estado
    limite_escalonamento_centavos: int = 3_000_000  # R$ 30.000
    janela_oferta_min: int = 15
    prazo_compensacao_h: int = 72  # boleto comum ~1-3 dias úteis; Pix/boleto híbrido: ~1


# ───────────────────────── Guardrails ─────────────────────────

REGEX_ESCALAR = re.compile(r"cancel|reembols|estorn|chargeback|processo|procon", re.I)


def agora() -> datetime:
    return datetime.now(timezone.utc)


def brl(c: int) -> str:
    return "R$ " + f"{c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def valores_permitidos(texto: str, permitidos: set[int]) -> bool:
    """Todo 'R$ x' escrito pela LLM precisa existir em `permitidos` (vindos de tool)."""
    for m in re.findall(r"R\$\s*([\d\.]+,\d{2})", texto):
        if int(m.replace(".", "").replace(",", "")) not in permitidos:
            return False
    return True


def cpf_valido(cpf: str) -> bool:
    d = re.sub(r"\D", "", cpf or "")
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def data_ok(s: Optional[str], futuro: bool) -> bool:
    try:
        d = date.fromisoformat(s or "")
    except ValueError:
        return False
    return d >= date.today() if futuro else d < date.today()


def say(s: Estado, *txt: str) -> list[str]:
    return s.get("respostas", []) + list(txt)


# ───────────────────────── Grafo ─────────────────────────

PERGUNTAS = {
    "origem": "De onde você vai sair?",
    "destino": "Para onde quer ir?",
    "data_ida": "Qual a data de ida?",
    "volta": "Qual a data de volta (ou é somente ida)?",
    "adultos": "Quantos passageiros?",
}


def faltantes(sl: Slots) -> list[str]:
    f = []
    if not sl.origem: f.append("origem")
    if not sl.destino: f.append("destino")
    if not sl.data_ida: f.append("data_ida")
    if not sl.data_volta and not sl.somente_ida: f.append("volta")
    if not sl.adultos: f.append("adultos")
    return f


def construir_grafo(deps: Deps, checkpointer=None):
    def escalar_se_erro(motivo: str) -> dict:
        return {"escalar_motivo": motivo}

    # ── entrada / roteamento ──
    def n_entrada(s: Estado):
        return {"respostas": []}

    def rota_entrada(s: Estado) -> str:
        ev = s.get("evento") or {}
        if ev.get("tipo") == "pagamento_confirmado":
            return "emitir_bilhete"
        if ev.get("tipo") == "boleto_vencido":
            return "boleto_vencido"
        if REGEX_ESCALAR.search(s.get("mensagem_in", "")) and s.get("etapa") != "ESCALADO":
            return "escalar"
        return {
            "QUALIFICAR": "qualificar", "ESCOLHA": "tratar_escolha", "DADOS": "capturar_dados",
            "CONFIRMAR_PRECO": "confirmar_preco", "AGUARDANDO_PAGAMENTO": "status_pagamento",
            "POS_VENDA": "pos_venda", "ESCALADO": "escalado",
        }[s.get("etapa", "QUALIFICAR")]

    # ── 1. QUALIFICAR ──
    def n_qualificar(s: Estado):
        atual = Slots(**s.get("slots", {}))
        ext = deps.llm.with_structured_output(Slots).invoke([
            ("system", f"Extraia dados de viagem. Hoje é {date.today()}. Converta cidades em "
                       "código IATA. Preencha só o que o cliente disse."),
            ("human", s.get("mensagem_in", "")),
        ])
        novo = atual.model_copy(update={k: v for k, v in ext.model_dump().items() if v is not None})
        for campo in ("origem", "destino"):
            v = (getattr(novo, campo) or "").upper()
            setattr(novo, campo, v if re.fullmatch(r"[A-Z]{3}", v) else None)
        if novo.data_ida and not data_ok(novo.data_ida, True): novo.data_ida = None
        if novo.data_volta and not data_ok(novo.data_volta, True): novo.data_volta = None
        falta = faltantes(novo)
        if falta:
            pergs = " ".join(PERGUNTAS[k] for k in falta[:2])
            return {"slots": novo.model_dump(), "etapa": "QUALIFICAR", "respostas": say(s, pergs)}
        return {"slots": novo.model_dump(), "respostas": say(s, "Perfeito! Buscando as melhores opções ⏳")}

    # ── 2. BUSCAR ──
    def n_buscar(s: Estado):
        try:
            brutas = deps.busca.buscar(s["slots"])
            ofertas = [Oferta(**o).model_dump() for o in brutas]
        except Exception:
            ofertas = []
        if not ofertas:
            erros = s.get("erros", 0) + 1
            if erros >= 3:
                return {"erros": erros, **escalar_se_erro("busca sem resultado 3x seguidas")}
            return {"erros": erros, "rebuscar": False, "respostas": say(
                s, "Não consegui confirmar disponibilidade agora. Quer tentar outras datas?")}
        return {"ofertas": ofertas, "erros": 0, "rebuscar": False}

    # ── 3. OFERTAR (texto 100% de template: preço só vem do estado) ──
    def n_apresentar(s: Estado):
        blocos = [f"*{o['rotulo']}* — {o['descricao']}\n{brl(o['preco_centavos'])} (total)"
                  for o in s["ofertas"]]
        txt = "Encontrei estas opções:\n\n" + "\n\n".join(blocos) + \
              "\n\nQual você prefere? (valores válidos por ~%d min)" % deps.janela_oferta_min
        return {"etapa": "ESCOLHA", "respostas": say(s, txt)}

    def n_tratar_escolha(s: Estado):
        ofertas = s["ofertas"]
        if any(datetime.fromisoformat(o["expira_em"]) < agora() for o in ofertas):
            return {"etapa": "QUALIFICAR", "ofertas": [], "rebuscar": True,
                    "respostas": say(s, "Os valores expiraram, vou atualizar 🔄")}
        it = deps.llm.with_structured_output(Intencao).invoke([
            ("system", "Classifique a resposta do cliente às ofertas 'Ideal' e 'Premium': "
                       "ideal, premium, recusa, duvida ou outro."),
            ("human", s["mensagem_in"]),
        ]).tipo
        if it in ("ideal", "premium"):
            escolhida = next(o for o in ofertas if o["rotulo"].lower() == it)
            return {"oferta_escolhida": escolhida, "etapa": "DADOS", "passageiros": [],
                    "respostas": say(s, f"Ótima escolha! Para reservar, preciso de nome completo, "
                                        f"CPF, data de nascimento e e-mail de cada passageiro "
                                        f"({s['slots']['adultos']}).")}
        if it == "recusa":
            return {"etapa": "QUALIFICAR", "ofertas": [],
                    "respostas": say(s, "Sem problema! O que quer ajustar (datas, destino, orçamento)?")}
        if it == "duvida":
            ctx = "\n".join(f"{o['rotulo']}: {o['descricao']} {brl(o['preco_centavos'])}" for o in ofertas)
            txt = deps.llm.invoke([
                ("system", "Responda em até 2 frases, PT-BR. Não cite valores, horários ou regras "
                           "que não estejam no contexto; se não souber, diga que vai confirmar.\n" + ctx),
                ("human", s["mensagem_in"]),
            ]).content
            if not valores_permitidos(txt, {o["preco_centavos"] for o in ofertas}):
                txt = "Não consegui confirmar essa informação agora. Posso te ajudar a escolher entre as opções?"
            return {"respostas": say(s, txt)}
        return {"respostas": say(s, "Prefere a *Ideal* ou a *Premium*?")}

    # ── 4. CAPTURAR DADOS ──
    def n_capturar(s: Estado):
        n = s["slots"]["adultos"]
        ext = deps.llm.with_structured_output(Dados).invoke([
            ("system", "Extraia dados de passageiros. Não invente nada ausente."),
            ("human", s["mensagem_in"]),
        ]).passageiros
        pax = [dict(p) for p in s.get("passageiros", [])]
        for i, novo in enumerate(ext):
            if i >= n: break
            while len(pax) <= i: pax.append({})
            pax[i].update({k: v for k, v in novo.model_dump().items() if v})
        avisos = []
        for i, p in enumerate(pax):
            if p.get("cpf") and not cpf_valido(p["cpf"]):
                avisos.append(f"O CPF do passageiro {i+1} não parece válido, pode conferir?"); p.pop("cpf")
            if p.get("nascimento") and not data_ok(p["nascimento"], False):
                avisos.append(f"Data de nascimento do passageiro {i+1} inválida (use AAAA-MM-DD)."); p.pop("nascimento")
            if p.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", p["email"]):
                avisos.append(f"E-mail do passageiro {i+1} inválido."); p.pop("email")
            p.setdefault("telefone", s["telefone"])
        campos = ["nome_completo", "cpf", "nascimento", "email"]
        while len(pax) < n: pax.append({"telefone": s["telefone"]})
        falta = [(i, c) for i, p in enumerate(pax) for c in campos if not p.get(c)]
        if falta:
            i = falta[0][0]
            nomes = {"nome_completo": "nome completo", "cpf": "CPF",
                     "nascimento": "data de nascimento", "email": "e-mail"}
            pedir = ", ".join(nomes[c] for j, c in falta if j == i)
            return {"passageiros": pax, "respostas": say(s, *avisos, f"Passageiro {i+1}: falta {pedir}.")}
        return {"passageiros": pax, "etapa": "RESERVA"}

    # ── 5. RESERVAR (trava tarifa; preço final = o que a API devolver) ──
    def n_reservar(s: Estado):
        of = s["oferta_escolhida"]
        if of["preco_centavos"] > deps.limite_escalonamento_centavos:
            return escalar_se_erro(f"pedido acima do limite: {brl(of['preco_centavos'])}")
        try:
            r = deps.emissor.reservar(of, s["passageiros"])
        except Exception as e:
            return escalar_se_erro(f"falha ao reservar: {type(e).__name__}")
        if r["preco_centavos"] != of["preco_centavos"]:
            novo = {**of, "preco_centavos": r["preco_centavos"]}
            return {"reserva": r, "oferta_escolhida": novo, "etapa": "CONFIRMAR_PRECO", "respostas": say(
                s, f"O valor foi atualizado pela companhia para {brl(r['preco_centavos'])}. Confirma?")}
        return {"reserva": r, "etapa": "COBRANCA"}

    def n_confirmar_preco(s: Estado):
        it = deps.llm.with_structured_output(Intencao).invoke([
            ("system", "O cliente aceita o novo valor? sim, recusa ou outro."),
            ("human", s["mensagem_in"])]).tipo
        if it == "sim":
            return {"etapa": "COBRANCA"}
        if it == "recusa":
            return {"etapa": "QUALIFICAR", "ofertas": [], "reserva": None,
                    "respostas": say(s, "Tudo bem, cancelei a reserva. Quer buscar outras opções?")}
        return {"respostas": say(s, "Confirma o novo valor? (sim/não)")}

    # ── 6. COBRAR (boleto direto no banco) ──
    def n_cobrar(s: Estado):
        r, of = s["reserva"], s["oferta_escolhida"]
        exp = datetime.fromisoformat(r["expira_em"])
        if exp < agora() + timedelta(hours=deps.prazo_compensacao_h):
            return escalar_se_erro("hold da tarifa menor que prazo de compensação do boleto")
        venc = min(date.today() + timedelta(days=2), exp.date())
        try:
            b = deps.cobranca.gerar_boleto(referencia=r["reserva_id"], valor_centavos=of["preco_centavos"],
                                           pagador=s["passageiros"][0], vencimento=venc)
        except Exception as e:
            return escalar_se_erro(f"falha ao gerar boleto: {type(e).__name__}")
        cobr = {**b, "valor_centavos": of["preco_centavos"]}
        return {"cobranca": cobr, "etapa": "AGUARDANDO_PAGAMENTO", "respostas": say(
            s, f"Reserva pronta! Total {brl(cobr['valor_centavos'])}.\n"
               f"Boleto (venc. {b['vencimento']}):\n{b['linha_digitavel']}\nPDF: {b['pdf_url']}\n"
               "Assim que o pagamento for confirmado eu emito seu bilhete automaticamente ✈️")}

    def n_status_pagamento(s: Estado):
        return {"respostas": say(s, "Ainda aguardando a confirmação do pagamento. "
                                    "Te aviso aqui assim que compensar.")}

    # ── 7/8. WEBHOOK → EMITIR (nunca assume pagamento) ──
    def n_emitir(s: Estado):
        if s.get("bilhete"):  # idempotência: webhook duplicado
            return {"respostas": s.get("respostas", [])}
        ev, c = s.get("evento") or {}, s.get("cobranca")
        if not c or ev.get("cobranca_id") != c["id"] or ev.get("valor_pago_centavos", 0) < c["valor_centavos"]:
            return escalar_se_erro("webhook de pagamento inconsistente com a cobrança")
        try:
            b = deps.emissor.emitir(s["reserva"]["reserva_id"])
        except Exception as e:
            return escalar_se_erro(f"⚠ PAGO E NÃO EMITIDO ({type(e).__name__}) reserva {s['reserva']['reserva_id']}")
        return {"bilhete": b, "etapa": "POS_VENDA", "respostas": say(
            s, f"Pagamento confirmado ✅ Bilhete emitido!\nVoucher: {b['voucher_url']}")}

    def n_boleto_vencido(s: Estado):
        return {"etapa": "QUALIFICAR", "ofertas": [], "reserva": None, "cobranca": None,
                "respostas": say(s, "Seu boleto venceu e a tarifa foi liberada. Quer que eu refaça a busca? (responda 'sim')")}

    # ── 9. PÓS-VENDA (placeholder do concierge) ──
    def n_pos_venda(s: Estado):
        return {"respostas": say(s, "Sua viagem está confirmada ✅ Precisa de algo? Estou por aqui 24h.")}

    # ── Escalonamento ──
    def n_escalar(s: Estado):
        motivo = s.get("escalar_motivo") or "cliente pediu cancelamento/reembolso ou tema sensível"
        deps.notificar(motivo, {"telefone": s.get("telefone"), "etapa": s.get("etapa"),
                                "cobranca": s.get("cobranca"), "reserva": s.get("reserva")})
        return {"escalar_motivo": None, "etapa": "ESCALADO", "respostas": say(
            s, "Já acionei nosso time para cuidar disso com prioridade. Retornamos em breve 🙏")}

    def n_escalado(s: Estado):
        return {"respostas": say(s, "Seu caso já está com nosso time e retornaremos em breve.")}

    # ── Montagem ──
    g = StateGraph(Estado)
    nos = {
        "entrada": n_entrada, "qualificar": n_qualificar, "buscar": n_buscar, "apresentar": n_apresentar,
        "tratar_escolha": n_tratar_escolha, "capturar_dados": n_capturar, "reservar": n_reservar,
        "confirmar_preco": n_confirmar_preco, "cobrar": n_cobrar, "status_pagamento": n_status_pagamento,
        "emitir_bilhete": n_emitir, "boleto_vencido": n_boleto_vencido, "pos_venda": n_pos_venda,
        "escalar": n_escalar, "escalado": n_escalado,
    }
    for nome, fn in nos.items():
        g.add_node(nome, fn)
    g.add_edge(START, "entrada")

    destinos = [n for n in nos if n != "entrada"]
    g.add_conditional_edges("entrada", rota_entrada, {n: n for n in destinos})

    def com_escalada(proximo: Callable[[Estado], str]):
        return lambda s: "escalar" if s.get("escalar_motivo") else proximo(s)

    g.add_conditional_edges("qualificar", lambda s: "buscar" if not faltantes(Slots(**s["slots"])) else END,
                            {"buscar": "buscar", END: END})
    g.add_conditional_edges("buscar", com_escalada(lambda s: "apresentar" if s.get("ofertas") and not s.get("erros") else END),
                            {"apresentar": "apresentar", "escalar": "escalar", END: END})
    g.add_conditional_edges("tratar_escolha", lambda s: "buscar" if s.get("rebuscar") else END,
                            {"buscar": "buscar", END: END})
    g.add_conditional_edges("capturar_dados", lambda s: "reservar" if s.get("etapa") == "RESERVA" else END,
                            {"reservar": "reservar", END: END})
    g.add_conditional_edges("reservar", com_escalada(lambda s: "cobrar" if s.get("etapa") == "COBRANCA" else END),
                            {"cobrar": "cobrar", "escalar": "escalar", END: END})
    g.add_conditional_edges("confirmar_preco", lambda s: "cobrar" if s.get("etapa") == "COBRANCA" else END,
                            {"cobrar": "cobrar", END: END})
    g.add_conditional_edges("cobrar", com_escalada(lambda s: END), {"escalar": "escalar", END: END})
    g.add_conditional_edges("emitir_bilhete", com_escalada(lambda s: END), {"escalar": "escalar", END: END})
    g.add_edge("apresentar", END)
    for n in ("status_pagamento", "boleto_vencido", "pos_venda", "escalar", "escalado"):
        g.add_edge(n, END)

    return g.compile(checkpointer=checkpointer)


# ───────────────────────── Implementações reais ─────────────────────────


def llm_padrao():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=settings.LLM_MODEL,
                      base_url=settings.LITELLM_BASE_URL or None,
                      api_key=settings.LITELLM_API_KEY or "sk-x", temperature=0)


class SerpApiBusca:
    """Google Flights via SerpAPI. Preço retornado é total (confirmado na doc)."""

    def __init__(self, api_key: str, janela_min: int = 15):
        self.key, self.janela = api_key, janela_min

    @staticmethod
    def _desc(v: dict) -> str:
        f = v["flights"]
        return (f"{f[0].get('airline','?')} {f[0]['departure_airport'].get('time','')[-5:]} "
                f"{f[0]['departure_airport']['id']}→{f[-1]['arrival_airport']['id']} · "
                f"{len(f)-1} escala(s) · {v.get('total_duration', 0)//60}h{v.get('total_duration', 0)%60:02d}")

    def buscar(self, slots: dict) -> list[dict]:
        import httpx
        p = {"engine": "google_flights", "departure_id": slots["origem"], "arrival_id": slots["destino"],
             "outbound_date": slots["data_ida"], "currency": "BRL", "hl": "pt",
             "adults": slots.get("adultos") or 1, "api_key": self.key}
        if slots.get("somente_ida"): p["type"] = 2
        else: p["return_date"] = slots["data_volta"]
        r = httpx.get("https://serpapi.com/search.json", params=p, timeout=30); r.raise_for_status()
        d = r.json()
        voos = [v for v in (d.get("best_flights") or []) + (d.get("other_flights") or []) if v.get("price")]
        if not voos: return []
        barato = min(voos, key=lambda v: v["price"])
        resto = [v for v in voos if v is not barato]
        exp = (agora() + timedelta(minutes=self.janela)).isoformat()
        mk = lambda rot, v: {"id": f"{rot}-{abs(hash(v.get('departure_token', self._desc(v))))}", "rotulo": rot,
                             "descricao": self._desc(v), "preco_centavos": int(v["price"]) * 100,
                             "expira_em": exp, "fonte": "serpapi_google_flights"}
        ofertas = [mk("Ideal", barato)]
        if resto: ofertas.append(mk("Premium", min(resto, key=lambda v: v.get("total_duration", 10**9))))
        return ofertas


class EmissorIndisponivel:
    """Sem integração real com fornecedor de emissão. Nunca use com cliente de
    verdade — existe só pra manter o grafo compilável enquanto
    VENDA_PASSAGEM_ATIVA=False."""

    def reservar(self, oferta, passageiros):
        raise RuntimeError("Emissor de bilhetes não configurado — integre um fornecedor real antes de ativar a venda.")

    def emitir(self, reserva_id):
        raise RuntimeError("Emissor de bilhetes não configurado — integre um fornecedor real antes de ativar a venda.")


class CobrancaIndisponivel:
    """Sem integração real com a API de boleto do banco. Mesma lógica do
    EmissorIndisponivel: bloqueia o fluxo em vez de gerar um boleto falso."""

    def gerar_boleto(self, *, referencia, valor_centavos, pagador, vencimento):
        raise RuntimeError("API de boleto não configurada — integre o banco real antes de ativar a venda.")


# ───────────────────────── Integração com o bot Captur ─────────────────────────

_checkpointer = None
_grafo = None
_cobranca_telefone: dict[str, str] = {}  # cobranca_id -> telefone; TODO produção: tabela no Postgres


def _notificar_diretoria(motivo: str, ctx: dict) -> None:
    texto = f"🚨 *[Captur - Venda de Passagem]* {motivo}\n{ctx}"
    import asyncio
    for fone in (TELEFONE_PAULA, TELEFONE_FLAVIA):
        asyncio.create_task(EvolutionService.enviar_mensagem(fone, texto))


async def iniciar_checkpointer() -> None:
    """Chamado uma vez no startup da API. Cria as tabelas de checkpoint do
    LangGraph no mesmo Postgres do resto do sistema (idempotente)."""
    global _checkpointer, _grafo
    if not settings.VENDA_PASSAGEM_ATIVA:
        print("[Venda Passagem] Recurso desligado (VENDA_PASSAGEM_ATIVA=False). Grafo não é inicializado.")
        return
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    cm = AsyncPostgresSaver.from_conn_string(dsn)
    _checkpointer = await cm.__aenter__()
    await _checkpointer.setup()
    _grafo = _construir_grafo_prod()
    print("[Venda Passagem] Grafo e checkpointer Postgres prontos.")


def _construir_grafo_prod():
    deps = Deps(
        llm=llm_padrao(),
        busca=SerpApiBusca(settings.SERPAPI_API_KEY),
        emissor=EmissorIndisponivel(),   # TROQUE por fornecedor real antes de vender de verdade
        cobranca=CobrancaIndisponivel(), # TROQUE pela API de boleto do banco antes de vender de verdade
        notificar=_notificar_diretoria,
        limite_escalonamento_centavos=settings.LIMITE_ESCALONAMENTO_CENTAVOS,
    )
    return construir_grafo(deps, checkpointer=_checkpointer)


async def _mirrorar_no_chatwoot(telefone: str, textos_cliente: list[str], respostas_bot: list[str]) -> None:
    """Loga a conversa numa caixa do Chatwoot pra equipe acompanhar, sem
    interferir no fluxo (mensagens indo como 'incoming'/'outgoing' normais)."""
    try:
        contato_id = await ChatwootService.obter_ou_criar_contato(telefone)
        if not contato_id:
            return
        conv_id = await ChatwootService.obter_ou_criar_conversa(contato_id, inbox_id=settings.CHATWOOT_INBOX_PASSAGEM)
        if not conv_id:
            return
        for t in textos_cliente:
            await ChatwootService.adicionar_mensagem(conv_id, t, message_type="incoming")
        for r in respostas_bot:
            await ChatwootService.adicionar_mensagem(conv_id, r, message_type="outgoing")
    except Exception as e:
        print(f"[Venda Passagem] Erro ao espelhar no Chatwoot: {e}")


async def processar_mensagem_passagem(db, telefone: str, texto: str) -> None:
    """Ponto de entrada chamado pelo webhook do WhatsApp quando o cliente está
    no fluxo de venda de passagem (EstadoAtendimento.estado == 'venda_passagem')."""
    if not settings.VENDA_PASSAGEM_ATIVA or _grafo is None:
        await EvolutionService.enviar_mensagem(
            telefone, "Essa opção de compra direta ainda não está disponível. "
                      "Digite *#menu* que já te encaminho pra um de nossos especialistas.")
        await update_estado_lead(db, telefone, novo_estado="aguardando_opcao")
        return

    cfg = {"configurable": {"thread_id": telefone}}
    res = await _grafo.ainvoke({"telefone": telefone, "mensagem_in": texto, "evento": None}, cfg)

    if res.get("cobranca"):
        _cobranca_telefone[res["cobranca"]["id"]] = telefone

    respostas = res.get("respostas", [])
    for t in respostas:
        await EvolutionService.enviar_mensagem(telefone, t)
    await _mirrorar_no_chatwoot(telefone, [texto], respostas)

    if res.get("etapa") == "ESCALADO":
        await update_estado_lead(db, telefone, novo_estado="atendimento_humano")


async def processar_pagamento_confirmado(telefone: str, cobranca_id: str, valor_pago_centavos: int) -> None:
    """Chamado pelo webhook de pagamento do banco (src/api/routes.py)."""
    if not settings.VENDA_PASSAGEM_ATIVA or _grafo is None:
        return
    cfg = {"configurable": {"thread_id": telefone}}
    evento = {"tipo": "pagamento_confirmado", "cobranca_id": cobranca_id,
              "valor_pago_centavos": valor_pago_centavos}
    res = await _grafo.ainvoke({"telefone": telefone, "mensagem_in": "", "evento": evento}, cfg)
    respostas = res.get("respostas", [])
    for t in respostas:
        await EvolutionService.enviar_mensagem(telefone, t)
    await _mirrorar_no_chatwoot(telefone, [], respostas)


def telefone_da_cobranca(cobranca_id: str) -> Optional[str]:
    return _cobranca_telefone.get(cobranca_id)
