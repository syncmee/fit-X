// fiT-X · Add-to-Home-Screen flow (mobile only).
// Android/Chrome: captures beforeinstallprompt and opens the native install
// dialog from our button/popup. iOS/Safari: shows Share → Add to Home Screen
// steps (Apple allows no programmatic prompt). Hidden once installed.
(function () {
  if (window.matchMedia("(min-width: 768px)").matches) return; // desktop: skip entirely

  var isStandalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  if (isStandalone) return; // already installed

  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  var deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    showUi();
  });

  function isIOSDevice() { return isIOS; }

  function chromeCanInstall() { return !!deferredPrompt; }

  function showUi() {
    var btn = document.getElementById("installAppBtn");
    if (btn) { btn.classList.remove("hidden"); btn.classList.add("flex"); }
    var popup = document.getElementById("installPopup");
    if (popup && popup.dataset.forceShow === "1") openPopup();
  }

  function markPrompted() {
    try { localStorage.setItem("fitxInstallPrompted", "1"); } catch (e) { /* private mode */ }
  }

  function openPopup() {
    var popup = document.getElementById("installPopup");
    if (!popup) return;
    var steps = document.getElementById("installStepsIos");
    var androidBtn = document.getElementById("installPopupAndroidBtn");
    if (isIOSDevice() || !chromeCanInstall()) {
      if (steps) steps.classList.remove("hidden");
      if (androidBtn) androidBtn.classList.add("hidden");
    } else if (androidBtn) {
      androidBtn.classList.remove("hidden");
    }
    popup.classList.remove("hidden");
    popup.classList.add("flex");
    markPrompted();
  }

  function closePopup() {
    var popup = document.getElementById("installPopup");
    if (popup) { popup.classList.add("hidden"); popup.classList.remove("flex"); }
    markPrompted();
  }

  function tryNativeInstall() {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    deferredPrompt.userChoice.finally(function () { deferredPrompt = null; });
    closePopup();
    var btn = document.getElementById("installAppBtn");
    if (btn) btn.classList.add("hidden");
  }

  // One-time post-onboarding popup: first mobile dashboard visit, dismissed never returns.
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("installAppBtn");
    var popup = document.getElementById("installPopup");
    if (!btn && !popup) return;

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js").catch(function () { /* best effort */ });
    }

    var prompted = false;
    try { prompted = localStorage.getItem("fitxInstallPrompted") === "1"; } catch (e) { /* private mode */ }
    // Only the dashboard carries the popup — other pages just register the SW
    // and leave the "prompted" flag alone for the dashboard to claim.
    if (!prompted && popup) {
      popup.dataset.forceShow = "1";
      setTimeout(openPopup, 1200);
    }

    if (btn) btn.addEventListener("click", openPopup);
    var closeX = document.getElementById("installPopupClose");
    if (closeX) closeX.addEventListener("click", closePopup);
    var dismiss = document.getElementById("installPopupDismiss");
    if (dismiss) dismiss.addEventListener("click", closePopup);
    var androidBtn = document.getElementById("installPopupAndroidBtn");
    if (androidBtn) androidBtn.addEventListener("click", tryNativeInstall);

    window.addEventListener("appinstalled", function () {
      markPrompted();
      if (btn) btn.classList.add("hidden");
      closePopup();
    });
  });
})();
