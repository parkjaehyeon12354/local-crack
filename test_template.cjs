// builder.html 의 틀 전개기를 그대로 꺼내 검사한다.
// 붙여넣은 사본이 아니라 실제 파일에서 읽는다 — 사본은 원본과 갈린다.
const fs = require('fs');
const src = fs.readFileSync('/mnt/d/코딩/크랙빌더/builder.html', 'utf8');
const grab = (name) => {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('없는 함수: ' + name);
  let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}' && --d === 0) return src.slice(i, k + 1);
  }
};
eval(grab('expandRange') + grab('expandTemplate'));
const eq = (a, b, m) => {
  const [x, y] = [JSON.stringify(a), JSON.stringify(b)];
  if (x !== y) { console.error('FAIL ' + m + '\n  got ' + x + '\n  want ' + y); process.exit(1); }
};

eq(expandRange('a,b,c'), ['a','b','c'], '쉼표');
eq(expandRange('a~b'), ['a','b'], '물결도 같다');
eq(expandRange('1..3'), ['1','2','3'], '범위');
eq(expandRange('0002..0004'), ['0002','0003','0004'], '앞자리 0 을 지켜야 한다');
eq(expandRange('S1..3'), ['S1','S2','S3'], '접두어');
eq(expandRange('5..1'), ['5..1'], '거꾸로면 손대지 않는다');
eq(expandRange(' a , , b '), ['a','b'], '빈 칸은 버린다');
eq(expandRange(''), [], '빈 입력');

const T = 'https://x/캐릭터/의상/상황.jpg';
eq(expandTemplate(T, [['캐릭터','a,b'],['의상','big'],['상황','1..2']]),
   ['https://x/a/big/1.jpg','https://x/a/big/2.jpg',
    'https://x/b/big/1.jpg','https://x/b/big/2.jpg'], '3자리 곱');
eq(expandTemplate(T, [['캐릭터','a'],['의상',''],['상황','1']]),
   ['https://x/a/의상/1.jpg'], '빈 자리는 그대로 둔다');
eq(expandTemplate('', [['캐릭터','a']]), [], '틀이 없으면 아무것도 안 만든다');
eq(expandTemplate('https://x/캐릭터/캐릭터.jpg', [['캐릭터','a']]),
   ['https://x/a/a.jpg'], '같은 자리가 두 번 나오면 둘 다 바꾼다');

console.log('틀 전개 ok');
