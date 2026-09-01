/* Расписание — PWA + Pyodide */
(function () {
  "use strict";

  const APP_VERSION = "1.1.1";

  // ---------- Состояние ----------
  const state = {
    pyodide: null,
    parserReady: false,
    data: { days: {}, groups: [], files: [], updatedAt: null },
    selectedGroup: null, // null = "Все группы" (не выбрана конкретная)
    selectedDate: null,  // ISO-дата
    weekStart: null,     // ISO первого дня показываемой недели
  };

  // ---------- DOM ----------
  const $ = (sel) => document.querySelector(sel);
  const calendarBar = $("#calendarBar");
  const weekLabel = $("#weekLabel");
  const prevWeekBtn = $("#prevWeekBtn");
  const nextWeekBtn = $("#nextWeekBtn");
  const todayBtn = $("#todayBtn");
  const datePickBtn = $("#datePickBtn");
  const dateInput = $("#dateInput");
  const groupBar = $("#groupBar");
  const dayView = $("#dayView");
  const emptyState = $("#emptyState");
  const bootOverlay = $("#bootOverlay");
  const bootText = $("#bootText");
  const fileInput = $("#fileInput");
  const groupList = $("#groupList");
  const drawerBackdrop = $("#menuOverlay");
  const settingsOverlay = $("#settingsOverlay");
  const themeSwitch = $("#themeSwitch");
  const tabTheme = $("#tabTheme");
  const tabColor = $("#tabColor");
  const settingsTheme = $("#settingsTheme");
  const settingsColor = $("#settingsColor");
  const colorGrid = $("#colorGrid");
  const toastEl = $("#toast");

  // ---------- IndexedDB ----------
  const DB_NAME = "schedule-db";
  const DB_VERSION = 1;
  const STORE = "kv";

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function dbGet(key) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(key);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function dbPut(key, value) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async function dbClear() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // ---------- Patterns ----------
  const DAY_NAMES = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
  const DAY_NAMES_FULL = ["Воскресенье", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"];
  const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
                  "июля", "августа", "сентября", "октября", "ноября", "декабря"];

  function isoOf(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }
  function todayISO() { return isoOf(new Date()); }
  function parseISO(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function addDays(base, n) {
    const d = new Date(base); d.setDate(d.getDate() + n); return d;
  }

  // ---------- Pyodide boot ----------
  function withTimeout(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise((_, rej) =>
        setTimeout(() => rej(new Error("Зависло на этапе: " + label + " (> " + ms + " мс)")), ms)
      ),
    ]);
  }

  async function boot() {
    try {
      if (!window.loadPyodide) throw new Error("loadPyodide недоступен (нет сети?)");

      showBoot("Загрузка Python (несколько МБ, один раз)…");
      let pyodide = await withTimeout(
        loadPyodide({ indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.6/full/" }),
        120000,
        "загрузка Python-движка"
      ).catch((e) => {
        throw new Error("Ошибка загрузки Python: " + (e && e.message));
      });

      showBoot("Загрузка модуля парсинга…");
      const resp = await fetch("parser.py?v=" + APP_VERSION, { cache: "no-store" });
      if (!resp.ok) throw new Error("Не удалось скачать parser.py (" + resp.status + ")");
      const src = await resp.text();

      showBoot("Компиляция парсера…");
      try {
        pyodide.runPython(src);
      } catch (e) {
        throw new Error("Ошибка компиляции парсера:\n" + (e && e.message ? e.message : String(e)));
      }

      const check = pyodide.runPython('"parse_to_json" in globals()');
      if (!check) throw new Error("Парсер не скомпилировался");

      state.pyodide = pyodide;
      state.parserReady = true;
      hideBoot();
      console.log("Pyodide готов");
    } catch (e) {
      console.error(e);
      renderBootError(e && e.message ? e.message : String(e));
    }
  }

  function renderBootError(msg) {
    bootOverlay.innerHTML =
      '<div class="boot-card">' +
      '<p class="boot-err"></p>' +
      '<button id="retryBootBtn" class="btn btn-primary">Повторить</button>' +
      '<button id="hardReloadBtn" class="btn btn-ghost">Полный сброс кэша</button>' +
      "</div>";
    bootOverlay.querySelector(".boot-err").textContent = "Не удалось запустить парсер:\n" + msg;
    bootOverlay.hidden = false;
    bootOverlay.querySelector("#retryBootBtn").addEventListener("click", () => location.reload());
    bootOverlay.querySelector("#hardReloadBtn").addEventListener("click", hardReset);
  }

  // Полный сброс: удаляем SW-кэши и регистрацию, чтобы избавиться от устаревших копий.
  async function hardReset() {
    const names = (await caches.keys()) || [];
    await Promise.all(names.map((n) => caches.delete(n)));
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all((regs || []).map((r) => r.unregister()));
    }
    location.reload();
  }


  function showBoot(t) {
    bootText.textContent = t;
    bootOverlay.hidden = false;
  }
  function hideBoot() { bootOverlay.hidden = true; }

  // ---------- Парсинг файла ----------
  async function parseFile(buffer) {
    const py = state.pyodide;
    // передаём байты в Python
    py.globals.set("__schedule_bytes__", new Uint8Array(buffer));
    py.runPython("__schedule_result__ = parse_to_json(bytes(__schedule_bytes__))");
    const jsonStr = py.globals.get("__schedule_result__");
    return JSON.parse(jsonStr);
  }

  // ---------- Слияние ----------
  function mergeFile(newParsed, filename) {
    const newDays = newParsed.days || {};
    const mergedDays = JSON.parse(JSON.stringify(state.data.days || {}));

    // Новые даты (перезаписывают пересечения), включая пустые дни
    for (const iso of Object.keys(newDays)) {
      mergedDays[iso] = newDays[iso] || {};
    }

    // Объединение групп: сохраняем все найденные группы
    const groups = Array.from(new Set([...(state.data.groups || []), ...(newParsed.groups || [])]));

    state.data = {
      days: mergedDays,
      groups,
      files: [...(state.data.files || []), filename],
      updatedAt: new Date().toISOString(),
    };
    return state.data;
  }

  // ---------- Рендер календаря (неделя) ----------
  function startOfWeek(d) {
    // неделя Пн..Вс
    const dow = (d.getDay() + 6) % 7;
    return new Date(d.getFullYear(), d.getMonth(), d.getDate() - dow);
  }

  function setWeekStart(iso) {
    state.weekStart = isoOf(startOfWeek(parseISO(iso)));
  }

  function renderCalendar() {
    const today = todayISO();
    const start = startOfWeek(parseISO(state.weekStart || state.selectedDate || today));
    state.weekStart = isoOf(start);

    calendarBar.innerHTML = "";
    for (let i = 0; i < 7; i++) {
      const d = addDays(start, i);
      const dISO = isoOf(d);
      const chip = document.createElement("button");
      chip.className = "day-chip";
      if (dISO === state.selectedDate) chip.classList.add("active");
      if (dISO === today) chip.classList.add("today");
      const dayData = state.data.days && state.data.days[dISO];
      // подсвечиваем день, только если у выбранной группы в этот день есть пары
      const group = state.selectedGroup;
      const hasLessons = dayData && group && ((dayData[group] || []).length > 0);
      if (hasLessons) chip.classList.add("has-lessons");
      chip.innerHTML = `<span class="dow">${DAY_NAMES[d.getDay()]}</span>
                        <span class="dnum">${d.getDate()}</span>
                        <span class="dmon">${MONTHS[d.getMonth()].slice(0, 3)}</span>`;
      chip.addEventListener("click", () => selectDate(dISO));
      calendarBar.appendChild(chip);
    }

    // подпись недели
    const end = addDays(start, 6);
    weekLabel.textContent = `${start.getDate()} ${MONTHS[start.getMonth()]} – ${end.getDate()} ${MONTHS[end.getMonth()]} ${end.getFullYear()}`;
  }

  function goPrevWeek() {
    const ref = parseISO(state.weekStart || state.selectedDate || todayISO());
    const prev = addDays(ref, -7);
    setWeekStart(isoOf(prev));
    if (!state.selectedDate) state.selectedDate = isoOf(prev);
    renderCalendar();
    updateTodayBar();
  }
  function goNextWeek() {
    const ref = parseISO(state.weekStart || state.selectedDate || todayISO());
    const next = addDays(ref, 7);
    setWeekStart(isoOf(next));
    if (!state.selectedDate) state.selectedDate = isoOf(next);
    renderCalendar();
    updateTodayBar();
  }

  function selectDate(iso) {
    state.selectedDate = iso;
    setWeekStart(iso);
    renderCalendar();
    renderDay();
    renderGroups();
    updateTodayBar();
    savePrefs();
  }

  function updateTodayBar() {
    todayBtn.hidden = !state.selectedDate || state.selectedDate === todayISO();
  }

  // Короткое имя группы для отображения (бар с выбором группы не трогаем —
  // там остаются полные названия из данных).
  function displayGroupName(name) {
    return String(name).replace(/языковая группа/gi, "группа");
  }

  // ---------- Рендер дня ----------
  function renderDay() {
    const iso = state.selectedDate || todayISO();
    // если расписания вообще нет -> показываем большой empty-state
    const hasAnyDay = state.data.days && Object.keys(state.data.days).length > 0;
    emptyState.hidden = hasAnyDay;
    if (!hasAnyDay) {
      dayView.innerHTML = "";
      return;
    }
    const dayData = (state.data.days && state.data.days[iso]) || {};
    // пустым считаем день, когда у выбранной группы нет пар (то же, что "дня нет")
    const hasLessons = !!(dayData && state.selectedGroup && (dayData[state.selectedGroup] || []).length);

    dayView.innerHTML = "";
    if (!hasLessons) {
      dayView.appendChild(buildEmptyDay());
      return;
    }

    // Заголовок
    const heading = document.createElement("div");
    heading.className = "day-heading";
    const d = parseISO(iso);
    const h2 = document.createElement("h2");
    h2.textContent = `${DAY_NAMES_FULL[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
    heading.appendChild(h2);
    if (state.selectedGroup) {
      const tag = document.createElement("span");
      tag.className = "group-tag";
      tag.textContent = displayGroupName(state.selectedGroup);
      heading.appendChild(tag);
    }
    dayView.appendChild(heading);

    // Показываем только выбранную группу
    renderGroupLessons(dayData, state.selectedGroup);
  }

  function renderGroupLessons(dayData, group) {
    const lessons = (dayData && dayData[group]) || [];
    if (!lessons.length) {
      const e = document.createElement("div");
      e.className = "empty-day";
      e.innerHTML = `<div class="icon">🕐</div>Занятий в этот день нет`;
      dayView.appendChild(e);
      return;
    }
    // сортируем по номеру пары
    const sorted = lessons.slice().sort((a, b) => (a.num || 0) - (b.num || 0));
    sorted.forEach((l) => {
      const card = document.createElement("div");
      card.className = "lesson-card";
      card.innerHTML = `
        <div class="lesson-top">
          <span class="lesson-num">${l.num || "?"}</span>
          ${l.time ? `<span class="lesson-time">${l.time.replace("-", " – ")}</span>` : ""}
        </div>
        <div class="lesson-text"></div>`;
      card.querySelector(".lesson-text").textContent = l.text;
      dayView.appendChild(card);
    });
  }

  function buildEmptyDay() {
    const d = document.createElement("div");
    d.className = "empty-day";
    const iso = state.selectedDate || todayISO();
    const dd = parseISO(iso);
    d.innerHTML = `<div class="icon">📭</div>
                   <p>${DAY_NAMES_FULL[dd.getDay()]}, ${dd.getDate()} ${MONTHS[dd.getMonth()]}<br>
                   <small>В этот день пар нет</small></p>`;
    return d;
  }

  // ---------- Группы (бар под календарём, всегда активен) ----------
  function renderGroupBar() {
    const groups = state.data.groups || [];
    groupBar.innerHTML = "";
    if (!groups.length) {
      groupBar.hidden = true;
      return;
    }
    groupBar.hidden = false;
    // если выбор не существует среди групп или не выбран — по умолчанию первая группа
    if (!state.selectedGroup || !groups.includes(state.selectedGroup)) {
      state.selectedGroup = groups[0];
    }

    groups.forEach((g) => {
      const c = document.createElement("button");
      c.className = "group-chip" + (state.selectedGroup === g ? " on" : "");
      c.textContent = g;
      c.addEventListener("click", () => { state.selectedGroup = g; afterGroupChange(); });
      groupBar.appendChild(c);
    });
  }

  function afterGroupChange() {
    renderGroupBar();
    renderCalendar();
    renderDay();
    renderGroups();
    savePrefs();
  }

  // ---------- Группы (меню) ----------
  function renderGroups() {
    groupList.innerHTML = "";
    const groups = state.data.groups || [];
    if (!groups.length) {
      groupList.innerHTML = `<p class="drawer-hint">Пока нет групп. Загрузите файл расписания.</p>`;
      return;
    }
    groups.forEach((g) => {
      const item = document.createElement("div");
      item.className = "group-item" + (state.selectedGroup === g ? " selected" : "");
      item.innerHTML = `<span class="radio"></span><span>${g}</span>`;
      item.addEventListener("click", () => {
        state.selectedGroup = g;
        renderGroupBar();
        renderDay();
        renderGroups();
        savePrefs();
        openDrawer(false);
      });
      groupList.appendChild(item);
    });
  }

  // ---------- Сохранение выбора ----------
  function savePrefs() {
    dbPut("prefs", { group: state.selectedGroup, date: state.selectedDate }).catch(() => {});
  }

  // ---------- UI helpers ----------
  function renderAll() {
    renderCalendar();
    renderDay();
    renderGroupBar();
    renderGroups();
    updateTodayBar();
  }

  function openDrawer(open) {
    drawerBackdrop.hidden = !open;
  }

  // ---------- Выбор произвольной даты ----------
  function openDatePicker() {
    dateInput.value = state.selectedDate || todayISO();
    dateInput.showPicker ? dateInput.showPicker() : dateInput.click();
  }

  function goToday() {
    selectDate(todayISO());
  }

  // ---------- Настройки: тема и цвет ----------
  const ACCENT_COLORS = [
    { name: "Фиолетовый", value: "#6c5ce7" },
    { name: "Синий", value: "#2f6fed" },
    { name: "Бирюзовый", value: "#00b894" },
    { name: "Зелёный", value: "#27ae60" },
    { name: "Оранжевый", value: "#e67e22" },
    { name: "Розовый", value: "#e84393" },
    { name: "Красный", value: "#e74c3c" },
  ];

  function applyTheme() {
    const dark = localStorage.getItem("rc-theme") === "dark";
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "");
    if (themeSwitch) themeSwitch.checked = dark;

    // статус-бар браузера в цвет шапки приложения (следует за темой)
    const tc = document.querySelector('meta[name="theme-color"]');
    if (tc) tc.setAttribute("content", dark ? "#171a22" : "#ffffff");
  }
  function setTheme(mode) {
    localStorage.setItem("rc-theme", mode);
    applyTheme();
  }

  function applyAccent() {
    const color = localStorage.getItem("rc-accent") || "#6c5ce7";
    document.documentElement.style.setProperty("--primary", color);
    // слегка затемняем для pressed-состояний
    document.documentElement.style.setProperty("--primary-dark", color);
  }
  function setAccent(color) {
    localStorage.setItem("rc-accent", color);
    applyAccent();
    renderColorGrid();
  }

  function renderColorGrid() {
    const cur = localStorage.getItem("rc-accent") || "#6c5ce7";
    colorGrid.innerHTML = "";
    ACCENT_COLORS.forEach((c) => {
      const sw = document.createElement("button");
      sw.className = "color-swatch" + (c.value === cur ? " on" : "");
      sw.style.background = c.value;
      sw.title = c.name;
      sw.addEventListener("click", () => setAccent(c.value));
      colorGrid.appendChild(sw);
    });
  }
  function setupColorGrid() {
    renderColorGrid();
    // отметить цвет в сетке после установки
  }

  function showSettingsTab(tab) {
    const theme = tab === "theme";
    tabTheme.classList.toggle("on", theme);
    tabColor.classList.toggle("on", !theme);
    settingsTheme.hidden = !theme;
    settingsColor.hidden = theme;
  }

  function openSettings() {
    applyTheme();
    applyAccent();
    showSettingsTab("theme");
    settingsOverlay.hidden = false;
  }

  let toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
  }

  // ---------- Загрузка файлов ----------
  async function handleFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) =>
      /\.(xlsx|xlsm|xls)$/i.test(f.name) && !f.name.startsWith("~$")
    );
    if (!files.length) { toast("Нет подходящих .xlsx файлов"); return; }
    if (!state.parserReady) { toast("Парсер ещё не готов, попробуйте ещё раз"); return; }

    let added = 0;
    for (const file of files) {
      try {
        const buffer = await file.arrayBuffer();
        showBoot(`Читаем «${file.name}»…`);
        const parsed = await parseFile(buffer);
        if (!parsed || (!parsed.days || !Object.keys(parsed.days).length)) {
          toast(`Не удалось найти расписание в «${file.name}»`);
          continue;
        }
        mergeFile(parsed, file.name);
        added++;
      } catch (e) {
        console.error(e);
        toast(`Ошибка при разборе «${file.name}»`);
      }
    }
    hideBoot();
    if (added) {
      await dbPut("data", state.data);
      // если выбранная дата не в диапазоне, выбираем сегодня
      state.selectedDate = state.selectedDate || todayISO();
      renderAll();
      toast(`Загружено файлов: ${added}. Даты объединены.`);
    }
  }

  // ---------- Очистка ----------
  async function clearAll() {
    state.data = { days: {}, groups: [], files: [], updatedAt: null };
    state.selectedGroup = null;
    state.selectedDate = todayISO();
    await dbClear();
    renderAll();
    toast("Расписание очищено");
  }

  // ---------- PWA install ----------
  let deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredPrompt = e;
  });

  // ---------- Init ----------
  // Однажды за версию чистим устаревшие SW-кэши, чтобы не цеплять старые
  // копии parser.py / app.js. Отмечаем в localStorage факт очистки.
  async function selfHeal() {
    const marker = "healed-" + APP_VERSION;
    try {
      if (localStorage.getItem(marker) === "1") return;
      if ("caches" in window) {
        const keys = await caches.keys();
        await Promise.all((keys || []).map((k) => {
          if (k.indexOf("pyodide") >= 0) return; // Pyodide-рантайм оставляем
          return caches.delete(k);
        }));
      }
      localStorage.setItem(marker, "1");
      console.log("selfHeal: старые кэши очищены");
    } catch (e) {
      console.warn("selfHeal skip", e);
    }
  }

  async function init() {
    selfHeal();
    applyTheme();
    applyAccent();
    // restore data
    try {
      const saved = await dbGet("data");
      if (saved) {
        state.data = {
          days: saved.days || {},
          groups: saved.groups || [],
          files: saved.files || [],
          updatedAt: saved.updatedAt || null,
        };
      }
    } catch (e) { console.warn("DB restore fail", e); }

    // restore preferences (выбранная группа/дата)
    try {
      const prefs = await dbGet("prefs");
      if (prefs) {
        if (prefs.group && (state.data.groups || []).includes(prefs.group)) state.selectedGroup = prefs.group;
        if (prefs.date) state.selectedDate = prefs.date;
      }
    } catch (e) { console.warn("DB prefs restore fail", e); }

    state.selectedDate = state.selectedDate || todayISO();
    renderAll();
    boot();

    // события
    $("#settingsBtn").addEventListener("click", openSettings);
    $("#closeMenuBtn").addEventListener("click", () => openDrawer(false));
    $("#clearAllBtn").addEventListener("click", async () => {
      if (confirm("Очистить всё расписание?")) await clearAll();
    });
    $("#loadFileBtn").addEventListener("click", () => fileInput.click());
    $("#emptyUploadBtn").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async (e) => {
      await handleFiles(e.target.files);
      fileInput.value = "";
    });

    // навигация по неделям и выбор даты
    prevWeekBtn.addEventListener("click", goPrevWeek);
    nextWeekBtn.addEventListener("click", goNextWeek);
    todayBtn.addEventListener("click", goToday);
    datePickBtn.addEventListener("click", openDatePicker);
    dateInput.addEventListener("change", () => {
      if (dateInput.value) selectDate(dateInput.value);
    });

    // настройки
    $("#closeSettingsBtn").addEventListener("click", () => { settingsOverlay.hidden = true; });
    $("#loadFileBtn2").addEventListener("click", () => { settingsOverlay.hidden = true; fileInput.click(); });
    $("#clearAllBtn2").addEventListener("click", async () => {
      if (confirm("Очистить всё расписание?")) {
        await clearAll();
        settingsOverlay.hidden = true;
      }
    });
    settingsOverlay.addEventListener("click", (e) => {
      if (e.target === settingsOverlay) settingsOverlay.hidden = true;
    });
    tabTheme.addEventListener("click", () => showSettingsTab("theme"));
    tabColor.addEventListener("click", () => showSettingsTab("color"));
    themeSwitch.addEventListener("change", () => {
      setTheme(themeSwitch.checked ? "dark" : "light");
    });
    setupColorGrid();

    // нативные жесты закрытия дровера
    drawerBackdrop.addEventListener("click", (e) => {
      if (e.target === drawerBackdrop) openDrawer(false);
    });

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch((e) => console.warn("SW:", e));
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
