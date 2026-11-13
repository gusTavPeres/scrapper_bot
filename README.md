# Monitor OLX

Bot que monitora buscas na OLX, avalia cada anúncio por regras (sem LLM, sem
navegador) e avisa no Telegram quando aparece uma oportunidade.

- **Raspagem:** HTTP com TLS de Chrome (`curl_cffi`) lendo o payload RSC do
  Next.js da OLX (~3x menor que o HTML; o HTML é a reserva). Cada busca roda
  sem parar: a cada intervalo lê só as novidades (por data) e 1x/dia relê tudo
  (é assim que detecta anúncio expirado). ~0,1 s de CPU e ~75 MB de RAM; se a
  RAM subir 5 MB, o log anota ("RAM subiu") p/ achar o evento que causou; acima
  de 200 MB o processo se reinicia sozinho (systemd/compose sobem de novo).
- **Avaliação** (`avaliador.py`): identidade do produto (modelo, sufixo Ti/Super,
  PC montado, acessório, lista de modelos, troca), regras do usuário
  ("sem X", "só com X", "prefiro X"), problemas por gravidade (leve / moderado /
  grave), golpe, preço vs. o teto do usuário ou a mediana de anúncios comparáveis,
  e um classificador que aprende com os 👍/👎 dos alertas (só passa a opinar com
  12 marcações, 4 de cada lado, e ignora especificação do produto). Busca com teto: 1x/dia
  o bot lê a busca SEM o teto e guarda a mediana do mercado (por memória: 8 GB e
  16 GB são mercados diferentes); dentro do teto só é 🟢 o que está 20%+ abaixo
  dela (pechincha), e metade do mercado é tratado como suspeito.
- **Alertas:** 🟢 COMPRAR (produto certo, sem problema, preço bom), 🟡 VALE
  NEGOCIAR (o preço compensa uma ressalva, ou o vendedor aceita proposta um pouco
  acima do teto), ⚠️ PECHINCHA SUSPEITA (40%+ abaixo do mercado com contato fora
  da OLX, pressa ou descrição curta, ou metade do mercado: padrão comum de golpe)
  e 💸 queda de preço /
  entrou no teto. Só depois de ler a
  descrição, e no máximo 5 por busca a cada rodada (o resto fica no `/top`).
  Anúncio republicado pelo vendedor não repete o alerta (parecido demais para
  ter certeza vem com o aviso); vendedor em série também não (mesma descrição ao
  mesmo preço em qualquer bairro, ou o 3º anúncio no mesmo bairro e preço);
  anúncio só de troca não alerta. Bloqueio da OLX
  só vira aviso depois de 1 h sem conseguir ler; Telegram fora do ar não perde
  alerta (sai quando ele volta).
- **Imóveis para alugar** (`/addaluguel`, ou tipo "Imóvel" no painel): cidade e
  bairro vão na URL da OLX; quartos, vagas, área e tipo (apartamento, casa,
  quarto) filtram pela ficha do anúncio. O teto vale para o **custo total do
  mês** (aluguel + condomínio + IPTU; IPTU anual digitado no campo mensal é
  dividido por 12, "condomínio incluso" é lido da descrição). 1x/dia o bot mede
  o mercado da cidade e dos bairros pedidos e compara cada anúncio com imóveis
  do mesmo bairro, nº de quartos e tamanho parecido. Com teto, todo anúncio novo
  que cabe nos filtros avisa: 🏠 NOVO NO SEU FILTRO, 🟢 ABAIXO DO MERCADO (entre
  os 25% mais baratos dos comparáveis) ou ⚠️ BARATO DEMAIS (menos da metade: o
  golpe clássico de aluguel). Temporada/diária, ponto comercial, vaga em
  república e quem "procura imóvel" ficam de fora; as regras livres valem
  ("só mobiliado", "sem fiador", "prefiro varanda"). `negocio=venda` faz o mesmo
  para imóveis à venda.
- **Mudança de formato da OLX não passa em silêncio:** cada página é conferida
  com o total de anúncios que a própria OLX declara, e 1x/dia uma busca ampla
  confere preço, data, foto, ordem por data e descrição. Se algo quebrar, o
  aviso chega no Telegram.
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
`/addcarro linea ano=2011-2016 estado=sp`,
`/addaluguel estado=sp cidade=campinas bairro=centro,cambui quartos=2 vagas=1 max=3000 obs= aceita pet`,
`/buscas`, `/top`, `/categorias <id>`,
`/scan <id>`, `/pausar <id>`, `/del <id> sim`, `/help`.
O painel web faz o mesmo e mostra o detalhe de cada avaliação.

## Segurança

O painel usa HTTP Basic **sem HTTPS**: ok em casa/VPN. Para expor na internet,
coloque um proxy reverso com HTTPS na frente (ou use `HOST=127.0.0.1` no `.env`
para aceitar só conexões da própria máquina). Segredos ficam só no `.env`
(ignorado pelo git).

## Testes

```bash
./venv/bin/python test_offline.py      # 99 testes de regressão (não usam rede nem o banco real)
./venv/bin/python test_live_olx.py     # canário contra a OLX real (layout, RSC, ordem, aluguel)
```
