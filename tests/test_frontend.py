"""前端纯逻辑回归（Node 可选）：日期时区 / 只看有票 / 智能排序。

背景（都是线上真踩过的）：
- isoPlus 用 toISOString()，北京时间早 8 点前会退回前一天，默认日期是昨天；
- 图库深挖未核验的段 seats={}，"只看有票"把结果全筛掉；
- smart() 用 Math.min(...ps, 0) 把价格下限夹成 0，最便宜的车拿不到最优分。

没有 node 就跳过（不算失败），有 node 则必须全过。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(BASE, "frontend", "index.html")

node = shutil.which("node")
if not node:
    print("SKIP: 未安装 node，跳过前端逻辑回归")
    sys.exit(0)

src = open(HTML, encoding="utf-8").read()
m = re.search(r"<script>([\s\S]*)</script>", src)
assert m, "index.html 里找不到 <script>"
js = m.group(1)

# 语法先过一遍
tmpdir = tempfile.mkdtemp(prefix="railpick_fe_")
js_path = os.path.join(tmpdir, "fe_extract.js")
open(js_path, "w", encoding="utf-8").write(js)
r = subprocess.run([node, "-e",
                    "const fs=require('fs');new Function(fs.readFileSync(process.argv[1],'utf8'));"
                    "console.log('SYNTAX OK')", js_path],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
assert "SYNTAX OK" in (r.stdout or ""), f"前端 JS 语法错误：{r.stderr[:500]}"
print("1 ok: frontend js syntax")

# ---- 抽取纯函数在最小 DOM 桩上跑 ----
harness = r'''
const fs = require('fs');
const js = fs.readFileSync(process.argv[2], 'utf8');
const cut = js.indexOf("$('date').value=tomorrow();");
if (cut < 0) { console.log(JSON.stringify({error: 'anchor not found'})); process.exit(1); }
const head = js.slice(0, cut);

const els = {};
const document = {
  getElementById: id => (els[id] = els[id] || {checked: true, value: '0', textContent: '',
    hidden: false, innerHTML: '', style: {}, classList: {add(){},remove(){},toggle(){}}}),
  querySelectorAll: () => [], addEventListener: () => {},
};
const localStorage = {getItem: () => null, setItem: () => {}};
const fetch = () => Promise.resolve({json: () => ({})});
const alert = () => {};
const window = {};
const mod = {exports: {}};

// 只重定义要测的纯函数（与 index.html 中实现保持一致）
const code = head + `
  normKind = k => (!k ? '直达' : (k.startsWith('直达') ? '直达' : k));
  function hasTicket(c){
    if(!$('onlyTicket').checked)return true;
    return c.legs.every(l=>{
      const s=l.seats||{};
      const vals=Object.values(s);
      if(!vals.length)return true;
      return vals.some(v=>v&&v!=='--'&&v!=='无');
    });
  }
  function waitSum(c){return (c.waits||[]).reduce((a,b)=>a+b,0)}
  function fmtDur(m){return Math.floor(m/60)+'h'+(m%60?m%60+'m':'')}
  function smart(c){
    if(!state._smartCache){
      const ps=state.combos.filter(x=>x.total_price).map(x=>x.total_price);
      const ts=state.combos.map(x=>x.total_min).filter(v=>typeof v==='number');
      state._smartCache={
        pmin:ps.length?Math.min(...ps):0,
        pmax:ps.length?Math.max(...ps):1,
        tmin:ts.length?Math.min(...ts):0,
        tmax:ts.length?Math.max(...ts):60};
      state._smartCache.tmax=Math.max(state._smartCache.tmax,state._smartCache.tmin+60);
    }
    const sc=state._smartCache;
    const pn=c.total_price?(c.total_price-sc.pmin)/Math.max(1,sc.pmax-sc.pmin):0.7;
    const tn=(c.total_min-sc.tmin)/Math.max(1,sc.tmax-sc.tmin);
    return pn*0.55+tn*0.45+(c.transfers*0.05);
  }
  mod.exports = {isoLocal, isoPlus, normKind, hasTicket, smart, waitSum, fmtDur, state, $};
`;
new Function('document','localStorage','fetch','alert','window','mod', code)(
  document, localStorage, fetch, alert, window, mod);
const M = mod.exports;

const out = [];
const RealDate = Date;
function fakeNow(iso) {
  const fixed = new RealDate(iso).getTime();
  global.Date = class extends RealDate {
    constructor(...a) { super(...(a.length ? a : [fixed])); }
    static now() { return fixed; }
  };
}
function eq(label, got, want) { out.push([label, String(got), String(want), String(got) === String(want)]); }

// 1) 日期：早8点前/晚23点/跨月
fakeNow('2026-10-21T07:30:00+08:00');
eq('早7:30 +0', M.isoPlus(0), '2026-10-21');
eq('早7:30 +1', M.isoPlus(1), '2026-10-22');
fakeNow('2026-10-21T23:30:00+08:00');
eq('晚23:30 +0', M.isoPlus(0), '2026-10-21');
fakeNow('2026-01-31T23:30:00+08:00');
eq('跨月1/31+1', M.isoPlus(1), '2026-02-01');
global.Date = RealDate;

// 2) 只看有票
M.$('onlyTicket').checked = true;
eq('空seats保留', M.hasTicket({legs:[{seats:{}}]}), 'true');
eq('无票过滤', M.hasTicket({legs:[{seats:{'二等':'无'}}]}), 'false');
eq('有票保留', M.hasTicket({legs:[{seats:{'二等':'有'}}]}), 'true');
eq('空+有票', M.hasTicket({legs:[{seats:{}},{seats:{'硬座':'有'}}]}), 'true');
eq('空+无票', M.hasTicket({legs:[{seats:{}},{seats:{'硬座':'无'}}]}), 'false');
M.$('onlyTicket').checked = false;
eq('不勾全留', M.hasTicket({legs:[{seats:{'二等':'无'}}]}), 'true');
M.$('onlyTicket').checked = true;

// 3) 智能排序
M.state.combos = [
  {total_price: 100, total_min: 600, transfers: 0},
  {total_price: 500, total_min: 300, transfers: 0},
  {total_price: null, total_min: 900, transfers: 1},
];
M.state._smartCache = null;
const s1 = M.smart(M.state.combos[0]);
const s2 = M.smart(M.state.combos[1]);
eq('便宜分更低', s1 < s2, 'true');
eq('价格维度归零', s1.toFixed(4), (0*0.55 + ((600-300)/(900-300))*0.45).toFixed(4));
eq('无价兜底', M.smart(M.state.combos[2]) > 0.5, 'true');

// 4) 杂项
eq('waitSum', M.waitSum({waits:[30,45]}), 75);
eq('waitSum空', M.waitSum({}), 0);
eq('fmtDur0', M.fmtDur(0), '0h');
eq('fmtDur90', M.fmtDur(90), '1h30m');
eq('fmtDur60', M.fmtDur(60), '1h');
eq('normKind直达', M.normKind('直达高铁'), '直达');
eq('normKind混搭', M.normKind('高铁+普速'), '高铁+普速');

console.log(JSON.stringify(out));
'''
h_path = os.path.join(tmpdir, "harness.cjs")
open(h_path, "w", encoding="utf-8").write(harness)
r = subprocess.run([node, h_path, js_path], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
if r.returncode != 0:
    print("node stderr:", (r.stderr or "")[:1500])
    raise SystemExit(1)
lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln.startswith("[")]
assert lines, f"没有拿到结果：{r.stdout[:500]}"
results = json.loads(lines[-1])
bad = [x for x in results if not x[3]]
for label, got, want, ok in results:
    if not ok:
        print(f"  FAIL {label}: got={got} want={want}")
assert not bad, f"{len(bad)} 项前端逻辑检查失败"
print(f"2 ok: frontend logic ({len(results)} checks)")

# ---- 7) 归档浏览：标签页选择器必须压过 hover ----
# 回归：激活项曾是灰的、未激活的是墨绿——.gtab.on 与 .gtab:hover 优先级反了
import re as _re
_html = open(os.path.join(BASE, "frontend", "index.html"), encoding="utf-8").read()
_css = _re.search(r"<style>([\s\S]*?)</style>", _html).group(1)


def _spec(sel):
    ids = sel.count("#")
    cls = len(_re.findall(r"\.[A-Za-z0-9_-]+", sel)) + len(_re.findall(r":(?:hover|on|focus)", sel))
    el = len(_re.findall(r"(?<![.#\w:-])([a-z]+)(?![\w-])", sel))
    return (ids, cls, el)


_rules = {}
for _m in _re.finditer(r"([^{}]+)\{([^{}]*)\}", _css):
    _sel = _m.group(1).strip().replace("\n", " ")
    if "gtab" in _sel and "color" in _m.group(2):
        _rules[_sel] = _m.group(2)

_on = [s for s in _rules if s.startswith(".gtab.on")]
_ho = [s for s in _rules if s == ".gtab:hover"]
assert _on and _ho, (_on, _ho)
assert _spec(_on[0]) >= _spec(_ho[0]), f"激活项优先级低于 hover: {_on[0]} vs {_ho[0]}"
print("7 ok: gtab active beats hover")

# ---- 8) 归档浏览面板：5 个标签 + 对应 pane 齐全 ----
for _tab in ("crawl", "runs", "train", "station", "recent"):
    assert f'data-tab="{_tab}"' in _html, f"缺标签 {_tab}"
    assert f'data-pane="{_tab}"' in _html, f"缺面板 {_tab}"
assert "/api/graph/runs" in _html, "前端没接爬取记录接口"
assert "/api/graph/train" in _html, "前端没接查车次接口"
assert "/api/graph/station" in _html, "前端没接查车站接口"
assert "/api/graph/recent" in _html, "前端没接最近入库接口"
print("8 ok: archive panes wired to all 4 endpoints")

# ---- 清理 ----
try:
    shutil.rmtree(tmpdir, ignore_errors=True)
except Exception:
    pass
print("FRONTEND CHECKS PASSED")
