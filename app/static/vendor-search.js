/**
 * 거래처 검색·자동완성 위젯
 *
 * 사용법:
 *   <div data-vendor-search
 *        data-name="vendor_id"
 *        data-initial-id="123"
 *        data-initial-name="OO이벤트">
 *   </div>
 *
 * 작동:
 *   - 검색창 + 결과 드롭다운 자동 렌더링
 *   - 선택 시 hidden input(data-name="vendor_id")에 ID 저장
 *   - 결과 없으면 "+ 새 거래처 등록" 모달
 */
(function() {
  function initWidget(host) {
    const fieldName = host.dataset.name || 'vendor_id';
    const initialId = host.dataset.initialId || '';
    const initialName = host.dataset.initialName || '';

    // ── host 안에는 검색창 + 결과만 (모달은 body 끝으로 분리) ──
    host.innerHTML = `
      <div class="vs-wrap" style="position: relative;">
        <input type="hidden" name="${fieldName}" value="${initialId}" class="vs-id">
        <div class="vs-input-row" style="display: flex; gap: 6px; align-items: stretch;">
          <input type="text" class="input vs-search" placeholder="🔍 거래처 이름·사업자번호로 검색"
                 autocomplete="off" value="${initialName.replace(/"/g, '&quot;')}"
                 style="flex: 1;">
          <button type="button" class="btn-secondary vs-clear" title="선택 해제"
                  style="padding: 0 14px; ${initialId ? '' : 'display:none;'}">✕</button>
        </div>
        <div class="vs-results" style="display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 50; background: white; border: 1px solid #E4E4E7; border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.12); max-height: 320px; overflow-y: auto; margin-top: 4px;">
        </div>
      </div>
    `;

    // 모달은 form 밖(body)에 단 1개만 생성하여 공유
    let modal = document.querySelector('.vs-modal-shared');
    if (!modal) {
      modal = document.createElement('div');
      modal.className = 'vs-modal-shared';
      modal.style.cssText = 'display:none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center;';
      modal.innerHTML = `
        <div style="background: white; padding: 24px; border-radius: 12px; width: 90%; max-width: 420px;">
          <h3 style="font-size: 17px; font-weight: 800; margin: 0 0 16px;">+ 새 거래처 등록</h3>
          <div style="display: flex; flex-direction: column; gap: 12px;">
            <div>
              <label style="font-size: 12px; font-weight: 600; color: #52525B; display: block; margin-bottom: 4px;">거래처명 *</label>
              <input type="text" class="input vs-q-name">
            </div>
            <div>
              <label style="font-size: 12px; font-weight: 600; color: #52525B; display: block; margin-bottom: 4px;">사업자번호</label>
              <input type="text" class="input vs-q-biz" placeholder="000-00-00000">
            </div>
            <div>
              <label style="font-size: 12px; font-weight: 600; color: #52525B; display: block; margin-bottom: 4px;">연락처</label>
              <input type="text" class="input vs-q-phone">
            </div>
            <div>
              <label style="font-size: 12px; font-weight: 600; color: #52525B; display: block; margin-bottom: 4px;">담당자명</label>
              <input type="text" class="input vs-q-contact">
            </div>
          </div>
          <div style="display: flex; gap: 8px; margin-top: 18px; justify-content: flex-end;">
            <button type="button" class="btn-secondary vs-cancel">취소</button>
            <button type="button" class="btn-primary vs-save">저장</button>
          </div>
        </div>
      `;
      document.body.appendChild(modal);
    }

    const idInput = host.querySelector('.vs-id');
    const searchInput = host.querySelector('.vs-search');
    const resultsBox = host.querySelector('.vs-results');
    const clearBtn = host.querySelector('.vs-clear');
    let lastQuery = '';
    let debounceTimer = null;
    let currentHost = null;  // 모달이 어느 위젯에서 열렸는지 추적

    function fetchResults(q) {
      fetch('/api/vendors/search?q=' + encodeURIComponent(q || '') + '&limit=10')
        .then(r => r.json())
        .then(rows => renderResults(rows, q));
    }

    function renderResults(rows, q) {
      resultsBox.innerHTML = '';
      if (rows.length === 0) {
        resultsBox.innerHTML = `
          <div style="padding: 16px; text-align: center; color: #71717A; font-size: 13px;">
            <div style="margin-bottom: 8px;">검색 결과가 없습니다</div>
            <button type="button" class="vs-add-btn" style="padding: 8px 14px; background: #0A0E1A; color: white; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer;">
              + "${escapeHtml(q || '새 거래처')}" 등록
            </button>
          </div>
        `;
        const addBtn = resultsBox.querySelector('.vs-add-btn');
        if (addBtn) {
          addBtn.addEventListener('click', () => openModal(q));
        }
      } else {
        rows.forEach(r => {
          const item = document.createElement('div');
          item.className = 'vs-item';
          item.style.cssText = 'padding: 10px 14px; cursor: pointer; border-bottom: 1px solid #F4F4F5;';
          item.innerHTML = `
            <div style="font-weight: 700; font-size: 14px;">${escapeHtml(r.name)}
              ${r.vendor_type === 'dealer' ? '<span style="display:inline-block; padding: 2px 6px; background: #FEF3C7; color: #92400E; font-size: 10px; border-radius: 4px; margin-left: 6px; font-weight: 700;">대리점</span>' : ''}
            </div>
            <div style="font-size: 11px; color: #71717A; margin-top: 2px;">
              ${r.biz_number ? '사업자 ' + escapeHtml(r.biz_number) : ''}${r.biz_number && r.phone ? ' · ' : ''}${r.phone ? '☎ ' + escapeHtml(r.phone) : ''}${r.contact_person ? ' · ' + escapeHtml(r.contact_person) : ''}
            </div>
          `;
          item.addEventListener('mouseenter', () => item.style.background = '#F4F4F5');
          item.addEventListener('mouseleave', () => item.style.background = '');
          item.addEventListener('click', () => selectVendor(r));
          resultsBox.appendChild(item);
        });
        // 맨 아래에 + 추가 옵션
        const addRow = document.createElement('div');
        addRow.style.cssText = 'padding: 10px 14px; border-top: 1px solid #E4E4E7; background: #FAFAFA; cursor: pointer; font-size: 12px; color: #0A0E1A; font-weight: 600;';
        addRow.innerHTML = `+ "${escapeHtml(q)}" 새 거래처 등록`;
        addRow.addEventListener('click', () => openModal(q));
        if (q && q.trim()) resultsBox.appendChild(addRow);
      }
      resultsBox.style.display = 'block';
    }

    function selectVendor(r) {
      idInput.value = r.id;
      searchInput.value = r.name;
      resultsBox.style.display = 'none';
      clearBtn.style.display = '';
      searchInput.dataset.selected = '1';
      // 커스텀 이벤트 발생 (다른 필드 자동 채우기 등에 사용)
      host.dispatchEvent(new CustomEvent('vendor:selected', { detail: r }));
    }

    function clearSelection() {
      idInput.value = '';
      searchInput.value = '';
      searchInput.dataset.selected = '';
      clearBtn.style.display = 'none';
      searchInput.focus();
    }

    function openModal(prefilledName) {
      currentHost = host;
      modal.__currentWidget = {
        selectVendor: selectVendor,
        searchInput: searchInput,
      };
      modal.style.display = 'flex';
      const nameField = modal.querySelector('.vs-q-name');
      nameField.value = prefilledName || searchInput.value || '';
      setTimeout(() => nameField.focus(), 50);
      resultsBox.style.display = 'none';
    }

    // ── 이벤트 연결 ──
    searchInput.addEventListener('focus', () => {
      if (searchInput.dataset.selected !== '1') {
        fetchResults(searchInput.value);
      }
    });
    searchInput.addEventListener('input', () => {
      // 입력 변경 시 ID 초기화
      if (idInput.value && searchInput.value !== initialName) {
        idInput.value = '';
        clearBtn.style.display = 'none';
        searchInput.dataset.selected = '';
      }
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        const q = searchInput.value.trim();
        if (q === lastQuery) return;
        lastQuery = q;
        fetchResults(q);
      }, 200);
    });
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        resultsBox.style.display = 'none';
        searchInput.blur();
      } else if (e.key === 'Enter') {
        // Enter는 form 제출 막고 결과만 닫기 (검색 input은 form submit 대상 아님)
        e.preventDefault();
        resultsBox.style.display = 'none';
      }
    });
    // 바깥 클릭 시 결과 숨김
    document.addEventListener('click', (e) => {
      if (!host.contains(e.target)) {
        resultsBox.style.display = 'none';
      }
    });
    clearBtn.addEventListener('click', clearSelection);

    // 모달 핸들러는 모달 자체에 한 번만 (위젯 인스턴스마다 중복 X)
    if (!modal.__handlersAttached) {
      modal.__handlersAttached = true;

      function closeSharedModal() {
        modal.style.display = 'none';
        modal.querySelectorAll('input').forEach(i => i.value = '');
        modal.__currentWidget = null;
      }

      async function saveSharedQuickAdd() {
        const widget = modal.__currentWidget;
        if (!widget) { closeSharedModal(); return; }
        const name = modal.querySelector('.vs-q-name').value.trim();
        if (!name) {
          alert('거래처명을 입력해주세요');
          return;
        }
        const formData = new FormData();
        formData.append('name', name);
        formData.append('biz_number', modal.querySelector('.vs-q-biz').value.trim());
        formData.append('phone', modal.querySelector('.vs-q-phone').value.trim());
        formData.append('contact_person', modal.querySelector('.vs-q-contact').value.trim());
        try {
          const res = await fetch('/api/vendors/quick-add', { method: 'POST', body: formData });
          const data = await res.json();
          if (data.ok) {
            widget.selectVendor({
              id: data.id, name: data.name,
              biz_number: formData.get('biz_number'),
              phone: formData.get('phone'),
              contact_person: formData.get('contact_person'),
              vendor_type: 'consumer',
            });
            closeSharedModal();
            if (data.existed) {
              alert('이미 등록된 거래처입니다. 기존 거래처가 선택되었어요.');
            }
          } else {
            alert(data.error || '저장 실패');
          }
        } catch (e) {
          alert('저장 실패: ' + e.message);
        }
      }

      modal.querySelector('.vs-cancel').addEventListener('click', closeSharedModal);
      modal.querySelector('.vs-save').addEventListener('click', saveSharedQuickAdd);
      modal.addEventListener('click', (e) => {
        if (e.target === modal) closeSharedModal();
      });
      // 모달 내 Enter 시 저장
      modal.querySelectorAll('input').forEach(inp => {
        inp.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); saveSharedQuickAdd(); }
        });
      });
    }
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  // 페이지 로드 시 자동 초기화
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-vendor-search]').forEach(initWidget);
  });
})();
