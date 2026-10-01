import hmac
import os
import re
import uuid
import httpx
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.database.connection import get_db
from src.bot.router import processar_mensagem
from src.bot.state import get_estado, update_estado
from src.services.transcription import TranscriptionService
from src.services.chatwoot import ChatwootService
from src.services.evolution import EvolutionService
from src.services.contract_engine import register_contract, convert_docx_to_pdf, BASE_DIR
from src.services import financeiro
from src.services import financial_agent
from src.services import flight_agent
from src.services import sales_copilot
from src.services import closer_agent
from src.services import team_search
from config.settings import settings

router = APIRouter()

# Inbox dedicada no Chatwoot onde a Paula conversa em privado com a IA financeira.
FINANCEIRO_INBOX_ID = 7

# Numeros autorizados a falar direto com a IA financeira pelo WhatsApp normal (texto ou audio),
# mandando mensagem pro numero oficial da Captur — sem precisar abrir o Chatwoot.
NUMEROS_FINANCEIRO_IA = {
    "5515997495563": "Paula",
    "5519990154309": "Régis",
}

# Numeros autorizados a pedir busca de cliente/passageiro pra Olívia (texto ou audio), sem
# acesso ao financeiro. Paula/Régis já têm essa busca embutida na IA financeira (NUMEROS_FINANCEIRO_IA).
# Adicione o numero da Ester/Thamires aqui quando tiver.
NUMEROS_BUSCA_CLIENTE = {
    "5515997607519": "Flávia",
}

_VALOR_CONTRATO_REGEX = re.compile(r'(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+\.\d{2})')

def extrair_valor_contrato(content: str) -> tuple[Optional[Decimal], str]:
    """Extrai um valor monetário do texto do comando /contrato, se houver, e devolve
    (valor, texto_sem_o_valor) para não poluir a extração do nome do passageiro."""
    match = _VALOR_CONTRATO_REGEX.search(content or "")
    if not match:
        return None, content
    raw = match.group(1)
    try:
        normalizado = raw.replace('.', '').replace(',', '.') if ',' in raw else raw
        valor = Decimal(normalizado)
    except (InvalidOperation, ValueError):
        return None, content
    texto_sem_valor = (content[:match.start()] + content[match.end():]).replace('R$', '').strip()
    return valor, texto_sem_valor

def _fmt_reais(valor) -> str:
    return f"R$ {Decimal(valor):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")

HTML_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "qrcode.html")
BRAND_ASSETS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config", "brand_assets")

@router.get("/health")
async def health_check():
    return {"status": "ok", "app": "Captur Bot & Contract Engine"}

@router.get("/qrcode", response_class=HTMLResponse)
async def qrcode_page():
    if os.path.exists(HTML_TEMPLATE_PATH):
        with open(HTML_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>Página de QR Code não encontrada.</h2>", status_code=404)

@router.get("/brand-assets/{filename}")
async def brand_assets(filename: str):
    file_path = os.path.join(BRAND_ASSETS_PATH, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return JSONResponse({"error": "File not found"}, status_code=404)

_redirecionamentos_enviados: dict[str, float] = {}
_contatos_comerciais_confirmados: set[str] = set()
_mensagens_processadas_ids: dict[str, float] = {}
_grupos_nomes_cache: dict[str, str] = {}
_mensagens_chatwoot_processadas: dict[int, float] = {}
_ultimos_documentos_whatsapp: dict[str, dict] = {}

def extrair_nome_passageiro(content: str, filename: str = "", default_name: str = "") -> str:
    # 1. Se a atendente digitou o nome após o comando: ex: /contrato Maria Santos
    for prefix in ["/contrato", "#contrato"]:
        if prefix in content.lower():
            idx = content.lower().find(prefix)
            resto = content[idx + len(prefix):].strip()
            resto = resto.lstrip(":- ").strip()
            if resto and len(resto) > 2 and not resto.startswith("#") and not resto.startswith("/"):
                return resto

    # 2. Se o arquivo tem o nome do titular (ex: Contrato Grupo Espanha Humberto.pdf -> Humberto)
    if filename:
        clean = Path(filename).stem
        for w in ["Contrato", "contrato", "CONTRATO", "Captur", "captur", "CAPTUR", "viagem", "Viagem", "VIAGEM"]:
            clean = clean.replace(w, "")
        clean = clean.replace("_", " ").replace("-", " ").strip()
        while "  " in clean:
            clean = clean.replace("  ", " ")
        if clean and len(clean) > 2:
            return clean

    # 3. Se for contato individual (não grupo), usa o nome do cliente
    if default_name and not default_name.startswith("👥") and not default_name.lower().startswith("grupo") and "@g.us" not in default_name:
        return default_name

    return "Passageiro Titular"

TERMOS_COMERCIAIS_TURISMO = {
    # Viagem & Turismo
    "viagem", "viagens", "viajar", "viajante", "viajantes", "passageiro", "passageiros",
    "passagem", "passagens", "voo", "voos", "aereo", "aéreo", "aérea", "aerea", "embarque", "desembarque",
    "bagagem", "bagagens", "mala", "malas", "aeroporto", "conexao", "conexão", "escala",
    # Hospedagem & Serviços
    "hotel", "hoteis", "hotéis", "pousada", "pousadas", "resort", "resorts", "hospedagem", "diaria", "diária", "diarias", "diárias",
    "quarto", "suite", "suíte", "all inclusive", "pensao", "pensão", "transfer", "passeio", "passeios",
    "excursao", "excursão", "guia", "roteiro", "itinerario", "itinerário", "cruzeiro", "cruzeiros", "navio", "cabine",
    "seguro viagem", "locacao", "locação", "aluguel",
    # Negociação & Vendas
    "orcamento", "orçamento", "cotacao", "cotação", "valor", "valores", "preco", "preço", "precos", "preços",
    "quanto custa", "qual o valor", "qual o preco", "promocao", "promoção", "pacote", "pacotes",
    "reserva", "reservar", "fechar", "fechamento", "proposta", "contrato", "voucher", "bilhete",
    "parcela", "parcelas", "parcelamento", "pagamento", "cartao", "cartão", "pix", "entrada", "boleto",
    "desconto", "comprovante", "fatura", "recibo",
    # Destinos & Agência
    "captur", "agencia", "agência", "disney", "orlando", "miami", "cancun", "cancún", "punta cana",
    "nordeste", "maceio", "maceió", "fortaleza", "natal", "porto de galinhas", "salvador", "maragogi",
    "gramado", "canela", "chile", "santiago", "argentina", "buenos aires", "bariloche", "europa",
    "paris", "portugal", "lisboa", "espanha", "madrid", "barcelona", "italia", "itália", "roma"
}

def eh_conversa_comercial(telefone: str, texto: str, instance: str) -> bool:
    """Garante privacidade estrita nos celulares das atendentes: apenas mensagens comerciais entram no Chatwoot."""
    # A Central Matriz Oficial é 100% comercial por padrão
    if instance.lower() == "captur":
        return True
    
    # Se o contato já foi previamente identificado como cliente comercial
    if telefone in _contatos_comerciais_confirmados:
        return True

    # Analisa o texto em busca de termos de turismo/vendas
    texto_low = (texto or "").lower()
    for termo in TERMOS_COMERCIAIS_TURISMO:
        if termo in texto_low:
            _contatos_comerciais_confirmados.add(telefone)
            return True

    return False

@router.get("/qr-status")
@router.get("/api/qr-status")
async def qr_status(instance: str = "captur"):
    headers = {"apikey": settings.EVOLUTION_GLOBAL_API_KEY}
    url_state = f"{settings.EVOLUTION_API_URL}/instance/connectionState/{instance}"
    url_connect = f"{settings.EVOLUTION_API_URL}/instance/connect/{instance}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res_state = await client.get(url_state, headers=headers)
            state_data = res_state.json()

            # Se a instância ainda não existir na Evolution, cria automaticamente
            if res_state.status_code == 404 or "not exist" in str(state_data).lower():
                await client.post(
                    f"{settings.EVOLUTION_API_URL}/instance/create",
                    json={
                        "instanceName": instance,
                        "token": settings.EVOLUTION_GLOBAL_API_KEY,
                        "qrcode": True,
                        "integration": "WHATSAPP-BAILEYS"
                    },
                    headers=headers
                )
                await client.post(
                    f"{settings.EVOLUTION_API_URL}/webhook/set/{instance}",
                    json={"webhook": {"url": "http://bot:8000/webhook/evolution", "enabled": True, "events": ["MESSAGES_UPSERT"]}},
                    headers=headers
                )
                res_state = await client.get(url_state, headers=headers)
                state_data = res_state.json()

            state = state_data.get("instance", {}).get("state", "close")

            if state == "open":
                return {"state": "open", "instance": instance}

            res_connect = await client.get(url_connect, headers=headers)
            connect_data = res_connect.json()

            return {
                "state": state,
                "instance": instance,
                "base64": connect_data.get("base64"),
                "pairingCode": connect_data.get("pairingCode"),
                "code": connect_data.get("code")
            }
    except Exception as e:
        return {"state": "error", "error": str(e), "instance": instance}

@router.post("/webhook/chatwoot")
@router.post("/api/webhook/chatwoot")
async def chatwoot_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Webhook do Chatwoot: processa comandos como /contrato digitados pelas atendentes."""
    try:
        payload = await request.json()
        event = payload.get("event")

        if event == "message_created":
            msg_id = payload.get("id")
            content = (payload.get("content") or "").strip()
            conv = payload.get("conversation", {})
            conv_id = conv.get("id")
            inbox_id = conv.get("inbox_id")
            message_type = payload.get("message_type")
            is_private = payload.get("private", False)
            contact = payload.get("sender", {})
            contact_inbox = conv.get("meta", {}).get("sender", {})
            raw_phone = contact_inbox.get("phone_number") or contact.get("phone_number") or ""
            raw_identifier = contact_inbox.get("identifier") or contact.get("identifier") or ""
            clean_phone = raw_phone.replace("+", "").strip() or raw_identifier
            client_name = contact_inbox.get("name") or contact.get("name") or "Cliente"

            # 1. Deduplicação para evitar processar o mesmo webhook repetido
            if msg_id:
                import time
                agora_ts = time.time()
                if msg_id in _mensagens_chatwoot_processadas:
                    if agora_ts - _mensagens_chatwoot_processadas[msg_id] < 60:
                        return {"status": "already_processed"}
                _mensagens_chatwoot_processadas[msg_id] = agora_ts

            # 1.5. Inbox dedicada da IA financeira: Paula pergunta, a IA responde na mesma conversa.
            # Nunca responde nada além dos dados reais retornados pelas ferramentas do agente.
            if inbox_id == FINANCEIRO_INBOX_ID and message_type == "outgoing" and not is_private and content and conv_id:
                try:
                    resposta = await financial_agent.perguntar(db, content)
                    await ChatwootService.adicionar_mensagem(conv_id, resposta, message_type="incoming")
                except Exception as e:
                    print(f"[Financeiro IA] Erro ao processar pergunta da Paula: {e}")
                    await ChatwootService.adicionar_mensagem(
                        conv_id, "Tive um erro ao consultar os dados agora, tenta de novo.", message_type="incoming"
                    )
                return {"status": "ok"}

            # 1.6. #agente / #humano: a atendente assume ou devolve o fechamento pra IA via nota privada
            if is_private and conv_id and clean_phone:
                comando = content.lower().strip()
                if comando == "#agente":
                    await closer_agent.ativar(db, clean_phone, conv_id)
                    return {"status": "ok"}
                if comando == "#humano":
                    await closer_agent.desativar(db, clean_phone, conv_id, motivo="atendente retomou manualmente")
                    return {"status": "ok"}

            # 2. Se a atendente ou o cliente digitou /contrato (ou enviou pelo clips com /contrato)
            if "/contrato" in content.lower() or "#contrato" in content.lower():
                token = f"captur-{uuid.uuid4().hex[:8]}"
                pdf_dest = BASE_DIR / "storage" / "contracts" / f"{token}_original.pdf"
                pdf_dest.parent.mkdir(parents=True, exist_ok=True)
                
                attachments = payload.get("attachments") or []
                downloaded = False
                doc_filename = ""

                # Se a atendente enviou o documento pelo clips antes e digitou /contrato em seguida
                if not attachments and conv_id:
                    try:
                        url_msgs = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations/{conv_id}/messages"
                        headers_cw = {
                            "api_access_token": settings.CHATWOOT_API_ACCESS_TOKEN,
                            "Content-Type": "application/json",
                            "X-Forwarded-Proto": "https"
                        }
                        async with httpx.AsyncClient(timeout=8.0) as client:
                            r = await client.get(url_msgs, headers=headers_cw)
                            if r.status_code == 200:
                                payload_msgs = r.json().get("payload", [])
                                for m in payload_msgs:
                                    att_list = m.get("attachments") or []
                                    if att_list:
                                        attachments = att_list
                                        print(f"[/contrato Chatwoot] Anexo recuperado da mensagem anterior {m.get('id')} na conversa {conv_id}!")
                                        break
                    except Exception as e:
                        print(f"[/contrato Chatwoot] Erro ao buscar anexo anterior: {e}")

                if attachments:
                    file_url = attachments[0].get("data_url")
                    doc_filename = attachments[0].get("data_url", "").split("/")[-1] or "Contrato.pdf"
                    if file_url:
                        try:
                            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                                resp = await client.get(file_url)
                                if resp.status_code == 200 and len(resp.content) > 100:
                                    temp_file = BASE_DIR / "storage" / "contracts" / f"{token}_upload"
                                    with open(temp_file, "wb") as f:
                                        f.write(resp.content)
                                    
                                    if str(file_url).lower().endswith(".docx"):
                                        convert_docx_to_pdf(str(temp_file), str(pdf_dest))
                                    else:
                                        with open(pdf_dest, "wb") as f:
                                            f.write(resp.content)
                                    downloaded = True
                                    print(f"[/contrato] Documento baixado com sucesso: {len(resp.content)} bytes de {file_url[:60]}")
                        except Exception as e:
                            print(f"[Chatwoot /contrato] Erro ao baixar anexo: {e}")

                if not downloaded:
                    default_pdf = BASE_DIR / "contrato_espanha_paulo_sergio.pdf"
                    if default_pdf.exists():
                        with open(default_pdf, "rb") as src, open(pdf_dest, "wb") as dst:
                            dst.write(src.read())
                    else:
                        docx_source = Path(r"C:\Users\User\Downloads\Contrato Grupo Espanha Paulo Sergio.docx")
                        if docx_source.exists():
                            convert_docx_to_pdf(str(docx_source), str(pdf_dest))

                # Extrai valor monetário (se a atendente digitou) antes de extrair o nome, pra não poluir o nome
                valor_contrato, content_sem_valor = extrair_valor_contrato(content)

                # Extrai nome do passageiro real
                passenger_name = extrair_nome_passageiro(content_sem_valor, filename=doc_filename, default_name=client_name)

                # Registrar o contrato no banco
                contract = register_contract(
                    client_name=passenger_name,
                    contract_title="Contrato de Prestação de Serviços Turísticos - Captur",
                    pdf_path=str(pdf_dest),
                    client_phone=clean_phone,
                    client_email="",
                    token=token,
                    valor_total=float(valor_contrato) if valor_contrato else None
                )

                # Lança automaticamente em Contas a Receber quando a atendente informou o valor
                if valor_contrato:
                    atendente_nome = (conv.get("meta", {}) or {}).get("assignee", {}).get("name") if conv else None
                    try:
                        await financeiro.registrar_conta_receber(
                            db, cliente_nome=passenger_name, valor_total=valor_contrato,
                            cliente_telefone=clean_phone, contrato_token=token,
                            descricao=contract['contract_title'], atendente_nome=atendente_nome
                        )
                        aviso_financeiro = f"\n\n💰 *Financeiro:* conta a receber de {_fmt_reais(valor_contrato)} já lançada automaticamente."
                    except Exception as e:
                        print(f"[Financeiro] Erro ao criar conta a receber automática: {e}")
                        aviso_financeiro = "\n\n⚠️ *Financeiro:* houve um erro ao lançar automaticamente, lance manualmente."
                else:
                    aviso_financeiro = "\n\n⚠️ *Financeiro:* nenhum valor detectado no comando — pra lançar automático, digite o valor junto, ex: `/contrato Maria Silva 45402,84`."

                # Gerar link público
                public_base = settings.PUBLIC_BASE_URL.rstrip("/")
                link_assinatura = f"{public_base}/assinar/{token}"

                # Responder na conversa do Chatwoot com link e texto pronto para envio ao cliente
                msg_chatwoot = (
                    f"✈️ *[Captur Inteligência - Contrato Gerado]*\n\n"
                    f"📄 *Contrato:* {contract['contract_title']}\n"
                    f"👤 *Passageiro Titular:* {passenger_name}\n"
                    f"🔒 *Código / Token:* `{token}`\n\n"
                    f"🔗 *Link de Assinatura Mobile:* \n👉 {link_assinatura}\n\n"
                    f"📋 *Texto pronto para enviar ao cliente:*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✈️ *Captur Agência de Viagens*\n\n"
                    f"Olá *{passenger_name}*, preparamos o seu contrato de viagem com toda a comodidade e segurança.\n\n"
                    f"Para assinar diretamente pelo celular em menos de 1 minuto, acesse o link seguro abaixo:\n"
                    f"👉 {link_assinatura}\n\n"
                    f"Ambiente seguro com validade jurídica (Lei nº 14.063/2020).\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    f"{aviso_financeiro}"
                )
                if conv_id:
                    try:
                        await ChatwootService.enviar_mensagem(conv_id, msg_chatwoot)
                    except Exception as e:
                        print(f"[Chatwoot] Erro ao enviar resposta na conversa {conv_id}: {e}")

                # Enviar no WhatsApp apenas se for conversa individual direta com o cliente (não grupo)
                is_group_conv = "@g.us" in clean_phone or "grupo" in client_name.lower() or "👥" in client_name
                if clean_phone and not is_group_conv:
                    msg_whatsapp = (
                        f"✈️ *Captur Agência de Viagens*\n\n"
                        f"Olá *{passenger_name}*, preparamos o seu contrato de viagem com toda a segurança e comodidade.\n\n"
                        f"Para assinar diretamente pelo celular em menos de 1 minuto, acesse o link seguro abaixo:\n"
                        f"👉 {link_assinatura}\n\n"
                        f"Ambiente seguro com validade jurídica (Lei nº 14.063/2020)."
                    )
                    try:
                        await EvolutionService.enviar_mensagem(clean_phone, msg_whatsapp, instance="captur")
                        print(f"[WhatsApp] Contrato enviado com sucesso para {clean_phone}!")
                    except Exception as e:
                        print(f"[WhatsApp] Erro ao enviar link para {clean_phone}: {e}")

                print(f"[/contrato] Contrato gerado: token={token}, titular={passenger_name}, link={link_assinatura}")
                return {"status": "ok"}

            # Enviar resposta digitada pelo atendente no Chatwoot diretamente para o WhatsApp do cliente
            if message_type == "outgoing" and not is_private and clean_phone and content:
                # Evita eco de mensagens que o próprio bot gerou
                is_synced_echo = (
                    content.startswith("✈️ *[Captur Inteligência")
                    or content.startswith("📋 *[Triagem Captur]")
                    or content.startswith("*Agência (WhatsApp Fixo 2640)*:")
                    or (content.startswith("*") and "*: " in content[:80])
                )
                if not is_synced_echo:
                    try:
                        await EvolutionService.enviar_mensagem(clean_phone, content, instance="captur")
                        print(f"[Chatwoot -> WhatsApp] Mensagem enviada para {clean_phone}: '{content[:50]}...'")
                        is_group_reply = "@g.us" in clean_phone or "grupo" in client_name.lower() or "👥" in client_name
                        if not is_group_reply:
                            retomou_ia = await closer_agent.retomar_apos_orcamento(db, clean_phone)
                            if not retomou_ia:
                                await update_estado(db, clean_phone, "atendimento_humano")
                                print(f"[Handoff] {clean_phone} marcado como atendimento_humano - bot silenciado a partir de agora.")
                    except Exception as e:
                        print(f"[Chatwoot -> WhatsApp] Erro ao enviar: {e}")

    except Exception as e:
        print(f"[Webhook Chatwoot] Erro geral: {e}")

    return {"status": "ok"}

@router.post("/webhook/evolution")
async def evolution_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.json()
    event = payload.get("event")
    event_str = str(event or "").lower().replace("-", "_").replace(".", "_")

    instance = payload.get("instance") or payload.get("instanceName") or "captur"

    if event_str == "messages_upsert" or not event:
        event_data = payload.get("data", {})
        
        messages = []
        if isinstance(event_data, dict):
            if "messages" in event_data and isinstance(event_data["messages"], list):
                messages = event_data["messages"]
            elif "key" in event_data:
                messages = [event_data]
        elif isinstance(event_data, list):
            messages = event_data

        for msg in messages:
            key = msg.get("key", {})
            remote_jid = key.get("remoteJid", "")
            if not remote_jid or "status@broadcast" in remote_jid:
                continue

            is_group = "@g.us" in remote_jid
            from_me = key.get("fromMe", False)
            msg_id_fromme = key.get("id")

            # Para conversas privadas (não-grupo) com mensagem enviada por este numero:
            # pode ser o proprio bot (eco da API) ou a atendente respondendo direto no app do WhatsApp.
            # So no segundo caso marcamos atendimento_humano - senao o bot se calaria sozinho
            # logo apos mandar a primeira pergunta do questionario.
            if not is_group and from_me:
                if instance == "captur" and not EvolutionService.foi_enviado_pelo_bot(msg_id_fromme):
                    telefone_humano = remote_jid.split("@")[0]
                    try:
                        estado_humano = await get_estado(db, telefone_humano)
                        if estado_humano.estado != 'atendimento_humano':
                            await update_estado(db, telefone_humano, "atendimento_humano")
                            print(f"[Handoff via WhatsApp direto] Atendente respondeu {telefone_humano} direto no app - bot silenciado a partir de agora.")
                    except Exception as e:
                        print(f"[Handoff via WhatsApp direto] Erro ao marcar atendimento_humano para {telefone_humano}: {e}")
                continue

            telefone = remote_jid.split("@")[0]

            # 🛡️ 1. Anti-Duplicação rigorosa: se o mesmo ID de mensagem já foi recebido, ignorar!
            msg_id = key.get("id")
            if msg_id:
                import time
                agora = time.time()
                if len(_mensagens_processadas_ids) > 10000:
                    _mensagens_processadas_ids.clear()
                if msg_id in _mensagens_processadas_ids:
                    print(f"[Anti-Dup] Mensagem duplicada {msg_id} ignorada.")
                    continue
                _mensagens_processadas_ids[msg_id] = agora

            # 🛡️ 2. Trava de Sincronização Histórica: Se a mensagem ao vivo tiver mais de 3 minutos, ignorar
            msg_ts = msg.get("messageTimestamp")
            if msg_ts:
                try:
                    import time
                    idade_segundos = time.time() - float(msg_ts)
                    if idade_segundos > 180:
                        print(f"[Anti-Sync] Mensagem antiga de {remote_jid} ignorada (idade: {int(idade_segundos)}s).")
                        continue
                except Exception:
                    pass

            # 🚨 3. SE A MENSAGEM CHEGOU NO CELULAR ANTIGO (Nokia / captur_antigo):
            if instance in ["captur_antigo", "nokia", "captur_nokia"]:
                if not is_group:
                    import time
                    agora = time.time()
                    ultimo_envio = _redirecionamentos_enviados.get(telefone, 0)
                    if (agora - ultimo_envio) > 43200:
                        _redirecionamentos_enviados[telefone] = agora
                        msg_aviso = (
                            "✈️ *Captur Agência de Viagens*\n\n"
                            "Olá! Informamos que este número não realiza mais atendimentos da agência.\n\n"
                            "Para falar com a nossa equipe de especialistas, clique no link abaixo para conversar no nosso *WhatsApp Oficial*:\n\n"
                            "👉 https://wa.me/551535372640\n\n"
                            "_(Telefone fixo e WhatsApp oficial: (15) 3537-2640)_\n\n"
                            "Estamos te aguardando por lá para lhe atender com todo o carinho!"
                        )
                        await EvolutionService.enviar_mensagem(telefone, msg_aviso, instance=instance)
                        print(f"[Redirecionamento] Aviso de novo número enviado para {telefone} via {instance} (SEM QUESTIONÁRIO).")
                continue

            message_content = msg.get("message", {}) or {}

            # 4. Extração de Texto e Mídias
            texto = (
                message_content.get("conversation")
                or message_content.get("extendedTextMessage", {}).get("text")
                or ""
            ).strip()

            doc_info = message_content.get("documentMessage")
            doc_name = ""
            if doc_info:
                doc_cap = doc_info.get("caption", "").strip()
                doc_name = doc_info.get("fileName", "Documento.pdf")
                texto = doc_cap if doc_cap else f"📄 [Documento: {doc_name}]"
                
                # Baixar documento do WhatsApp e salvar em cache para este chat/grupo
                try:
                    async with httpx.AsyncClient(timeout=25.0) as client:
                        evo_b64_url = f"{settings.EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{instance}"
                        b64_resp = await client.post(
                            evo_b64_url,
                            headers={"apikey": settings.EVOLUTION_GLOBAL_API_KEY},
                            json={"message": msg, "convertToMp4": False}
                        )
                        if b64_resp.status_code in (200, 201):
                            b64_str = b64_resp.json().get("base64")
                            if b64_str:
                                import base64, time
                                if "," in b64_str:
                                    b64_str = b64_str.split(",")[1]
                                raw_bytes = base64.b64decode(b64_str)
                                _ultimos_documentos_whatsapp[remote_jid] = {
                                    "bytes": raw_bytes,
                                    "fileName": doc_name,
                                    "timestamp": time.time()
                                }
                                print(f"[WhatsApp Cache] Documento '{doc_name}' ({len(raw_bytes)} bytes) salvo em cache para {remote_jid}")
                except Exception as e:
                    print(f"[WhatsApp Doc Cache Error] {e}")

            if not texto:
                if "imageMessage" in message_content:
                    cap = message_content["imageMessage"].get("caption", "")
                    texto = cap.strip() if cap else "📷 [Imagem]"
                elif "audioMessage" in message_content:
                    texto = "🎵 [Mensagem de Áudio]"
                elif "videoMessage" in message_content:
                    cap = message_content["videoMessage"].get("caption", "")
                    texto = cap.strip() if cap else "🎥 [Vídeo]"
                elif "stickerMessage" in message_content:
                    texto = "🖼️ [Figurinha]"

            # 4.5. IA Financeira pelo WhatsApp normal (Paula/Régis) — texto ou áudio, sem precisar do Chatwoot
            if not is_group and telefone in NUMEROS_FINANCEIRO_IA:
                if from_me:
                    continue  # Ignora mensagens enviadas pelo próprio bot para a equipe

                nome_autorizado = NUMEROS_FINANCEIRO_IA[telefone]
                is_audio_in = "audioMessage" in message_content
                texto_financeiro = texto
                if is_audio_in:
                    try:
                        audio_bytes = await TranscriptionService.obter_audio_evolution(
                            msg_id, instance_name=instance, msg_dict=msg
                        )
                        if audio_bytes:
                            texto_financeiro = await TranscriptionService.transcrever_audio_bytes(
                                audio_bytes, filename=f"{msg_id}.ogg"
                            )
                            print(f"[Financeiro IA WhatsApp] Áudio de {nome_autorizado} transcrito: '{texto_financeiro}'")
                        else:
                            texto_financeiro = ""
                    except Exception as e:
                        print(f"[Financeiro IA WhatsApp] Erro ao transcrever áudio de {nome_autorizado}: {e}")
                        texto_financeiro = ""

                if texto_financeiro:
                    try:
                        resposta = await financial_agent.perguntar(db, texto_financeiro)
                    except Exception as e:
                        resposta = "Tive um erro ao consultar os dados agora, tenta de novo em instantes."
                        print(f"[Financeiro IA WhatsApp] Erro ao processar pergunta de {nome_autorizado}: {e}")

                    audio_enviado = False
                    if is_audio_in or settings.ELEVENLABS_API_KEY:
                        try:
                            b64_audio = await TranscriptionService.gerar_audio_elevenlabs(resposta)
                            if b64_audio:
                                await EvolutionService.enviar_audio(telefone, b64_audio, instance=instance)
                                audio_enviado = True
                                print(f"[Financeiro IA WhatsApp] Resposta em áudio (ElevenLabs) enviada para {nome_autorizado}")
                        except Exception as e:
                            print(f"[Financeiro IA WhatsApp] Erro ao gerar/enviar áudio ElevenLabs: {e}")

                    if not audio_enviado:
                        try:
                            await EvolutionService.enviar_mensagem(telefone, resposta, instance=instance)
                        except Exception as e:
                            print(f"[Financeiro IA WhatsApp] Erro ao responder texto para {nome_autorizado}: {e}")
                else:
                    try:
                        await EvolutionService.enviar_mensagem(
                            telefone,
                            "Não consegui entender o áudio, pode tentar de novo ou mandar por texto?",
                            instance=instance
                        )
                    except Exception as e:
                        print(f"[Financeiro IA WhatsApp] Erro ao responder falha de transcrição pra {nome_autorizado}: {e}")
                continue

            # 4.6. Busca de cliente pela Olívia (equipe geral) pelo WhatsApp normal — texto ou áudio
            if not is_group and telefone in NUMEROS_BUSCA_CLIENTE:
                if from_me:
                    continue

                nome_autorizado = NUMEROS_BUSCA_CLIENTE[telefone]
                is_audio_in = "audioMessage" in message_content
                texto_busca = texto
                if is_audio_in:
                    try:
                        audio_bytes = await TranscriptionService.obter_audio_evolution(
                            msg_id, instance_name=instance, msg_dict=msg
                        )
                        if audio_bytes:
                            texto_busca = await TranscriptionService.transcrever_audio_bytes(
                                audio_bytes, filename=f"{msg_id}.ogg"
                            )
                            print(f"[Busca Cliente WhatsApp] Áudio de {nome_autorizado} transcrito: '{texto_busca}'")
                        else:
                            texto_busca = ""
                    except Exception as e:
                        print(f"[Busca Cliente WhatsApp] Erro ao transcrever áudio de {nome_autorizado}: {e}")
                        texto_busca = ""

                if texto_busca:
                    try:
                        resposta = await team_search.perguntar(db, texto_busca)
                    except Exception as e:
                        resposta = "Tive um erro ao buscar agora, tenta de novo em instantes."
                        print(f"[Busca Cliente WhatsApp] Erro ao processar pedido de {nome_autorizado}: {e}")

                    audio_enviado = False
                    if is_audio_in or settings.ELEVENLABS_API_KEY:
                        try:
                            b64_audio = await TranscriptionService.gerar_audio_elevenlabs(resposta)
                            if b64_audio:
                                await EvolutionService.enviar_audio(telefone, b64_audio, instance=instance)
                                audio_enviado = True
                        except Exception as e:
                            print(f"[Busca Cliente WhatsApp] Erro ao gerar/enviar áudio ElevenLabs: {e}")

                    if not audio_enviado:
                        try:
                            await EvolutionService.enviar_mensagem(telefone, resposta, instance=instance)
                        except Exception as e:
                            print(f"[Busca Cliente WhatsApp] Erro ao responder texto para {nome_autorizado}: {e}")
                else:
                    try:
                        await EvolutionService.enviar_mensagem(
                            telefone,
                            "Não consegui entender o áudio, pode tentar de novo ou mandar por texto?",
                            instance=instance
                        )
                    except Exception as e:
                        print(f"[Busca Cliente WhatsApp] Erro ao responder falha de transcrição pra {nome_autorizado}: {e}")
                continue

            # 5. Comando /contrato direto no WhatsApp (funciona em chats individuais e grupos!)
            eh_comando_contrato = "/contrato" in texto.lower() or "#contrato" in texto.lower()
            if eh_comando_contrato:
                token = f"captur-{uuid.uuid4().hex[:8]}"
                pdf_dest = BASE_DIR / "storage" / "contracts" / f"{token}_original.pdf"
                pdf_dest.parent.mkdir(parents=True, exist_ok=True)
                
                downloaded = False
                doc_name_found = ""

                # 1. Verifica se há anexo na mensagem atual
                if doc_info and remote_jid in _ultimos_documentos_whatsapp:
                    cached = _ultimos_documentos_whatsapp[remote_jid]
                    with open(pdf_dest, "wb") as f:
                        f.write(cached["bytes"])
                    doc_name_found = cached.get("fileName", "")
                    downloaded = True
                # 2. Se a atendente enviou o documento pelo clips antes e digitou /contrato agora
                elif remote_jid in _ultimos_documentos_whatsapp:
                    import time
                    cached = _ultimos_documentos_whatsapp[remote_jid]
                    if time.time() - cached.get("timestamp", 0) < 900: # 15 minutos
                        with open(pdf_dest, "wb") as f:
                            f.write(cached["bytes"])
                        doc_name_found = cached.get("fileName", "")
                        downloaded = True
                        print(f"[WhatsApp /contrato] Anexo recente '{doc_name_found}' reutilizado para {remote_jid}!")

                if not downloaded:
                    default_pdf = BASE_DIR / "contrato_espanha_paulo_sergio.pdf"
                    if default_pdf.exists():
                        with open(default_pdf, "rb") as src, open(pdf_dest, "wb") as dst:
                            dst.write(src.read())

                contract_target = remote_jid if is_group else telefone
                valor_contrato, texto_sem_valor = extrair_valor_contrato(texto)
                contract_name = extrair_nome_passageiro(texto_sem_valor, filename=doc_name_found, default_name=f"Cliente {telefone}")

                contract = register_contract(
                    client_name=contract_name,
                    contract_title="Contrato de Prestação de Serviços Turísticos - Captur",
                    pdf_path=str(pdf_dest),
                    client_phone=contract_target,
                    token=token,
                    valor_total=float(valor_contrato) if valor_contrato else None
                )

                if valor_contrato and not is_group:
                    atendente_nome = {
                        "ester": "Ester", "thamires": "Thamires", "flavia": "Flávia", "paula": "Paula"
                    }.get(instance.lower())
                    try:
                        await financeiro.registrar_conta_receber(
                            db, cliente_nome=contract_name, valor_total=valor_contrato,
                            cliente_telefone=contract_target, contrato_token=token,
                            descricao=contract['contract_title'], atendente_nome=atendente_nome
                        )
                        aviso_financeiro = f"\n\n💰 Financeiro: conta a receber de {_fmt_reais(valor_contrato)} já lançada automaticamente."
                    except Exception as e:
                        print(f"[Financeiro] Erro ao criar conta a receber automática (WhatsApp): {e}")
                        aviso_financeiro = ""
                else:
                    aviso_financeiro = ""

                public_base = settings.PUBLIC_BASE_URL.rstrip("/")
                link = f"{public_base}/assinar/{token}"
                msg_resp = (
                    f"✈️ *Captur Agência de Viagens*\n\n"
                    f"Contrato pronto para assinatura digital pelo celular:\n"
                    f"📄 *Contrato:* {contract['contract_title']}\n"
                    f"👤 *Titular:* {contract_name}\n"
                    f"👉 {link}\n\n"
                    f"Ambiente seguro com validade jurídica (Lei nº 14.063/2020)."
                    f"{aviso_financeiro}"
                )
                await EvolutionService.enviar_mensagem(contract_target, msg_resp, instance=instance)
                print(f"[WhatsApp /contrato] Link enviado para {contract_target} via {instance}: {link}")
                continue

            # 👥 6. SINCRONIZAÇÃO DE GRUPOS NO CHATWOOT COM ROTEAMENTO PARA A CAIXA CORRETA
            if is_group:
                group_nome = _grupos_nomes_cache.get(remote_jid)
                if not group_nome:
                    try:
                        async with httpx.AsyncClient(timeout=4.0) as client:
                            evo_url = f"{settings.EVOLUTION_API_URL}/group/findGroupInfos/{instance}?groupJid={remote_jid}"
                            evo_resp = await client.get(evo_url, headers={"apikey": settings.EVOLUTION_GLOBAL_API_KEY})
                            if evo_resp.status_code == 200:
                                subj = evo_resp.json().get("subject")
                                if subj:
                                    group_nome = f"👥 {subj}"
                                    _grupos_nomes_cache[remote_jid] = group_nome
                    except Exception:
                        pass
                if not group_nome:
                    group_nome = f"👥 Grupo WhatsApp {telefone[-4:]}"

                # Direcionar para a caixa e atendente responsável
                group_nome_low = group_nome.lower()
                if "ester" in group_nome_low:
                    inbox_alvo = 2
                    assignee_alvo = 11
                elif "thamires" in group_nome_low:
                    inbox_alvo = 3
                    assignee_alvo = 12
                elif "flavia" in group_nome_low:
                    inbox_alvo = 5
                    assignee_alvo = 10
                elif "paula" in group_nome_low:
                    inbox_alvo = 6
                    assignee_alvo = 2
                else:
                    inbox_alvo = 1
                    assignee_alvo = None

                # Remetente dentro do grupo
                sender_jid = key.get("participant") or msg.get("participant") or ""
                sender_phone = sender_jid.split("@")[0] if sender_jid else ""
                sender_push = msg.get("pushName")

                if from_me:
                    sender_display = "Agência (WhatsApp Fixo 2640)"
                    cw_msg_type = "outgoing"
                else:
                    cw_msg_type = "incoming"
                    if sender_push and not sender_push.isdigit() and len(sender_push) > 1:
                        sender_display = sender_push
                    elif sender_phone:
                        sender_display = f"+{sender_phone}"
                    else:
                        sender_display = "Participante"

                msg_formatada = f"*{sender_display}*: {texto}" if texto else f"*{sender_display}*: [Mensagem de Mídia]"

                try:
                    contato_id = await ChatwootService.obter_ou_criar_contato_grupo(remote_jid, group_nome)
                    if contato_id:
                        conv_id = await ChatwootService.obter_ou_criar_conversa(contato_id, inbox_id=inbox_alvo, assignee_id=assignee_alvo)
                        if conv_id:
                            await ChatwootService.adicionar_mensagem(conv_id, msg_formatada, message_type=cw_msg_type, source_id=msg_id)
                            print(f"[Grupo WhatsApp -> Chatwoot] Mensagem de '{sender_display}' no grupo '{group_nome}' sincronizada na Caixa {inbox_alvo} (Conv: {conv_id})")
                except Exception as e:
                    print(f"[Erro Grupo Chatwoot] {e}")

                # IMPORTANTE: Grupos NUNCA disparam questionário ou IA comercial de triagem!
                continue

            # 🛡️ 7. Filtro de Privacidade Estrita para celulares das atendentes:
            if not eh_conversa_comercial(telefone, texto, instance):
                print(f"[Privacidade] Conversa pessoal/não-comercial de {telefone} ignorada na instância {instance}.")
                continue

            # 8. Sincronizar conversa individual no Chatwoot na caixa da atendente correspondente
            push_name = msg.get("pushName") or f"Cliente {telefone}"
            
            estado_obj = await get_estado(db, telefone)
            agente_id_atribuido = getattr(estado_obj, "agente_id", None) if estado_obj else None
            
            inbox_alvo = {
                11: 2, # Ester
                12: 3, # Thamires
                10: 5, # Flávia
                2: 6   # Paula
            }.get(agente_id_atribuido)

            if not inbox_alvo:
                inbox_alvo = {
                    "captur": 1,
                    "ester": 2,
                    "thamires": 3,
                    "flavia": 5,
                    "paula": 6
                }.get(instance.lower(), 1)

            try:
                contato_id = await ChatwootService.obter_ou_criar_contato(telefone, nome=push_name)
                if contato_id:
                    conv_id = await ChatwootService.obter_ou_criar_conversa(contato_id, inbox_id=inbox_alvo, assignee_id=agente_id_atribuido)
                    if conv_id and texto:
                        await ChatwootService.adicionar_mensagem(conv_id, texto, message_type="incoming", source_id=msg_id)
                        print(f"[Chatwoot Sync] Mensagem de {telefone} sincronizada na Caixa {inbox_alvo} (Instância: {instance})")
                        if inbox_alvo in (settings.CHATWOOT_INBOX_ESTER, settings.CHATWOOT_INBOX_THAMIRES):
                            import asyncio as _asyncio
                            _asyncio.create_task(sales_copilot.sugerir_resposta_chatwoot(conv_id, texto))
            except Exception as e:
                print(f"[Chatwoot Sync Error] {e}")

            # 9. Nas instâncias pessoais das atendentes, o atendimento é 100% humano direto. Não roda questionário!
            if instance.lower() in ("paula", "flavia", "ester", "thamires"):
                continue

            # 10. Processar fluxo do robô (apenas na Central Matriz e chats que NÃO estejam em atendimento humano nem sejam da equipe)
            if texto:
                if telefone in NUMEROS_FINANCEIRO_IA or telefone in ("5519990154309", "5515997495563", "5515997607519"):
                    print(f"[WhatsApp Matriz] Mensagem de membro da equipe/desenvolvedor ({telefone}). Robô comercial silenciado.")
                    continue
                if estado_obj and estado_obj.estado in ('atendimento_humano', 'aguardando_orcamento_vendedora'):
                    print(f"[WhatsApp Matriz] Cliente {telefone} já está em atendimento humano com atendente. Robô silenciado.")
                    continue
                if estado_obj and estado_obj.estado == 'venda_passagem':
                    await flight_agent.processar_mensagem_passagem(db, telefone, texto)
                    continue
                if estado_obj and estado_obj.estado == 'ia_fechamento':
                    conv_id_fechamento = getattr(estado_obj, 'chatwoot_conversation_id', None)
                    if conv_id_fechamento:
                        await closer_agent.processar_mensagem_fechamento(db, telefone, texto, conv_id_fechamento)
                    continue
                print(f"\n[WhatsApp Matriz] Mensagem de {telefone}: '{texto}'")
                await processar_mensagem(db, telefone, texto)

    return {"status": "ok"}


@router.post("/webhook/pagamento-passagem")
async def webhook_pagamento_passagem(request: Request, x_webhook_token: str = Header("")):
    """Confirmação de pagamento de boleto vinda da API do banco, pra venda
    self-service de passagem aérea (src/services/flight_agent.py). Formato do
    payload ainda precisa ser adaptado ao banco real escolhido por Régis."""
    if not settings.WEBHOOK_TOKEN_PASSAGEM or not hmac.compare_digest(x_webhook_token, settings.WEBHOOK_TOKEN_PASSAGEM):
        raise HTTPException(status_code=401)
    payload = await request.json()
    cobranca_id = payload["id"]
    valor_pago_centavos = int(round(float(payload["valor_pago"]) * 100))
    telefone = flight_agent.telefone_da_cobranca(cobranca_id)
    if not telefone:
        raise HTTPException(status_code=404)
    await flight_agent.processar_pagamento_confirmado(telefone, cobranca_id, valor_pago_centavos)
    return {"status": "ok"}
