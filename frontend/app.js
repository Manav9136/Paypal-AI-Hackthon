/* ═══════════════════════════════════════════════════════════════
   VoicePay AI — Application Logic
   Voice-guided payment frontend using Web Speech API
   and prepared for a Python FastAPI backend + PayPal SDK.
   ═══════════════════════════════════════════════════════════════ */

// ─── CONFIGURATION ───────────────────────────────────────────
/**
 * Base URL for the FastAPI backend.
 * - Development: defaults to http://localhost:8000
 * - Production: set window.VOICEPAY_API_BASE before this script loads.
 *   In index.html (server-injected), add before this script tag:
 *     <script>window.VOICEPAY_API_BASE = 'https://api.yourdomain.com';</script>
 * Never hardcode a production URL here.
 */
const API_BASE = (typeof window !== 'undefined' && window.VOICEPAY_API_BASE)
  ? window.VOICEPAY_API_BASE.replace(/\/$/, '')
  : 'http://localhost:8000';

/**
 * Demo mode: when true, the frontend uses mock data instead of
 * calling the real backend. Clearly labelled — never claims
 * a real payment was made.
 */
let demoMode = false;

// ─── DOM REFERENCES ──────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
  // Navigation
  mobileMenuBtn:   $('#mobileMenuBtn'),
  mobileNav:       $('#mobileNav'),
  navLinks:        $$('.nav-link'),
  mobileNavLinks:  $$('.mobile-nav-link'),

  // Mic / input
  micBtn:          $('#micBtn'),
  micRing:         $('#micRing'),
  micInstruction:  $('#micInstruction'),
  micUnsupported:  $('#micUnsupported'),
  statusDot:       $('#statusDot'),
  statusText:      $('#statusText'),
  transcriptionArea: $('#transcriptionArea'),
  transcriptionText: $('#transcriptionText'),
  langSelect:      $('#langSelect'),
  textForm:        $('#textForm'),
  textInput:       $('#textInput'),
  sendBtn:         $('#sendBtn'),

  // Review
  reviewCard:      $('#reviewCard'),
  reviewRecipient: $('#reviewRecipient'),
  reviewAmount:    $('#reviewAmount'),
  reviewCurrency:  $('#reviewCurrency'),
  reviewPurpose:   $('#reviewPurpose'),
  recipientHint:   $('#recipientHint'),
  amountHint:      $('#amountHint'),
  proceedBtn:      $('#proceedBtn'),
  cancelReviewBtn: $('#cancelReviewBtn'),
  validationMsg:   $('#validationMsg'),

  // Confirmation modal
  confirmModal:    $('#confirmModal'),
  confirmRecipient:$('#confirmRecipient'),
  confirmAmount:   $('#confirmAmount'),
  confirmPurpose:  $('#confirmPurpose'),
  confirmPayBtn:   $('#confirmPayBtn'),
  cancelConfirmBtn:$('#cancelConfirmBtn'),

  // Checkout
  checkoutSection: $('#checkout-section'),
  checkoutCard:    $('#checkoutCard'),
  checkoutSummary: $('#checkoutSummary'),
  paypalContainer: $('#paypal-button-container'),
  checkoutLoading: $('#checkoutLoading'),
  checkoutSuccess: $('#checkoutSuccess'),
  checkoutFailure: $('#checkoutFailure'),
  successDetails:  $('#successDetails'),
  failureDetails:  $('#failureDetails'),
  newPaymentBtn:   $('#newPaymentBtn'),
  retryPaymentBtn: $('#retryPaymentBtn'),
  cancelCheckoutBtn:$('#cancelCheckoutBtn'),

  // Transactions
  transactionsList:$('#transactionsList'),
  demoNotice:      $('#demoNotice'),
  emptyState:      $('#emptyState'),
  transactionsLoading: $('#transactionsLoading'),

  // Toast
  toastContainer:  $('#toastContainer'),
};

// ─── STATE ───────────────────────────────────────────────────
let recognition = null;       // SpeechRecognition instance
let isListening = false;
let currentPayment = null;    // { recipient, amount, currency, purpose }
let currentOrderId = null;

// ─── INITIALIZATION ──────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initLucide();
  initNav();
  initSpeechRecognition();
  initEventListeners();
  checkBackendAvailability();
  loadTransactions();
});

/** Render all Lucide icons in the page */
function initLucide() {
  if (window.lucide) {
    lucide.createIcons();
  }
}

// ─── NAVIGATION ──────────────────────────────────────────────

function initNav() {
  // Mobile menu toggle
  dom.mobileMenuBtn.addEventListener('click', () => {
    const isOpen = dom.mobileNav.classList.toggle('open');
    dom.mobileMenuBtn.setAttribute('aria-expanded', isOpen);
    dom.mobileNav.setAttribute('aria-hidden', !isOpen);
  });

  // Close mobile menu on link click
  dom.mobileNavLinks.forEach(link => {
    link.addEventListener('click', () => {
      dom.mobileNav.classList.remove('open');
      dom.mobileMenuBtn.setAttribute('aria-expanded', 'false');
      dom.mobileNav.setAttribute('aria-hidden', 'true');
    });
  });

  // Active nav link tracking
  const allNavLinks = [...dom.navLinks, ...dom.mobileNavLinks];
  allNavLinks.forEach(link => {
    link.addEventListener('click', () => {
      dom.navLinks.forEach(l => l.classList.remove('active'));
      const nav = link.dataset.nav;
      dom.navLinks.forEach(l => {
        if (l.dataset.nav === nav) l.classList.add('active');
      });
    });
  });
}

// ─── SPEECH RECOGNITION ─────────────────────────────────────

function initSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  if (!SpeechRecognition) {
    // Browser doesn't support speech recognition
    dom.micBtn.disabled = true;
    dom.micUnsupported.hidden = false;
    dom.micInstruction.hidden = true;
    setStatus('ready');
    return;
  }

  recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = dom.langSelect.value;

  recognition.onstart = () => {
    isListening = true;
    dom.micBtn.classList.add('listening');
    dom.micInstruction.textContent = 'Listening… speak now';
    dom.transcriptionArea.hidden = false;
    dom.transcriptionText.textContent = '';
    setStatus('listening');
  };

  recognition.onresult = (event) => {
    let interim = '';
    let final = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        final += transcript;
      } else {
        interim += transcript;
      }
    }
    dom.transcriptionText.textContent = final || interim;
    if (final) {
      handlePaymentRequest(final.trim());
    }
  };

  recognition.onerror = (event) => {
    console.warn('Speech recognition error:', event.error);
    stopListening();
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      showToast('Microphone access denied. Please allow microphone permissions or use text input.', 'error');
    } else if (event.error === 'no-speech') {
      showToast('No speech detected. Try again or type your request.', 'info');
    } else if (event.error !== 'aborted') {
      showToast(`Speech error: ${event.error}. Try typing your request instead.`, 'error');
    }
    setStatus('ready');
  };

  recognition.onend = () => {
    stopListening();
  };
}

function startListening() {
  if (!recognition) return;
  recognition.lang = dom.langSelect.value;
  try {
    recognition.start();
  } catch (e) {
    // Already started — ignore
    console.warn('Recognition start error:', e);
  }
}

function stopListening() {
  isListening = false;
  dom.micBtn.classList.remove('listening');
  dom.micInstruction.textContent = 'Tap the mic and say what you want to pay.';
  if (recognition) {
    try { recognition.stop(); } catch (e) { /* ignore */ }
  }
}

// ─── EVENT LISTENERS ─────────────────────────────────────────

function initEventListeners() {
  // Mic button
  dom.micBtn.addEventListener('click', () => {
    if (isListening) {
      stopListening();
      setStatus('ready');
    } else {
      startListening();
    }
  });

  // Text form submit
  dom.textForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = dom.textInput.value.trim();
    if (!text) return;
    handlePaymentRequest(text);
  });

  // Example chips
  $$('.chip[data-example]').forEach(chip => {
    chip.addEventListener('click', () => {
      const example = chip.dataset.example;
      dom.textInput.value = example;
      handlePaymentRequest(example);
    });
  });

  // Review actions
  dom.proceedBtn.addEventListener('click', validateAndProceed);
  dom.cancelReviewBtn.addEventListener('click', resetPaymentFlow);

  // Confirmation modal
  dom.confirmPayBtn.addEventListener('click', initiateCheckout);
  dom.cancelConfirmBtn.addEventListener('click', closeConfirmModal);

  // Checkout actions
  dom.newPaymentBtn.addEventListener('click', resetPaymentFlow);
  dom.retryPaymentBtn.addEventListener('click', initiateCheckout);
  dom.cancelCheckoutBtn.addEventListener('click', resetPaymentFlow);

  // Keyboard: close modal on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !dom.confirmModal.hidden) {
      closeConfirmModal();
    }
  });
}

// ─── PAYMENT FLOW ────────────────────────────────────────────

/**
 * Handle a payment request string — send to backend for AI parsing,
 * or use demo mock data if backend is unavailable.
 */
async function handlePaymentRequest(message) {
  if (!message) return;

  // Stop listening if active
  stopListening();
  setStatus('processing');
  disableInputs(true);

  // Provide voice feedback
  speak('Processing your request.');

  try {
    let parsed;

    if (demoMode) {
      // ── DEMO MODE: local mock parsing ──
      parsed = mockParsePayment(message);
    } else {
      // ── LIVE MODE: call backend ──
      parsed = await apiPost('/parse-payment', { message });
    }

    if (!parsed || (!parsed.recipient && !parsed.amount)) {
      showToast('Could not understand the payment request. Please try again.', 'error');
      speak('Sorry, I could not understand that. Please try again.');
      setStatus('ready');
      disableInputs(false);
      return;
    }

    // Populate review card
    currentPayment = {
      recipient: parsed.recipient || '',
      amount:    parsed.amount || '',
      currency:  parsed.currency || 'USD',
      purpose:   parsed.purpose || '',
      ai_mode:   parsed.ai_mode || 'unknown',
    };

    showReviewCard(currentPayment);
    speak(`I understood: pay ${currentPayment.recipient} ${currentPayment.amount} ${currentPayment.currency} for ${currentPayment.purpose || 'a payment'}. Please review the details.`);
    setStatus('ready');
    disableInputs(false);

  } catch (err) {
    console.error('Parse error:', err);
    showToast('Failed to process request. Please try again.', 'error');
    speak('Something went wrong. Please try again.');
    setStatus('error');
    disableInputs(false);
  }
}

/** Show the AI review card with extracted details */
function showReviewCard(payment) {
  dom.reviewCard.hidden = false;
  dom.reviewRecipient.value = payment.recipient;
  dom.reviewAmount.value = payment.amount;
  dom.reviewPurpose.value = payment.purpose;

  // Set currency
  const currOpt = dom.reviewCurrency.querySelector(`option[value="${payment.currency}"]`);
  if (currOpt) {
    dom.reviewCurrency.value = payment.currency;
  } else {
    dom.reviewCurrency.value = 'USD';
  }

  // Show parsing mode hints
  const _aiLabel = payment.ai_mode === 'openai'   ? 'Parsed by AI — please verify'
                 : payment.ai_mode === 'fallback' ? 'Parsed by rule-based parser — please verify'
                 : 'Inferred — please verify';
  // Update the review-card badge text
  const _aiBadge = dom.reviewCard.querySelector('.ai-badge');
  if (_aiBadge) _aiBadge.textContent = _aiLabel;
  if (payment.recipient) {
    dom.recipientHint.textContent = _aiLabel;
    dom.recipientHint.className = 'field-hint ai-inferred';
  } else {
    dom.recipientHint.textContent = 'Required — please enter a recipient';
    dom.recipientHint.className = 'field-hint';
  }
  if (payment.amount) {
    dom.amountHint.textContent = _aiLabel;
    dom.amountHint.className = 'field-hint ai-inferred';
  } else {
    dom.amountHint.textContent = 'Required — please enter an amount';
    dom.amountHint.className = 'field-hint';
  }

  clearValidation();

  // Scroll into view
  dom.reviewCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/** Validate the review form and open confirmation modal */
function validateAndProceed() {
  const recipient = dom.reviewRecipient.value.trim();
  const amount = parseFloat(dom.reviewAmount.value);
  const currency = dom.reviewCurrency.value;
  const purpose = dom.reviewPurpose.value.trim();

  const errors = [];

  if (!recipient) {
    errors.push('Recipient is required.');
    dom.recipientHint.textContent = 'Required — please enter a recipient';
    dom.recipientHint.className = 'field-hint';
  }
  if (!amount || isNaN(amount) || amount <= 0) {
    errors.push('A valid amount greater than 0 is required.');
    dom.amountHint.textContent = 'Enter a valid positive amount';
    dom.amountHint.className = 'field-hint';
  }
  if (amount > 10000) {
    errors.push('Amount cannot exceed 10,000 for sandbox transactions.');
    dom.amountHint.textContent = 'Maximum 10,000 for sandbox';
    dom.amountHint.className = 'field-hint';
  }

  if (errors.length > 0) {
    dom.validationMsg.hidden = false;
    dom.validationMsg.textContent = errors.join(' ');
    speak('Please fix the highlighted errors.');
    return;
  }

  clearValidation();

  // Update currentPayment with user-edited values
  currentPayment = { recipient, amount, currency, purpose };

  // Show confirmation modal
  openConfirmModal();
}

function clearValidation() {
  dom.validationMsg.hidden = true;
  dom.validationMsg.textContent = '';
}

// ─── CONFIRMATION MODAL ──────────────────────────────────────

function openConfirmModal() {
  dom.confirmRecipient.textContent = currentPayment.recipient;
  dom.confirmAmount.textContent = `${formatCurrency(currentPayment.amount, currentPayment.currency)}`;
  dom.confirmPurpose.textContent = currentPayment.purpose || '—';
  dom.confirmModal.hidden = false;

  // Trap focus
  dom.confirmPayBtn.focus();
  speak(`Please confirm: pay ${currentPayment.recipient} ${currentPayment.amount} ${currentPayment.currency}.`);
}

function closeConfirmModal() {
  dom.confirmModal.hidden = true;
}

// ─── CHECKOUT ────────────────────────────────────────────────

/**
 * Initiate checkout: create order via backend, then show PayPal buttons.
 * In demo mode, simulate the flow with a clear "demo" label.
 */
async function initiateCheckout() {
  closeConfirmModal();

  // Show checkout section
  dom.checkoutSection.hidden = false;
  dom.checkoutSection.scrollIntoView({ behavior: 'smooth', block: 'start' });

  // Update summary
  dom.checkoutSummary.textContent =
    `${formatCurrency(currentPayment.amount, currentPayment.currency)} to ${currentPayment.recipient}`;

  // Reset checkout states
  showCheckoutState('loading');

  if (demoMode) {
    // ── DEMO: simulate checkout after a short delay ──
    await delay(2000);
    showCheckoutState('success');
    dom.successDetails.textContent =
      `[DEMO] ${formatCurrency(currentPayment.amount, currentPayment.currency)} to ${currentPayment.recipient}. No real money was transferred.`;
    speak('Demo payment complete. No real money was transferred.');

    // Add to demo transactions
    addDemoTransaction(currentPayment);
    return;
  }

  // ── LIVE: call backend to create order ──
  try {
    const orderData = await apiPost('/create-order', {
      recipient: currentPayment.recipient,
      amount:    currentPayment.amount,
      currency:  currentPayment.currency,
      purpose:   currentPayment.purpose,
    });

    currentOrderId = orderData.order_id || orderData.id;
    if (!currentOrderId) {
      throw new Error('No order ID received from backend.');
    }

    showCheckoutState('paypal');

    /*
     * ╔══════════════════════════════════════════════════════════════╗
     * ║  PAYPAL SDK INTEGRATION POINT                               ║
     * ║                                                              ║
     * ║  To complete integration:                                    ║
     * ║  1. Add the PayPal JS SDK script tag in index.html:         ║
     * ║     <script src="https://www.paypal.com/sdk/js              ║
     * ║       ?client-id=YOUR_SANDBOX_CLIENT_ID"></script>           ║
     * ║                                                              ║
     * ║  2. Uncomment the renderPayPalButtons() call below.         ║
     * ║                                                              ║
     * ║  The createOrder callback returns the backend-created        ║
     * ║  order ID, and onApprove calls the backend to capture.       ║
     * ╚══════════════════════════════════════════════════════════════╝
     */
    renderPayPalButtons(currentOrderId);

  } catch (err) {
    console.error('Create order error:', err);
    showCheckoutState('failure');
    dom.failureDetails.textContent = err.message || 'Could not create the payment order. Please try again.';
    speak('Payment creation failed. Please try again.');
  }
}

/**
 * Render PayPal SDK buttons inside the container.
 * Requires the PayPal JS SDK to be loaded in the page.
 */
function renderPayPalButtons(orderId) {
  if (typeof paypal === 'undefined') {
    // PayPal SDK not loaded — show helpful message
    dom.paypalContainer.innerHTML = `
      <div style="text-align:center; padding: 2rem; color: #64748b; font-size: 0.875rem;">
        <p style="margin-bottom:0.5rem; font-weight:600;">PayPal SDK not loaded</p>
        <p>Add the PayPal JS SDK script tag to <code>index.html</code> with your Sandbox client ID to enable checkout buttons.</p>
      </div>
    `;
    return;
  }

  dom.paypalContainer.innerHTML = '';

  paypal.Buttons({
    // The order was already created on the backend; return its ID
    createOrder: () => orderId,

    // Capture the order via the backend after PayPal approval
    onApprove: async (data) => {
      showCheckoutState('loading');
      try {
        const captureResult = await apiPost('/capture-order', {
          order_id: data.orderID,
        });

        // Only show success when the backend confirms capture
        if (captureResult.status === 'COMPLETED' || captureResult.success) {
          showCheckoutState('success');
          dom.successDetails.textContent =
            `${formatCurrency(currentPayment.amount, currentPayment.currency)} to ${currentPayment.recipient} — Payment captured successfully.`;
          speak('Payment completed successfully!');
          loadTransactions(); // Refresh list
        } else {
          throw new Error(captureResult.message || 'Payment was not completed.');
        }
      } catch (err) {
        console.error('Capture error:', err);
        showCheckoutState('failure');
        dom.failureDetails.textContent = err.message || 'Payment capture failed.';
        speak('Payment capture failed. Please try again.');
      }
    },

    onCancel: () => {
      showToast('Payment was cancelled.', 'info');
      speak('Payment cancelled.');
      showCheckoutState('paypal');
    },

    onError: (err) => {
      console.error('PayPal error:', err);
      showCheckoutState('failure');
      dom.failureDetails.textContent = 'An error occurred with PayPal. Please try again.';
      speak('A PayPal error occurred.');
    },
  }).render('#paypal-button-container');
}

// ─── CHECKOUT STATE MANAGEMENT ────────────────────────────────
function showCheckoutState(state) {
  // Show only the selected checkout state
  dom.checkoutLoading.hidden = state !== 'loading';
  dom.checkoutSuccess.hidden = state !== 'success';
  dom.checkoutFailure.hidden = state !== 'failure';

  // Show PayPal buttons only during checkout
  dom.paypalContainer.style.display =
    state === 'paypal' ? 'block' : 'none';

  // Show cancel button except after successful payment
  dom.cancelCheckoutBtn.style.display =
    state === 'success' ? 'none' : '';

  // Prevent repeated confirmation while processing
  dom.confirmPayBtn.disabled = state === 'loading';

  // Clear old messages when starting a new state
  if (state === 'loading' || state === 'paypal') {
    dom.successDetails.textContent = '';
    dom.failureDetails.textContent = '';
  }
}

// ─── TRANSACTIONS ────────────────────────────────────────────

/** Load transaction history from backend or show demo data */
async function loadTransactions() {
  dom.transactionsLoading.hidden = false;
  dom.emptyState.hidden = true;
  dom.transactionsList.innerHTML = '';

  if (demoMode) {
    dom.transactionsLoading.hidden = true;
    const demoTxns = getDemoTransactions();
    if (demoTxns.length === 0) {
      dom.emptyState.hidden = false;
    } else {
      dom.demoNotice.hidden = false;
      renderTransactions(demoTxns);
    }
    return;
  }

  try {
    const txns = await apiGet('/transactions');
    dom.transactionsLoading.hidden = true;

    if (!txns || txns.length === 0) {
      dom.emptyState.hidden = false;
    } else {
      dom.demoNotice.hidden = true;
      renderTransactions(txns);
    }
  } catch (err) {
    console.error('Transactions fetch error:', err);
    dom.transactionsLoading.hidden = true;
    dom.emptyState.hidden = false;

    // Fall back to demo data
    const demoTxns = getDemoTransactions();
    if (demoTxns.length > 0) {
      dom.demoNotice.hidden = false;
      renderTransactions(demoTxns);
      dom.emptyState.hidden = true;
    }
  }
}

/** Render transaction cards */
function renderTransactions(txns) {
  dom.transactionsList.innerHTML = txns.map(txn => {
    const statusClass = (txn.status || 'completed').toLowerCase();
    const iconName = statusClass === 'completed' ? 'check-circle-2'
                   : statusClass === 'pending'   ? 'clock'
                   : 'alert-circle';
    return `
      <div class="txn-card">
        <div class="txn-icon ${statusClass}">
          <i data-lucide="${iconName}"></i>
        </div>
        <div class="txn-info">
          <div class="txn-recipient">${escapeHtml(txn.recipient || txn.merchant || '—')}</div>
          <div class="txn-date">${formatDate(txn.date)}</div>
        </div>
        <div class="txn-right">
          <div class="txn-amount">${formatCurrency(txn.amount, txn.currency || 'USD')}</div>
          <span class="txn-status ${statusClass}">${capitalize(statusClass)}</span>
        </div>
      </div>
    `;
  }).join('');

  // Re-init Lucide icons for the new elements
  initLucide();
}

// ─── API HELPERS ─────────────────────────────────────────────

/**
 * POST to the backend API.
 * Centralised so endpoint adjustments are easy.
 */
async function apiPost(path, body) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let errorMsg = `Server error (${response.status})`;
    try {
      const errData = await response.json();
      errorMsg = errData.detail || errData.message || errorMsg;
    } catch (_) { /* ignore parse error */ }
    throw new Error(errorMsg);
  }

  return response.json();
}

/** GET from the backend API. */
async function apiGet(path) {
  // Include session token if stored (from /session/start)
  const headers = {};
  const sessionToken = sessionStorage.getItem('vp_session_token');
  if (sessionToken) {
    headers['X-Session-Token'] = sessionToken;
  }

  const response = await fetch(`${API_BASE}${path}`, { headers });

  if (!response.ok) {
    throw new Error(`Server error (${response.status})`);
  }

  return response.json();
}

/**
 * Check if the backend is available.
 * If reachable, disable demo mode automatically.
 */
async function checkBackendAvailability() {
  try {
    const resp = await fetch(`${API_BASE}/`, { method: 'GET', signal: AbortSignal.timeout(3000) });
    if (resp.ok) {
      demoMode = false;
      console.log('✓ Backend connected — live mode');
    }
  } catch (e) {
    demoMode = true;
    console.log('ℹ Backend unavailable — running in demo mode');
    showToast('Backend not connected. Running in Demo Mode.', 'info');
  }
}

// ─── DEMO / MOCK DATA ────────────────────────────────────────

/**
 * Mock payment parser — extracts details from natural language.
 * Only used in demo mode.
 */
function mockParsePayment(message) {
  const lower = message.toLowerCase();

  // Try to extract amount (e.g., "10 dollars", "$24.99", "50 usd")
  let amount = null;
  const amountMatch = lower.match(/\$?([\d]+(?:\.[\d]{1,2})?)\s*(?:dollars?|usd|eur|gbp|inr)?/);
  if (amountMatch) {
    amount = parseFloat(amountMatch[1]);
  }

  // Try to extract currency
  let currency = 'USD';
  if (/eur|euros?/i.test(lower))  currency = 'EUR';
  if (/gbp|pounds?/i.test(lower)) currency = 'GBP';
  if (/inr|rupees?/i.test(lower)) currency = 'INR';
  if (/cad/i.test(lower))         currency = 'CAD';
  if (/aud/i.test(lower))         currency = 'AUD';

  // Try to extract recipient (word after "pay")
  let recipient = '';
  const recipientMatch = message.match(/pay\s+([A-Z][a-zA-Z\s]*?)(?:\s+\$?[\d]|\s+for\s|\s*$)/i);
  if (recipientMatch) {
    recipient = recipientMatch[1].trim();
  }

  // Try to extract purpose (after "for")
  let purpose = '';
  const purposeMatch = message.match(/for\s+(.+)/i);
  if (purposeMatch) {
    purpose = purposeMatch[1].trim().replace(/\.$/, '');
  }

  return { recipient, amount, currency, purpose };
}

/** In-memory demo transactions */
let demoTransactionStore = [];

function getDemoTransactions() {
  return demoTransactionStore;
}

function addDemoTransaction(payment) {
  demoTransactionStore.unshift({
    date: new Date().toISOString(),
    recipient: payment.recipient,
    amount: payment.amount,
    currency: payment.currency,
    status: 'Completed',
    purpose: payment.purpose,
  });
  loadTransactions();
}

// ─── TEXT-TO-SPEECH ──────────────────────────────────────────

/**
 * Speak a message using the Web Speech API if available.
 * Gracefully does nothing if TTS is unsupported.
 */
function speak(text) {
  if (!('speechSynthesis' in window)) return;

  // Cancel any queued speech
  window.speechSynthesis.cancel();

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.05;
  utterance.pitch = 1;
  utterance.volume = 0.8;

  // Try to use the language matching the selected recognition language
  const lang = dom.langSelect.value;
  utterance.lang = lang;

  window.speechSynthesis.speak(utterance);
}

// ─── UI HELPERS ──────────────────────────────────────────────

/** Set the status indicator state: 'ready' | 'listening' | 'processing' | 'error' */
function setStatus(state) {
  dom.statusDot.className = `status-dot ${state}`;
  const labels = {
    ready:      'Ready',
    listening:  'Listening…',
    processing: 'Processing…',
    error:      'Error',
  };
  dom.statusText.textContent = labels[state] || 'Ready';
}

/** Disable/enable input controls during processing */
function disableInputs(disabled) {
  dom.micBtn.disabled = disabled;
  dom.sendBtn.disabled = disabled;
  dom.textInput.disabled = disabled;
  $$('.chip').forEach(c => c.disabled = disabled);
}

/** Reset the entire payment flow to the initial state */
function resetPaymentFlow() {
  currentPayment = null;
  currentOrderId = null;

  dom.reviewCard.hidden = true;
  dom.confirmModal.hidden = true;
  dom.checkoutSection.hidden = true;
  dom.textInput.value = '';
  dom.transcriptionArea.hidden = true;
  dom.transcriptionText.textContent = '';

  // Reset checkout sub-states
  dom.checkoutLoading.hidden = true;
  dom.checkoutSuccess.hidden = true;
  dom.checkoutFailure.hidden = true;
  dom.paypalContainer.style.display = 'block';
  dom.paypalContainer.innerHTML = '';

  clearValidation();
  setStatus('ready');
  disableInputs(false);

  // Scroll back to payment section
  document.getElementById('payment-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Show a toast notification */
function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;

  const iconName = type === 'success' ? 'check-circle-2'
                 : type === 'error'   ? 'alert-circle'
                 : 'info';

  toast.innerHTML = `<i data-lucide="${iconName}"></i><span>${escapeHtml(message)}</span>`;
  dom.toastContainer.appendChild(toast);

  // Render icon
  if (window.lucide) lucide.createIcons({ nodes: [toast] });

  // Auto-remove after 5 seconds
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(20px)';
    toast.style.transition = 'opacity 0.3s, transform 0.3s';
    setTimeout(() => toast.remove(), 300);
  }, 5000);
}

// ─── FORMATTING UTILITIES ────────────────────────────────────

function formatCurrency(amount, currency = 'USD') {
  try {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: currency,
    }).format(amount);
  } catch (_) {
    return `${currency} ${parseFloat(amount).toFixed(2)}`;
  }
}

function formatDate(dateStr) {
  if (!dateStr) return '—';
  try {
    return new Intl.DateTimeFormat('en-US', {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(dateStr));
  } catch (_) {
    return dateStr;
  }
}

function capitalize(str) {
  return str.charAt(0).toUpperCase() + str.slice(1);
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ─── SHOPPING → PAYMENT BRIDGE ───────────────────────────────
// Listen for the custom event dispatched by shopping.js when the user
// selects a product and taps "Pay for This Product".
// This runs after DOMContentLoaded so we register it separately.
window.addEventListener('load', () => {
  document.addEventListener('voicepay:shopping-checkout', (e) => {
    const { recipient, amount, currency, purpose, ai_mode } = e.detail || {};
    if (!recipient || !amount) return;

    // Pre-fill currentPayment with the product's details
    currentPayment = {
      recipient,
      amount: Number(amount),
      currency: currency || 'USD',
      purpose: purpose || '',
      ai_mode: ai_mode || 'shopping_agent',
    };

    // Show the review card so the user can confirm before checkout
    showReviewCard(currentPayment);

    // Scroll to the VoicePay section
    document.getElementById('payment-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });

    speak(`Ready to pay ${formatCurrency(amount, currency || 'USD')} for ${purpose || recipient}. Please review the details below.`);
  });
});

// ─── ENHANCED TRANSACTION RENDERING ──────────────────────────
// Override renderTransactions to show richer card data (order_id, capture_id, sandbox badge)
// Only applied when backend provides these fields.
const _origRenderTransactions = typeof renderTransactions === 'function' ? renderTransactions : null;

function renderTransactions(txns) {
  dom.transactionsList.innerHTML = txns.map(txn => {
    const statusRaw = (txn.status || 'completed').toLowerCase();
    const statusClass = statusRaw === 'completed' ? 'completed'
                      : statusRaw === 'pending'   ? 'pending'
                      : 'failed';
    const iconName = statusClass === 'completed' ? 'check-circle-2'
                   : statusClass === 'pending'   ? 'clock'
                   : 'alert-circle';
    const isDemo = txn.demo === true;
    const shortOrderId = txn.order_id ? txn.order_id.slice(0, 12) + '…' : '';
    const shortCaptureId = txn.capture_id ? txn.capture_id.slice(0, 12) + '…' : '';
    return `
      <div class="txn-card" role="listitem">
        <div class="txn-icon ${statusClass}">
          <i data-lucide="${iconName}"></i>
        </div>
        <div class="txn-info">
          <div class="txn-recipient">${escapeHtml(txn.recipient || txn.merchant || '—')}</div>
          ${txn.purpose ? `<div class="txn-purpose">${escapeHtml(txn.purpose)}</div>` : ''}
          <div class="txn-date">${formatDate(txn.date)}</div>
          ${shortOrderId ? `<div class="txn-id">Order: <code>${escapeHtml(shortOrderId)}</code></div>` : ''}
          ${shortCaptureId ? `<div class="txn-id">Capture: <code>${escapeHtml(shortCaptureId)}</code></div>` : ''}
        </div>
        <div class="txn-right">
          <div class="txn-amount">${formatCurrency(txn.amount, txn.currency || 'USD')}</div>
          <span class="txn-status ${statusClass}">${capitalize(statusClass)}</span>
          <span class="txn-sandbox-badge">
            <i data-lucide="test-tube-diagonal"></i> Sandbox
          </span>
          ${isDemo ? '<span class="txn-demo-badge">Demo</span>' : ''}
        </div>
      </div>
    `;
  }).join('');

  // Re-init Lucide icons for new elements
  initLucide();
}

// ─── SESSION NOTICE INTEGRATION ──────────────────────────────
// Show session input if /transactions returns 401/403
const _origLoadTransactions = loadTransactions;
async function loadTransactions() {
  dom.transactionsLoading.hidden = false;
  dom.emptyState.hidden = true;
  dom.transactionsList.innerHTML = '';
  const sessionNotice = document.getElementById('sessionNotice');

  if (demoMode) {
    dom.transactionsLoading.hidden = true;
    const demoTxns = getDemoTransactions();
    if (demoTxns.length === 0) {
      dom.emptyState.hidden = false;
    } else {
      dom.demoNotice.hidden = false;
      renderTransactions(demoTxns);
    }
    return;
  }

  try {
    const headers = {};
    const sessionToken = sessionStorage.getItem('vp_session_token');
    if (sessionToken) headers['X-Session-Token'] = sessionToken;

    const response = await fetch(`${API_BASE}/transactions`, { headers });

    if (response.status === 401 || response.status === 403) {
      dom.transactionsLoading.hidden = true;
      dom.emptyState.hidden = true;
      if (sessionNotice) sessionNotice.hidden = false;
      return;
    }

    if (!response.ok) throw new Error(`Server error (${response.status})`);
    const txns = await response.json();
    dom.transactionsLoading.hidden = true;
    if (sessionNotice) sessionNotice.hidden = true;

    if (!txns || txns.length === 0) {
      dom.emptyState.hidden = false;
    } else {
      dom.demoNotice.hidden = true;
      renderTransactions(txns);
    }
  } catch (err) {
    console.error('Transactions fetch error:', err);
    dom.transactionsLoading.hidden = true;
    const demoTxns = getDemoTransactions();
    if (demoTxns.length > 0) {
      dom.demoNotice.hidden = false;
      renderTransactions(demoTxns);
      dom.emptyState.hidden = true;
    } else {
      dom.emptyState.hidden = false;
    }
  }
}
