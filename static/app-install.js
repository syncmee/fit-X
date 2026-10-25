// fiT-X · Add-to-Home-Screen flow (mobile only).
// Android/Chrome: captures beforeinstallprompt and opens the native install
// dialog from our button/popup. iOS/Safari: shows Share → Add to Home Screen
// steps (Apple allows no programmatic prompt).
// The auto-popup shows at most once per 7 days per device — dismissing it
// must not turn every page load into an interstitial. The toolbar button
// always works on demand. People who installed via the home-screen bookmark
// run in standalone mode and never see any of this.
(function () {
  if (window.matchMedia("(min-width: 768px)").matches) return; // desktop: skip entirely

  var isStandalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  if (isStandalone) return; // installed (bookmark/home-screen) — never prompt

  var DISMISS_KEY = "fitxInstallPromptDismissedAt";
  var DISMISS_COOLDOWN = 7 * 24 * 60 * 60 * 1000; // 7 days

  function recentlyDismissed() {
    try {
      var at = parseInt(localStorage.getItem(DISMISS_KEY) || "0", 10);
      return at && Date.now() - at < DISMISS_COOLDOWN;
    } catch (e) { return false; }
  }

  function markDismissed() {
    try { localStorage.setItem(DISMISS_KEY, String(Date.now())); } catch (e) { /* private mode */ }
  }

  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  var deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
  });

  function chromeCanInstall() { return !!deferredPrompt; }

  function showButton() {
    var btn = document.getElementById("installAppBtn");
    if (btn) { btn.classList.remove("hidden"); btn.classList.add("flex"); }
  }

  function hideButton() {
    var btn = document.getElementById("installAppBtn");
    if (btn) { btn.classList.add("hidden"); btn.classList.remove("flex"); }
  }

  function openPopup() {
    var popup = document.getElementById("installPopup");
    if (!popup) return;
    var iosSteps = document.getElementById("installStepsIos");
    var genericSteps = document.getElementById("installStepsGeneric");
    var androidBtn = document.getElementById("installPopupAndroidBtn");
    if (isIOS) {
      if (iosSteps) iosSteps.classList.remove("hidden");
    } else if (chromeCanInstall()) {
      if (androidBtn) androidBtn.classList.remove("hidden");
    } else if (genericSteps) {
      genericSteps.classList.remove("hidden");
    }
    popup.classList.remove("hidden");
    popup.classList.add("flex");
  }

  function closePopup() {
    var popup = document.getElementById("installPopup");
    if (popup) { popup.classList.add("hidden"); popup.classList.remove("flex"); }
  }

  function tryNativeInstall() {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    deferredPrompt.userChoice.finally(function () { deferredPrompt = null; });
    closePopup();
    hideButton();
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Register on every page that loads this script — the worker's offline
    // fallback and asset caching apply site-wide, not just where the
    // install button lives.
    if ("serviceWorker" in navigator) {
      // Root scope so the worker controls the whole app, not just /static/.
      navigator.serviceWorker.register("/sw.js").catch(function () { /* best effort */ });
    }

    var btn = document.getElementById("installAppBtn");
    var popup = document.getElementById("installPopup");
    if (!btn && !popup) return;

    showButton();
    // Auto-open only when the user hasn't recently dismissed it; the button
    // still opens it on demand at any time.
    if (popup && !recentlyDismissed()) setTimeout(openPopup, 1200);

    if (btn) btn.addEventListener("click", openPopup);
    var closeX = document.getElementById("installPopupClose");
    if (closeX) closeX.addEventListener("click", function () { markDismissed(); closePopup(); });
    var dismiss = document.getElementById("installPopupDismiss");
    if (dismiss) dismiss.addEventListener("click", function () { markDismissed(); closePopup(); });
    var androidBtn = document.getElementById("installPopupAndroidBtn");
    if (androidBtn) androidBtn.addEventListener("click", tryNativeInstall);

    window.addEventListener("appinstalled", function () {
      hideButton();
      closePopup();
    });
  });
})();
