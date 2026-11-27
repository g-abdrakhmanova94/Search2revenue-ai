const byId = id => document.getElementById(id);
const query = byId("query");
const form = byId("search");
let lastSearch = 0;
let requestedSearch;
let lastAnalytics = 0;
let suggestionTimer;
let suggestionRequest;
const REQUEST_TIMEOUT_MS = 15000;

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function link(value, url, className) {
  const target = URL.parse(url);
  if (target?.protocol !== "https:" || target.username || target.password) {
    return element("span", value, className);
  }
  const node = element("a", value, className);
  node.href = target.href;
  node.target = "_blank";
  node.rel = "noopener noreferrer";
  return node;
}

function product(item) {
  const card = element("article", "", "card");
  const meta = element("p", "", "meta");
  meta.append(...[item.category, item.code].filter(Boolean).map(value => element("span", value)));
  card.append(meta);
  card.append(element("h3", item.name));
  if (item.description) card.append(element("p", item.description, "small"));
  if (Array.isArray(item.confirmed) && item.confirmed.length) card.append(element("p", item.confirmed.join("; "), "facts"));
  if (Array.isArray(item.unknown) && item.unknown.length) card.append(element("p", "Требует проверки: " + item.unknown.join(", "), "warning"));
  if (item.reason) {
    card.append(element("p", item.reason, "facts"));
    card.append(link("Основание рекомендации", item.source, "source"));
  }
  for (const fact of item.evidence || []) {
    const cited = element("p", "", "evidence");
    const origin = fact.kind === "vendor" ? "Документация" : "Каталог Softline";
    cited.append(link(origin + " — " + fact.field, fact.url, "source"), element("span", fact.value));
    card.append(cited);
  }
  const foot = element("div", "", "foot");
  const currency = String(item.currency || "").toUpperCase();
  const amount = item.price > 0 && /^[A-Z]{3}$/.test(currency || "RUB") ?
    new Intl.NumberFormat("ru-RU", {style:"currency", currency:currency === "RUR" ? "RUB" : (currency || "RUB"), maximumFractionDigits:0}).format(item.price) : "Цена по запросу";
  foot.append(element("strong", amount), link(item.quote_only ? "Запросить цену в Softline" : "Открыть в Softline", item.url));
  card.append(foot);
  if (item.quote_only) card.append(element("p", "Редакцию и условия поставки уточняет Softline.", "small"));
  else if (!item.available) card.append(element("p", "Нет в наличии по данным каталога", "small"));
  return card;
}

async function api(path, params={}, signal=undefined, options={}) {
  const url = new URL(path, window.location.origin);
  for (const [key,value] of Object.entries(params)) url.searchParams.set(key,value);
  const controller = signal ? null : new AbortController();
  const timer = controller && setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, {...options, signal: signal || controller.signal});
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(typeof data?.error === "string" && data.error
        ? data.error : "Сервис временно недоступен. Повторите запрос.");
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("Сервис вернул неполный ответ. Повторите запрос.");
    }
    return data;
  } catch (error) {
    if (controller?.signal.aborted) throw new Error("Время ожидания истекло. Повторите запрос.");
    if (error instanceof TypeError) throw new Error("Не удалось связаться с сервисом. Повторите запрос.");
    throw error;
  } finally {
    if (controller) clearTimeout(timer);
  }
}

function clearResults() {
  byId("results").hidden = false;
  byId("extras").hidden = true;
  byId("products").replaceChildren();
  byId("recommendations").replaceChildren();
  byId("notice").replaceChildren();
  byId("related").replaceChildren();
  byId("related").hidden = true;
}

function showLoading() {
  clearResults();
  byId("results").setAttribute("aria-busy", "true");
  byId("status").textContent = "Ищем в каталоге…";
  byId("products").append(element("div", "", "search-placeholder"));
}

function showResults(data) {
  clearResults();
  byId("results").setAttribute("aria-busy", "false");
  const alternatives = data.alternatives || [];
  const count = data.items.length;
  const companions = data.recommendations.length;
  const goods = count === 1 ? "Найден 1 товар" : "Найдено " + count + " товара";
  const extras = companions === 1 ? " и 1 сопутствующий товар" : companions ? " и " + companions + " сопутствующих товара" : "";
  byId("status").textContent = count ? goods + extras : alternatives.length ? "Есть альтернатива" : data.status === "needs_clarification" ? "Нужно уточнение" : "Нет результатов";
  const spelling = Object.entries(data.corrections || {});
  if (spelling.length) byId("notice").append(element("p", spelling.map(([a,b])=>a+" → "+b).join(", "), "notice"));
  if (data.rewritten_to) byId("notice").append(element("p", "Уточнённый поиск · " + data.rewritten_to, "notice"));
  if (data.clarification) byId("notice").append(element("p", data.clarification, "notice"));
  if (!data.clarification && data.top_four_count > 0 && data.top_four_count < 4) {
    byId("notice").append(element("p", "В каталоге найдено меньше четырёх подходящих семейств.", "notice"));
  }
  if (alternatives.length) {
    byId("notice").append(element("p", "Точного совпадения нет. Посмотрите аналог.", "notice"));
  } else if (data.status === "no_results") {
    byId("notice").append(element("p", "Попробуйте уточнить название, артикул или условия покупки.", "notice"));
  }
  byId("products").replaceChildren(...[...data.items, ...alternatives].map(product));
  byId("recommendations").replaceChildren(...data.recommendations.map(product));
  byId("extras").hidden = data.recommendations.length === 0;
  showRelated(Array.isArray(data.related_queries) ? data.related_queries : [], data.related_info);
}

function showRelated(searches, info) {
  if (!searches.length && !info) return;
  const related = byId("related");
  if (searches.length) {
    const buttons = element("div", "", "related-actions");
    for (const value of searches) {
      const button = element("button", value);
      button.type = "button";
      button.addEventListener("click", () => {
        query.value = value;
        query.focus();
        return runSearch();
      });
      buttons.append(button);
    }
    related.append(element("h3", "Связанные запросы"), buttons);
  }
  if (info) related.append(link(info.title, info.source, "source"), element("p", info.detail));
  related.hidden = false;
}

async function runSearch({writeRoute = true} = {}) {
  clearSuggestions();
  const value = query.value.trim();
  if (!value) return;
  const current = ++lastSearch;
  const profile = byId("buyer-context").value;
  requestedSearch = {value, profile};
  if (writeRoute) setRoute({q: value, context: profile});
  byId("submit").disabled = true;
  showLoading();
  try {
    const data = await api("/api/search", profile ? {q:value, context:profile} : {q:value});
    if (current !== lastSearch) return;
    const groups = [data.items, data.recommendations, data.alternatives ?? []];
    if (!groups.every(group => Array.isArray(group) && group.every(item => typeof item?.name === "string"))) {
      throw new Error("Сервис вернул неполный ответ. Повторите запрос.");
    }
    showResults(data);
    if (!byId("analytics").hidden) void renderAnalytics();
  } catch (error) {
    if (current === lastSearch) {
      clearResults();
      byId("status").textContent = error.message;
    }
  } finally {
    if (current === lastSearch) {
      byId("submit").disabled = false;
      byId("results").setAttribute("aria-busy", "false");
    }
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  runSearch();
});
byId("buyer-context").addEventListener("change", runSearch);
for (const button of document.querySelectorAll("[data-query]")) {
  button.addEventListener("click", () => {
    query.value = button.dataset.query;
    runSearch();
  });
}

function clearSuggestions() {
  clearTimeout(suggestionTimer);
  suggestionRequest?.abort();
  suggestionRequest = null;
  byId("suggestions").replaceChildren();
  const preview = byId("companion-preview");
  preview.replaceChildren();
  preview.hidden = true;
}

function renderCompanionPreview(item) {
  if (!item) return;
  const preview = byId("companion-preview");
  preview.replaceChildren(
    element("strong", "Сопутствующий товар"),
    link(item.name, item.url, "preview-link"),
    element("span", item.reason),
  );
  preview.hidden = false;
}

query.addEventListener("input", () => {
  clearSuggestions();
  const value = query.value.trim();
  if (value.length < 4) return;
  suggestionTimer = setTimeout(async () => {
    const request = new AbortController();
    suggestionRequest = request;
    try {
      const profile = byId("buyer-context").value;
      const params = profile ? {q:value, context:profile} : {q:value};
      const results = await api("/api/suggestions", params, request.signal);
      if (suggestionRequest !== request || query.value.trim() !== value) return;
      byId("suggestions").replaceChildren(...results.items.map(item => {
        const option = element("option");
        option.value = item.code || item.name;
        option.label = item.name;
        return option;
      }));
      renderCompanionPreview(results.companion_preview);
    } catch {}
  }, 260);
});

async function renderAnalytics() {
  const current = ++lastAnalytics;
  let data;
  try {
    data = await api("/api/analytics");
    if (!data.statuses || !Number.isInteger(data.events)) {
      throw new Error("Статистика временно недоступна.");
    }
  } catch (error) {
    if (current === lastAnalytics) byId("stats").textContent = error.message;
    return;
  }
  if (current !== lastAnalytics) return;
  const labels = ["Запросов: "+data.events, "С 4 семействами: "+data.four_results,
    "Без результатов: "+(data.statuses.no_results || 0),
    "Требуют уточнения: "+(data.statuses.needs_clarification || 0),
    "С рекомендациями: "+data.with_recommendations, "Обработка на сервере: "+data.mean_ms+" мс в среднем"];
  byId("stats").replaceChildren(...labels.map(text => element("span", text)));
  byId("analytics-manager").hidden = !data.manager;
  if (data.manager) renderManager(data);
}

function renderManager(data) {
  const issues = data.issues.map(issue => {
    const row = element("li");
    row.append(element("strong", issue.query), element("span", issue.explanation + " · " + issue.hits + " раз"));
    return row;
  });
  byId("issue-list").replaceChildren(...(issues.length ? issues : [element("li", "Пока нет записей")]));
  const proposals = data.proposals.map(proposal => {
    const card = element("article", "", "proposal");
    const passed = proposal.report.regressions.filter(item => item.passed).length;
    const names = {pending: "На одобрении", approved: "Одобрено", failed: "Проверка не пройдена", stale: "Каталог изменился"};
    const status = element("span", names[proposal.status] || proposal.status);
    card.append(
      element("strong", proposal.query + " → " + proposal.replacement),
      element("span", "Выдача " + proposal.report.before + " → " + proposal.report.after
        + " · контрольные тесты " + passed + "/" + proposal.report.regressions.length),
      status,
    );
    if (proposal.report.top.length) {
      const first = proposal.report.top[0];
      card.append(link(first.name, first.url, "source"));
    }
    if (proposal.status === "pending") {
      const approve = element("button", "Одобрить");
      approve.type = "button";
      approve.addEventListener("click", async () => {
        approve.disabled = true;
        try {
          await api("/api/improvements/approve", {}, undefined, {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({id: proposal.id}),
          });
          status.textContent = names.approved;
          approve.hidden = true;
          byId("manager-status").textContent = "Предложение одобрено.";
          await renderAnalytics();
        } catch (error) {
          byId("manager-status").textContent = error.message;
          approve.disabled = false;
        }
      });
      card.append(approve);
    }
    return card;
  });
  byId("proposal-list").replaceChildren(...(proposals.length ? proposals : [element("p", "Пока нет предложений")]));
}

byId("analytics-refresh").addEventListener("click", async () => {
  try {
    byId("manager-status").textContent = "";
    await renderAnalytics();
  } catch (error) {
    byId("manager-status").textContent = error.message;
  }
});

async function showAnalytics(open) {
  byId("analytics").hidden = !open;
  byId("analytics-toggle").setAttribute("aria-expanded", String(open));
  if (open) await renderAnalytics();
}

function setRoute(changes) {
  if (!window.history || !window.location.href) return;
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(changes)) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  if (url.href !== window.location.href) window.history.pushState(null, "", url.href);
}

function setLayout(list) {
  byId("workspace").setAttribute("data-layout", list ? "list" : "columns");
  byId("layout-toggle").setAttribute("aria-pressed", String(list));
  byId("layout-toggle").textContent = list ? "Результаты по колонкам" : "Результаты списком";
}

function restoreRoute() {
  const {searchParams} = new URL(window.location.href);
  const context = searchParams.get("context") || "";
  const profile = ["windows-office", "windows-server"].includes(context) ? context : "";
  const value = (searchParams.get("q") || "").slice(0, 240);
  const sameSearch = value.trim() === requestedSearch?.value && profile === requestedSearch?.profile;
  clearSuggestions();
  byId("buyer-context").value = profile;
  query.value = value;
  setLayout(searchParams.get("layout") === "list");
  showAnalytics(searchParams.get("view") === "analytics");
  if (query.value.trim()) {
    if (!sameSearch) runSearch({writeRoute: false});
  } else {
    ++lastSearch;
    requestedSearch = undefined;
    clearResults();
    byId("results").setAttribute("aria-busy", "false");
    byId("submit").disabled = false;
    byId("status").textContent = "Введите запрос или выберите пример.";
  }
}

byId("analytics-toggle").addEventListener("click", async () => {
  const open = byId("analytics").hidden;
  setRoute({view: open ? "analytics" : ""});
  const loaded = showAnalytics(open);
  if (open) byId("analytics-title").focus?.();
  await loaded;
});

byId("layout-toggle").addEventListener("click", () => {
  const list = byId("workspace").getAttribute("data-layout") !== "list";
  setLayout(list);
  setRoute({layout: list ? "list" : ""});
});

function setTheme(dark) {
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  byId("theme-toggle").setAttribute("aria-pressed", String(dark));
  byId("theme-toggle").textContent = dark ? "Светлая тема" : "Тёмная тема";
  try { window.localStorage.setItem("search-theme", dark ? "dark" : "light"); } catch {}
}

byId("theme-toggle").addEventListener("click", () => {
  setTheme(document.documentElement.getAttribute("data-theme") !== "dark");
});

if (window.location.href) {
  try { setTheme(window.localStorage.getItem("search-theme") === "dark"); } catch {}
  window.addEventListener("popstate", restoreRoute);
  restoreRoute();
}
