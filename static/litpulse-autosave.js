/*
 * litpulse-autosave.js —— 在 pdf.js 阅读器里画的高亮/文字/手绘自动存回我的文库
 * --------------------------------------------------------------------------
 * 运行环境：由 pdfserve 在返回 viewer.html 时注入（不修改 pdf.js 原文件）。
 * 仅当打开的是【文库 PDF】且链接带一次性保存令牌(lptoken)时启用；
 * 普通问答跳页（uploads，无令牌）不会启用，也不会发起任何写请求。
 *
 * 原理：
 *   pdf.js 把标注编辑结果写进 pdfDocument.annotationStorage。我们 hook 它的
 *   setValue / deleteValue 来感知"标注有改动"，防抖后调用 saveDocument() 导出
 *   烘焙好标注的 PDF，PUT 回 /library-save 覆盖文库原文件。
 */
(function () {
  "use strict";

  function parseConfig() {
    // 令牌与文件名放在 hash：#page=5&lptoken=xxx&lpname=yyy.pdf
    var hash = (location.hash || "").replace(/^#/, "");
    var p = {};
    hash.split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      if (i > 0) p[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1));
    });
    var name = p.lpname || "";
    // 客户端白名单（服务端还会再严格校验一次）
    if (!/^[A-Za-z0-9_.\-]+\.pdf$/.test(name)) name = "";
    return { token: p.lptoken || "", name: name };
  }

  var CFG = parseConfig();
  if (!CFG.token || !CFG.name) return;   // 非文库文档，不启用

  var BASE = location.origin;            // 与 pdf.js、PDF 同源（pdfserve）
  var SAVE_URL = BASE + "/library-save?name=" + encodeURIComponent(CFG.name);

  var state = {
    doc: null,
    storage: null,
    dirty: false,
    saving: false,
    timer: null,
    badge: null,
    lastStatus: "armed",
  };
  // 暴露只读状态，便于真机自检（不影响保存逻辑）
  window.__litpulse = {
    config: { hasToken: !!CFG.token, name: CFG.name, url: SAVE_URL },
    state: state,
  };

  // -- 极简角标提示 ---------------------------------------------------------
  function badge(text, color) {
    if (!state.badge) {
      var d = document.createElement("div");
      d.style.cssText =
        "position:fixed;right:14px;bottom:14px;z-index:99999;padding:7px 14px;" +
        "border-radius:18px;font:13px/1.4 system-ui,sans-serif;color:#fff;" +
        "box-shadow:0 2px 10px rgba(0,0,0,.25);transition:opacity .3s;opacity:0;";
      document.body.appendChild(d);
      state.badge = d;
    }
    state.badge.textContent = text;
    state.badge.style.background = color || "#1a73e8";
    state.badge.style.opacity = "1";
    clearTimeout(state.badge._t);
    state.badge._t = setTimeout(function () { state.badge.style.opacity = "0"; }, 2600);
  }

  function nowHM() {
    var d = new Date();
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }

  // -- 导出并回传 -----------------------------------------------------------
  async function saveAndUpload() {
    if (!state.dirty || state.saving || !state.doc) return;
    state.saving = true;
    state.lastStatus = "saving";
    badge("💾 自动保存中…", "#b26a00");
    try {
      // saveDocument() 会把 annotationStorage 里的标注烘焙进 PDF
      var data = await state.doc.saveDocument();
      var buf = data instanceof Uint8Array ? data : new Uint8Array(data);

      var resp = await fetch(SAVE_URL, {
        method: "PUT",
        headers: { "Content-Type": "application/pdf",
                   "X-LitPulse-Token": CFG.token },
        body: buf,
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);

      // 保存成功：重置"已修改"标记，避免重复回传同一份
      try { if (typeof state.storage.resetModified === "function") state.storage.resetModified(); } catch (e) {}
      state.dirty = false;
      state.lastStatus = "saved";
      state.lastSavedAt = nowHM();
      badge("✅ 已自动保存到文库 " + nowHM(), "#188038");
    } catch (err) {
      console.error("[LitPulse] 自动保存失败：", err);
      state.lastStatus = "error";
      state.lastError = String(err && err.message || err);
      badge("⚠️ 自动保存失败，请用右上角 ⬇ 下载备份", "#d93025");
    } finally {
      state.saving = false;
    }
  }

  function scheduleSave() {
    state.dirty = true;
    clearTimeout(state.timer);
    // 防抖：连续绘制时合并，停笔 1.2 秒后再保存
    state.timer = setTimeout(saveAndUpload, 1200);
  }

  // hook annotationStorage 的写操作以感知"标注增/改"；删除用 size 轮询兜底
  function hookStorage(pdfDoc) {
    var s = pdfDoc && pdfDoc.annotationStorage;
    if (!s || s === state.storage) return;
    state.storage = s;
    state.doc = pdfDoc;
    state._lastSize = s.size;

    if (typeof s.setValue === "function" && !s.__lp_setValue) {
      var orig = s.setValue.bind(s);
      s.__lp_setValue = true;
      s.setValue = function () {
        var r = orig.apply(null, arguments);
        try { scheduleSave(); } catch (e) {}
        return r;
      };
    }

    // 兜底：这个版本删除标注不经过 setValue，用 size 变化（变小）感知
    if (!state._sizeWatcher) {
      state._sizeWatcher = setInterval(function () {
        if (!state.storage) return;
        var cur = state.storage.size;
        if (cur < state._lastSize) {
          try { scheduleSave(); } catch (e) {}
        }
        state._lastSize = cur;
      }, 1200);
    }
  }

  function start(App) {
    var eb = App.eventBus;
    var attach = function () {
      if (App.pdfDocument) hookStorage(App.pdfDocument);
    };
    eb.on("documentloaded", attach);
    eb.on("pagesloaded", attach);
    attach();
  }

  // 等 PDFViewerApplication 就绪
  var tries = 0;
  var ready = setInterval(function () {
    var App = window.PDFViewerApplication;
    if (App && App.eventBus && App.initialized) {
      clearInterval(ready);
      start(App);
    } else if (++tries > 100) {
      clearInterval(ready);
    }
  }, 100);
})();
