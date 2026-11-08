/* 화면 → 이미지(PNG) 저장
   - saveNodeAsImage(node, filename)        : 화면 영역을 그대로 이미지로
   - saveStaffRosterImage(projectId)        : 프로젝트 인력 배치표를 카드 이미지로
   html2canvas 는 필요할 때만 불러옴. 휴대폰은 '공유'(카톡 등) → 실패 시 다운로드.
*/
(function () {
  var H2C = 'https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js';
  var loading = null;
  function loadLib() {
    if (window.html2canvas) return Promise.resolve();
    if (loading) return loading;
    loading = new Promise(function (ok, no) {
      var s = document.createElement('script'); s.src = H2C; s.onload = ok;
      s.onerror = function () { loading = null; no(new Error('이미지 도구를 불러오지 못했습니다. 인터넷 연결을 확인하세요.')); };
      document.head.appendChild(s);
    });
    return loading;
  }
  function toast(msg) {
    var t = document.getElementById('si-toast');
    if (!t) { t = document.createElement('div'); t.id = 'si-toast'; t.style.cssText = 'position:fixed; left:50%; bottom:90px; transform:translateX(-50%); z-index:100000; background:#0B1220; background-image:linear-gradient(#0B1220,#0B1220); color:#fff; padding:12px 18px; border-radius:12px; font-size:14px; font-weight:700; box-shadow:0 10px 30px rgba(0,0,0,.25); max-width:90vw; text-align:center;'; document.body.appendChild(t); }
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(t._h); if (msg.indexOf('…') < 0) t._h = setTimeout(function () { t.style.display = 'none'; }, 2600);
  }
  function safeName(s) { return String(s || '인력배치').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 80); }
  function deliver(canvas, filename) {
    return new Promise(function (ok) {
      canvas.toBlob(function (blob) {
        var file = null;
        try { file = new File([blob], filename, { type: 'image/png' }); } catch (e) {}
        var isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
        if (isMobile && file && navigator.canShare && navigator.canShare({ files: [file] })) {
          navigator.share({ files: [file], title: filename }).then(function () { toast('✓ 이미지를 공유했습니다'); ok(); })
            .catch(function () { download(blob, filename); ok(); });
          return;
        }
        download(blob, filename); ok();
      }, 'image/png');
    });
  }
  function download(blob, filename) {
    var url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    toast('✓ 이미지 저장: ' + filename);
  }
  function capture(node, filename) {
    toast('이미지 만드는 중…');
    return loadLib().then(function () {
      return (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve());
    }).then(function () {
      return window.html2canvas(node, { backgroundColor: '#FFFFFF', scale: Math.min(2, (window.devicePixelRatio || 1) + 0.5), useCORS: true, logging: false,
        windowWidth: Math.max(node.scrollWidth + 40, 800) });
    }).then(function (canvas) { return deliver(canvas, filename); })
      .catch(function (e) { toast('이미지 저장 실패: ' + (e && e.message || e)); });
  }

  // 화면 영역 그대로 (가로 스크롤 영역도 펼쳐서)
  window.saveNodeAsImage = function (node, filename, title) {
    if (typeof node === 'string') node = document.querySelector(node);
    if (!node) return;
    var box = document.createElement('div');
    box.style.cssText = 'position:fixed; left:-100000px; top:0; background:#fff; padding:24px; font-family:Pretendard, sans-serif; color:#0F172A;';
    if (title) box.innerHTML = '<div style="font-size:22px; font-weight:800; margin-bottom:4px;">' + title + '</div><div style="font-size:12px; color:#64748B; margin-bottom:14px;">' + new Date().toLocaleString('ko-KR') + ' 기준</div>';
    var clone = node.cloneNode(true);
    clone.style.overflow = 'visible'; clone.style.maxWidth = 'none'; clone.style.width = node.scrollWidth + 'px';
    clone.querySelectorAll('*').forEach(function (el) { var cs = el.style; if (cs.position === 'sticky') cs.position = 'static'; });
    clone.querySelectorAll('.sf-head, .sf-name').forEach(function (el) { el.style.position = 'static'; });
    // 저장 이미지에는 '배치하기' 같은 조작 링크 제외
    clone.querySelectorAll('a').forEach(function (a) { if (/배치/.test(a.textContent) && !a.classList.contains('sf-bar')) { if (a.closest('.sf-name')) { var sm = a.parentNode; a.remove(); if (sm) sm.innerHTML = sm.innerHTML.replace(/\s*·\s*$/, ''); } else if (!a.closest('.sf-name')) a.remove(); } });
    box.appendChild(clone); document.body.appendChild(box);
    return capture(box, safeName(filename) + '.png').then(function () { box.remove(); }, function () { box.remove(); });
  };

  // 프로젝트 인력 배치표 카드
  var WD = ['일', '월', '화', '수', '목', '금', '토'];
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  window.saveStaffRosterImage = function (pid) {
    toast('이미지 만드는 중…');
    return fetch('/api/staffing/project/' + pid, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { return buildRoster(d); });
  };
  window.buildStaffRosterFromState = function (d) { return buildRoster(d); };

  function buildRoster(d) {
    var ev = {}; (d.project.event_days || []).forEach(function (x) { ev[x] = 1; });
    var used = {}; d.people.forEach(function (p) { p.days.forEach(function (x) { used[x] = 1; }); });
    var days = (d.days || []).filter(function (x) { return ev[x] || used[x]; });
    if (!days.length) days = d.days || [];
    var people = d.people.filter(function (p) { return p.days.length; });
    var offer = people.filter(function (p) { return p.offer; }).length;
    var manDays = people.reduce(function (n, p) { return n + p.days.length; }, 0);
    var period = days.length ? (fmt(days[0]) + (days.length > 1 ? ' ~ ' + fmt(days[days.length - 1]) : '')) : '';
    var cw = days.length > 10 ? 44 : 64;
    var h = '<div style="width:' + Math.max(560, 200 + days.length * cw + 70) + 'px; padding:28px; background:#fff; font-family:Pretendard, sans-serif; color:#0F172A;">'
      + '<div style="font-size:13px; font-weight:700; color:#4F46E5;">👷 인력 배치표</div>'
      + '<div style="font-size:26px; font-weight:800; letter-spacing:-0.02em; margin-top:4px;">' + esc(d.project.name) + '</div>'
      + '<div style="font-size:14px; color:#475569; margin-top:6px;">' + esc(period) + ' · ' + days.length + '일 · 총 <b>' + people.length + '명</b>' + (offer ? ' (오퍼 ' + offer + '명)' : '') + ' · 연인원 ' + manDays + '일</div>'
      + '<table style="border-collapse:collapse; margin-top:18px; font-size:14px;">'
      + '<tr><th style="text-align:left; padding:8px 12px; background:#0B1220; color:#fff; border-radius:8px 0 0 0; min-width:150px;">이름</th>';
    days.forEach(function (x, i) {
      var dt = new Date(x + 'T00:00:00'), we = dt.getDay() === 0 || dt.getDay() === 6;
      h += '<th style="width:' + cw + 'px; padding:6px 0; background:' + (ev[x] ? '#0B1220' : '#334155') + '; color:' + (we ? '#FCA5A5' : '#fff') + '; font-size:12px; line-height:1.3;' + (i === days.length - 1 ? 'border-radius:0 8px 0 0;' : '') + '">' + (dt.getMonth() + 1) + '/' + dt.getDate() + '<br><span style="font-weight:500; opacity:.8;">' + WD[dt.getDay()] + (ev[x] ? '' : ' 준비') + '</span></th>';
    });
    h += '<th style="padding:6px 10px; background:#0B1220; color:#fff; font-size:12px;">일수</th></tr>';
    people.forEach(function (p, r) {
      var set = {}; p.days.forEach(function (x) { set[x] = 1; });
      var bg = r % 2 ? '#F8FAFC' : '#FFFFFF';
      h += '<tr><td style="padding:9px 12px; font-weight:700; background:' + bg + '; border-bottom:1px solid #E2E8F0;">' + esc(p.name)
        + (p.offer ? ' <span style="font-size:11px; color:#6D28D9; background:#EDE9FE; padding:2px 7px; border-radius:999px;">★ 오퍼</span>' : '') + '</td>';
      days.forEach(function (x) {
        h += '<td style="text-align:center; background:' + bg + '; border-bottom:1px solid #E2E8F0;">' + (set[x] ? '<span style="display:inline-block; width:22px; height:22px; border-radius:6px; background:' + (p.offer ? '#7C3AED' : '#2563EB') + '; vertical-align:middle;"><span style="display:block; width:6px; height:11px; margin:4px 0 0 8px; border:solid #fff; border-width:0 3px 3px 0; transform:rotate(45deg);"></span></span>' : '<span style="color:#CBD5E1;">·</span>') + '</td>';
      });
      h += '<td style="text-align:center; font-weight:800; background:' + bg + '; border-bottom:1px solid #E2E8F0;">' + p.days.length + '</td></tr>';
    });
    h += '<tr><td style="padding:8px 12px; font-weight:800; background:#EEF2FF;">일별 인원</td>';
    days.forEach(function (x) { var n = people.filter(function (p) { return p.days.indexOf(x) >= 0; }).length; h += '<td style="text-align:center; font-weight:800; background:#EEF2FF;">' + n + '</td>'; });
    h += '<td style="text-align:center; font-weight:800; background:#EEF2FF;">' + manDays + '</td></tr></table>';
    if (!people.length) h += '<div style="margin-top:16px; color:#64748B;">배치된 인원이 없습니다.</div>';
    h += '<div style="margin-top:16px; font-size:11px; color:#94A3B8;">' + new Date().toLocaleString('ko-KR') + ' 기준 · 정산관리</div></div>';
    var box = document.createElement('div'); box.style.cssText = 'position:fixed; left:-100000px; top:0;'; box.innerHTML = h;
    document.body.appendChild(box);
    var name = '인력배치_' + safeName(d.project.name) + (days.length ? '_' + days[0].slice(5).replace('-', '') : '') + '.png';
    return capture(box.firstChild, name).then(function () { box.remove(); }, function () { box.remove(); });
  }
  function fmt(x) { var d = new Date(x + 'T00:00:00'); return d.getFullYear() + '.' + (d.getMonth() + 1) + '.' + d.getDate() + '(' + WD[d.getDay()] + ')'; }
})();
