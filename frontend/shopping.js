/* ═══════════════════════════════════════════════════════════════
   VoicePay AI — AI Shopping Agent Module
   Handles product search, comparison, and selection.
   Communicates with backend /parse-shopping and /search-products.
   Integrates with the main app.js payment flow.
   ═══════════════════════════════════════════════════════════════ */

'use strict';

// ─── SHOPPING STATE ──────────────────────────────────────────
const ShoppingAgent = {
  currentSearch: null,       // Last parsed shopping request
  currentProducts: [],       // Current product list
  selectedProduct: null,     // Selected product for checkout
  shoppingMicActive: false,  // Is shopping voice input active?
  shoppingRecognition: null, // SpeechRecognition for shopping input
  conversationHistory: [],   // Chat history for follow-ups
};

// ─── DOM SHORTCUTS ───────────────────────────────────────────
const $s = (sel) => document.querySelector(sel);

const shopDom = {
  shoppingInput:        $s('#shoppingInput'),
  shoppingSearchBtn:    $s('#shoppingSearchBtn'),
  shopMicBtn:           $s('#shopMicBtn'),
  shopTranscriptionArea:$s('#shopTranscriptionArea'),
  shopTranscriptionText:$s('#shopTranscriptionText'),
  shoppingStatus:       $s('#shoppingStatus'),
  shoppingStatusText:   $s('#shoppingStatusText'),

  parsedPrefsCard:      $s('#parsedPrefsCard'),
  parsedPrefsGrid:      $s('#parsedPrefsGrid'),
  shoppingAiMode:       $s('#shoppingAiMode'),
  refineSearchBtn:      $s('#refineSearchBtn'),

  shoppingError:        $s('#shoppingError'),
  shoppingErrorTitle:   $s('#shoppingErrorTitle'),
  shoppingErrorMsg:     $s('#shoppingErrorMsg'),
  shoppingRetryBtn:     $s('#shoppingRetryBtn'),

  productsSection:      $s('#productsSection'),
  productsHeading:      $s('#productsHeading'),
  productsGrid:         $s('#productsGrid'),

  overBudgetSection:    $s('#overBudgetSection'),
  overBudgetGrid:       $s('#overBudgetGrid'),

  followupPanel:        $s('#followupPanel'),
  followupInput:        $s('#followupInput'),
  followupSendBtn:      $s('#followupSendBtn'),

  selectedProductPanel: $s('#selectedProductPanel'),
  selectedProductBody:  $s('#selectedProductBody'),
  clearSelectedProductBtn: $s('#clearSelectedProductBtn'),
  proceedToPayBtn:      $s('#proceedToPayBtn'),
  backToProductsBtn:    $s('#backToProductsBtn'),

  heroShopBtn:          $s('#heroShopBtn'),
  heroPayBtn:           $s('#heroPayBtn'),
  viewTransactionsBtn:  $s('#viewTransactionsBtn'),
  refreshTransactionsBtn: $s('#refreshTransactionsBtn'),
  sessionNotice:        $s('#sessionNotice'),
  sessionTokenInput:    $s('#sessionTokenInput'),
  sessionSubmitBtn:     $s('#sessionSubmitBtn'),
};

// ─── INITIALIZATION ──────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initShoppingEventListeners();
  initShoppingVoice();
  restoreSessionToken();
});

function initShoppingEventListeners() {
  // Search button
  shopDom.shoppingSearchBtn?.addEventListener('click', () => {
    const query = shopDom.shoppingInput?.value.trim();
    if (query) runShoppingSearch(query);
    else showToastSafe('Please describe what you are looking for.', 'info');
  });

  // Shopping textarea — Enter submits (without Shift)
  shopDom.shoppingInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const query = shopDom.shoppingInput.value.trim();
      if (query) runShoppingSearch(query);
    }
  });

  // Shopping mic button
  shopDom.shopMicBtn?.addEventListener('click', toggleShoppingMic);

  // Example shopping chips
  document.querySelectorAll('[data-shop-example]').forEach(chip => {
    chip.addEventListener('click', () => {
      const example = chip.dataset.shopExample;
      if (shopDom.shoppingInput) shopDom.shoppingInput.value = example;
      runShoppingSearch(example);
    });
  });

  // Refine search
  shopDom.refineSearchBtn?.addEventListener('click', () => {
    if (shopDom.shoppingInput) {
      shopDom.shoppingInput.focus();
      shopDom.shoppingInput.select();
    }
    shopDom.parsedPrefsCard.hidden = true;
  });

  // Retry
  shopDom.shoppingRetryBtn?.addEventListener('click', () => {
    shopDom.shoppingError.hidden = true;
    if (shopDom.shoppingInput) shopDom.shoppingInput.focus();
  });

  // Follow-up
  shopDom.followupSendBtn?.addEventListener('click', sendFollowup);
  shopDom.followupInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendFollowup();
  });
  document.querySelectorAll('[data-followup]').forEach(chip => {
    chip.addEventListener('click', () => {
      const followup = chip.dataset.followup;
      if (shopDom.followupInput) shopDom.followupInput.value = followup;
      sendFollowup();
    });
  });

  // Selected product actions
  shopDom.clearSelectedProductBtn?.addEventListener('click', clearSelectedProduct);
  shopDom.proceedToPayBtn?.addEventListener('click', proceedToPayFromProduct);
  shopDom.backToProductsBtn?.addEventListener('click', () => {
    shopDom.selectedProductPanel.hidden = true;
    if (shopDom.productsSection) shopDom.productsSection.hidden = false;
  });

  // Hero CTAs — scroll to sections
  shopDom.heroShopBtn?.addEventListener('click', (e) => {
    e.preventDefault();
    document.getElementById('shopping-section')?.scrollIntoView({ behavior: 'smooth' });
    // Update nav active state
    setNavActive('shopping');
  });
  shopDom.heroPayBtn?.addEventListener('click', (e) => {
    e.preventDefault();
    document.getElementById('payment-section')?.scrollIntoView({ behavior: 'smooth' });
    setNavActive('pay');
  });

  // View transactions after payment
  shopDom.viewTransactionsBtn?.addEventListener('click', () => {
    document.getElementById('transactions-section')?.scrollIntoView({ behavior: 'smooth' });
    if (typeof loadTransactions === 'function') loadTransactions();
  });

  // Refresh transactions
  shopDom.refreshTransactionsBtn?.addEventListener('click', () => {
    if (typeof loadTransactions === 'function') loadTransactions();
  });

  // Session token for transactions
  shopDom.sessionSubmitBtn?.addEventListener('click', submitSessionToken);
  shopDom.sessionTokenInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitSessionToken();
  });
}

// ─── SHOPPING VOICE INPUT ────────────────────────────────────

function initShoppingVoice() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    if (shopDom.shopMicBtn) shopDom.shopMicBtn.disabled = true;
    return;
  }

  ShoppingAgent.shoppingRecognition = new SpeechRecognition();
  ShoppingAgent.shoppingRecognition.continuous = false;
  ShoppingAgent.shoppingRecognition.interimResults = true;
  ShoppingAgent.shoppingRecognition.lang = 'en-US';

  ShoppingAgent.shoppingRecognition.onstart = () => {
    ShoppingAgent.shoppingMicActive = true;
    shopDom.shopMicBtn?.classList.add('listening');
    if (shopDom.shopTranscriptionArea) shopDom.shopTranscriptionArea.hidden = false;
    if (shopDom.shopTranscriptionText) shopDom.shopTranscriptionText.textContent = 'Listening…';
  };

  ShoppingAgent.shoppingRecognition.onresult = (event) => {
    let interim = '';
    let final = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const t = event.results[i][0].transcript;
      if (event.results[i].isFinal) final += t;
      else interim += t;
    }
    if (shopDom.shopTranscriptionText) {
      shopDom.shopTranscriptionText.textContent = final || interim;
    }
    if (final) {
      if (shopDom.shoppingInput) shopDom.shoppingInput.value = final.trim();
      runShoppingSearch(final.trim());
    }
  };

  ShoppingAgent.shoppingRecognition.onerror = (event) => {
    stopShoppingMic();
    if (event.error === 'not-allowed') {
      showToastSafe('Microphone access denied. Please type your search instead.', 'error');
    } else if (event.error === 'no-speech') {
      showToastSafe('No speech detected. Try again or type your request.', 'info');
    }
  };

  ShoppingAgent.shoppingRecognition.onend = () => stopShoppingMic();
}

function toggleShoppingMic() {
  if (ShoppingAgent.shoppingMicActive) {
    stopShoppingMic();
  } else {
    if (!ShoppingAgent.shoppingRecognition) {
      showToastSafe('Voice input is not supported in this browser.', 'error');
      return;
    }
    const langSelect = document.getElementById('langSelect');
    ShoppingAgent.shoppingRecognition.lang = langSelect?.value || 'en-US';
    try {
      ShoppingAgent.shoppingRecognition.start();
    } catch (e) {
      console.warn('Shopping recognition start error:', e);
    }
  }
}

function stopShoppingMic() {
  ShoppingAgent.shoppingMicActive = false;
  shopDom.shopMicBtn?.classList.remove('listening');
  try { ShoppingAgent.shoppingRecognition?.stop(); } catch (e) { /* ignore */ }
}

// ─── MAIN SEARCH FLOW ────────────────────────────────────────

async function runShoppingSearch(query) {
  if (!query) return;

  // Reset previous results
  hideShoppingResults();
  setShoppingStatus(true, 'Understanding your request…');

  // Disable search button during search
  if (shopDom.shoppingSearchBtn) shopDom.shoppingSearchBtn.disabled = true;

  // Add to conversation history
  ShoppingAgent.conversationHistory.push({ role: 'user', content: query });

  try {
    // Step 1: Parse the shopping request
    let parsed;
    if (typeof demoMode !== 'undefined' && demoMode) {
      parsed = mockParseShoppingRequest(query);
    } else {
      parsed = await apiPostSafe('/parse-shopping', { message: query });
    }

    ShoppingAgent.currentSearch = parsed;

    // Show parsed preferences
    showParsedPreferences(parsed);

    // Validate budget was found
    if (!parsed.budget || parsed.budget <= 0) {
      setShoppingStatus(false);
      showShoppingError(
        'Budget not found',
        'Please include your budget in your request. For example: "I have $100 to spend on headphones."'
      );
      if (shopDom.shoppingSearchBtn) shopDom.shoppingSearchBtn.disabled = false;
      return;
    }

    setShoppingStatus(true, 'Searching product catalog…');

    // Step 2: Search products
    let searchResult;
    if (typeof demoMode !== 'undefined' && demoMode) {
      searchResult = mockSearchProducts(parsed);
    } else {
      searchResult = await apiPostSafe('/search-products', {
        budget: parsed.budget,
        currency: parsed.currency || 'USD',
        category: parsed.category || null,
        query: parsed.query || query,
        preferences: parsed.preferences || [],
        max_results: 3,
      });
    }

    setShoppingStatus(false);

    if (!searchResult.products || searchResult.products.length === 0) {
      // Check if there are over-budget alternatives
      if (searchResult.over_budget_alternatives && searchResult.over_budget_alternatives.length > 0) {
        showShoppingError(
          `No products found within your $${parsed.budget.toFixed(2)} budget`,
          `All ${searchResult.total_searched || 0} products in this category exceed your budget (including estimated tax). ` +
          `Consider increasing your budget or try a different category.`
        );
        // Still show over-budget alternatives
        renderOverBudgetAlternatives(searchResult.over_budget_alternatives);
      } else {
        showShoppingError(
          'No products found',
          parsed.category
            ? `No ${parsed.category} products found within your $${parsed.budget.toFixed(2)} budget.`
            : `No products found matching your request within your budget. Try a different category or increase your budget.`
        );
      }
    } else {
      ShoppingAgent.currentProducts = searchResult.products;
      renderProductRecommendations(searchResult, parsed);
      showFollowupPanel();
    }

    // Add AI response to history
    const count = searchResult.products?.length || 0;
    ShoppingAgent.conversationHistory.push({
      role: 'assistant',
      content: `Found ${count} product${count !== 1 ? 's' : ''} within your budget.`,
    });

  } catch (err) {
    setShoppingStatus(false);
    console.error('Shopping search error:', err);
    showShoppingError('Search failed', err.message || 'Could not complete the search. Please try again.');
    ShoppingAgent.conversationHistory.push({
      role: 'assistant',
      content: `Search failed: ${err.message}`,
    });
  }

  if (shopDom.shoppingSearchBtn) shopDom.shoppingSearchBtn.disabled = false;
}

// ─── FOLLOW-UP HANDLING ──────────────────────────────────────

async function sendFollowup() {
  const followupText = shopDom.followupInput?.value.trim();
  if (!followupText) return;

  if (shopDom.followupInput) shopDom.followupInput.value = '';

  const currentQuery = ShoppingAgent.currentSearch;
  if (!currentQuery) return;

  // Build a refined query based on follow-up
  let refinedQuery = followupText;

  // Augment with context from current search
  const lowerFollowup = followupText.toLowerCase();
  if (lowerFollowup.includes('cheaper') || lowerFollowup.includes('lower price') || lowerFollowup.includes('budget')) {
    // Reduce budget by 20%
    const newBudget = (currentQuery.budget * 0.8).toFixed(2);
    refinedQuery = `${followupText}. Budget is now $${newBudget}. Category: ${currentQuery.category || 'any'}.`;
    if (currentQuery) currentQuery.budget = parseFloat(newBudget);
  } else if (lowerFollowup.includes('better battery') || lowerFollowup.includes('longer battery')) {
    refinedQuery = `Find ${currentQuery.category || 'products'} with the best battery life within $${currentQuery.budget}. ${followupText}`;
    if (currentQuery.preferences) {
      currentQuery.preferences = [...new Set([...currentQuery.preferences, 'battery'])];
    }
  } else if (lowerFollowup.includes('compare')) {
    // Show a comparison view
    showProductComparison();
    return;
  } else {
    // Generic: append to current context
    refinedQuery = `${followupText}. Previous context: ${currentQuery.query || ''} with budget $${currentQuery.budget}.`;
  }

  await runShoppingSearch(refinedQuery);
}

function showProductComparison() {
  if (ShoppingAgent.currentProducts.length < 2) {
    showToastSafe('Need at least 2 products to compare.', 'info');
    return;
  }
  // Scroll to products and highlight
  shopDom.productsGrid?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  showToastSafe('Comparing the top products — check the cards below.', 'info');
}

// ─── RENDER FUNCTIONS ─────────────────────────────────────────

function showParsedPreferences(parsed) {
  if (!shopDom.parsedPrefsCard) return;
  shopDom.parsedPrefsCard.hidden = false;

  const modeLabel = parsed.ai_mode === 'openai' ? 'AI Parsed' :
                    parsed.ai_mode === 'fallback' ? 'Rule-based parser' : 'Demo mode';
  if (shopDom.shoppingAiMode) shopDom.shoppingAiMode.textContent = modeLabel;

  const prefs = [];
  if (parsed.budget) prefs.push({ label: 'Budget', value: `$${Number(parsed.budget).toFixed(2)} ${parsed.currency || 'USD'}`, icon: 'dollar-sign' });
  if (parsed.category) prefs.push({ label: 'Category', value: capitalizeWords(parsed.category), icon: 'tag' });
  if (parsed.brand) prefs.push({ label: 'Brand', value: parsed.brand, icon: 'award' });
  if (parsed.preferences?.length) prefs.push({ label: 'Preferences', value: parsed.preferences.slice(0, 4).join(', '), icon: 'sliders' });
  if (parsed.query) prefs.push({ label: 'Search', value: truncate(parsed.query, 60), icon: 'search' });

  if (shopDom.parsedPrefsGrid) {
    shopDom.parsedPrefsGrid.innerHTML = prefs.map(p => `
      <div class="pref-chip">
        <i data-lucide="${p.icon}" aria-hidden="true"></i>
        <span class="pref-label">${escapeHtmlSafe(p.label)}:</span>
        <span class="pref-value">${escapeHtmlSafe(p.value)}</span>
      </div>
    `).join('');
    if (window.lucide) lucide.createIcons({ nodes: [shopDom.parsedPrefsGrid] });
  }
}

function renderProductRecommendations(searchResult, parsed) {
  if (!shopDom.productsSection) return;

  const products = searchResult.products || [];
  shopDom.productsSection.hidden = false;

  const budget = parsed.budget || 0;
  const currency = parsed.currency || 'USD';

  if (shopDom.productsHeading) {
    shopDom.productsHeading.textContent =
      `${products.length} Product${products.length !== 1 ? 's' : ''} Found Within Your $${Number(budget).toFixed(2)} Budget`;
  }

  if (shopDom.productsGrid) {
    shopDom.productsGrid.innerHTML = products.map((product, index) =>
      renderProductCard(product, budget, currency, index)
    ).join('');

    // Attach event listeners to product cards
    shopDom.productsGrid.querySelectorAll('[data-select-product]').forEach(btn => {
      btn.addEventListener('click', () => {
        const productId = btn.dataset.selectProduct;
        const product = products.find(p => p.id === productId);
        if (product) selectProduct(product, budget, currency);
      });
    });

    shopDom.productsGrid.querySelectorAll('[data-view-product]').forEach(btn => {
      btn.addEventListener('click', () => {
        const productId = btn.dataset.viewProduct;
        const product = products.find(p => p.id === productId);
        if (product) showProductDetail(product, budget, currency);
      });
    });

    if (window.lucide) lucide.createIcons({ nodes: [shopDom.productsGrid] });
  }

  // Over-budget alternatives
  if (searchResult.over_budget_alternatives?.length > 0) {
    renderOverBudgetAlternatives(searchResult.over_budget_alternatives);
  } else if (shopDom.overBudgetSection) {
    shopDom.overBudgetSection.hidden = true;
  }

  shopDom.productsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderProductCard(product, budget, currency, index) {
  const label = product.comparison_label || '';
  const labelMap = {
    best_overall: { text: '🏆 Best Overall', cls: 'badge-best-overall' },
    best_budget:  { text: '💰 Best Budget',  cls: 'badge-best-budget'  },
    best_features:{ text: '⚡ Best Features', cls: 'badge-best-features' },
  };
  const badge = labelMap[label] || null;

  const price = Number(product.price || 0);
  const tax   = Number(product.estimated_tax || 0);
  const ship  = Number(product.estimated_shipping || 0);
  const total = Number(product.total_estimated || price);
  const remaining = Number(budget) - total;
  const withinBudget = remaining >= 0;

  const specsHtml = product.specs
    ? Object.entries(product.specs).slice(0, 4).map(([k, v]) =>
        `<div class="spec-row"><span class="spec-key">${escapeHtmlSafe(k)}</span><span class="spec-val">${escapeHtmlSafe(String(v))}</span></div>`
      ).join('')
    : '';

  const prosHtml = (product.pros || []).slice(0, 3).map(p =>
    `<li><i data-lucide="check" aria-hidden="true"></i>${escapeHtmlSafe(p)}</li>`
  ).join('');
  const consHtml = (product.cons || []).slice(0, 2).map(c =>
    `<li><i data-lucide="x" aria-hidden="true"></i>${escapeHtmlSafe(c)}</li>`
  ).join('');

  return `
    <div class="product-card${badge ? ' product-card-featured' : ''}" data-product-id="${escapeHtmlSafe(product.id)}">
      ${badge ? `<div class="product-badge ${badge.cls}">${badge.text}</div>` : ''}
      <div class="product-card-header">
        <div class="product-emoji" aria-hidden="true">${product.image_emoji || '📦'}</div>
        <div class="product-title-group">
          <div class="product-brand">${escapeHtmlSafe(product.brand || '')}</div>
          <h4 class="product-name">${escapeHtmlSafe(product.name)}</h4>
          <div class="product-merchant">
            <i data-lucide="store" aria-hidden="true"></i>
            ${escapeHtmlSafe(product.merchant || 'Demo Store')}
          </div>
        </div>
      </div>

      <div class="product-pricing">
        <div class="product-price-main">
          <span class="product-price">${formatCurrencySafe(price, 'USD')}</span>
        </div>
        <div class="product-price-breakdown">
          <div class="price-row">
            <span>Est. tax (8%)</span>
            <span>+${formatCurrencySafe(tax, 'USD')}</span>
          </div>
          ${ship > 0 ? `<div class="price-row"><span>Shipping</span><span>+${formatCurrencySafe(ship, 'USD')}</span></div>` : '<div class="price-row free-shipping"><i data-lucide="truck" aria-hidden="true"></i><span>Free Shipping</span><span>$0.00</span></div>'}
          <div class="price-row price-total">
            <strong>Est. Total</strong>
            <strong>${formatCurrencySafe(total, 'USD')}</strong>
          </div>
        </div>
        <div class="budget-remaining ${withinBudget ? 'within' : 'over'}">
          <i data-lucide="${withinBudget ? 'check-circle' : 'alert-circle'}" aria-hidden="true"></i>
          ${withinBudget
            ? `$${Math.abs(remaining).toFixed(2)} remaining from your budget`
            : `$${Math.abs(remaining).toFixed(2)} over your budget`}
        </div>
      </div>

      ${specsHtml ? `<div class="product-specs">${specsHtml}</div>` : ''}

      ${product.recommendation_reason ? `
        <div class="product-reason">
          <i data-lucide="sparkles" aria-hidden="true"></i>
          <span>${escapeHtmlSafe(product.recommendation_reason)}</span>
        </div>
      ` : ''}

      ${(prosHtml || consHtml) ? `
        <div class="product-pros-cons">
          ${prosHtml ? `<ul class="pros-list">${prosHtml}</ul>` : ''}
          ${consHtml ? `<ul class="cons-list">${consHtml}</ul>` : ''}
        </div>
      ` : ''}

      <div class="product-data-note">
        <i data-lucide="info" aria-hidden="true"></i>
        Demo catalog — prices &amp; availability for demonstration only
      </div>

      <div class="product-actions">
        <button class="btn btn-primary" data-select-product="${escapeHtmlSafe(product.id)}" type="button">
          <i data-lucide="shopping-cart" aria-hidden="true"></i>
          Select Product
        </button>
        <button class="btn btn-outline btn-sm" data-view-product="${escapeHtmlSafe(product.id)}" type="button">
          <i data-lucide="eye" aria-hidden="true"></i>
          View Details
        </button>
      </div>
    </div>
  `;
}

function renderOverBudgetAlternatives(products) {
  if (!shopDom.overBudgetSection || !products?.length) return;
  shopDom.overBudgetSection.hidden = false;

  if (shopDom.overBudgetGrid) {
    shopDom.overBudgetGrid.innerHTML = products.map(p => `
      <div class="product-card product-card-over-budget">
        <div class="product-over-budget-note">
          <i data-lucide="trending-up" aria-hidden="true"></i>
          ${formatCurrencySafe(p.total_estimated || p.price, 'USD')} estimated total
        </div>
        <div class="product-card-header">
          <div class="product-emoji" aria-hidden="true">${p.image_emoji || '📦'}</div>
          <div class="product-title-group">
            <div class="product-brand">${escapeHtmlSafe(p.brand || '')}</div>
            <h4 class="product-name">${escapeHtmlSafe(p.name)}</h4>
          </div>
        </div>
        <div class="product-price">${formatCurrencySafe(p.price, 'USD')}</div>
        ${p.pros?.[0] ? `<p class="product-top-pro"><i data-lucide="check" aria-hidden="true"></i>${escapeHtmlSafe(p.pros[0])}</p>` : ''}
      </div>
    `).join('');
    if (window.lucide) lucide.createIcons({ nodes: [shopDom.overBudgetGrid] });
  }
}

function showProductDetail(product, budget, currency) {
  // For now: show a toast with full detail, scroll to product card
  showToastSafe(`Viewing: ${product.name} — ${formatCurrencySafe(product.price, 'USD')}`, 'info');
  const card = document.querySelector(`[data-product-id="${CSS.escape(product.id)}"]`);
  card?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ─── PRODUCT SELECTION ────────────────────────────────────────

function selectProduct(product, budget, currency) {
  ShoppingAgent.selectedProduct = product;

  // Highlight selected card
  document.querySelectorAll('.product-card').forEach(c => c.classList.remove('product-card-selected'));
  const card = document.querySelector(`[data-product-id="${CSS.escape(product.id)}"]`);
  card?.classList.add('product-card-selected');

  // Show selected panel
  showSelectedProductPanel(product, budget, currency);

  showToastSafe(`"${product.name}" selected. Review the checkout summary below.`, 'success');

  // Scroll to selected panel
  shopDom.selectedProductPanel?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function showSelectedProductPanel(product, budget, currency) {
  if (!shopDom.selectedProductPanel) return;
  shopDom.selectedProductPanel.hidden = false;

  const total = Number(product.total_estimated || product.price);
  const remaining = Number(budget) - total;

  if (shopDom.selectedProductBody) {
    shopDom.selectedProductBody.innerHTML = `
      <div class="selected-product-info">
        <span class="selected-product-emoji" aria-hidden="true">${product.image_emoji || '📦'}</span>
        <div class="selected-product-details">
          <div class="selected-product-name">${escapeHtmlSafe(product.name)}</div>
          <div class="selected-product-brand">${escapeHtmlSafe(product.brand || '')}</div>
          <div class="selected-product-merchant">
            <i data-lucide="store" aria-hidden="true"></i>
            ${escapeHtmlSafe(product.merchant || 'Demo Store')}
          </div>
        </div>
      </div>
      <div class="selected-product-pricing">
        <div class="selected-price-row">
          <span>Unit Price</span>
          <strong>${formatCurrencySafe(product.price, 'USD')}</strong>
        </div>
        <div class="selected-price-row">
          <span>Est. Tax</span>
          <span>+${formatCurrencySafe(product.estimated_tax || 0, 'USD')}</span>
        </div>
        <div class="selected-price-row">
          <span>Est. Shipping</span>
          <span>${product.estimated_shipping > 0 ? '+' + formatCurrencySafe(product.estimated_shipping, 'USD') : 'Free'}</span>
        </div>
        <div class="selected-price-row selected-price-total">
          <strong>Estimated Total</strong>
          <strong>${formatCurrencySafe(total, 'USD')}</strong>
        </div>
        <div class="selected-budget-remaining ${remaining >= 0 ? 'within' : 'over'}">
          <i data-lucide="${remaining >= 0 ? 'check-circle' : 'alert-circle'}" aria-hidden="true"></i>
          ${remaining >= 0
            ? `Budget remaining: $${Math.abs(remaining).toFixed(2)}`
            : `$${Math.abs(remaining).toFixed(2)} over your budget`}
        </div>
      </div>
      <p class="selected-product-note">
        <i data-lucide="info" aria-hidden="true"></i>
        Tax is estimated at 8%. The final PayPal charge will be the product price: <strong>${formatCurrencySafe(product.price, 'USD')}</strong>.
      </p>
      <div class="sandbox-note">
        <i data-lucide="test-tube-diagonal" aria-hidden="true"></i>
        Sandbox transaction — no real money will be transferred.
      </div>
    `;
    if (window.lucide) lucide.createIcons({ nodes: [shopDom.selectedProductBody] });
  }
}

function clearSelectedProduct() {
  ShoppingAgent.selectedProduct = null;
  document.querySelectorAll('.product-card').forEach(c => c.classList.remove('product-card-selected'));
  if (shopDom.selectedProductPanel) shopDom.selectedProductPanel.hidden = true;
  if (shopDom.productsSection) shopDom.productsSection.hidden = false;
}

// ─── SHOPPING → PAYMENT HANDOFF ──────────────────────────────

function proceedToPayFromProduct() {
  const product = ShoppingAgent.selectedProduct;
  if (!product) {
    showToastSafe('No product selected. Please select a product first.', 'error');
    return;
  }

  // Pre-fill the payment form with product details
  const paymentData = {
    recipient: product.merchant || 'VoicePay Demo Store',
    amount: Number(product.price),  // Charge product price (backend validates)
    currency: 'USD',
    purpose: product.name,
    ai_mode: 'shopping_agent',
  };

  // Scroll to payment section
  document.getElementById('payment-section')?.scrollIntoView({ behavior: 'smooth' });
  setNavActive('pay');

  // Pass the shopping context to the main payment flow
  // Uses the main app.js currentPayment variable and review card
  if (typeof showReviewCard === 'function') {
    if (typeof currentPayment !== 'undefined') {
      // We can't directly set let variables across files, so use a custom event
    }
    // Dispatch a custom event to trigger the payment flow
    const event = new CustomEvent('voicepay:shopping-checkout', {
      detail: paymentData,
    });
    document.dispatchEvent(event);
  } else {
    // Fallback: populate the text input
    const textInput = document.getElementById('textInput');
    if (textInput) {
      textInput.value = `Pay ${paymentData.recipient} ${paymentData.amount} USD for ${paymentData.purpose}`;
    }
    showToastSafe('Payment details loaded. Review and confirm to proceed.', 'info');
  }
}

// ─── UI STATE HELPERS ─────────────────────────────────────────

function setShoppingStatus(visible, text = '') {
  if (!shopDom.shoppingStatus) return;
  shopDom.shoppingStatus.hidden = !visible;
  if (shopDom.shoppingStatusText) shopDom.shoppingStatusText.textContent = text;
}

function hideShoppingResults() {
  if (shopDom.productsSection) shopDom.productsSection.hidden = true;
  if (shopDom.parsedPrefsCard) shopDom.parsedPrefsCard.hidden = true;
  if (shopDom.shoppingError) shopDom.shoppingError.hidden = true;
  if (shopDom.overBudgetSection) shopDom.overBudgetSection.hidden = true;
  if (shopDom.selectedProductPanel) shopDom.selectedProductPanel.hidden = true;
}

function showShoppingError(title, msg) {
  if (!shopDom.shoppingError) return;
  shopDom.shoppingError.hidden = false;
  if (shopDom.shoppingErrorTitle) shopDom.shoppingErrorTitle.textContent = title;
  if (shopDom.shoppingErrorMsg) shopDom.shoppingErrorMsg.textContent = msg;
  shopDom.shoppingError.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function showFollowupPanel() {
  if (shopDom.followupPanel) shopDom.followupPanel.hidden = false;
}

// ─── SESSION TOKEN ────────────────────────────────────────────

function restoreSessionToken() {
  // Token may have been stored from a previous session/start call
  const stored = sessionStorage.getItem('vp_session_token');
  if (stored && shopDom.sessionTokenInput) {
    shopDom.sessionTokenInput.value = '••••••••';
  }
}

async function submitSessionToken() {
  const token = shopDom.sessionTokenInput?.value.trim();
  if (!token || token === '••••••••') return;

  // Use the same configurable API base as the rest of the app (Issue G).
  const _sessionApiBase = (typeof window !== 'undefined' && window.VOICEPAY_API_BASE)
    ? window.VOICEPAY_API_BASE.replace(/\/$/, '')
    : 'http://localhost:8000';

  try {
    const resp = await fetch(`${_sessionApiBase}/session/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    if (resp.ok) {
      sessionStorage.setItem('vp_session_token', token);
      if (shopDom.sessionNotice) shopDom.sessionNotice.hidden = true;
      showToastSafe('Authenticated. Loading transactions…', 'success');
      if (typeof loadTransactions === 'function') loadTransactions();
    } else {
      showToastSafe('Invalid session token. Check your DEMO_SESSION_TOKEN in backend/.env.', 'error');
    }
  } catch (e) {
    showToastSafe('Could not reach the backend. Is the server running?', 'error');
  }
}

// Show session notice when /transactions returns 401
function showSessionNoticeIfNeeded() {
  const stored = sessionStorage.getItem('vp_session_token');
  if (!stored && shopDom.sessionNotice) {
    shopDom.sessionNotice.hidden = false;
  }
}

// ─── DEMO MODE FALLBACKS ──────────────────────────────────────

/** Demo shopping parser — only used when backend is unreachable */
function mockParseShoppingRequest(query) {
  const lower = query.toLowerCase();

  // Budget
  let budget = null;
  const budgetPatterns = [
    /budget\s+(?:of\s+)?\$?\s*([\d,]+(?:\.\d{1,2})?)/i,
    /\$\s*([\d,]+(?:\.\d{1,2})?)/,
    /([\d,]+(?:\.\d{1,2})?)\s*(?:dollars?|usd)/i,
    /under\s+\$?\s*([\d,]+)/i,
    /less\s+than\s+\$?\s*([\d,]+)/i,
    /around\s+\$?\s*([\d,]+)/i,
  ];
  for (const pat of budgetPatterns) {
    const m = lower.match(pat);
    if (m) {
      const candidate = parseFloat(m[1].replace(',', ''));
      if (candidate > 0 && candidate < 100000) { budget = candidate; break; }
    }
  }

  // Category
  const categoryMap = {
    headphones: ['headphone', 'headset', 'earphone', 'earbuds', 'over-ear', 'auriculares'],
    laptops: ['laptop', 'notebook', 'computer', 'macbook'],
    keyboards: ['keyboard', 'mechanical keyboard'],
    monitors: ['monitor', 'display', 'screen'],
    smartwatches: ['smartwatch', 'watch', 'wearable', 'fitness tracker'],
    tablets: ['tablet', 'ipad'],
    webcams: ['webcam', 'camera', 'web camera'],
    smartphones: ['phone', 'smartphone'],
  };
  let category = null;
  for (const [cat, kws] of Object.entries(categoryMap)) {
    if (kws.some(kw => lower.includes(kw))) { category = cat; break; }
  }

  const prefKeywords = ['wireless', 'bluetooth', 'noise cancelling', 'battery', 'portable',
    'gaming', 'professional', 'budget', 'premium', 'programming', 'student'];
  const preferences = prefKeywords.filter(p => lower.includes(p));

  return { budget, currency: 'USD', category, query, preferences, brand: null, ai_mode: 'demo' };
}

/** Demo product search — only used when backend is unreachable */
function mockSearchProducts(parsed) {
  // Inline demo catalog subset for frontend demo mode
  const DEMO_PRODUCTS = [
    { id: 'wh-anker-q45', name: 'Anker Soundcore Q45', brand: 'Anker', category: 'headphones', category_display: 'Wireless Headphones', price: 49.99, image_emoji: '🎧', specs: { 'Battery Life': '50 hours', 'Connectivity': 'Bluetooth 5.0', 'Type': 'Over-ear' }, pros: ['Very budget-friendly', 'Long 50hr battery'], cons: ['Average noise cancellation'], tags: ['wireless', 'budget', 'bass', 'battery'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'wh-jbl-760nc', name: 'JBL Tune 760NC', brand: 'JBL', category: 'headphones', category_display: 'Wireless Headphones', price: 79.99, image_emoji: '🎧', specs: { 'Battery Life': '35 hours', 'ANC': 'Active Noise Cancelling', 'Type': 'Over-ear' }, pros: ['Good ANC for the price', 'Foldable design'], cons: ['Bass-heavy sound signature'], tags: ['wireless', 'budget', 'noise-cancelling'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'wh-sennheiser-hd450bt', name: 'Sennheiser HD 450BT', brand: 'Sennheiser', category: 'headphones', category_display: 'Wireless Headphones', price: 99.99, image_emoji: '🎧', specs: { 'Battery Life': '30 hours', 'ANC': 'Active Noise Cancelling', 'Codec': 'aptX' }, pros: ['Balanced audiophile sound', 'aptX codec support'], cons: ['Larger form factor'], tags: ['wireless', 'audiophile', 'mid-range', 'noise-cancelling'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'wh-bose-qc45', name: 'Bose QuietComfort 45', brand: 'Bose', category: 'headphones', category_display: 'Wireless Headphones', price: 249.99, image_emoji: '🎧', specs: { 'Battery Life': '24 hours', 'ANC': 'World-class ANC', 'Type': 'Over-ear' }, pros: ['Best-in-class ANC', 'Exceptional comfort'], cons: ['No aptX codec', 'Premium price'], tags: ['noise-cancelling', 'wireless', 'premium', 'comfort'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'wh-sony-xm5', name: 'Sony WH-1000XM5', brand: 'Sony', category: 'headphones', category_display: 'Wireless Headphones', price: 299.99, image_emoji: '🎧', specs: { 'Battery Life': '30 hours', 'ANC': 'Industry-leading ANC', 'Codec': 'LDAC' }, pros: ['Top-tier ANC', '30hr battery', 'LDAC hi-res audio'], cons: ['Expensive', 'Not foldable'], tags: ['noise-cancelling', 'wireless', 'premium', 'battery'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'lt-acer-aspire5', name: 'Acer Aspire 5', brand: 'Acer', category: 'laptops', category_display: 'Laptops', price: 349.99, image_emoji: '💻', specs: { 'CPU': 'Intel Core i5-1235U', 'RAM': '8GB DDR4', 'Storage': '256GB SSD' }, pros: ['Excellent value', 'Good for everyday programming'], cons: ['8GB RAM limits multitasking'], tags: ['intel', 'student', 'programming', 'budget'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'lt-lenovo-ideapad3', name: 'Lenovo IdeaPad 3', brand: 'Lenovo', category: 'laptops', category_display: 'Laptops', price: 499.99, image_emoji: '💻', specs: { 'CPU': 'AMD Ryzen 5 5500U', 'RAM': '8GB DDR4', 'Storage': '512GB SSD' }, pros: ['Fast Ryzen 5 CPU', '512GB storage'], cons: ['Average display'], tags: ['amd', 'student', 'programming', 'budget'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'lt-asus-vivobook15', name: 'ASUS VivoBook 15', brand: 'ASUS', category: 'laptops', category_display: 'Laptops', price: 599.99, image_emoji: '💻', specs: { 'CPU': 'AMD Ryzen 7 5700U', 'RAM': '16GB DDR4', 'Storage': '512GB NVMe SSD' }, pros: ['16GB RAM', 'Great for AI/ML', 'Fast NVMe SSD'], cons: ['Heavier build'], tags: ['amd', 'programming', 'ai', 'performance'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'kb-anne-pro2', name: 'Anne Pro 2', brand: 'Obins', category: 'keyboards', category_display: 'Mechanical Keyboards', price: 69.99, image_emoji: '⌨️', specs: { 'Layout': '60%', 'Switches': 'Gateron Red', 'Wireless': 'Bluetooth 4.0' }, pros: ['Compact 60% design', 'Wireless connectivity', 'Great for programmers'], cons: ['No function row'], tags: ['wireless', 'compact', 'budget', 'programmers'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'kb-keychron-k2', name: 'Keychron K2 Pro', brand: 'Keychron', category: 'keyboards', category_display: 'Mechanical Keyboards', price: 99.99, image_emoji: '⌨️', specs: { 'Layout': '75%', 'Switches': 'Gateron G Pro', 'Wireless': 'Bluetooth 5.1' }, pros: ['75% layout keeps arrow keys', 'Great build quality', 'Mac/Windows compatible'], cons: ['No wrist rest included'], tags: ['wireless', 'compact', 'mechanical', 'programmers'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'sw-fitbit-charge6', name: 'Fitbit Charge 6', brand: 'Fitbit', category: 'smartwatches', category_display: 'Smart Watches', price: 99.99, image_emoji: '⌚', specs: { 'Battery': '7 days', 'GPS': 'Built-in GPS', 'Heart Rate': 'Continuous' }, pros: ['Excellent fitness tracking', 'Long battery life'], cons: ['Limited apps'], tags: ['fitness', 'budget', 'health', 'gps'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'sw-garmin-fr55', name: 'Garmin Forerunner 55', brand: 'Garmin', category: 'smartwatches', category_display: 'Smart Watches', price: 199.99, image_emoji: '⌚', specs: { 'Battery': '20 days', 'GPS': 'Built-in GPS', 'Running Dynamics': 'Advanced' }, pros: ['20-day battery', 'Advanced running metrics'], cons: ['Limited smartwatch features'], tags: ['running', 'gps', 'fitness', 'battery'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'tb-amazon-fire-hd10', name: 'Amazon Fire HD 10', brand: 'Amazon', category: 'tablets', category_display: 'Tablets', price: 129.99, image_emoji: '📱', specs: { 'Display': '10.1" Full HD', 'Storage': '32GB', 'OS': 'Fire OS (Android)' }, pros: ['Very affordable', 'Great for media'], cons: ['Limited app store', 'Plastic build'], tags: ['budget', 'amazon', 'entertainment', 'reading'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'wc-ms-lifecam', name: 'Microsoft LifeCam HD-3000', brand: 'Microsoft', category: 'webcams', category_display: 'Webcams', price: 29.99, image_emoji: '📷', specs: { 'Resolution': '720p HD', 'Frame Rate': '30fps', 'Mic': 'Built-in microphone' }, pros: ['Very affordable', 'Plug-and-play'], cons: ['Only 720p', 'No autofocus'], tags: ['budget', '720p', 'office', 'meetings'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
    { id: 'wc-logitech-c920', name: 'Logitech C920 HD Pro', brand: 'Logitech', category: 'webcams', category_display: 'Webcams', price: 69.99, image_emoji: '📷', specs: { 'Resolution': '1080p Full HD', 'Frame Rate': '30fps', 'Autofocus': 'Yes' }, pros: ['Sharp 1080p image', 'Autofocus', 'Wide compatibility'], cons: ['No 60fps support'], tags: ['1080p', 'office', 'streaming', 'meetings'], merchant: 'VoicePay Demo Store', estimated_tax_rate: 0.08, estimated_shipping: 0 },
  ];

  const budget = parsed.budget || 0;
  const category = parsed.category;
  const prefs = parsed.preferences || [];

  // Filter by category
  let candidates = category
    ? DEMO_PRODUCTS.filter(p => p.category === category)
    : DEMO_PRODUCTS;

  // Calculate totals and scores
  const scored = candidates.map(p => {
    const tax = p.price * (p.estimated_tax_rate || 0.08);
    const ship = p.estimated_shipping || 0;
    const total = p.price + tax + ship;
    const withinBudget = total <= budget;
    let score = withinBudget ? 10 : 0;
    const tags = p.tags || [];
    prefs.forEach(pref => { if (tags.some(t => t.includes(pref))) score += 2; });
    if (withinBudget) score += (budget - total) / budget;
    return { ...p, estimated_tax: parseFloat(tax.toFixed(2)), total_estimated: parseFloat(total.toFixed(2)), score, withinBudget };
  }).sort((a, b) => b.score - a.score);

  const within = scored.filter(p => p.withinBudget);
  const over = scored.filter(p => !p.withinBudget);
  const top = within.slice(0, 3);

  // Assign labels
  const labeled = top.map((p, i) => ({
    ...p,
    comparison_label: i === 0 ? 'best_overall' : i === 1 ? 'best_budget' : 'best_features',
    recommendation_reason: `This product fits within your $${budget.toFixed(2)} budget with $${(budget - p.total_estimated).toFixed(2)} remaining.`,
    data_source: 'demo_catalog',
  }));

  return {
    products: labeled,
    within_budget_count: within.length,
    over_budget_count: over.length,
    over_budget_alternatives: over.slice(0, 2),
    budget, currency: 'USD',
    category_searched: category,
    query: parsed.query,
    demo_catalog: true,
    catalog_note: 'Demo catalog — prices for demonstration only.',
    total_searched: scored.length,
  };
}

// ─── UTILITY HELPERS ──────────────────────────────────────────

/** Safe wrapper — uses main app's apiPost if available, else own fetch */
async function apiPostSafe(path, body) {
  const API_BASE = (typeof window !== 'undefined' && window.VOICEPAY_API_BASE)
    ? window.VOICEPAY_API_BASE.replace(/\/$/, '')
    : 'http://localhost:8000';

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
    } catch (_) { /* ignore */ }
    throw new Error(errorMsg);
  }
  return response.json();
}

/** Show toast — uses main app's showToast if available */
function showToastSafe(message, type = 'info') {
  if (typeof showToast === 'function') {
    showToast(message, type);
  } else {
    console.log(`[${type.toUpperCase()}] ${message}`);
  }
}

function formatCurrencySafe(amount, currency = 'USD') {
  if (typeof formatCurrency === 'function') return formatCurrency(amount, currency);
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(amount);
  } catch (_) {
    return `${currency} ${parseFloat(amount).toFixed(2)}`;
  }
}

function escapeHtmlSafe(text) {
  if (typeof escapeHtml === 'function') return escapeHtml(String(text));
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

function capitalizeWords(str) {
  return str.replace(/\b\w/g, c => c.toUpperCase());
}

function truncate(str, len) {
  return str.length > len ? str.slice(0, len) + '…' : str;
}

function setNavActive(navId) {
  document.querySelectorAll('.nav-link').forEach(l => {
    l.classList.toggle('active', l.dataset.nav === navId);
  });
}

// ─── CROSS-MODULE EVENT BRIDGE ────────────────────────────────
// Listen for shopping-checkout event dispatched from proceedToPayFromProduct()
// This is handled in app.js but we keep the dispatch here.
