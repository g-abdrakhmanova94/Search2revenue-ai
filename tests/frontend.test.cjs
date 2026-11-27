const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(join(__dirname, "..", "web", "app.js"), "utf8");

function node(tag) {
  return {
    tag, textContent: "", children: [], hidden: true, value: "", listeners: {}, attributes: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(event, handler) { this.listeners[event] = handler; },
    setAttribute(key, value) { this.attributes[key] = value; },
    focus() { this.focused = true; },
  };
}

function frontend(fetch = async () => { throw new Error("Unexpected fetch"); }) {
  const nodes = new Map();
  const example = node("button");
  example.dataset = { query: "Пробный запрос" };
  const timers = new Map();
  let timerId = 0;
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, node(id));
      return nodes.get(id);
    },
    createElement: node,
    querySelectorAll: selector => selector === "[data-query]" ? [example] : [],
  };
  const context = vm.createContext({
    document, URL, fetch, AbortController, process: {env: process.env},
    window: {location: {origin: "http://localhost"}},
    setTimeout(callback, delay) {
      timers.set(++timerId, {callback, delay});
      return timerId;
    },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(script + "\nglobalThis.ui = { element, link, product, api, runSearch, renderAnalytics, restoreRoute };", context);
  return { ui: context.ui, get: id => document.getElementById(id), example, timers, location: context.window.location };
}

test("untrusted product text is rendered as text", () => {
  const { ui } = frontend();
  const label = "<strong>product</strong>";
  const heading = ui.element("h3", label, "product");
  assert.equal(heading.tag, "h3");
  assert.equal(heading.textContent, label);
  assert.equal(Object.hasOwn(heading, "innerHTML"), false);
  assert.equal(heading.className, "product");
});

test("external links accept HTTPS and enforce new-tab isolation", () => {
  const { ui } = frontend();
  const valid = ui.link("Vendor", "https://store.softline.ru/test/", "source");
  assert.equal(valid.tag, "a");
  assert.equal(valid.href, "https://store.softline.ru/test/");
  assert.equal(valid.target, "_blank");
  assert.equal(valid.rel, "noopener noreferrer");
  for (const url of ["http://store.softline.ru", "about:blank", "https://name@store.softline.ru", "https://:password@store.softline.ru", "invalid"]) {
    const invalid = ui.link("Vendor", url, "source");
    assert.equal(invalid.tag, "span");
    assert.equal(invalid.href, undefined);
    assert.equal(invalid.textContent, "Vendor");
  }
});

test("product separates confirmed and unverified terms and labels unavailable price", () => {
  const { ui } = frontend();
  const item = {
    category: "Software", code: "ABC", name: "Product", description: "Detail",
    confirmed: ["200 users"], unknown: ["delivery"], price: null, available: false,
    reason: "Why", source: "https://cryptopro.ru/test/", url: "https://store.softline.ru/test/",
  };
  const card = ui.product(item);
  assert.equal(card.tag, "article");
  assert.equal(card.className, "card");
  assert.deepEqual(card.children[0].children.map(item => item.textContent), ["Software", "ABC"]);
  assert.equal(card.children[1].textContent, "Product");
  assert.equal(card.children[2].textContent, "Detail");
  assert.equal(card.children[3].textContent, "200 users");
  assert.equal(card.children[4].textContent, "Требует проверки: delivery");
  assert.equal(card.children[6].href, item.source);
  assert.equal(card.children[7].children[0].textContent, "Цена по запросу");
  assert.equal(card.children[8].textContent, "Нет в наличии по данным каталога");
  const minimal = ui.product({
    ...item, description: "", confirmed: [], unknown: [], reason: "", price: 25, currency: "RUR", available: true,
  });
  assert.equal(minimal.children.length, 3);
  assert.match(minimal.children[2].children[0].textContent, /25.*₽/);
});

test("clarification and rejected requests keep the search form usable", async () => {
  let seen;
  const {ui, get} = frontend(async url => {
    seen = url;
    return {ok: true, json: async () => ({
      items: [], corrections: {astraa: "astra"}, status: "needs_clarification",
      clarification: "Уточните количество", recommendations: [],
    })};
  });
  get("query").value = "  Astra & Linux  ";
  await ui.runSearch();
  assert.equal(seen.pathname, "/api/search");
  assert.equal(seen.searchParams.get("q"), "Astra & Linux");
  assert.equal(get("results").hidden, false);
  assert.equal(get("status").textContent, "Нужно уточнение");
  assert.equal(get("notice").children[0].textContent, "astraa → astra");
  assert.equal(get("notice").children[1].textContent, "Уточните количество");
  assert.equal(get("products").children.length, 0);
  assert.equal(get("extras").hidden, true);
  assert.equal(get("submit").disabled, false);

  const failed = frontend(async () => ({ok: false, json: async () => ({error: "Сервис недоступен"})}));
  failed.get("query").value = "Поиск";
  await failed.ui.runSearch();
  assert.equal(failed.get("status").textContent, "Сервис недоступен");
  assert.equal(failed.get("submit").disabled, false);
});

test("a related query starts a new search and keeps focus in the search field", async () => {
  const queries = [];
  const {ui, get} = frontend(async url => {
    queries.push(url.searchParams.get("q"));
    return {ok: true, json: async () => ({
      items: [], recommendations: [], status: "needs_clarification",
      related_queries: queries.length === 1 ? ["MaxPatrol EPP продление"] : [],
      related_info: queries.length === 1 ? {
        title: "Интеграции MaxPatrol EPP", detail: "MaxPatrol SIEM и PT Sandbox",
        source: "https://global.ptsecurity.com/en/products/endpoint/",
      } : null,
    })};
  });
  get("query").value = "max patrol";
  await ui.runSearch();
  const related = get("related");
  assert.equal(related.hidden, false);
  assert.equal(related.children[2].tag, "a");
  await related.children[1].children[0].listeners.click();
  assert.deepEqual(queries, ["max patrol", "MaxPatrol EPP продление"]);
  assert.equal(get("query").focused, true);
  assert.equal(related.hidden, true);
});

test("a busy search can be retried and an analytics failure stays in its own panel", async () => {
  let searches = 0;
  const {ui, get} = frontend(async url => {
    if (url.pathname === "/api/analytics") {
      return {ok: false, json: async () => ({error: "Аналитика недоступна"})};
    }
    if (++searches === 1) {
      return {ok: false, status: 503, json: async () => ({error: "Поиск занят"})};
    }
    return {ok: true, json: async () => ({
      items: [{name: "Astra Linux", confirmed: [], unknown: [], available: true}],
      corrections: {}, status: "limited", recommendations: [], elapsed_ms: 10,
    })};
  });
  get("analytics").hidden = false;
  get("query").value = "Astra";
  await ui.runSearch();
  assert.equal(get("status").textContent, "Поиск занят");
  assert.equal(get("submit").disabled, false);
  await ui.runSearch();
  assert.equal(get("status").textContent, "Найден 1 товар");
  assert.equal(get("products").children.length, 1);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(get("stats").textContent, "Аналитика недоступна");
});

test("analytics reports failed and clarification counts independently", async () => {
  const { ui, get } = frontend(async url => {
    assert.equal(url.pathname, "/api/analytics");
    return {ok: true, json: async () => ({
      events: 17, four_results: 12, statuses: {no_results: 1, needs_clarification: 2},
      with_recommendations: 3, mean_ms: 42,
    })};
  });
  await ui.renderAnalytics();
  assert.deepEqual(get("stats").children.map(item => item.textContent), [
    "Запросов: 17", "С 4 семействами: 12", "Без результатов: 1", "Требуют уточнения: 2",
    "С рекомендациями: 3", "Обработка на сервере: 42 мс в среднем",
  ]);
});

test("an HTML gateway failure preserves the query and offers a retry", async () => {
  const {ui, get} = frontend(async () => ({
    ok: false,
    status: 502,
    json: async () => { throw new SyntaxError("Unexpected token <"); },
  }));
  get("query").value = "Лицензия на 100 мест";
  await ui.runSearch();
  assert.equal(get("status").textContent, "Сервис временно недоступен. Повторите запрос.");
  assert.equal(get("query").value, "Лицензия на 100 мест");
  assert.equal(get("submit").disabled, false);
});

test("a stalled foreground request times out and clears its loading state", async () => {
  const {ui, get, timers} = frontend(async (_url, {signal}) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new Error("Aborted")), {once: true});
  }));
  get("query").value = "Сервер";
  const running = ui.runSearch();
  assert.equal(get("submit").disabled, true);
  const [{callback, delay}] = timers.values();
  assert.equal(delay, 15000);
  callback();
  await running;
  assert.equal(get("status").textContent, "Время ожидания истекло. Повторите запрос.");
  assert.equal(get("submit").disabled, false);
  assert.equal(get("results").attributes["aria-busy"], "false");
});

test("an incomplete search response leaves the form ready to retry", async () => {
  for (const payload of [{items: []}, {items: [null], recommendations: []}]) {
    const {ui, get} = frontend(async () => ({ok: true, json: async () => payload}));
    get("query").value = "Astra Linux";
    await ui.runSearch();
    assert.equal(get("status").textContent, "Сервис вернул неполный ответ. Повторите запрос.");
    assert.equal(get("submit").disabled, false);
  }
});

test("back restores the search even when the unsent draft already matches the restored URL", async () => {
  const pending = [];
  const {ui, get, location} = frontend(url => new Promise(resolve => pending.push({url, resolve})));
  get("query").value = "Office";
  const old = ui.runSearch({writeRoute: false});
  get("query").value = "Astra";
  location.href = "http://localhost/?q=Astra";
  ui.restoreRoute();
  assert.deepEqual(pending.map(({url}) => url.searchParams.get("q")), ["Office", "Astra"]);
  const reply = name => ({ok: true, json: async () => ({items: [{name}], recommendations: []})});
  pending[1].resolve(reply("Astra"));
  pending[0].resolve(reply("Office"));
  await old;
  assert.equal(get("products").children[0].children[1].textContent, "Astra");
  location.href += "&layout=list";
  ui.restoreRoute();
  assert.equal(pending.length, 2);
});

test("pending analytics does not disable search or obscure the number of add-ons", async () => {
  const card = {name: "Товар", confirmed: [], unknown: []};
  let finishAnalytics;
  const {ui, get} = frontend(async url => {
    if (url.pathname === "/api/analytics") {
      return new Promise(resolve => { finishAnalytics = resolve; });
    }
    return {ok: true, json: async () => ({
      items: [card], recommendations: [card, card, card], corrections: {}, status: "ok",
    })};
  });
  get("query").value = "Сервер";
  get("analytics").hidden = false;
  await ui.runSearch();
  assert.equal(get("submit").disabled, false);
  assert.equal(get("status").textContent, "Найден 1 товар и 3 сопутствующих товара");
  finishAnalytics({ok: false, json: async () => ({error: "Временно недоступно"})});
});


const emptyResult = {items: [], recommendations: [], corrections: {}, clarification: null, status: "no_results", elapsed_ms: 7};

test("a functional alternative is marked separately from exact results", async () => {
  const {ui, get} = frontend(async () => ({ok: true, json: async () => ({
    ...emptyResult, alternatives: [{name: "Microsoft Office 2024", confirmed: [], unknown: []}],
  })}));
  get("query").value = "Microsoft Office 2021";
  await ui.runSearch();
  assert.equal(get("products").children.length, 1);
  assert.equal(get("notice").children[0].textContent, "Точного совпадения нет. Посмотрите аналог.");
  assert.equal(get("status").textContent, "Есть альтернатива");
  assert.equal(get("extras").hidden, true);
});

test("blank search does not send an HTTP request", async () => {
  const {ui, get} = frontend();
  get("query").value = "   ";
  await ui.runSearch();
  assert.equal(get("results").hidden, true);
  assert.equal(get("status").textContent, "");
});

test("form submit and example button trigger search", async () => {
  const seen = [];
  const {get, example} = frontend(async url => {
    seen.push(url.searchParams.get("q"));
    return {ok: true, json: async () => emptyResult};
  });
  let called = false;
  get("query").value = "Строка формы";
  get("search").listeners.submit({preventDefault() { called = true; }});
  example.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(called, true);
  assert.equal(get("query").value, "Пробный запрос");
  assert.deepEqual(seen, ["Строка формы", "Пробный запрос"]);
  assert.equal(get("status").textContent, "Нет результатов");
  assert.equal(get("notice").children.length, 1);
});

test("older search responses cannot override the latest search", async () => {
  const pending = new Map();
  const {ui, get} = frontend(async url => new Promise(resolve => {
    pending.set(url.searchParams.get("q"), payload => resolve({ok: true, json: async () => payload}));
  }));
  get("query").value = "первый";
  const first = ui.runSearch();
  get("query").value = "второй";
  const second = ui.runSearch();
  pending.get("второй")(emptyResult);
  await second;
  pending.get("первый")({...emptyResult, status: "needs_clarification"});
  await first;
  assert.equal(get("status").textContent, "Нет результатов");
});

test("changing the demo profile sends context for the same query", async () => {
  let params;
  const {get} = frontend(async url => {
    params = url.searchParams;
    return {ok: true, json: async () => emptyResult};
  });
  get("query").value = "Сервер";
  const profile = get("buyer-context");
  profile.value = "windows-server";
  await profile.listeners.change();
  assert.equal(params.get("q"), "Сервер");
  assert.equal(params.get("context"), "windows-server");
});

test("suggestions debounce inputs and prefer manufacturer codes", async () => {
  const calls = [];
  const {get, timers} = frontend(async (url, options) => {
    calls.push([url, options]);
    return {ok: true, json: async () => ({items: [{code: "SKU", name: "Name"}, {name: "Без артикула"}]})};
  });
  const field = get("query");
  field.value = "xyz";
  field.listeners.input();
  assert.equal(timers.size, 0);
  field.value = "первый";
  field.listeners.input();
  field.value = "второй";
  field.listeners.input();
  assert.equal(timers.size, 1);
  const [{callback, delay}] = timers.values();
  assert.equal(delay, 260);
  await callback();
  assert.equal(calls[0][0].pathname, "/api/suggestions");
  assert.equal(calls[0][0].searchParams.get("q"), "второй");
  assert.equal(calls[0][1].signal.aborted, false);
  assert.deepEqual(get("suggestions").children.map(x => [x.tag, x.value, x.label]), [
    ["option", "SKU", "Name"], ["option", "Без артикула", "Без артикула"],
  ]);
  field.value = "123";
  field.listeners.input();
  assert.equal(calls[0][1].signal.aborted, true);
  assert.equal(get("suggestions").children.length, 0);
});

test("only the current suggestion request can show a companion", async () => {
  const replies = [];
  const {get, timers} = frontend(async (_url, options) => new Promise(resolve => {
    replies.push({resolve, signal: options.signal});
  }));
  const field = get("query");
  const preview = get("companion-preview");
  field.value = "Astra Linux";
  field.listeners.input();
  const first = [...timers.values()][0].callback();
  field.value = "Office";
  field.listeners.input();
  field.value = "Astra Linux";
  field.listeners.input();
  const latest = [...timers.values()][0].callback();
  const companion = name => ({
    ok: true,
    json: async () => ({
      items: [],
      companion_preview: {
        name,
        url: "https://store.softline.ru/example/",
        reason: "Поддерживает Astra Linux",
      },
    }),
  });
  replies[1].resolve(companion("Новая подсказка"));
  await latest;
  replies[0].resolve(companion("Старая подсказка"));
  await first;
  assert.equal(replies[0].signal.aborted, true);
  assert.equal(preview.hidden, false);
  assert.equal(preview.children[1].textContent, "Новая подсказка");
  assert.equal(preview.children[1].rel, "noopener noreferrer");
  field.value = "";
  field.listeners.input();
  assert.equal(preview.hidden, true);
  assert.equal(preview.children.length, 0);
});

test("a late analytics error cannot replace a newer successful refresh", async () => {
  const requests = [];
  const {get, ui} = frontend(async () => new Promise(resolve => requests.push(resolve)));
  const first = get("analytics-toggle").listeners.click();
  const second = ui.renderAnalytics();
  requests[1]({ok: true, json: async () => ({
    events: 2, four_results: 1, statuses: {}, with_recommendations: 0, mean_ms: 10,
  })});
  await second;
  requests[0]({ok: false, json: async () => ({error: "Старый сбой"})});
  await first;
  assert.equal(get("stats").children[0].textContent, "Запросов: 2");
  assert.equal(get("stats").textContent, "");
});

test("analytics button opens and closes the panel and shows API errors", async () => {
  let calls = 0;
  const {get} = frontend(async () => {
    calls += 1;
    return {ok: false, json: async () => ({error: "Нет данных"})};
  });
  const toggle = get("analytics-toggle");
  await toggle.listeners.click();
  assert.equal(get("analytics").hidden, false);
  assert.equal(toggle.attributes["aria-expanded"], "true");
  assert.equal(get("stats").textContent, "Нет данных");
  await toggle.listeners.click();
  assert.equal(get("analytics").hidden, true);
  assert.equal(toggle.attributes["aria-expanded"], "false");
  assert.equal(calls, 1);
});

test("manager shows diagnostics and sends approval only on click", async () => {
  let approved = false;
  const { ui, get } = frontend(async (url, options) => {
    if (url.pathname === "/api/improvements/approve") {
      assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(options.body), {id: 7});
      approved = true;
      return {ok: true, json: async () => ({approved: true})};
    }
    return {ok: true, json: async () => ({
      manager: true, events: 1, four_results: 0, statuses: {no_results: 1},
      with_recommendations: 0, mean_ms: 12,
      issues: [{query: "asrta", explanation: "Нет совпадений", hits: 1}],
      proposals: [{id: 7, query: "asrta", replacement: "astra", status: approved ? "approved" : "pending",
        report: {before: 0, after: 4, regressions: [{passed: true}], top: []}}],
    })};
  });
  await ui.renderAnalytics();
  assert.equal(get("analytics-manager").hidden, false);
  assert.equal(get("issue-list").children[0].children[0].textContent, "asrta");
  assert.equal(approved, false);
  const card = get("proposal-list").children[0];
  assert.equal(card.children[2].textContent, "На одобрении");
  await card.children[3].listeners.click();
  assert.equal(approved, true);
  assert.equal(get("proposal-list").children[0].children[2].textContent, "Одобрено");
});

test("manager sees confirmed approval when the analytics refresh fails", async () => {
  let approved = false;
  const {ui, get} = frontend(async url => {
    if (url.pathname === "/api/improvements/approve") {
      approved = true;
      return {ok: true, json: async () => ({approved: true})};
    }
    if (approved) return {ok: false, json: async () => ({error: "Статистика недоступна"})};
    return {ok: true, json: async () => ({
      manager: true, events: 1, four_results: 0, statuses: {}, issues: [],
      proposals: [{
        id: 7, query: "asrta", replacement: "astra", status: "pending",
        report: {before: 0, after: 4, regressions: [], top: []},
      }],
    })};
  });
  await ui.renderAnalytics();
  const card = get("proposal-list").children[0];
  const approve = card.children[3];
  await approve.listeners.click();
  assert.equal(card.children[2].textContent, "Одобрено");
  assert.equal(approve.hidden, true);
  assert.equal(get("manager-status").textContent, "Предложение одобрено.");
  assert.equal(get("stats").textContent, "Статистика недоступна");
});
