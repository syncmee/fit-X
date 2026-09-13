// fiT-X · Add-to-Home-Screen flow (mobile only).
// Android/Chrome: captures beforeinstallprompt and opens the native install
// dialog from our button/popup. iOS/Safari: shows Share → Add to Home Screen
// steps (Apple allows no programmatic prompt).
// The popup appears on EVERY browser visit; people who installed via the
// home-screen bookmark run in standalone mode and never see any of this.
(function () {
  if (window.matchMedia("(min-width: 768px)").matches) return; // desktop: skip entirely

  var isStandalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  if (isStandalone) return; // installed (bookmark/home-screen) — never prompt

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
    var btn = document.getElementById("installAppBtn");
    var popup = document.getElementById("installPopup");
    if (!btn && !popup) return;

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js").catch(function () { /* best effort */ });
    }

    showButton();
    if (popup) setTimeout(openPopup, 1200);

    if (btn) btn.addEventListener("click", openPopup);
    var closeX = document.getElementById("installPopupClose");
    if (closeX) closeX.addEventListener("click", closePopup);
    var dismiss = document.getElementById("installPopupDismiss");
    if (dismiss) dismiss.addEventListener("click", closePopup);
    var androidBtn = document.getElementById("installPopupAndroidBtn");
    if (androidBtn) androidBtn.addEventListener("click", tryNativeInstall);

    window.addEventListener("appinstalled", function () {
      hideButton();
      closePopup();
    });
  });
})();
