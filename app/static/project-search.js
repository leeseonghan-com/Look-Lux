/**
 * 프로젝트 검색·자동완성 위젯
 * (vendor-search.js 와 동일한 UX / 구조)
 *
 * 사용법:
 *   <div data-project-search
 *        data-name="project_id"
 *        data-initial-id="123"
 *        data-initial-name="OO기업 신년행사">
 *   </div>
 *
 * 작동:
 *   - 검색창 + 결과 드롭다운 자동 렌더링
 *   - 선택 시 hidden input(data-name="project_id")에 ID 저장
 *   - 선택 시 'project:selected' 커스텀 이벤트 발생
 */
(function() {
  function initWidget(host) {
    const fieldName = host.dataset.name || 'project_id';
    const initialId = host.dataset.initialId || '';
    const initialName = host.dataset.initialName || '';

    host.innerHTML = `
      <div class="ps-wrap" style="position: relative;">
        <input type="hidden" name="${fieldName}" value="${initialId}" class="ps-id">
        <div class="ps-input-row" style="display: flex; gap: 6px; align-items: stretch;">
          <input type="text" class="input ps-search" placeholder="🔍 프로젝트명·코드·거래처명으로 검색"
                 autocomplete="off" value="${initialName.replace(/"/g, '&quot;')}"
                 style="flex: 1;">
          <button type="button" class="btn-secondary ps-clear" title="선택 해제"
                  style="padding: 0 14px; ${initialId ? '' : 'display:none;'}">✕</button>
        </div>
        <div class="ps-results" style="display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 50; background: white; border: 1px solid #E4E4E7; border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.12); max-height: 320px; overflow-y: auto; margin-top: 4px;">
        </div>
      </div>
    `;

    const idInput = host.querySelector('.ps-id');
    const searchInput = host.querySelector('.ps-search');
    const resultsBox = host.querySelector('.ps-results');
    const clearBtn = host.querySelector('.ps-clear');
    let lastQuery = null;
    let debounceTimer = null;

    function fetchResults(q) {
      fetch('/api/projects/search?q=' + encodeURIComponent(q || '') + '&limit=15')
        .then(r => r.json())
        .then(rows => renderResults(rows, q))
        .catch(() => { resultsBox.style.display = 'none'; });
    }

    function statusBadge(st) {
      const map = {
        '준비중': ['#F3F4F6', '#4B5563'],
        '진행중': ['#DBEAFE', '#1E40AF'],
        '완료':   ['#D1FAE5', '#065F46'],
        '취소':   ['#FEE2E2', '#991B1B'],
      };
      const c = map[st] || ['#F3F4F6', '#4B5563'];
      if (!st) return '';
      return `<span style="display:inline-block; padding:2px 6px; background:${c[0]}; color:${c[1]}; font-size:10px; border-radius:4px; margin-left:6px; font-weight:700;">${escapeHtml(st)}</span>`;
    }

    function renderResults(rows, q) {
      resultsBox.innerHTML = '';
      if (!rows || rows.length === 0) {
        resultsBox.innerHTML = `
          <div style="padding: 16px; text-align: center; color: #71717A; font-size: 13px;">
            <div style="margin-bottom: 8px;">검색 결과가 없습니다</div>
            <a href="/projects/new" target="_blank" style="display:inline-block; padding: 8px 14px; background: #0A0E1A; color: white; border-radius: 6px; font-size: 13px; font-weight: 600; text-decoration: none;">
              + 새 프로젝트 등록
            </a>
          </div>
        `;
        resultsBox.style.display = 'block';
        return;
      }
      rows.forEach(r => {
        const item = document.createElement('div');
        item.className = 'ps-item';
        item.style.cssText = 'padding: 10px 14px; cursor: pointer; border-bottom: 1px solid #F4F4F5;';
        const sub = [
          r.code ? escapeHtml(r.code) : '',
          r.event_date ? escapeHtml(r.event_date) : '',
          r.vendor_name ? '🏢 ' + escapeHtml(r.vendor_name) : '',
          r.location ? '📍 ' + escapeHtml(r.location) : '',
        ].filter(Boolean).join(' · ');
        item.innerHTML = `
          <div style="font-weight: 700; font-size: 14px;">${escapeHtml(r.name)}${statusBadge(r.status)}</div>
          <div style="font-size: 11px; color: #71717A; margin-top: 2px;">${sub}</div>
        `;
        item.addEventListener('mouseenter', () => item.style.background = '#F4F4F5');
        item.addEventListener('mouseleave', () => item.style.background = '');
        item.addEventListener('click', () => selectProject(r));
        resultsBox.appendChild(item);
      });
      resultsBox.style.display = 'block';
    }

    function selectProject(r) {
      idInput.value = r.id;
      searchInput.value = r.name;
      resultsBox.style.display = 'none';
      clearBtn.style.display = '';
      searchInput.dataset.selected = '1';
      host.dispatchEvent(new CustomEvent('project:selected', { detail: r }));
    }

    function clearSelection() {
      idInput.value = '';
      searchInput.value = '';
      searchInput.dataset.selected = '';
      clearBtn.style.display = 'none';
      lastQuery = null;
      searchInput.focus();
    }

    searchInput.addEventListener('focus', () => {
      if (searchInput.dataset.selected !== '1') {
        fetchResults(searchInput.value);
      }
    });
    searchInput.addEventListener('input', () => {
      if (idInput.value) {
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
        e.preventDefault();
        // 결과가 1건이면 자동 선택
        const items = resultsBox.querySelectorAll('.ps-item');
        if (items.length === 1) { items[0].click(); }
        else { resultsBox.style.display = 'none'; }
      }
    });
    document.addEventListener('click', (e) => {
      if (!host.contains(e.target)) {
        resultsBox.style.display = 'none';
      }
    });
    clearBtn.addEventListener('click', clearSelection);
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-project-search]').forEach(initWidget);
  });
})();
