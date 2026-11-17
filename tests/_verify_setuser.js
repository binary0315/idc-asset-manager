// 运行时验证 setUser() 三条路径都会初始化日期（P1 回归）。
// 从 app.js 提取 todayStr/initMoveDate/setUser 三个函数，在 mock DOM 上真实执行。
const fs = require('fs'), vm = require('vm');
const js = fs.readFileSync(process.argv[2], 'utf8');
// 词法扫描：跳过字符串/模板串/注释/正则里的花括号，避免裸计数切错位置。
function extract(name) {
  const i = js.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到函数 ' + name);
  let depth = 0, started = false, state = 'code', prev = '';
  for (let j = i; j < js.length; j++) {
    const ch = js[j], nx = js[j + 1];
    if (state === 'line') { if (ch === '\n') state = 'code'; continue; }
    if (state === 'block') { if (ch === '*' && nx === '/') { state = 'code'; j++; } continue; }
    if (state === 'sq' || state === 'dq' || state === 'tpl') {
      if (ch === '\\') { j++; continue; }
      if ((state === 'sq' && ch === "'") || (state === 'dq' && ch === '"') || (state === 'tpl' && ch === '`')) state = 'code';
      continue;
    }
    if (state === 'regex') {
      if (ch === '\\') { j++; continue; }
      if (ch === '\n' || ch === '/') state = 'code';   // 量词 {n,m} 的括号被跳过
      continue;
    }
    if (ch === '/' && nx === '/') { state = 'line'; j++; continue; }
    if (ch === '/' && nx === '*') { state = 'block'; j++; continue; }
    if (ch === '/') { if (!prev || '(,=:[!&|?{;+-*/'.includes(prev)) state = 'regex'; prev = '/'; continue; }
    if (ch === '"') { state = 'dq'; prev = ch; continue; }
    if (ch === "'") { state = 'sq'; prev = ch; continue; }
    if (ch === '`') { state = 'tpl'; prev = ch; continue; }
    if (ch === '{') { depth++; started = true; prev = ch; continue; }
    if (ch === '}') { depth--; prev = ch; if (started && depth === 0) return js.slice(i, j + 1); continue; }
    if (!/\s/.test(ch)) prev = ch;
  }
  throw new Error('函数未闭合 ' + name);
}
const code = ['todayStr', 'initMoveDate', 'setUser'].map(extract).join('\n');

const movedAt = { value: '' };
const mkEl = () => ({
  classList: { add() {}, remove() {}, toggle() {} }, dataset: {},
  set innerHTML(v) {}, get innerHTML() { return ''; }, textContent: ''
});
const els = {
  '#login': mkEl(), '#app': mkEl(), '#who': mkEl(), '#usersTab': mkEl(),
  '#recycleTab': mkEl(), '#itemFormCard': mkEl(), '#importCard': mkEl(),
  '#moveFormCard': mkEl(), '#movementImportCard': mkEl(), '#serverImportCard': mkEl(),
  '#serverComponentImportCard': mkEl(), '#itemActionHeader': mkEl(), '#movementActionHeader': mkEl(),
  '#moveForm': { elements: { moved_at: movedAt } }
};
const sandbox = {
  console, Date, String, Number, Math, JSON, Object,
  $: s => els[s] || mkEl(),
  document: {
    querySelector: s => els[s] || null,
    querySelectorAll: () => []
  },
  esc: s => String(s ?? ''),
  me: null, csrf: ''
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'setuser.js' });

const today = new Date(), p = n => String(n).padStart(2, '0');
const expected = today.getFullYear() + '-' + p(today.getMonth() + 1) + '-' + p(today.getDate());
// 模拟三条登录路径都会调用 setUser（唯一入口）
for (const path of ['setup', 'login', 'session-restore']) {
  movedAt.value = '';
  sandbox.setUser({ id: 1, username: 'u', is_admin: 1, can_view: 1, can_edit: 1, csrf: 'c' });
  console.log(path + ' -> moved_at:', movedAt.value);
  if (movedAt.value !== expected) {
    console.error('FAIL ' + path + ' 日期未默认今天，期望 ' + expected);
    process.exit(1);
  }
}
console.log('PASS setUser 三条路径均默认今天');
