/* 폰에서 채팅 사이드바를 서랍으로 (mobile.js)
 *
 * ★CSS 만으로는 못 한다 — 여는 단추가 HTML 에 없다. 그 단추 하나와 덮개만 만든다.
 * ★chat.html 을 안 건드리는 이유: 그 파일은 1394줄이고 이미 서랍(#drawer)·모달·
 *   위임 핸들러가 얽혀 있다. 밖에서 얹으면 PC 동작에 손대지 않는다.
 */
(function () {
  'use strict';
  var side = document.getElementById('side');
  if (!side) return;                    // 채팅 말고 다른 페이지

  var btn = document.createElement('button');
  btn.id = 'sideOpen';
  btn.type = 'button';
  btn.textContent = '☰';
  btn.setAttribute('aria-label', '채팅 목록');
  btn.style.display = 'none';           // 넓은 화면에선 안 보인다 (CSS 가 켠다)

  var mask = document.createElement('div');
  mask.id = 'sideMask';

  document.body.appendChild(mask);
  document.body.appendChild(btn);

  function close() { document.body.classList.remove('side-on'); }
  btn.addEventListener('click', function () {
    document.body.classList.toggle('side-on');
  });
  mask.addEventListener('click', close);

  // 방을 고르면 닫는다 — 안 닫으면 고른 방이 서랍에 가려 안 보인다
  side.addEventListener('click', function (e) {
    if (e.target.closest('a,button,[data-id],.row')) close();
  });
  addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  // 화면을 넓히면(가로로 눕히면) 서랍 상태를 걷는다
  addEventListener('resize', function () { if (innerWidth > 600) close(); });
})();
