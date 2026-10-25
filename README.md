# Monitor OLX

Bot que monitora buscas na OLX, avalia cada anúncio por regras (sem LLM, sem
navegador) e avisa no Telegram quando aparece uma oportunidade.

- **Raspagem:** HTTP com TLS de Chrome (`curl_cffi`) lendo o payload RSC do
  Next.js da OLX (~3x menor que o HTML; o HTML é a reserva). Cada busca roda
  sem parar: a cada intervalo lê só as novidades (por data) e 1x/dia relê tudo
  (é assim que detecta anúncio expirado). ~0,1 s de CPU e ~60 MB de RAM por ciclo.
- **Avaliação** (`avaliador.py`): identidade do produto (modelo, sufixo Ti/Super,
  PC montado, acessório, lista de modelos, troca), regras do usuário
  ("sem X", "só com X", "prefiro X"), problemas por gravidade (leve / moderado /
  grave), golpe, preço vs. o teto do usuário ou a mediana de anúncios comparáveis,
  e um classificador que aprende com os 👍/👎 dos alertas.
- **Alertas:** 🟢 COMPRAR (produto certo, sem problema, preço bom), 🟡 VALE
  NEGOCIAR (o preço compensa uma ressalva, ou o vendedor aceita proposta um pouco
  acima do teto) e 💸 queda de preço / entrou no teto. Só depois de ler a
  descrição, e no máximo 5 por busca a cada rodada (o resto fica no `/top`).
- **Qualquer categoria e estado:** a busca da OLX alcança tudo (eletrônicos,
  carros, motos, imóveis, barcos...). Termo amplo mistura categorias ("civic" traz
  carros e peças): o resumo do 1º scan lista as categorias e o `/categorias`
  filtra. Veículos comparam preço com o mesmo ano; imóveis separam venda e aluguel.

## Instalar

Requisitos: Python 3.10+ (ou Docker). Telegram é opcional, mas é por onde chegam os alertas.

**Linux / macOS**
```bash
./setup.sh             # venv, dependências, .env (senha do painel gerada) e testes
nano .env              # TELEGRAM_TOKEN do @BotFather
./run.sh               # painel em http://localhost:8080 (usuário/senha no .env)
./setup.sh --servico   # opcional, Linux: serviço systemd (bot_web@$USER) + rotação de log
```

**Docker (qualquer sistema, inclusive Windows)**
```bash
cp .env.example .env   # defina BOT_PASS e TELEGRAM_TOKEN
docker compose up -d   # painel em http://localhost:8080; banco no volume "dados"
```

**Windows sem Docker**
```bat
py -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env
REM edite o .env: BOT_PASS (obrigatória) e TELEGRAM_TOKEN
venv\Scripts\python main.py
```

**Telegram:** crie o bot com o @BotFather, ponha o token em `TELEGRAM_TOKEN` e
inicie o programa. Sem `TELEGRAM_CHAT_ID`, mande qualquer mensagem ao bot: ele
responde com o seu chat id. Coloque-o no `.env` e reinicie.

## Usar

Telegram: `/add rtx 5060 max=1.900`, `/add rtx 3090 com defeito`,
`/addcarro linea ano=2011-2016 estado=go`, `/buscas`, `/top`, `/categorias <id>`,
`/scan <id>`, `/pausar <id>`, `/del <id> sim`, `/help`.
O painel web faz o mesmo e mostra o detalhe de cada avaliação.

## Segurança

O painel usa HTTP Basic **sem HTTPS**: ok em casa/VPN. Para expor na internet,
coloque um proxy reverso com HTTPS na frente (ou use `HOST=127.0.0.1` no `.env`
para aceitar só conexões da própria máquina). Segredos ficam só no `.env`
(ignorado pelo git).

## Testes

```bash
./venv/bin/python test_offline.py      # 68 testes de regressão (não usam rede nem o banco real)
./venv/bin/python test_live_olx.py     # canário contra a OLX real (layout, RSC, ordem)
```
