"""
주식/ETF 비교 대시보드 업데이트 스크립트
- 종목 목록: Google Sheets에서 읽어옴
- 시세 데이터: pykrx (KRX 공식 데이터, 해외 서버에서도 동작)
- GitHub Actions에서 매일 평일 오후 8시(KST) 자동 실행
"""

import json, os, sys, csv, io
import requests
from datetime import datetime, date, timedelta
from pykrx import stock

# ── Google Sheets에서 종목 목록 읽기 ──
def fetch_config(sheet_id):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    res = requests.get(url, timeout=10)
    res.raise_for_status()
    reader = csv.DictReader(io.StringIO(res.content.decode('utf-8')))
    items = []
    for row in reader:
        code   = row.get('종목코드', '').strip().zfill(6)
        name   = row.get('종목명', '').strip()
        start  = row.get('시작일', '').strip().replace('. ', '-').replace('.', '-')
        display = row.get('표시', '').strip().upper()
        if code and name and start and display == 'Y':
            # 날짜 형식 정리: 2026. 01. 01 → 2026-01-01
            import re
            start = re.sub(r'[^\d]', '-', start)
            start = re.sub(r'-+', '-', start).strip('-')
            short = row.get('축약명', '').strip() or name  # 없으면 정식명 사용
            items.append({'code': code, 'name': name, 'short': short, 'start': start})
    print(f"[설정] {len(items)}개 종목 (Y): {[x['code']+' '+x['name'] for x in items]}")
    return items

# ── pykrx로 일별 시세 가져오기 ──
def fetch_pykrx(code, start_date_str):
    """
    start_date_str: 'YYYY-MM-DD'
    데이터가 없는 구간(상장 전)은 자동으로 건너뜀
    반환: [{'date': 'YYYY/MM/DD', 'close': int}, ...]
    """
    start = start_date_str.replace('-', '')
    end   = date.today().strftime('%Y%m%d')

    try:
        df = stock.get_market_ohlcv(start, end, code)
    except Exception as e:
        print(f'  [{code}] pykrx 오류: {e}')
        return []

    if df is None or df.empty:
        print(f'  [{code}] 데이터 없음')
        return []

    rows = []
    for dt, row in df.iterrows():
        c = int(row['종가'])
        if c > 0:
            rows.append({'date': dt.strftime('%Y/%m/%d'), 'close': c})

    if not rows:
        print(f'  [{code}] 유효 데이터 없음')
        return []

    rows.sort(key=lambda x: x['date'])
    actual_start = rows[0]['date']
    req_start    = start_date_str.replace('-', '/')
    if actual_start != req_start:
        print(f'  [{code}] 시작일 자동조정: {req_start} → {actual_start} (상장일 기준)')
    print(f'  [{code}] {len(rows)}개 ({rows[0]["date"]} ~ {rows[-1]["date"]})')
    return rows

# ── 등락률 계산 ──
def add_changes(rows):
    for i, r in enumerate(rows):
        r['change'] = 0.0 if i == 0 else round((r['close'] / rows[i-1]['close'] - 1) * 100, 2)
    return rows

# ── HTML 생성 ──
def build_html(stocks):
    now = datetime.utcnow().strftime('%Y/%m/%d %H:%M UTC')
    palette = [
        ('#f04f5a','rgba(240,79,90,0.08)'),
        ('#4f9cf0','rgba(79,156,240,0.08)'),
        ('#3ecf8e','rgba(62,207,142,0.08)'),
        ('#f5a623','rgba(245,166,35,0.08)'),
        ('#b57bee','rgba(181,123,238,0.08)'),
        ('#50d8d7','rgba(80,216,215,0.08)'),
    ]
    # 누적수익률 높은 순으로 정렬
    stocks = sorted(stocks, key=lambda s: (s['data'][-1]['close']/s['data'][0]['close']-1), reverse=True)

    def sign(v): return f'+{v:.2f}' if v >= 0 else f'{v:.2f}'
    def col(v):  return palette[0][0] if v >= 0 else palette[1][0]

    all_dates = sorted(set(r['date'] for s in stocks for r in s['data']))
    labels = [d[5:] for d in all_dates]

    def align(data, mode):
        dmap = {r['date']: r for r in data}
        return [dmap[d][mode] if d in dmap else None for d in all_dates]

    def cum_vals(data):
        base = data[0]['close']
        dmap = {r['date']: r['close'] for r in data}
        return [round((dmap[d]/base-1)*100,2) if d in dmap else None for d in all_dates]

    all_cum_js = ',\n'.join(f'  {json.dumps(cum_vals(s["data"]))}' for s in stocks)
    all_day_js = ',\n'.join(f'  {json.dumps(align(s["data"],"change"))}' for s in stocks)

    rate_rows_html = ''
    for i, s in enumerate(stocks):
        c = palette[i % len(palette)][0]
        rate_rows_html += (
            f'<div class="rate-row" id="rateRowWrap{i}">'
            f'<span class="rate-label" style="color:{c}">{s["name"][:6]}</span>'
            f'<div class="rate-cells" id="rateRow{i}"></div></div>\n'
        )

    rate_js_lines = []
    for i, s in enumerate(stocks):
        rate_js_lines.append(f'buildRow("rateRow{i}", {json.dumps([r for r in s["data"]][-30:])});')
        if i >= MAX_ACTIVE:
            rate_js_lines.append(f'document.getElementById("rateRowWrap{i}").style.display="none";')
    rate_js = '\n'.join(rate_js_lines)

    EYE_ON  = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>' 
    EYE_OFF = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'

    chips_html = ''
    MAX_ACTIVE = 6
    for i, s in enumerate(stocks):
        c    = palette[i % len(palette)][0]
        last = s['data'][-1]
        cum  = round((last['close'] / s['data'][0]['close'] - 1) * 100, 2)
        chips_html += (
            f'<div class="chip{" off" if i >= MAX_ACTIVE else ""}" id="chip{i}" style="--c:{c}" onclick="toggleDetail({i})">' 
            f'<div class="chip-dot" style="background:{c}"></div>'
            f'<div class="chip-info">'
            f'<span class="chip-name">{s["short"]}</span>'
            f'<span class="chip-code">{s["code"]}</span>'
            f'</div>'
            f'<div class="chip-right">'
            f'<span class="chip-cum" style="color:{c}">{sign(cum)}%</span>'
            f'<span class="chip-eye" id="eye{i}" onclick="event.stopPropagation();toggleStock({i})">'
            f'<svg id="eyeIcon{i}" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{EYE_ON}</svg>'
            f'</span>'
            f'</div>'
            f'<div class="chip-detail" id="chipDetail{i}">'
            f'<div style="color:{c};font-size:11px;font-weight:700;margin-bottom:5px;padding-bottom:4px;border-bottom:1px solid #2a3f5f">{s["name"]}</div>'
            f'<div class="cd-row"><span>현재가</span><span>{last["close"]:,}원</span></div>'
            f'<div class="cd-row"><span>전일대비</span><span style="color:{col(last["change"])}">{sign(last["change"])}%</span></div>'
            f'<div class="cd-row"><span>누적</span><span style="color:{col(cum)}">{sign(cum)}%</span></div>'
            f'</div>'
            f'</div>'
        )

    palette_js  = json.dumps([p[0] for p in palette])
    names_js    = json.dumps([s['name'] for s in stocks])
    labels_js   = json.dumps(labels)
    stocks_sub  = ' vs '.join(s['name'] for s in stocks)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>주식 비교 대시보드</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0e1117;--surface:#161b27;--border:#252d3d;--text:#e2e8f0;--muted:#6b7a99;--mono:'JetBrains Mono',monospace;--sans:'Noto Sans KR',sans-serif}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding:20px 16px 48px}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}}
.title-group h1{{font-size:16px;font-weight:700}}
.title-group .sub{{font-size:10px;color:var(--muted);margin-top:2px;font-family:var(--mono)}}
.updated{{font-size:10px;color:var(--muted);font-family:var(--mono);text-align:right}}
.chips{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;margin-bottom:12px}}
.chip{{display:flex;align-items:center;gap:6px;padding:7px 8px 7px 10px;border-radius:10px;width:100%;min-width:0;background:var(--surface);border:1px solid color-mix(in srgb,var(--c) 30%,transparent);cursor:pointer;transition:opacity .2s;position:relative;user-select:none}}
.chip.off{{opacity:0.28;filter:grayscale(.7)}}
.chip-dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
.chip-info{{display:flex;flex-direction:column;flex:1;min-width:0;overflow:hidden}}
.chip-name{{font-size:11px;color:var(--text);font-family:var(--mono);line-height:1.2}}
.chip-code{{font-size:9px;color:var(--muted);font-family:var(--mono)}}
.chip-right{{display:flex;flex-direction:column;align-items:flex-end;gap:2px;margin-left:auto;flex-shrink:0}}
.chip-cum{{font-size:10px;font-family:var(--mono);font-weight:600;white-space:nowrap;text-align:right}}
.chip-right{{display:flex;flex-direction:column;align-items:flex-end;gap:2px;margin-left:auto;flex-shrink:0}}
.chip-eye{{padding:2px 3px;border-radius:4px;display:flex;align-items:center;transition:color .1s;color:var(--muted)}}
.chip-eye:hover{{color:var(--text)}}
.chip.off .chip-eye svg{{stroke:#8aa0bc}}
.chip.off .chip-eye{{opacity:1!important}}
.chip-detail{{display:none;position:absolute;top:calc(100% + 6px);left:0;z-index:20;background:#1a2438;border:1px solid #2a3f5f;border-radius:8px;padding:8px 12px;min-width:150px;font-size:11px;font-family:var(--mono);color:var(--text);white-space:nowrap;box-shadow:0 4px 16px rgba(0,0,0,.4)}}
.chip.show-detail .chip-detail{{display:block}}
.cd-row{{display:flex;justify-content:space-between;gap:14px;margin-bottom:3px}}
.cd-row span:first-child{{color:var(--muted)}}
.tabs{{display:flex;gap:3px;margin-bottom:10px}}
.tab{{padding:4px 11px;border-radius:5px;font-size:10px;font-family:var(--mono);cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--muted);transition:all .12s}}
.tab.active{{background:var(--surface);color:var(--text);border-color:#2a3f5f}}
.chart-box{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 14px 10px;margin-bottom:12px}}
.chart-box h2{{font-size:10px;color:var(--muted);font-family:var(--mono);margin-bottom:6px;text-transform:uppercase;letter-spacing:.8px}}
.chart-wrap{{position:relative;width:100%;height:240px}}
.chart-wrap-sm{{position:relative;width:100%;height:180px}}
.tooltip-panel{{background:#1a2438;border:1px solid #2a3f5f;border-radius:8px;padding:8px 12px;margin-top:10px;font-size:11px;font-family:var(--mono);color:var(--text);display:none}}
.tooltip-panel.visible{{display:block}}
.tp-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
.tp-date{{color:var(--muted);font-size:11px;font-weight:600}}
.tp-close{{cursor:pointer;color:var(--muted);font-size:16px;line-height:1;padding:0 3px}}
.tp-close:hover{{color:#ef4444}}
.tp-rows{{display:flex;flex-wrap:wrap;gap:5px 14px}}
.tp-row{{display:flex;align-items:center;gap:5px;font-size:11px}}
.tp-dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0}}
.rate-table{{margin-top:10px;border-top:1px solid var(--border);padding-top:8px}}
.rate-row{{display:flex;gap:5px;align-items:flex-start;margin-bottom:6px}}
.rate-label{{font-size:10px;font-family:var(--mono);width:60px;flex-shrink:0;padding-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.rate-cells{{display:flex;gap:3px;flex-wrap:wrap}}
.rate-cell{{font-size:10px;font-family:var(--mono);padding:2px 5px;border-radius:3px;text-align:center;min-width:50px}}
.rate-cell .rc-d{{color:var(--muted);font-size:9px;display:block}}
.rate-cell .rc-v{{font-weight:600}}
.rate-cell.up{{background:rgba(240,79,90,.1)}}.rate-cell.dn{{background:rgba(79,156,240,.1)}}
.rate-cell{{cursor:pointer;border:1px solid transparent;transition:border-color .1s,background .1s}}
.rate-cell.selected{{border-color:rgba(255,255,255,.35)!important;background:rgba(255,255,255,.07)!important}}
.rate-cell.selected .rc-d{{color:var(--text)}}
.rate-cell:hover{{border-color:rgba(255,255,255,.15)}}
.disclaimer{{font-size:10px;color:#3a4a5a;font-family:var(--mono);margin-top:8px;text-align:right}}
.copyright{{font-size:10px;color:#3a4a5a;font-family:var(--mono);text-align:center;margin-top:20px;padding-top:12px;border-top:1px solid var(--border)}}
</style>
</head>
<body>
<header>
  <div class="title-group">
    <h1>주식 비교 대시보드 <span style="font-size:10px;font-weight:400;color:#f04f5a;font-family:var(--mono)">NXT(시간외)미반영</span></h1>
    <div class="sub">{stocks_sub}</div>
  </div>
  <div class="updated">{now}</div>
</header>

<div class="chips" id="chipsWrap">{chips_html}</div>

<div class="tabs">
  <button class="tab active" onclick="setTab('cum')">누적 수익률</button>
  <button class="tab" onclick="setTab('day')">일별 등락률</button>
</div>

<div class="chart-box" id="tab-cum">
  <h2>누적 수익률 (%)</h2>
  <div class="chart-wrap"><canvas id="chartCum"></canvas></div>
  <div class="tooltip-panel" id="tpCum">
    <div class="tp-header"><span class="tp-date" id="tpCumDate"></span><span class="tp-close" onclick="closeTooltip('cum')">×</span></div>
    <div class="tp-rows" id="tpCumRows"></div>
  </div>
  <div class="disclaimer">* 정규장 종가 기준 · 시간외거래 미반영 · 데이터 오류 가능성 있음</div>
</div>

<div class="chart-box" id="tab-day" style="display:none">
  <h2>일별 등락률 (%)</h2>
  <div class="chart-wrap-sm"><canvas id="chartDay"></canvas></div>
  <div class="tooltip-panel" id="tpDay">
    <div class="tp-header"><span class="tp-date" id="tpDayDate"></span><span class="tp-close" onclick="closeTooltip('day')">×</span></div>
    <div class="tp-rows" id="tpDayRows"></div>
  </div>
  <div class="rate-table">{rate_rows_html}</div>
  <div class="disclaimer">* 정규장 종가 기준 · 시간외거래 미반영 · 데이터 오류 가능성 있음</div>
</div>

<script>
const labels  = {labels_js};
const cumData = [{all_cum_js}];
const dayData = [{all_day_js}];
const palette = {palette_js};
const names   = {names_js};
const active  = names.map((_,i)=>i<6);
let pinnedCum=false, pinnedDay=false;

const EYE_ON  = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
const EYE_OFF = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';

function sign(v){{return v==null?'—':(v>=0?'+':'')+v.toFixed(2);}}
Chart.defaults.color='#6b7a99';
Chart.defaults.borderColor='#252d3d';
Chart.defaults.font.family="'JetBrains Mono',monospace";

const endLabelPlugin={{
  id:'endLabel',
  afterDatasetsDraw(chart){{
    const ctx=chart.ctx;
    chart.data.datasets.forEach((ds,i)=>{{
      if(!active[i])return;
      const meta=chart.getDatasetMeta(i);
      if(meta.hidden)return;
      const pts=meta.data.filter(p=>p&&!isNaN(p.x));
      if(!pts.length)return;
      const last=pts[pts.length-1];
      const vals=ds.data.filter(v=>v!=null);
      if(!vals.length)return;
      ctx.save();
      ctx.font='600 10px JetBrains Mono,monospace';
      ctx.fillStyle=ds.borderColor;
      ctx.textAlign='left';
      ctx.fillText(sign(vals[vals.length-1])+'%',last.x+6,last.y+4);
      ctx.restore();
    }});
  }}
}};

function mkDatasets(data){{
  return data.map((d,i)=>{{
    const c=palette[i%palette.length];
    return {{label:names[i],data:d,borderColor:c,backgroundColor:c+'14',
      borderWidth:1.6,pointRadius:0,tension:0.2,fill:false,
      borderDash:i===0?[]:[5+i,3],hidden:!active[i],spanGaps:false}};
  }});
}}

function showTooltip(which,idx){{
  const data=which==='cum'?cumData:dayData;
  document.getElementById(which==='cum'?'tpCumDate':'tpDayDate').textContent=labels[idx];
  document.getElementById(which==='cum'?'tpCumRows':'tpDayRows').innerHTML=
    names.map((n,i)=>{{
      if(!active[i]||data[i][idx]==null)return'';
      const c=palette[i%palette.length],v=data[i][idx],vc=v>=0?'#f04f5a':'#4f9cf0';
      return `<div class="tp-row"><div class="tp-dot" style="background:${{c}}"></div><span>${{n}}</span><span style="color:${{vc}};font-weight:600">${{sign(v)}}%</span></div>`;
    }}).join('');
  document.getElementById(which==='cum'?'tpCum':'tpDay').classList.add('visible');
}}

function closeTooltip(which){{
  if(which==='cum'){{pinnedCum=false;document.getElementById('tpCum').classList.remove('visible');}}
  else{{pinnedDay=false;document.getElementById('tpDay').classList.remove('visible');}}
}}

const makeOpts=which=>({{
  responsive:true,maintainAspectRatio:false,
  layout:{{padding:{{right:52}}}},
  plugins:{{
    legend:{{display:false}},
    tooltip:{{enabled:false,external(ctx){{
      const pinned=which==='cum'?pinnedCum:pinnedDay;
      if(pinned)return;
      const {{tooltip}}=ctx;
      if(tooltip.opacity===0)return;
      showTooltip(which,tooltip.dataPoints[0].dataIndex);
    }}}}
  }},
  scales:{{
    x:{{ticks:{{font:{{size:10}},maxTicksLimit:12,maxRotation:0}},grid:{{color:'#1a2535'}}}},
    y:{{ticks:{{font:{{size:10}},callback:v=>sign(v)+'%'}},grid:{{color:'#1a2535'}}}}
  }},
  interaction:{{mode:'index',intersect:false}},
  onClick(e,els){{
    if(!els.length)return;
    if(which==='cum')pinnedCum=true; else pinnedDay=true;
    showTooltip(which,els[0].index);
  }}
}});

const chartCum=new Chart(document.getElementById('chartCum'),{{type:'line',data:{{labels,datasets:mkDatasets(cumData)}},options:makeOpts('cum'),plugins:[endLabelPlugin]}});
const chartDay=new Chart(document.getElementById('chartDay'),{{type:'line',data:{{labels,datasets:mkDatasets(dayData)}},options:makeOpts('day'),plugins:[endLabelPlugin]}});
// 초기 하이드 종목 눈 아이콘 교체
names.forEach((_,i)=>{{
  if(!active[i]){{
    const el=document.getElementById('eyeIcon'+i);
    if(el)el.innerHTML=EYE_OFF;
  }}
}});

function toggleDetail(i){{
  const chip=document.getElementById('chip'+i);
  if(!active[i])return;  // 하이드 상태면 무반응
  const wasOpen=chip.classList.contains('show-detail');
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('show-detail'));
  if(!wasOpen)chip.classList.add('show-detail');
}}

function toggleStock(i){{
  active[i]=!active[i];
  const chip=document.getElementById('chip'+i);
  chip.classList.toggle('off',!active[i]);
  chip.classList.remove('show-detail');  // 상세 닫기
  document.getElementById('eyeIcon'+i).innerHTML=active[i]?EYE_ON:EYE_OFF;
  const wrap=document.getElementById('rateRowWrap'+i);
  if(wrap)wrap.style.display=active[i]?'':'none';
  chartCum.data.datasets=mkDatasets(cumData);
  chartDay.data.datasets=mkDatasets(dayData);
  chartCum.update();chartDay.update();
}}

document.addEventListener('click',e=>{{
  if(!e.target.closest('.chip'))
    document.querySelectorAll('.chip').forEach(c=>c.classList.remove('show-detail'));
}});

function buildRow(id,rows){{
  const el=document.getElementById(id);
  if(!el)return;
  el.innerHTML=rows.map(r=>{{
    const cls=r.change>=0?'up':'dn',c=r.change>=0?'#f04f5a':'#4f9cf0';
    return `<div class="rate-cell ${{cls}}"><span class="rc-d">${{r.date.slice(5)}}</span><span class="rc-v" style="color:${{c}}">${{sign(r.change)}}%</span></div>`;
  }}).join('');
}}
{rate_js}

// 날짜 선택 하이라이트
let selectedDate = null;
document.addEventListener('click', e => {{
  const cell = e.target.closest('.rate-cell');
  if (!cell) return;
  const date = cell.querySelector('.rc-d')?.textContent;
  if (!date) return;
  if (selectedDate === date) {{
    selectedDate = null;
    document.querySelectorAll('.rate-cell').forEach(c => c.classList.remove('selected'));
  }} else {{
    selectedDate = date;
    document.querySelectorAll('.rate-cell').forEach(c => {{
      c.classList.toggle('selected', c.querySelector('.rc-d')?.textContent === date);
    }});
  }}
}});

function setTab(t){{
  document.getElementById('tab-cum').style.display=t==='cum'?'':' none';
  document.getElementById('tab-day').style.display=t==='day'?'':' none';
  document.querySelectorAll('.tab').forEach((el,i)=>
    el.classList.toggle('active',(i===0&&t==='cum')||(i===1&&t==='day')));
}}
</script>
  <div class="copyright">© 2026 코렐리안 · All Rights Reserved</div>
</body>
</html>"""


if __name__ == '__main__':
    SHEET_ID = os.environ.get('SHEET_ID', '')
    if not SHEET_ID:
        print('오류: SHEET_ID 환경변수 없음')
        sys.exit(1)

    print('=== 주식 비교 대시보드 업데이트 ===')
    items = fetch_config(SHEET_ID)
    if not items:
        print('오류: 종목 목록 비어있음')
        sys.exit(1)

    stocks = []
    for item in items:
        print(f"\n[수집] {item['code']} {item['name']}")
        data = fetch_pykrx(item['code'], item['start'])
        if not data:
            print(f"  경고: {item['code']} 건너뜀")
            continue
        stocks.append({'code': item['code'], 'name': item['name'], 'short': item.get('short', item['name']), 'data': add_changes(data)})

    if len(stocks) < 2:
        print('오류: 최소 2개 종목 필요')
        sys.exit(1)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'etf_dashboard.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(build_html(stocks))

    print(f'\n✓ 완료: {out}')
    for s in stocks:
        cum = (s['data'][-1]['close'] / s['data'][0]['close'] - 1) * 100
        print(f"  {s['name']}: {cum:+.2f}%")
