(() => {
  let scanTarget = 'inventory';
  let nativeStream = null;
  let nativeTimer = null;
  let zxingControls = null;
  let cachedState = null;
  let zxingLoading = null;

  function status(text) {
    const el = document.getElementById('barcodeScannerStatus');
    if (el) el.textContent = text;
  }

  async function getState() {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error('Voorraad kon niet worden geladen.');
    cachedState = await r.json();
    return cachedState;
  }

  function stopScanner() {
    if (nativeTimer) clearInterval(nativeTimer);
    nativeTimer = null;
    if (nativeStream) nativeStream.getTracks().forEach(t => t.stop());
    nativeStream = null;
    if (zxingControls) {
      try { zxingControls.stop(); } catch {}
      zxingControls = null;
    }
    const video = document.getElementById('barcodeVideo');
    if (video) {
      try { video.pause(); } catch {}
      video.srcObject = null;
    }
  }

  function closeSoon() {
    setTimeout(() => {
      stopScanner();
      const d = document.getElementById('barcodeScannerDialog');
      if (d?.open) d.close();
    }, 300);
  }

  async function useBarcode(value) {
    const barcode = String(value || '').trim();
    if (!barcode) return;
    const state = cachedState || await getState();
    const found = (state.items || []).find(i => String(i.barcode || '').trim() === barcode);
    if (!found) {
      status(`Geen artikel gevonden voor ${barcode}.`);
      return;
    }
    if (scanTarget === 'transaction') {
      const select = document.getElementById('itemSelect');
      if (select) {
        select.value = found.id;
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
      status(`${found.name} geselecteerd.`);
      closeSoon();
      return;
    }
    status(`Gevonden: ${found.name} · voorraad ${Number(found.stock || 0)}.`);
    const row = document.querySelector(`[data-item-row="${CSS.escape(found.id)}"]`) || document.querySelector(`[data-barcode-row="${CSS.escape(found.id)}"]`);
    row?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function loadZXing() {
    if (window.ZXingBrowser?.BrowserMultiFormatReader) return Promise.resolve(window.ZXingBrowser);
    if (zxingLoading) return zxingLoading;
    zxingLoading = new Promise((resolve, reject) => {
      const existing = document.querySelector('script[data-stockroom-zxing]');
      if (existing) {
        existing.addEventListener('load', () => resolve(window.ZXingBrowser), { once: true });
        existing.addEventListener('error', reject, { once: true });
        return;
      }
      const script = document.createElement('script');
      script.src = 'https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/umd/zxing-browser.min.js';
      script.async = true;
      script.dataset.stockroomZxing = '1';
      script.onload = () => window.ZXingBrowser?.BrowserMultiFormatReader ? resolve(window.ZXingBrowser) : reject(new Error('ZXing kon niet worden geladen.'));
      script.onerror = () => reject(new Error('ZXing kon niet worden geladen.'));
      document.head.appendChild(script);
    });
    return zxingLoading;
  }

  async function startNative(video) {
    if (!('BarcodeDetector' in window)) throw new Error('native-not-supported');
    const supported = BarcodeDetector.getSupportedFormats ? await BarcodeDetector.getSupportedFormats() : [];
    const wanted = ['ean_13','ean_8','upc_a','upc_e','code_128','code_39','itf'];
    const formats = supported.length ? wanted.filter(f => supported.includes(f)) : wanted;
    if (!formats.length) throw new Error('native-no-formats');
    const detector = new BarcodeDetector({ formats });
    nativeStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false
    });
    video.srcObject = nativeStream;
    await video.play();
    nativeTimer = setInterval(async () => {
      try {
        const codes = await detector.detect(video);
        if (codes[0]?.rawValue) {
          const value = codes[0].rawValue;
          stopScanner();
          await useBarcode(value);
        }
      } catch {}
    }, 250);
    status('Richt de achtercamera op de barcode.');
  }

  async function startZXing(video) {
    const ZXing = await loadZXing();
    const reader = new ZXing.BrowserMultiFormatReader();
    zxingControls = await reader.decodeFromVideoDevice(undefined, video, async (result) => {
      if (!result) return;
      const value = typeof result.getText === 'function' ? result.getText() : result.text;
      if (!value) return;
      stopScanner();
      await useBarcode(value);
    });
    status('Richt de achtercamera op de barcode.');
  }

  async function openScanner(target) {
    scanTarget = target;
    stopScanner();
    cachedState = null;
    const dialog = document.getElementById('barcodeScannerDialog');
    const video = document.getElementById('barcodeVideo');
    if (!dialog || !video) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      if (!dialog.open) dialog.showModal();
      status('Camera vereist HTTPS en cameratoegang. Voer de barcode anders handmatig in.');
      return;
    }
    if (!dialog.open) dialog.showModal();
    status('Camera wordt gestart…');
    try {
      await getState();
      try {
        await startNative(video);
      } catch {
        stopScanner();
        status('Alternatieve scanner wordt gestart…');
        await startZXing(video);
      }
    } catch (err) {
      stopScanner();
      if (err?.name === 'NotAllowedError') status('Cameratoegang is geweigerd. Sta camera toe voor deze website en probeer opnieuw.');
      else if (err?.name === 'NotFoundError') status('Geen camera gevonden.');
      else status('Camerascanner kon niet starten. Voer de barcode hieronder handmatig in.');
    }
  }

  document.addEventListener('click', e => {
    const inventory = e.target.closest('#scanInventoryBtn');
    const transaction = e.target.closest('#scanTransactionBtn');
    if (!inventory && !transaction) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    openScanner(transaction ? 'transaction' : 'inventory');
  }, true);

  document.addEventListener('click', e => {
    if (!e.target.closest('[data-close-scanner]')) return;
    stopScanner();
  }, true);

  document.getElementById('barcodeScannerDialog')?.addEventListener('close', stopScanner);
  window.addEventListener('pagehide', stopScanner);
})();
