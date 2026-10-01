import httpx
from typing import Optional, Dict, Any, List
from config.settings import settings

class ChatwootService:
    @staticmethod
    def _headers():
        return {
            "api_access_token": settings.CHATWOOT_API_ACCESS_TOKEN,
            "Content-Type": "application/json",
            "X-Forwarded-Proto": "https"
        }

    @staticmethod
    async def obter_ou_criar_contato(telefone: str, nome: str = None, email: str = None, identifier: str = None) -> Optional[int]:
        """Localiza ou cria um contato pelo número de telefone ou identificador e retorna o ID do contato."""
        numero_limpo = telefone.replace("+", "").strip()
        nome_contato = nome or f"Cliente {numero_limpo}"
        headers = ChatwootService._headers()

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                # 1. Tentar buscar contato existente por telefone ou identificador
                search_term = identifier or numero_limpo
                search_url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/contacts/search"
                resp = await client.get(search_url, params={"q": search_term}, headers=headers)
                if resp.status_code == 200:
                    payload = resp.json().get("payload", [])
                    if payload:
                        return payload[0]["id"]

                # 2. Criar contato se não encontrado
                create_url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/contacts"
                contact_data = {
                    "inbox_id": settings.CHATWOOT_INBOX_ID,
                    "name": nome_contato,
                }
                
                # Validação de telefone vs identificador (para grupos ou IDs longos)
                is_group = "@g.us" in telefone or len(numero_limpo) > 15 or "-" in numero_limpo or bool(identifier)
                if is_group:
                    contact_data["identifier"] = identifier or telefone
                else:
                    contact_data["phone_number"] = f"+{numero_limpo}"

                if email:
                    contact_data["email"] = email
                    
                create_resp = await client.post(create_url, json=contact_data, headers=headers)
                if create_resp.status_code in (200, 201):
                    return create_resp.json().get("payload", {}).get("contact", {}).get("id")
                else:
                    print(f"Aviso ao criar contato no Chatwoot: {create_resp.text}")
            except Exception as e:
                print(f"Erro ao buscar/criar contato no Chatwoot: {e}")
        return None

    @staticmethod
    async def obter_ou_criar_contato_grupo(group_jid: str, group_name: str) -> Optional[int]:
        """Localiza ou cria um contato específico para Grupos de WhatsApp."""
        return await ChatwootService.obter_ou_criar_contato(
            telefone=group_jid,
            nome=group_name,
            identifier=group_jid
        )

    @staticmethod
    async def transferir_para_agente(telefone: str, agente_id: int, mensagem: str):
        """Cria/encontra contato, abre a conversa no Chatwoot e atribui ao agente."""
        contato_id = await ChatwootService.obter_ou_criar_contato(telefone)
        if not contato_id:
            print(f"Não foi possível obter contato no Chatwoot para {telefone}")
            return None

        headers = ChatwootService._headers()
        conv_url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {
            "source_id": str(contato_id),
            "inbox_id": settings.CHATWOOT_INBOX_ID,
            "contact_id": contato_id,
            "assignee_id": agente_id,
            "status": "open",
            "message": {
                "content": f"[Triagem Captur]: Cliente transferido. Opção escolhida: {mensagem}"
            }
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(conv_url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    conv_data = resp.json()
                    print(f"Conversa criada com sucesso no Chatwoot (ID: {conv_data.get('id')}) para o agente {agente_id}")
                    return conv_data
                else:
                    print(f"Erro ao criar conversa no Chatwoot: {resp.text}")
            except Exception as e:
                print(f"Erro na requisição para criar conversa no Chatwoot: {e}")
        return None

    @staticmethod
    async def obter_ou_criar_conversa(contato_id: int, inbox_id: int = None, assignee_id: int = None) -> Optional[int]:
        """Busca conversa aberta do contato no Chatwoot estritamente na inbox especificada ou cria uma nova se não existir."""
        inbox = inbox_id or settings.CHATWOOT_INBOX_ID
        headers = ChatwootService._headers()
        base_url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                # 1. Buscar conversas do contato estritamente nesta inbox
                resp = await client.get(f"{base_url}/contacts/{contato_id}/conversations", headers=headers)
                if resp.status_code == 200:
                    convs = resp.json().get("payload", [])
                    for c in convs:
                        if c.get("inbox_id") == inbox and c.get("status") in ("open", "pending"):
                            return c.get("id")

                # 2. Criar nova conversa se nenhuma encontrada para esta inbox
                create_payload = {
                    "source_id": str(contato_id),
                    "inbox_id": inbox,
                    "contact_id": contato_id,
                    "status": "open"
                }
                if assignee_id:
                    create_payload["assignee_id"] = assignee_id

                c_resp = await client.post(f"{base_url}/conversations", json=create_payload, headers=headers)
                if c_resp.status_code in (200, 201):
                    return c_resp.json().get("id")
            except Exception as e:
                print(f"[Chatwoot] Erro ao obter/criar conversa: {e}")
        return None

    @staticmethod
    async def adicionar_mensagem(conversation_id: int, content: str, message_type: str = "incoming", source_id: str = None):
        """Adiciona mensagem em uma conversa no Chatwoot com proteção contra duplicação via source_id."""
        headers = ChatwootService._headers()
        url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
        payload = {
            "content": content,
            "message_type": message_type,
            "private": False
        }
        if source_id:
            payload["source_id"] = str(source_id)

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                return resp.json() if resp.status_code in (200, 201) else None
            except Exception as e:
                print(f"[Chatwoot] Erro ao adicionar mensagem {message_type}: {e}")
                return None

    @staticmethod
    async def enviar_mensagem(conversation_id: int, content: str, is_private: bool = False):
        """Envia mensagem dentro de uma conversa do Chatwoot."""
        headers = ChatwootService._headers()
        url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
        payload = {
            "content": content,
            "message_type": "outgoing",
            "private": is_private
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                return resp.json() if resp.status_code in (200, 201) else None
            except Exception as e:
                print(f"Erro ao enviar mensagem no Chatwoot: {e}")
                return None

    @staticmethod
    async def obter_mensagens_conversa(conversation_id: int) -> List[Dict[str, Any]]:
        """Busca mensagens e anexos recentes de uma conversa no Chatwoot."""
        headers = ChatwootService._headers()
        url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.json().get("payload", [])
            except Exception as e:
                print(f"Erro ao obter mensagens no Chatwoot: {e}")
        return []

    @staticmethod
    async def resolver_conversa(conversation_id: int) -> bool:
        """Marca a conversa como resolvida no Chatwoot. Se o cliente responder depois, o
        Chatwoot reabre a mesma conversa sozinho — não perde histórico, não duplica."""
        headers = ChatwootService._headers()
        url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/toggle_status"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(url, json={"status": "resolved"}, headers=headers)
                return resp.status_code in (200, 201)
            except Exception as e:
                print(f"Erro ao resolver conversa {conversation_id} no Chatwoot: {e}")
                return False

    @staticmethod
    async def criar_conversa_com_briefing(
        telefone: str,
        agente_id: int,
        briefing_texto: str,
        nome: str = None,
        email: str = None,
        custom_attributes: dict = None,
        inbox_id: int = None
    ) -> Optional[int]:
        target_inbox = inbox_id or settings.CHATWOOT_INBOX_ID
        contato_id = await ChatwootService.obter_ou_criar_contato(telefone, nome=nome, email=email)
        if not contato_id:
            return None

        headers = ChatwootService._headers()
        conv_url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations"
        payload = {
            "source_id": str(contato_id),
            "inbox_id": target_inbox,
            "contact_id": contato_id,
            "assignee_id": agente_id,
            "status": "open",
            "message": {
                "content": briefing_texto
            }
        }
        if custom_attributes:
            payload["custom_attributes"] = custom_attributes

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(conv_url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    return resp.json().get("id")
            except Exception as e:
                print(f"Erro ao criar conversa com briefing no Chatwoot: {e}")
        return None
