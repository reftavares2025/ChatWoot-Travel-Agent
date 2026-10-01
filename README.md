# Projeto Captur Itapetininga - WhatsApp & Chatwoot

Infraestrutura de atendimento via WhatsApp, integrado com Evolution API e Chatwoot.

## Pré-requisitos
- Docker e Docker Compose instalados.
- Python 3.11+ para rodar o bot (FastAPI).

## Passo a Passo

### 1. Subindo a Infraestrutura (Docker)
Crie um arquivo `.env.chatwoot` na pasta `docker` se necessário (conforme docs do Chatwoot) ou use o env padrão.
Na raiz do projeto, rode:
```bash
cd docker
docker-compose up -d
```
Isso vai iniciar:
- **PostgreSQL 15** na porta 5432
- **Redis 7** na porta 6379
- **Evolution API** na porta 8080
- **Chatwoot** na porta 3000

### 2. Lendo o QR Code (Evolution API)
1. Acesse seu Postman ou crie uma requisição HTTP.
2. Crie uma instância na Evolution API (POST `http://localhost:8080/instance/create`) enviando um nome para a instância (ex: `captur`).
3. Pegue o base64 gerado (ou via endpoint de conexão) e leia com o WhatsApp do aparelho da Captur.

### 3. Configurando o Chatwoot e Atendentes
1. Acesse o painel web: `http://localhost:3000`.
2. Crie uma conta de Administrador.
3. Cadastre as **Caixas de Entrada (Inboxes)** (neste caso, a API enviará os contatos para cá).
4. Em **Agentes**, cadastre as 12 vendedoras com seus e-mails e senhas para que acessem o painel via navegador.
5. Anote o `ACCOUNT_ID`, e o `API_ACCESS_TOKEN` gerado, colocando-os no seu arquivo `.env` do projeto Python.

### 4. Rodando o Bot FastAPI
Copie o `.env.example` para `.env` e preencha as credenciais do banco e APIs:
```bash
cp .env.example .env
```
Instale as dependências:
```bash
pip install -r requirements.txt
```
Inicie a aplicação:
```bash
python -m src.main
```
A API rodará na porta `8000`. Configure a URL do webhook na Evolution API apontando para `http://<SEU_IP>:8000/api/webhook/evolution`.
