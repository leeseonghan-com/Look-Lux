/* 인력 간편 배치 팝업
   - openStaffPicker(projectId)
   - 행 = 사람, 열 = 프로젝트 날짜. 칸을 누르면 배치/해제
   - 근무자 이름을 누르면 행사 전체 날짜로 바로 추가
   - 저장 시 서버에 통째로 반영 후 화면 새로고침
*/
(function () {
  var st = null;          // 현재 상태
  var WD = ['일', '월', '화', '수', '목', '금', '토'];

  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === 'style') e.style.cssText = attrs[k];
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    }
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function keyOf(p) { return p.worker_id ? 'w' + p.worker_id : 'n' + p.name; }
  function addDays(iso, n) { var d = new Date(iso + 'T00:00:00'); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); }
  function lbl(iso) { var d = new Date(iso + 'T00:00:00'); return (d.getMonth() + 1) + '/' + d.getDate() + '<br><span style="font-size:10px;' + (d.getDay() === 0 || d.getDay() === 6 ? 'color:#DC2626;' : 'color:#94A3B8;') + '">' + WD[d.getDay()] + '</span>'; }

  function ensureModal() {
    var m = document.getElementById('sp-modal');
    if (m) return m;
    m = el('div', { id: 'sp-modal', style: 'position:fixed; inset:0; z-index:9999; background:rgba(15,23,42,.45); display:none; align-items:flex-start; justify-content:center; padding:4vh 12px; overflow-y:auto;' });
    m.addEventListener('click', function (e) { if (e.target === m) close(); });
    m.appendChild(el('div', { id: 'sp-box', style: 'background:#fff; background-image:linear-gradient(#fff,#fff); width:100%; max-width:860px; border-radius:16px; box-shadow:0 20px 60px rgba(0,0,0,.25); overflow:hidden;' }));
    document.body.appendChild(m);
    return m;
  }
  function close() { var m = document.getElementById('sp-modal'); if (m) m.style.display = 'none'; }

  function chipsHtml() {
    var h = '';
    var inPeople = {}; st.people.forEach(function (p) { inPeople[keyOf(p)] = 1; });
    var q = (st.q || '').trim(), shown = 0;
    st.workers.forEach(function (w) {
      if (q && w.name.indexOf(q) < 0) return;
      var on = inPeople['w' + w.id];
      shown++;
      h += '<button type="button" data-act="addw" data-id="' + w.id + '" ' + (on ? 'disabled' : '') + ' style="padding:6px 12px; border-radius:999px; font-size:13px; font-weight:700; cursor:pointer; border:1.5px solid ' + (on ? '#86EFAC' : '#CBD5E1') + '; background:' + (on ? '#DCFCE7' : '#fff') + '; color:' + (on ? '#166534' : '#0F172A') + ';">' + (on ? '✓ ' : '+ ') + esc(w.name) + ' <span style="font-size:10px; color:#94A3B8; font-weight:500;">' + esc(w.type) + '</span></button>';
    });
    if (!shown) h += '<span style="font-size:12px; color:#94A3B8;">' + (q ? '검색 결과 없음 — Enter 또는 [+ 직접 추가]로 새 이름을 넣을 수 있어요' : '등록된 근무자가 없습니다. 이름을 입력해 추가하세요.') + '</span>';
    return h;
  }

  function render() {
    var box = document.getElementById('sp-box');
    var days = st.days, ev = st.eventDays;
    var inPeople = {}; st.people.forEach(function (p) { inPeople[keyOf(p)] = 1; });

    var h = '';
    h += '<div style="padding:16px 20px; border-bottom:1px solid #E5E7EB; display:flex; justify-content:space-between; align-items:center; gap:10px;">'
       + '<div><div style="font-size:12px; color:#64748B; font-weight:700;">👷 인력 배치</div>'
       + '<div style="font-size:18px; font-weight:800; color:#0F172A;">' + esc(st.project.name) + '</div></div>'
       + '<button type="button" data-act="close" style="font-size:22px; background:none; border:none; cursor:pointer; color:#64748B;">✕</button></div>';

    // ① 사람 추가
    h += '<div style="padding:14px 20px; background:#F8FAFC; border-bottom:1px solid #E5E7EB;">'
       + '<div style="font-size:13px; font-weight:800; margin-bottom:8px;">① 이름을 누르면 추가됩니다 <span style="font-weight:500; color:#64748B;">(행사 날짜 전체로 자동 배치)</span></div>'
       + '<div style="display:flex; gap:8px; margin-bottom:8px;">'
       + '<input id="sp-q" placeholder="🔍 이름 검색 또는 새 이름 입력 후 Enter" value="' + esc(st.q) + '" style="flex:1; padding:9px 12px; border:1px solid #CBD5E1; border-radius:9px; font-size:14px;">'
       + '<button type="button" data-act="addname" style="padding:9px 14px; border-radius:9px; border:1px solid #0A0E1A; background:#0A0E1A; color:#fff; font-weight:700; font-size:13px; white-space:nowrap;">+ 직접 추가</button></div>'
       + '<div id="sp-chips" style="display:flex; flex-wrap:wrap; gap:6px; max-height:112px; overflow-y:auto;">' + chipsHtml() + '</div>';
    h += '</div>';

    // ② 날짜 표
    h += '<div style="padding:14px 20px;">'
       + '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; flex-wrap:wrap; gap:6px;">'
       + '<div style="font-size:13px; font-weight:800;">② 칸을 눌러 날짜를 켜고 끄세요 <span style="font-weight:500; color:#64748B;">(⚠ = 그날 다른 현장 배치)</span></div>'
       + '<div style="display:flex; gap:6px;"><button type="button" data-act="before" style="font-size:12px; padding:5px 10px; border:1px solid #CBD5E1; border-radius:7px; background:#fff;">◀ 셋업일 추가</button>'
       + '<button type="button" data-act="after" style="font-size:12px; padding:5px 10px; border:1px solid #CBD5E1; border-radius:7px; background:#fff;">철수일 추가 ▶</button></div></div>';
    if (!st.people.length) {
      h += '<div style="text-align:center; padding:28px; color:#94A3B8; border:1.5px dashed #E2E8F0; border-radius:12px;">위에서 이름을 눌러 인원을 추가하세요</div>';
    } else {
      h += '<div style="overflow-x:auto;"><table style="border-collapse:separate; border-spacing:0; width:100%; font-size:13px;">'
         + '<thead><tr><th style="text-align:left; padding:6px 8px; position:sticky; left:0; background:#fff; min-width:150px;">이름 · 오퍼</th>';
      days.forEach(function (d) {
        var isEv = ev.indexOf(d) >= 0;
        h += '<th style="padding:4px 2px; min-width:46px; text-align:center; font-weight:700; ' + (isEv ? 'background:#EEF2FF; color:#3730A3;' : 'color:#64748B;') + ' border-radius:6px;">' + lbl(d) + (isEv ? '' : '<div style="font-size:9px;">준비</div>') + '</th>';
      });
      h += '<th></th></tr></thead><tbody>';
      st.people.forEach(function (p, i) {
        var k = keyOf(p), busy = st.busy[k] || {};
        h += '<tr><td style="padding:6px 8px; position:sticky; left:0; background:#fff; border-top:1px solid #F1F5F9;">'
           + '<div style="font-weight:800;">' + esc(p.name) + '</div>'
           + '<button type="button" data-act="offer" data-i="' + i + '" style="margin-top:4px; font-size:12px; font-weight:800; padding:4px 10px; border-radius:999px; cursor:pointer; border:1.5px solid ' + (p.offer ? '#7C3AED' : '#E2E8F0') + '; background:' + (p.offer ? '#7C3AED' : '#fff') + '; background-image:linear-gradient(' + (p.offer ? '#7C3AED,#7C3AED' : '#fff,#fff') + '); color:' + (p.offer ? '#fff' : '#94A3B8') + ';">' + (p.offer ? '★ 오퍼' : '☆ 오퍼') + '</button></td>';
        days.forEach(function (d) {
          var on = p.days.indexOf(d) >= 0, b = busy[d];
          var bg = on ? (b ? '#FEE2E2' : '#0A0E1A') : '#F1F5F9', fg = on ? (b ? '#991B1B' : '#fff') : '#CBD5E1';
          h += '<td style="padding:3px 2px; border-top:1px solid #F1F5F9; text-align:center;">'
             + '<button type="button" data-act="cell" data-i="' + i + '" data-d="' + d + '" title="' + (b ? '⚠ 이날 ' + esc(b.join(', ')) + ' 배치됨' : '') + '" style="width:42px; height:36px; border-radius:8px; border:' + (b ? '1.5px solid #FCA5A5' : '0') + '; background:' + bg + '; background-image:linear-gradient(' + bg + ',' + bg + '); color:' + fg + '; font-weight:800; font-size:14px; cursor:pointer;">' + (on ? '✓' : (b ? '⚠' : '')) + '</button></td>';
        });
        h += '<td style="padding:3px 6px; border-top:1px solid #F1F5F9; white-space:nowrap;">'
           + '<button type="button" data-act="all" data-i="' + i + '" style="font-size:11px; padding:4px 7px; border:1px solid #CBD5E1; border-radius:6px; background:#fff;">전체</button> '
           + '<button type="button" data-act="del" data-i="' + i + '" style="font-size:11px; padding:4px 7px; border:1px solid #FCA5A5; border-radius:6px; background:#FEF2F2; color:#DC2626;">빼기</button></td></tr>';
      });
      h += '</tbody></table></div>';
      var n = st.people.length, md = st.people.reduce(function (a, p) { return a + p.days.length; }, 0);
      var no = st.people.filter(function (p) { return p.offer; }).length;
      h += '<div style="font-size:12px; color:#64748B; margin-top:8px;">총 <b>' + n + '명</b>' + (no ? ' (오퍼 <b style="color:#7C3AED;">' + no + '명</b>)' : '') + ' · 연인원 <b>' + md + '인·일</b></div>';
    }
    h += '</div>';

    h += '<div style="padding:14px 20px; border-top:1px solid #E5E7EB; display:flex; gap:8px; justify-content:flex-end; background:#F8FAFC;">'
       + '<button type="button" data-act="close" style="padding:11px 18px; border-radius:10px; border:1px solid #CBD5E1; background:#fff; font-weight:700;">취소</button>'
       + '<button type="button" data-act="save" style="padding:11px 26px; border-radius:10px; border:0; background:#2563EB; background-image:linear-gradient(#2563EB,#2563EB); color:#fff; font-weight:800; font-size:15px;">' + (st.saving ? '저장 중…' : '💾 저장') + '</button></div>';

    box.innerHTML = h;
    var qi = document.getElementById('sp-q');
    if (qi) {
      // ★ 한글 입력(IME) 보호: 입력창은 절대 다시 만들지 않고, 아래 이름 목록만 갱신
      var composing = false;
      var refresh = function () { st.q = qi.value; var c = document.getElementById('sp-chips'); if (c) c.innerHTML = chipsHtml(); };
      qi.addEventListener('compositionstart', function () { composing = true; });
      qi.addEventListener('compositionend', function () { composing = false; refresh(); });
      qi.addEventListener('input', function (e) { if (composing || e.isComposing) { st.q = qi.value; var c = document.getElementById('sp-chips'); if (c) c.innerHTML = chipsHtml(); return; } refresh(); });
      qi.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { if (e.isComposing || composing) return; e.preventDefault(); st.q = qi.value; addByName(); }
      });
      if (st.focusQ) { qi.focus(); var L = qi.value.length; qi.setSelectionRange(L, L); st.focusQ = false; }
    }
  }

  function addPerson(p) {
    if (st.people.some(function (x) { return keyOf(x) === keyOf(p); })) return;
    p.days = (st.eventDays.length ? st.eventDays : st.days).slice();
    p.offer = !!p.offer;
    st.people.push(p);
  }
  function addByName() {
    var name = (st.q || '').trim();
    if (!name) return;
    var w = st.workers.filter(function (x) { return x.name === name; })[0];
    addPerson(w ? { worker_id: w.id, name: w.name } : { worker_id: null, name: name });
    st.q = ''; st.focusQ = true; render();
  }

  function onClick(e) {
    var b = e.target.closest('[data-act]'); if (!b || !st) return;
    var act = b.getAttribute('data-act'), i = parseInt(b.getAttribute('data-i'), 10);
    if (act === 'close') return close();
    if (act === 'addw') { var w = st.workers.filter(function (x) { return String(x.id) === b.getAttribute('data-id'); })[0]; if (w) addPerson({ worker_id: w.id, name: w.name }); st.q = ''; st.focusQ = true; return render(); }
    if (act === 'addname') { var qe = document.getElementById('sp-q'); if (qe) st.q = qe.value; return addByName(); }
    if (act === 'cell') { var d = b.getAttribute('data-d'), p = st.people[i], j = p.days.indexOf(d); if (j >= 0) p.days.splice(j, 1); else p.days.push(d); return render(); }
    if (act === 'all') { var pp = st.people[i]; pp.days = pp.days.length === st.days.length ? [] : st.days.slice(); return render(); }
    if (act === 'offer') { st.people[i].offer = !st.people[i].offer; return render(); }
    if (act === 'del') { st.people.splice(i, 1); return render(); }
    if (act === 'before') { st.days.unshift(addDays(st.days[0], -1)); return render(); }
    if (act === 'after') { st.days.push(addDays(st.days[st.days.length - 1], 1)); return render(); }
    if (act === 'save') return save();
  }
  function onChange(e) {
    return;
  }

  function save() {
    if (st.saving) return;
    var empty = st.people.filter(function (p) { return !p.days.length; });
    if (empty.length && !confirm(empty.map(function (p) { return p.name; }).join(', ') + ' 님은 선택한 날짜가 없어 배치에서 빠집니다. 계속할까요?')) return;
    st.saving = true; render();
    fetch('/api/staffing/project/' + st.project.id, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ people: st.people })
    }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function () { close(); location.reload(); })
      .catch(function (err) { st.saving = false; render(); alert('저장 실패: ' + err.message); });
  }

  window.openStaffPicker = function (pid) {
    var m = ensureModal();
    document.getElementById('sp-box').innerHTML = '<div style="padding:40px; text-align:center; color:#64748B;">불러오는 중…</div>';
    m.style.display = 'flex';
    fetch('/api/staffing/project/' + pid, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        st = { project: d.project, days: d.days, eventDays: d.project.event_days, people: d.people,
               workers: d.workers, busy: d.busy, q: '', saving: false };
        var box = document.getElementById('sp-box');
        if (!box.__bound) { box.addEventListener('click', onClick); box.addEventListener('change', onChange); box.__bound = true; }
        render();
      })
      .catch(function (err) { document.getElementById('sp-box').innerHTML = '<div style="padding:40px; text-align:center; color:#DC2626;">불러오기 실패: ' + esc(err.message) + '</div>'; });
  };
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
})();
