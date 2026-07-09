/* ============================================================
   공통 JS — 금액 자동 콤마 + 단가표 자동완성
   ============================================================ */

/**
 * 금액 입력란 자동 콤마 포맷
 * 사용법: <input class="money-input" name="amount"> 형태
 * 폼 제출 시 자동으로 콤마 제거하고 숫자만 전송
 */
function formatMoney(num) {
  if (num === '' || num === null || num === undefined) return '';
  return Number(num).toLocaleString('ko-KR');
}

function parseMoney(str) {
  if (!str) return 0;
  return parseInt(String(str).replace(/[^\d-]/g, '')) || 0;
}

function attachMoneyInput(input) {
  if (input.dataset.moneyAttached) return;
  input.dataset.moneyAttached = '1';

  // type=number 이면 콤마 표시 불가 → type=text 로 강제 변환
  if (input.type === 'number') {
    input.type = 'text';
    input.setAttribute('inputmode', 'numeric');
  }

  // 초기값 포맷
  if (input.value) {
    input.value = formatMoney(parseMoney(input.value));
  }

  // 입력 중 실시간 콤마
  input.addEventListener('input', (e) => {
    const cursorEnd = input.value.length - input.selectionStart;
    const raw = parseMoney(input.value);
    input.value = raw === 0 && input.value === '' ? '' : formatMoney(raw);
    // 커서 위치 복원
    const newPos = Math.max(0, input.value.length - cursorEnd);
    input.setSelectionRange(newPos, newPos);
  });

  // 폼 제출 시 콤마 제거 (hidden 필드로 대체)
  const form = input.closest('form');
  if (form && !form.dataset.moneyHooked) {
    form.dataset.moneyHooked = '1';
    form.addEventListener('submit', () => {
      form.querySelectorAll('.money-input').forEach((el) => {
        el.value = String(parseMoney(el.value));
      });
    });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.money-input').forEach(attachMoneyInput);
  // 모든 date input에 직접 입력 도우미 부착
  document.querySelectorAll('input[type="date"]').forEach(attachDateInput);
});

/**
 * 날짜 입력 도우미
 *   - HTML <input type="date">는 브라우저 네이티브 캘린더 지원
 *   - 추가: type=text로 같이 입력 가능한 형태로 보강
 *   - 사용자가 키보드로 2026-06-08 같이 직접 타이핑 가능
 */
function attachDateInput(input) {
  if (input.dataset.dateAttached) return;
  input.dataset.dateAttached = '1';

  // 이미 type=date라면 그대로 (네이티브 캘린더 우선)
  // 사용자가 키보드로 입력하다 캘린더 클릭하면 둘 다 동작
  // showPicker() API가 있으면 입력란 클릭 시 캘린더 자동 표시
  input.addEventListener('focus', () => {
    if (input.showPicker && !input.dataset.suppressPicker) {
      try { input.showPicker(); } catch (e) { /* 무시 */ }
    }
  });

  // 키보드 타이핑 중에는 캘린더 안 떠도 되도록 표시 (suppressPicker 토글)
  input.addEventListener('keydown', (e) => {
    // 숫자/하이픈/방향키 입력 시에는 캘린더 안 띄움
    if (/[0-9\-]/.test(e.key) || ['ArrowLeft', 'ArrowRight', 'Backspace', 'Delete', 'Tab'].includes(e.key)) {
      input.dataset.suppressPicker = '1';
      setTimeout(() => { delete input.dataset.suppressPicker; }, 100);
    }
  });
}

// 동적 추가 input 처리용 export
window.attachDateInput = attachDateInput;

// 동적으로 추가된 input도 처리할 수 있게 expose
window.attachMoneyInput = attachMoneyInput;
window.formatMoney = formatMoney;
window.parseMoney = parseMoney;


/* ============================================================
   단가표 자동완성 (견적서 등에서 사용)
   ============================================================ */

class ItemAutocomplete {
  constructor(input, options = {}) {
    this.input = input;
    this.onSelect = options.onSelect || (() => {});
    this.category = options.category || ''; // '물품' | '렌탈' | ''
    this.dropdown = null;
    this.items = [];
    this.timer = null;
    this.selectedIndex = -1;
    // 인스턴스를 input에 저장하여 외부에서 category 등을 동적으로 변경 가능
    input._autocomplete = this;
    this.init();
  }

  init() {
    this.input.setAttribute('autocomplete', 'off');
    this.input.addEventListener('input', () => this.onInput());
    this.input.addEventListener('keydown', (e) => this.onKey(e));
    this.input.addEventListener('blur', () => setTimeout(() => this.close(), 200));
    this.input.addEventListener('focus', () => {
      if (this.input.value.length >= 1) this.onInput();
    });
  }

  onInput() {
    clearTimeout(this.timer);
    const q = this.input.value.trim();
    if (q.length < 1) {
      this.close();
      return;
    }
    this.timer = setTimeout(() => this.search(q), 200);
  }

  async search(q) {
    const url = `/api/items/search?q=${encodeURIComponent(q)}` +
      (this.category ? `&category=${encodeURIComponent(this.category)}` : '');
    try {
      const res = await fetch(url);
      this.items = await res.json();
      console.log('[자동완성] 검색:', q, '카테고리:', this.category, '결과:', this.items.length, '개');
      this.render();
    } catch (e) {
      console.error('자동완성 실패:', e);
    }
  }

  render() {
    this.close();
    if (!this.items.length) {
      // 결과 없으면 "검색 결과 없음" 표시
      this.renderEmpty();
      return;
    }
    this.dropdown = document.createElement('div');
    this.dropdown.className = 'ac-dropdown';
    this.dropdown.style.cssText = `
      position: absolute; z-index: 50; background: white;
      border: 1px solid #cbd5e1; border-radius: 6px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.1);
      max-height: 280px; overflow-y: auto;
      font-size: 13px; min-width: 240px;
    `;
    const rect = this.input.getBoundingClientRect();
    this.dropdown.style.left = (window.scrollX + rect.left) + 'px';
    this.dropdown.style.top = (window.scrollY + rect.bottom + 2) + 'px';
    this.dropdown.style.width = Math.max(rect.width, 280) + 'px';

    const isRentalMode = (this.category === '렌탈');
    this.items.forEach((item, idx) => {
      const opt = document.createElement('div');
      opt.style.cssText = 'padding: 8px 10px; cursor: pointer; border-bottom: 1px solid #f1f5f9;';
      // 용도 배지 색상
      const usage = item.usage || '';
      const badgeStyle = {
        '겸용':  'background:#fef3c7;color:#92400e;',
        '납품':  'background:#dbeafe;color:#1e40af;',
        '렌탈':  'background:#fed7aa;color:#9a3412;',
        '미설정': 'background:#f3f4f6;color:#6b7280;',
      }[usage] || 'background:#f3f4f6;color:#6b7280;';
      // 양쪽 가격 표시 (현재 모드의 가격을 굵게 강조)
      const hasSale = (item.consumer_price || 0) > 0;
      const hasRent = (item.rental_daily || 0) > 0;
      let priceHtml = '';
      if (hasSale) {
        const emphasis = !isRentalMode ? 'font-weight:700;color:#1e40af;' : 'color:#94a3b8;';
        priceHtml += `<span style="${emphasis}">📦${formatMoney(item.consumer_price)}원</span>`;
      }
      if (hasRent) {
        if (priceHtml) priceHtml += ' · ';
        const emphasis = isRentalMode ? 'font-weight:700;color:#9a3412;' : 'color:#94a3b8;';
        priceHtml += `<span style="${emphasis}">🎬${formatMoney(item.rental_daily)}원/일</span>`;
      }
      if (!priceHtml) priceHtml = '<span style="color:#dc2626;">가격 미설정</span>';

      opt.innerHTML = `
        <div style="font-weight:600;">${this.escape(item.name)}
          <span style="font-size:11px; padding:1px 5px; ${badgeStyle} border-radius:3px; margin-left:4px;">${usage}</span>
        </div>
        <div style="font-size:11px; color:#64748b; margin-top:2px;">
          ${this.escape(item.spec || '')}${item.spec?' · ':''}${priceHtml} · ${item.unit}
        </div>
      `;
      opt.addEventListener('mousedown', (e) => {
        e.preventDefault();
        this.select(item);
      });
      opt.addEventListener('mouseenter', () => {
        this.selectedIndex = idx;
        this.highlight();
      });
      this.dropdown.appendChild(opt);
    });
    document.body.appendChild(this.dropdown);
    this.selectedIndex = -1;
  }

  renderEmpty() {
    this.dropdown = document.createElement('div');
    this.dropdown.className = 'ac-dropdown';
    this.dropdown.style.cssText = `
      position: absolute; z-index: 50; background: white;
      border: 1px solid #cbd5e1; border-radius: 6px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.1);
      padding: 10px 12px; font-size: 12px; color: #94a3b8;
    `;
    const rect = this.input.getBoundingClientRect();
    this.dropdown.style.left = (window.scrollX + rect.left) + 'px';
    this.dropdown.style.top = (window.scrollY + rect.bottom + 2) + 'px';
    this.dropdown.style.minWidth = Math.max(rect.width, 280) + 'px';
    this.dropdown.textContent = '💭 단가표에 없는 항목입니다 (직접 입력하세요)';
    document.body.appendChild(this.dropdown);
  }

  highlight() {
    if (!this.dropdown) return;
    [...this.dropdown.children].forEach((c, i) => {
      c.style.background = i === this.selectedIndex ? '#eff6ff' : 'white';
    });
  }

  onKey(e) {
    if (!this.dropdown) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this.selectedIndex = Math.min(this.selectedIndex + 1, this.items.length - 1);
      this.highlight();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
      this.highlight();
    } else if (e.key === 'Enter') {
      if (this.selectedIndex >= 0 && this.items[this.selectedIndex]) {
        e.preventDefault();
        this.select(this.items[this.selectedIndex]);
      }
    } else if (e.key === 'Escape') {
      this.close();
    }
  }

  select(item) {
    this.input.value = item.name;
    this.onSelect(item);
    this.close();
  }

  close() {
    if (this.dropdown) {
      this.dropdown.remove();
      this.dropdown = null;
    }
  }

  escape(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }
}

window.ItemAutocomplete = ItemAutocomplete;
