/* app.js — Monitor OLX */

let buscaAtiva = null;
let buscas     = [];
let resultados = [];          // última lista carregada (o detalhe sai daqui)
let ws         = null;
let charts     = {};

// ── SEGURANÇA / FORMATO ───────────────────────────────────
// Todo dado vindo do servidor (título, descrição, texto do avaliador) é de
// TERCEIROS: passa por esc() antes de entrar em innerHTML.

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function urlSegura(u) {
  return /^https:\/\//i.test(u || "") ? esc(u) : "#";
}

function brl(v) {
  return v ? "R$ " + Math.round(v).toLocaleString("pt-BR") : "—";
}

// o SQLite grava UTC sem fuso ("2026-10-16 04:17:23"); sem o Z o navegador
// lia como hora local e mostrava 3h errado
function dataLocal(s) {
  return s ? new Date(s.replace(" ", "T") + "Z").toLocaleString("pt-BR") : "—";
}

// ── API ───────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch("/api" + path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = Array.isArray(j.detail) ? j.detail.map(d => d.msg).join("; ") : (j.detail || msg);
    } catch (_) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

// ── WEBSOCKET ─────────────────────────────────────────────

function conectarWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => handleWS(JSON.parse(e.data));
  ws.onclose = () => setTimeout(conectarWS, 3000);
}

function handleWS(msg) {
  switch (msg.tipo) {
    case "fila":
      document.getElementById("fila-count").textContent = msg.tamanho ?? 0;
      break;
    case "busca_status":
    case "busca_criada":
    case "busca_deletada":
    case "busca_concluida":
      // só a lista lateral; a tela da busca aberta só se o evento for DELA
      // (antes: 7 buscas x 2 eventos a cada 30 min redesenhavam a tela e a
      // rolagem voltava ao topo no meio da leitura)
      carregarBuscas(msg.busca_id == buscaAtiva);
      break;
    case "avaliado":
      if (buscaAtiva && msg.busca_id == buscaAtiva) debounceResultados();
      break;
    case "scraping":
      if (buscaAtiva && msg.busca_id == buscaAtiva) piscarFila();
      break;
  }
}

let _debounceTimer = null;
function debounceResultados() {
  clearTimeout(_debounceTimer);
  _debounceTimer = setTimeout(() => carregarResultados(buscaAtiva), 2000);
}

// ── STATUS ────────────────────────────────────────────────

async function carregarStatus() {
  try {
    const s = await api("GET", "/status");
    document.getElementById("fila-count").textContent = s.fila_pendente;
  } catch (_) {}
}

// ── BUSCAS ────────────────────────────────────────────────

async function carregarBuscas(recarregarAberta = true) {
  try {
    buscas = await api("GET", "/buscas");
    renderSidebar();
    if (buscaAtiva && !buscas.some(b => b.id == buscaAtiva)) {   // apagada (ex.: pelo Telegram)
      buscaAtiva = null;
      voltarLista();
      document.getElementById("main-area").innerHTML =
        '<div class="welcome-screen"><div class="welcome-icon">◈</div><h2>Busca removida.</h2></div>';
    } else if (buscaAtiva && recarregarAberta) {
      debounceResultados();
    }
  } catch (e) {
    console.error(e);
  }
}

function rotuloStatus(b) {
  if (b.status === "pausada") return ["badge-pausada", "PAUSADA"];
  if (b.status === "erro")    return ["badge-erro", "ERRO"];
  return b.fase === "scan" ? ["badge-scan", "SCAN"] : ["badge-monitor", "MONITOR"];
}

function renderSidebar() {
  const container = document.getElementById("lista-buscas");
  if (!buscas.length) {
    container.innerHTML = '<div class="empty-state">Nenhuma busca criada.</div>';
    return;
  }
  container.innerHTML = buscas.map(b => {
    const [cls, label] = rotuloStatus(b);
    return `
    <div class="busca-item ${b.id == buscaAtiva ? 'ativo' : ''}" onclick="selecionarBusca(${Number(b.id)})">
      <div class="busca-item-nome">${esc(b.nome)}</div>
      <div class="busca-item-meta">
        <span class="badge ${cls}">${label}</span>
        <span class="badge" style="color:var(--text3);border-color:var(--border2)">${esc(b.tipo)}</span>
      </div>
      <div class="busca-item-contadores" style="margin-top:6px">
        <span class="cnt-comprar">▲${Number(b.total_comprar) || 0}</span>
        <span class="cnt-ver">~${Number(b.total_ver) || 0}</span>
        <span style="color:var(--text3)">${Number(b.total_anuncios) || 0} total</span>
      </div>
    </div>`;
  }).join("");
}

async function selecionarBusca(id) {
  buscaAtiva = id;
  renderSidebar();
  document.getElementById("welcome")?.remove();
  document.body.classList.add("com-busca");
  await carregarResultados(id);
}

// ── RESULTADOS ────────────────────────────────────────────

let filtroAtivo = null;

async function carregarResultados(bid, rec = filtroAtivo) {
  filtroAtivo = rec;
  const busca = buscas.find(b => b.id == bid);
  if (!busca) return;

  const [lista, stats] = await Promise.all([
    api("GET", `/buscas/${bid}/resultados?limite=300${rec ? '&recomendacao=' + rec : ''}`),
    api("GET", `/buscas/${bid}/stats`),
  ]);
  resultados = lista;

  const main = document.getElementById("main-area");
  main.innerHTML = `
    <div class="busca-detalhe">
      <div class="detalhe-header">
        <div>
          <button class="btn-ghost voltar" onclick="voltarLista()">← buscas</button>
          <div class="detalhe-nome">${esc(busca.nome)}</div>
          <div class="detalhe-termo mono">${esc(busca.termo || "(todos)")} · ${esc(busca.estado)}</div>
        </div>
        <div class="detalhe-acoes">
          <button class="btn-icon" title="Scan imediato" onclick="scanAgora(${Number(bid)})">↺</button>
          ${busca.status === 'pausada'
            ? `<button class="btn-ghost" onclick="retomar(${Number(bid)})">▶ Retomar</button>`
            : `<button class="btn-ghost" onclick="pausar(${Number(bid)})">⏸ Pausar</button>`}
          <button class="btn-ghost" onclick="editarBusca(${Number(bid)})">✎ Editar</button>
          <button class="btn-danger" onclick="deletarBusca(${Number(bid)})">✕</button>
        </div>
      </div>

      <div class="stats-row">
        <div class="stat-card"><div class="stat-label">DO PRODUTO</div>
          <div class="stat-value accent">${Number(stats.total) || 0}</div></div>
        <div class="stat-card"><div class="stat-label">COMPRAR</div>
          <div class="stat-value green">${Number(stats.qtd_comprar) || 0}</div></div>
        <div class="stat-card"><div class="stat-label">VER</div>
          <div class="stat-value yellow">${Number(stats.qtd_ver) || 0}</div></div>
        <div class="stat-card"><div class="stat-label">SCORE MÉDIO</div>
          <div class="stat-value">${stats.score_medio ? Number(stats.score_medio).toFixed(1) : '—'}</div></div>
        <div class="stat-card"><div class="stat-label">PREÇO MÉDIO</div>
          <div class="stat-value" style="font-size:18px">${brl(stats.preco_medio)}</div></div>
      </div>

      <div class="charts-row">
        <div class="chart-card">
          <div class="chart-title">HISTÓRICO DE PREÇOS (30 DIAS)</div>
          <canvas id="chart-precos" height="140"></canvas>
        </div>
        <div class="chart-card">
          <div class="chart-title">DISTRIBUIÇÃO DE SCORES</div>
          <canvas id="chart-scores" height="140"></canvas>
        </div>
      </div>

      <div class="filtros-row">
        ${['', 'COMPRAR', 'VER', 'IGNORAR'].map(r => `
          <button class="filtro-btn ${filtroAtivo == (r || null) ? 'ativo' : ''}"
                  onclick="carregarResultados(${Number(bid)}, ${r ? "'" + r + "'" : null})">
            ${r || 'Todos'}
          </button>`).join('')}
        <span style="color:var(--text3);font-size:12px;margin-left:auto">${lista.length} anúncios</span>
      </div>

      <div class="resultados-grid" id="resultados-grid">
        ${lista.length
          ? lista.map(renderCard).join("")
          : '<div class="empty-state" style="grid-column:1/-1">Nenhum resultado encontrado.</div>'}
      </div>
    </div>`;

  renderChartPrecos(stats.historico_precos || []);
  renderChartScores(stats.distribuicao_scores || []);
}

function voltarLista() {
  document.body.classList.remove("com-busca");
}

function renderCard(a) {
  const rec   = (a.recomendacao || "").toUpperCase();
  const score = Number(a.score) || null;
  const pct   = a.preco_vs_mercado_pct;

  const recClass   = { COMPRAR: "comprar", VER: "ver", IGNORAR: "ignorar" }[rec] || "sem-analise";
  const scoreClass = !score ? "na" : score >= 7 ? "alto" : score >= 5 ? "medio" : "baixo";

  let pctHtml = "";
  if (pct != null) {
    const cls = pct <= -20 ? "pct-bom" : pct > 15 ? "pct-caro" : "";
    pctHtml = `<span class="anuncio-preco-pct ${cls}">${pct >= 0 ? '+' : ''}${Number(pct)}%</span>`;
  }
  const thumb = a.imagem
    ? `<img class="anuncio-thumb" src="${urlSegura(a.imagem)}" loading="lazy" alt=""
         referrerpolicy="no-referrer" onerror="this.remove()">`
    : "";
  const vend = a.vendedor ? ` · ${a.vendedor === 'loja' ? '🏬 loja' : '👤 particular'}` : "";
  const expirado = a.ativo === 0;

  return `
  <div class="anuncio-card ${recClass} ${expirado ? 'expirado' : ''}" onclick="verDetalhe(${Number(a.id)})">
    ${expirado ? '<div class="aviso-expirado">⚰️ não visto há 2+ dias — provavelmente expirado</div>' : ''}
    <div style="display:flex;gap:10px;align-items:flex-start">
      ${thumb}
      <div style="flex:1;min-width:0">
        <div class="anuncio-top">
          <div class="anuncio-titulo">${esc(a.titulo || 'Sem título')}</div>
          <div class="score-badge ${scoreClass}">${a.feedback === 1 ? '👍' : a.feedback === 0 ? '👎' : (score ?? '?')}</div>
        </div>
        <div class="anuncio-preco">${brl(a.preco)}${pctHtml}</div>
        <div class="anuncio-meta">${esc(a.local)} · ${esc(a.envio)}${vend}</div>
      </div>
    </div>
    ${a.motivo ? `<div class="anuncio-resumo">${esc(a.motivo)}</div>` : ''}
    ${a.alerta === 'negociar' ? '<div class="anuncio-rec rec-negociar">VALE NEGOCIAR</div>'
      : rec ? `<div class="anuncio-rec rec-${rec.toLowerCase()}">${rec}</div>` : ''}
  </div>`;
}

// ── DETALHE ANÚNCIO ───────────────────────────────────────

function verDetalhe(anuncioId) {
  // sai da lista já carregada (antes rebaixava 500 resultados a cada clique
  // e não achava anúncio fora deles)
  const item = resultados.find(r => r.id == anuncioId);
  if (!item) return;

  document.getElementById("detalhe-titulo").textContent = item.titulo?.slice(0, 60) || "Anúncio";
  const pct = item.preco_vs_mercado_pct;
  const pctHtml = pct != null
    ? `<span class="${pct <= -20 ? 'pct-bom' : pct > 15 ? 'pct-caro' : ''}">${pct >= 0 ? '+' : ''}${Number(pct)}% vs mercado</span>`
    : "";
  const lista = (titulo, cls, itens) => itens?.length ? `
      <div class="detalhe-secao">
        <div class="detalhe-secao-titulo">${titulo}</div>
        <ul class="lista-pros-contras ${cls}">${itens.map(p => `<li>${esc(p)}</li>`).join('')}</ul>
      </div>` : '';
  const pros = item.pros, contras = item.contras, obs = item.observacoes;
  const rec = (item.recomendacao || "").toLowerCase();

  document.getElementById("detalhe-body").innerHTML = `
    <div class="detalhe-anuncio">
      <div class="detalhe-preco-row">
        <div class="detalhe-preco-val">${brl(item.preco)}</div>
        ${pctHtml}
        <span class="badge badge-${esc(rec || 'pendente')}" style="margin-left:auto">${item.alerta === 'negociar' ? 'VALE NEGOCIAR' : esc(item.recomendacao || 'SEM ANÁLISE')}</span>
        ${item.score ? `<span class="mono" style="font-size:20px;font-weight:700">${Number(item.score)}/10</span>` : ''}
      </div>
      <div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:4px">
          ${[item.local, item.vendedor, item.data_postagem && "postado " + item.data_postagem.replace("T", " ").slice(0, 16)]
              .filter(Boolean).map(esc).join(" · ")}
        </div>
        <a href="${urlSegura(item.url)}" target="_blank" rel="noopener noreferrer"
           style="color:var(--accent);font-size:13px;text-decoration:none">Abrir anúncio ↗</a>
      </div>
      ${item.resumo ? `
      <div class="detalhe-secao">
        <div class="detalhe-secao-titulo">AVALIAÇÃO</div>
        <p style="font-size:13px;line-height:1.6">${esc(item.resumo)}</p>
        ${item.motivo ? `<p style="font-size:12px;color:var(--text2);margin-top:8px;font-style:italic">${esc(item.motivo)}</p>` : ''}
      </div>` : ''}
      ${lista("PONTOS POSITIVOS", "pros", pros)}
      ${lista("PONTOS NEGATIVOS", "contras", contras)}
      ${lista("OBSERVAÇÕES", "obs", obs)}
      ${item.descricao && item.descricao !== '(sem descrição)' ? `
      <div class="detalhe-secao">
        <div class="detalhe-secao-titulo">DESCRIÇÃO</div>
        <p style="font-size:12px;line-height:1.6;white-space:pre-wrap;color:var(--text2)">${esc(item.descricao)}</p>
      </div>` : ''}
      <div class="feedback-row" id="feedback-row"></div>
      <div style="font-size:11px;color:var(--text3);font-family:monospace">
        Avaliado em: ${dataLocal(item.gerado_em)}
      </div>
    </div>`;

  renderFeedback(item);
  abrirModal("modal-detalhe");
}

// 👍/👎 ensinam o classificador (o mesmo dos botões no Telegram)
function renderFeedback(item) {
  const fb = item.feedback;
  document.getElementById("feedback-row").innerHTML = `
    <span class="hint">Ensine o bot:</span>
    <button class="btn-ghost ${fb === 1 ? 'marcado' : ''}" onclick="darFeedback(${Number(item.id)}, ${fb === 1 ? 'null' : 'true'})">👍 bom achado</button>
    <button class="btn-ghost ${fb === 0 ? 'marcado' : ''}" onclick="darFeedback(${Number(item.id)}, ${fb === 0 ? 'null' : 'false'})">👎 não quero</button>`;
}

async function darFeedback(aid, gostou) {
  try {
    await api("POST", `/anuncios/${aid}/feedback`, { gostou });
    const item = resultados.find(r => r.id == aid);
    if (item) { item.feedback = gostou === null ? null : (gostou ? 1 : 0); renderFeedback(item); }
    toast(gostou === null ? "Avaliação removida" : gostou ? "👍 Anotado" : "👎 Anotado — não aparece mais como achado", "ok");
  } catch (e) {
    toast("Erro: " + e.message, "err");
  }
}

// ── GRÁFICOS ──────────────────────────────────────────────

function renderChartPrecos(dados) {
  const ctx = document.getElementById("chart-precos")?.getContext("2d");
  if (!ctx || typeof Chart === "undefined") return;
  if (charts.precos) charts.precos.destroy();
  const ordem = [...dados].reverse();
  charts.precos = new Chart(ctx, {
    type: "line",
    data: {
      labels: ordem.map(d => d.dia),
      datasets: [{
        data: ordem.map(d => Math.round(d.preco_medio)),
        borderColor: "#5b8dee", backgroundColor: "rgba(91,141,238,0.1)",
        borderWidth: 2, fill: true, tension: 0.4, pointRadius: 3, pointBackgroundColor: "#5b8dee",
      }]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#555a6a", font: { size: 10 } }, grid: { color: "#252830" } },
        y: { ticks: { color: "#555a6a", font: { size: 10 }, callback: v => brl(v) },
             grid: { color: "#252830" } },
      }
    }
  });
}

function renderChartScores(dados) {
  const ctx = document.getElementById("chart-scores")?.getContext("2d");
  if (!ctx || typeof Chart === "undefined") return;
  if (charts.scores) charts.scores.destroy();
  const mapa = {};
  dados.forEach(d => mapa[d.score] = d.quantidade);
  const labels = Array.from({ length: 10 }, (_, i) => i + 1);
  charts.scores = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: labels.map(s => mapa[s] || 0),
        backgroundColor: labels.map(s => s >= 8 ? "#2dd4a0" : s >= 5 ? "#f5c842" : "#f05454"),
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#555a6a", font: { size: 10 } }, grid: { display: false } },
        y: { ticks: { color: "#555a6a", font: { size: 10 } }, grid: { color: "#252830" } },
      }
    }
  });
}

// ── AÇÕES DE BUSCA ────────────────────────────────────────

async function acao(promessa, ok) {
  try {
    await promessa;
    if (ok) toast(ok, "ok");
    carregarBuscas();
  } catch (e) {
    toast("Erro: " + e.message, "err");
  }
}

const pausar  = bid => acao(api("POST", `/buscas/${bid}/pausar`), "Busca pausada");
const retomar = bid => acao(api("POST", `/buscas/${bid}/retomar`), "Busca retomada");
const scanAgora = bid => acao(api("POST", `/buscas/${bid}/scan-agora`), "Scan agendado (~30 s)");

async function deletarBusca(bid) {
  if (!confirm("Deletar esta busca e todos os resultados?")) return;
  await acao(api("DELETE", `/buscas/${bid}`));
  buscaAtiva = null;
  voltarLista();
  document.getElementById("main-area").innerHTML =
    '<div class="welcome-screen"><div class="welcome-icon">◈</div><h2>Busca removida.</h2></div>';
}

// ── MODAL NOVA / EDITAR BUSCA ─────────────────────────────

const CAMPOS = {   // id do input -> [campo da API, valor padrão]
  "f-nome": ["nome", ""], "f-tipo": ["tipo", "produto"], "f-termo": ["termo", ""],
  "f-estado": ["estado", "brasil"], "f-intervalo": ["intervalo_min", 30],
  "f-preco-min": ["preco_min", 0], "f-preco-max": ["preco_max", 0],
  "f-envio": ["filtro_envio", "qualquer"], "f-vendedor": ["filtro_vendedor", "qualquer"],
  "f-garantia": ["garantia", ""], "f-obs": ["instrucoes", ""],
  "f-condicao": ["condicao", "funcionando"],
};

function preencherForm(b) {
  for (const [id, [campo, padrao]] of Object.entries(CAMPOS)) {
    document.getElementById(id).value = b?.[campo] ?? padrao;
  }
  document.getElementById("f-inclusao").value = (b?.criterios_inclusao || []).join("\n");
  document.getElementById("f-exclusao").value = (b?.criterios_exclusao || []).join("\n");
}

function abrirModalNovaBusca() {
  document.getElementById("modal-busca-titulo").textContent = "Nova Busca";
  document.getElementById("busca-id-edit").value = "";
  preencherForm(null);
  document.getElementById("grupo-categorias").hidden = true;   // só existem após o 1º scan
  abrirModal("modal-busca");
}

async function carregarCategorias(bid) {
  const grupo = document.getElementById("grupo-categorias");
  const cats = await api("GET", `/buscas/${bid}/categorias`).catch(() => []);
  grupo.hidden = !cats.length;
  document.getElementById("f-categorias").innerHTML = cats.map(c => `
    <label class="categoria-opcao">
      <input type="checkbox" value="${esc(c.nome)}" ${c.selecionada ? "checked" : ""}>
      ${esc(c.nome)} <span class="hint">${Number(c.n)}</span>
    </label>`).join("");
}

function editarBusca(bid) {
  const b = buscas.find(x => x.id == bid);
  if (!b) return;
  document.getElementById("modal-busca-titulo").textContent = "Editar Busca";
  document.getElementById("busca-id-edit").value = bid;
  preencherForm(b);        // TODOS os campos: antes salvar apagava instruções/vendedor
  carregarCategorias(bid);
  abrirModal("modal-busca");
}

async function salvarBusca() {
  const bid = document.getElementById("busca-id-edit").value;
  const dados = {};
  for (const [id, [campo]] of Object.entries(CAMPOS)) {
    dados[campo] = document.getElementById(id).value.trim();
  }
  for (const c of ["intervalo_min", "preco_min", "preco_max"]) dados[c] = parseInt(dados[c]) || 0;
  dados.estado = dados.estado || "brasil";
  const linhas = id => document.getElementById(id).value.split("\n").map(s => s.trim()).filter(Boolean);
  dados.criterios_inclusao = linhas("f-inclusao");
  dados.criterios_exclusao = linhas("f-exclusao");
  if (bid && !document.getElementById("grupo-categorias").hidden) {
    dados.categorias = [...document.querySelectorAll("#f-categorias input:checked")].map(i => i.value);
  }

  if (!dados.nome || (!dados.termo && dados.tipo !== "carro")) {
    toast("Preencha nome e termo de busca.", "err");
    return;
  }
  try {
    if (bid) {
      await api("PATCH", `/buscas/${bid}`, dados);
      toast("Busca atualizada!", "ok");
    } else {
      await api("POST", "/buscas", dados);
      toast("Busca criada! Scan em ~30 s.", "ok");
    }
    fecharModal("modal-busca");
    carregarBuscas();
  } catch (e) {
    toast("Erro: " + e.message, "err");
  }
}

document.getElementById("btn-nova-busca").addEventListener("click", abrirModalNovaBusca);

// ── MODAIS / TOAST / PROGRESSO ────────────────────────────

function abrirModal(id)  { document.getElementById(id).classList.remove("hidden"); }
function fecharModal(id) { document.getElementById(id).classList.add("hidden"); }

document.querySelectorAll(".modal-overlay").forEach(el => {
  el.addEventListener("click", e => { if (e.target === el) fecharModal(el.id); });
});

let _toastTimer = null;
function toast(msg, tipo = "ok") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast ${tipo}`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

let _progressoTimer = null;
function piscarFila() {
  const el = document.getElementById("fila-count");
  el.style.color = "var(--yellow)";
  clearTimeout(_progressoTimer);
  _progressoTimer = setTimeout(() => { el.style.color = ""; }, 3000);
}

// ── INIT ──────────────────────────────────────────────────

async function init() {
  await carregarStatus();
  await carregarBuscas();
  conectarWS();
  setInterval(carregarStatus, 30000);
}

init();
