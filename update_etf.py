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
            items.append({'code': code, 'name': name, 'start': start})
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
        ('#f04f5a', 'rgba(240,79,90,0.08)'),
        ('#4f9cf0', 'rgba(79,156,240,0.08)'),
        ('#3ecf8e', 'rgba(62,207,142,0.08)'),
        ('#f5a623', 'rgba(245,166,35,0.08)'),
        ('#b57bee', 'rgba(181,123,238,0.08)'),
        ('#50d8d7', 'rgba(80,216,215,0.08)'),
    ]

    def sign(v): return f'+{v:.2f}' if v >= 0 else f'{v:.2f}'
    def col(v):  return palette[0][0] if v >= 0 else palette[1][0]

    # 전체 날짜 유니온 (상장일이 다른 종목 대비)
    all_dates = sorted(set(r['date'] for s in stocks for r in s['data']))
    labels = [d[5:] for d in all_dates]  # MM/DD 형식

    # 각 종목 데이터를 전체 날짜에 맞춰 정렬 (없는 날은 None)
    def align_data(data, mode):
        dmap = {r['date']: r for r in data}
        result = []
        for d in all_dates:
            if d in dmap:
                result.append(dmap[d][mode])
            else:
                result.append(None)
        return result

    def cum_vals(data):
        # 실제 데이터 있는 첫날 기준
        first_close = data[0]['close']
        dmap = {r['date']: r['close'] for r in data}
        result = []
        for d in all_dates:
            if d in dmap:
                result.append(round((dmap[d] / first_close - 1) * 100, 2))
            else:
                result.append(None)
        return result

    all_cum_js = ',\n'.join(
        f"  {json.dumps(cum_vals(s['data']))}"
        for s in stocks
    )

    rate_rows_html = ''
    for i, s in enumerate(stocks):
        c = palette[i % len(palette)][0]
        rate_rows_html += f'<div class="rate-row" id="rateRowWrap{i}"><span class="rate-label" style="color:{c}">{s["name"][:6]}</span><div class="rate-cells" id="rateRow{i}"></div></div>\n'

    rate_js = '\n'.join(
        f"buildRow('rateRow{i}', {json.dumps([r for r in s['data'] if r][-30:])});"
        for i, s in enumerate(stocks)
    )

    # 칩 HTML
    chips_html = ''
    for i, s in enumerate(stocks):
        c = palette[i % len(palette)][0]
        last = s['data'][-1]
        cum  = round((last['close'] / s['data'][0]['close'] - 1) * 100, 2)
        chips_html += f'''
  <div class="chip" id="chip{i}" style="--c:{c}" onclick="toggleStock({i})">
    <div class="chip-dot" style="background:{c}"></div>
    <div class="chip-info">
      <span class="chip-name">{s["name"]}</span>
      <span class="chip-code">{s["code"]}</span>
    </div>
    <span class="chip-cum" style="color:{c}">{sign(cum)}%</span>
    <div class="chip-detail" id="chipDetail{i}">
      <div class="cd-row"><span>현재가</span><span>{last["close"]:,}원</span></div>
      <div class="cd-row"><span>전일대비</span><span style="color:{col(last["change"])}">{sign(last["change"])}%</span></div>
      <div class="cd-row"><span>누적수익률</span><span style="color:{col(cum)}">{sign(cum)}%</span></div>
    </div>
    <span class="chip-x" onclick="event.stopPropagation();toggleStock({i})">×</span>
  </div>''' 

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
:root{{--bg:#0e1117;--surface:#161b27;--border:#252d3d;--text:#e2e8f0;--muted:#6b7a99;
  --mono:'JetBrains Mono',monospace;--sans:'Noto Sans KR',sans-serif;}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding:20px 16px 48px}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}}
.title-group h1{{font-size:16px;font-weight:700}}
.title-group .sub{{font-size:10px;color:var(--muted);margin-top:2px;font-family:var(--mono)}}
.updated{{font-size:10px;color:var(--muted);font-family:var(--mono);text-align:right}}

/* 칩 */
.chips{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}}
.chip{{display:flex;align-items:center;gap:6px;padding:5px 8px 5px 10px;border-radius:20px;
  background:var(--surface);border:1px solid color-mix(in srgb, var(--c) 30%, transparent);
  cursor:pointer;transition:opacity .2s;position:relative;user-select:none}}
.chip.off{{opacity:0.28;filter:grayscale(.6)}}
.chip-dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
.chip-info{{display:flex;flex-direction:column}}
.chip-name{{font-size:11px;color:var(--text);font-family:var(--mono);line-height:1.2}}
.chip-code{{font-size:9px;color:var(--muted);font-family:var(--mono)}}
.chip-cum{{font-size:11px;font-family:var(--mono);font-weight:600;margin-left:2px}}
.chip-x{{font-size:14px;color:var(--muted);padding:0 2px;line-height:1;transition:color .1s;margin-left:2px}}
.chip-x:hover{{color:#ef4444}}
.chip.off .chip-x{{color:#3a4a5a}}

/* 칩 상세 팝업 */
.chip-detail{{display:none;position:absolute;top:calc(100% + 6px);left:0;z-index:20;
  background:#1a2438;border:1px solid #2a3f5f;border-radius:8px;
  padding:8px 12px;min-width:150px;font-size:11px;font-family:var(--mono);
  color:var(--text);white-space:nowrap;pointer-events:none}}
.chip:hover .chip-detail{{display:block}}
.cd-row{{display:flex;justify-content:space-between;gap:14px;margin-bottom:3px}}
.cd-row span:first-child{{color:var(--muted)}}

/* 탭 */
.tabs{{display:flex;gap:3px;margin-bottom:10px}}
.tab{{padding:4px 11px;border-radius:5px;font-size:10px;font-family:var(--mono);
  cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--muted);transition:all .12s}}
.tab.active{{background:var(--surface);color:var(--text);border-color:#2a3f5f}}

/* 차트 박스 */
.chart-box{{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:14px 14px 10px;margin-bottom:12px;position:relative}}
.chart-box h2{{font-size:10px;color:var(--muted);font-family:var(--mono);
  margin-bottom:10px;text-transform:uppercase;letter-spacing:.8px}}
.chart-wrap{{position:relative;width:100%;height:240px;margin-top:8px}}
.chart-wrap-sm{{position:relative;width:100%;height:180px;margin-top:8px}}

/* 툴팁 */
.chart-tooltip{{
  display:none;position:absolute;z-index:10;
  background:#1a2438;border:1px solid #2a3f5f;border-radius:7px;
  padding:7px 10px;font-size:11px;font-family:var(--mono);
  color:var(--text);min-width:130px;pointer-events:none;
}}
.chart-tooltip.pinned{{display:block;pointer-events:auto}}
.tt-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}}
.tt-date{{color:var(--muted);font-size:10px}}
.tt-close{{cursor:pointer;color:var(--muted);font-size:15px;line-height:1;padding:0 2px}}
.tt-close:hover{{color:#ef4444}}
.tt-row{{display:flex;justify-content:space-between;gap:12px;margin-bottom:2px}}
.tt-arrow{{width:1px;height:0;border-left:1px dashed #2a3f5f;margin:0 auto;transition:height .1s}}

/* 등락률 표 */
.rate-table{{margin-top:10px;border-top:1px solid var(--border);padding-top:8px}}
.rate-row{{display:flex;gap:5px;align-items:flex-start;margin-bottom:6px}}
.rate-label{{font-size:10px;font-family:var(--mono);width:60px;flex-shrink:0;padding-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.rate-cells{{display:flex;gap:3px;flex-wrap:wrap}}
.rate-cell{{font-size:10px;font-family:var(--mono);padding:2px 5px;border-radius:3px;text-align:center;min-width:50px}}
.rate-cell .rc-d{{color:var(--muted);font-size:9px;display:block}}
.rate-cell .rc-v{{font-weight:600}}
.rate-cell.up{{background:rgba(240,79,90,.1)}}.rate-cell.dn{{background:rgba(79,156,240,.1)}}
</style>
</head>
<body>
<header>
  <div class="title-group">
    <h1>주식 비교 대시보드</h1>
    <div class="sub">{' vs '.join(s['name'] for s in stocks)}</div>
  </div>
  <div class="updated">{now}</div>
</header>

<div class="chips">{chips_html}</div>

<div class="tabs">
  <button class="tab active" onclick="setTab('cum')">누적 수익률</button>
  <button class="tab" onclick="setTab('day')">일별 등락률</button>
</div>

<div class="chart-box" id="tab-cum">
  <h2>누적 수익률 (%)</h2>
  <div id="tooltipCum" class="chart-tooltip">
    <div class="tt-header">
      <span class="tt-date" id="ttCumDate"></span>
      <span class="tt-close" onclick="closeTooltip('cum')">×</span>
    </div>
    <div id="ttCumRows"></div>
    <div class="tt-arrow" id="ttCumArrow"></div>
  </div>
  <div class="chart-wrap"><canvas id="chartCum"></canvas></div>
</div>

<div class="chart-box" id="tab-day" style="display:none">
  <h2>일별 등락률 (%)</h2>
  <div id="tooltipDay" class="chart-tooltip">
    <div class="tt-header">
      <span class="tt-date" id="ttDayDate"></span>
      <span class="tt-close" onclick="closeTooltip('day')">×</span>
    </div>
    <div id="ttDayRows"></div>
    <div class="tt-arrow" id="ttDayArrow"></div>
  </div>
  <div class="chart-wrap-sm"><canvas id="chartDay"></canvas></div>
  <div class="rate-table">{rate_rows_html}</div>
</div>

<script>
const labels = {json.dumps(labels)};
const allData = {json.dumps([align_data(s['data'], 'change') for s in stocks])};
const cumData = [
{all_cum_js}
];
const palette = {json.dumps([p[0] for p in palette])};
const names   = {json.dumps([s['name'] for s in stocks])};
const active  = allData.map(()=>true);
let pinnedCum = false, pinnedDay = false;

function sign(v){{return(v>=0?'+':'')+v.toFixed(2);}}
Chart.defaults.color='#6b7a99';
Chart.defaults.borderColor='#252d3d';
Chart.defaults.font.family="'JetBrains Mono',monospace";

function mkDatasets(mode){{
  return allData.map((d,i)=>{{
    const c=palette[i%palette.length];
    return {{
      label:names[i],
      data: mode==='cum'?cumData[i]:d.map(r=>r.change),
      borderColor:c, backgroundColor:c+'14',
      borderWidth:1.6, pointRadius:0, tension:0.2, fill:false,
      borderDash:i===0?[]:[5+i,3],
      hidden:!active[i]
    }};
  }});
}}

// 끝지점 라벨 플러그인
const endLabelPlugin = {{
  id:'endLabel',
  afterDatasetsDraw(chart){{
    const ctx=chart.ctx;
    chart.data.datasets.forEach((ds,i)=>{{
      if(ds.hidden||!active[i])return;
      const meta=chart.getDatasetMeta(i);
      if(!meta.visible)return;
      const pts=meta.data;
      if(!pts.length)return;
      const last=pts[pts.length-1];
      const val=ds.data[ds.data.length-1];
      ctx.save();
      ctx.font='bold 10px JetBrains Mono,monospace';
      ctx.fillStyle=ds.borderColor;
      ctx.textAlign='left';
      ctx.fillText(sign(val)+'%', last.x+6, last.y+3);
      ctx.restore();
    }});
  }}
}};

const commonOpts = (tooltipId, dateId, rowsId, arrowId, pinnedRef)=>{{
  const obj = {{
    responsive:true, maintainAspectRatio:false,
    layout:{{padding:{{right:52}}}},
    plugins:{{
      legend:{{display:false}},
      tooltip:{{
        enabled:false,
        external(context){{
          const tp=document.getElementById(tooltipId);
          const isPinned = tooltipId==='tooltipCum'?pinnedCum:pinnedDay;
          if(isPinned)return;
          const {{chart,tooltip}}=context;
          if(tooltip.opacity===0){{tp.style.display='none';return;}}
          const date=labels[tooltip.dataPoints[0].dataIndex];
          document.getElementById(dateId).textContent=date;
          document.getElementById(rowsId).innerHTML=tooltip.dataPoints
            .filter(p=>active[p.datasetIndex])
            .map(p=>`<div class="tt-row"><span style="color:${{palette[p.datasetIndex%palette.length]}}">${{names[p.datasetIndex]}}</span><span>${{sign(p.raw)}}%</span></div>`)
            .join('');
          tp.style.display='block';
          // 클릭 위치가 차트 오른쪽 절반이면 툴팁을 왼쪽에, 왼쪽 절반이면 오른쪽에
          const caretX=tooltip.caretX;
          const chartW=chart.width;
          const tpW=140;
          if(caretX > chartW * 0.55){{
            tp.style.left=Math.max(0, caretX-tpW-20)+'px';
          }}else{{
            tp.style.left=Math.min(caretX+20, chartW-tpW)+'px';
          }}
          tp.style.top='-2px';
          const arrow=document.getElementById(arrowId);
          const tpH=tp.offsetHeight;
          const caretY=tooltip.caretY;
          arrow.style.height=Math.max(0,caretY-tpH-10)+'px';
        }}
      }}
    }},
    scales:{{
      x:{{ticks:{{font:{{size:10}},maxTicksLimit:12,maxRotation:0}},grid:{{color:'#1a2535'}}}},
      y:{{ticks:{{font:{{size:10}},callback:v=>sign(v)+'%'}},grid:{{color:'#1a2535'}}}}
    }},
    interaction:{{mode:'index',intersect:false}},
    onClick(e,els,chart){{
      if(tooltipId==='tooltipCum'){{
        pinnedCum=!pinnedCum;
        document.getElementById(tooltipId).classList.toggle('pinned',pinnedCum);
      }}else{{
        pinnedDay=!pinnedDay;
        document.getElementById(tooltipId).classList.toggle('pinned',pinnedDay);
      }}
    }}
  }};
  return obj;
}};

const chartCum = new Chart(document.getElementById('chartCum'),{{
  type:'line',
  data:{{labels,datasets:mkDatasets('cum')}},
  options:commonOpts('tooltipCum','ttCumDate','ttCumRows','ttCumArrow'),
  plugins:[endLabelPlugin]
}});
const chartDay = new Chart(document.getElementById('chartDay'),{{
  type:'line',
  data:{{labels,datasets:mkDatasets('day')}},
  options:commonOpts('tooltipDay','ttDayDate','ttDayRows','ttDayArrow'),
  plugins:[endLabelPlugin]
}});

function closeTooltip(which){{
  if(which==='cum'){{pinnedCum=false;document.getElementById('tooltipCum').classList.remove('pinned');document.getElementById('tooltipCum').style.display='none';}}
  else{{pinnedDay=false;document.getElementById('tooltipDay').classList.remove('pinned');document.getElementById('tooltipDay').style.display='none';}}
}}

function toggleStock(i){{
  active[i]=!active[i];
  document.getElementById('chip'+i).classList.toggle('off',!active[i]);
  const wrap=document.getElementById('rateRowWrap'+i);
  if(wrap)wrap.style.display=active[i]?'':'none';
  chartCum.data.datasets=mkDatasets('cum');
  chartDay.data.datasets=mkDatasets('day');
  chartCum.update(); chartDay.update();
}}

function buildRow(id,rows){{
  const el=document.getElementById(id);
  if(!el)return;
  el.innerHTML=rows.map(r=>{{
    const cls=r.change>=0?'up':'dn';
    const c=r.change>=0?'#f04f5a':'#4f9cf0';
    return `<div class="rate-cell ${{cls}}"><span class="rc-d">${{r.date.slice(5)}}</span><span class="rc-v" style="color:${{c}}">${{sign(r.change)}}%</span></div>`;
  }}).join('');
}}
{rate_js}

function setTab(t){{
  document.getElementById('tab-cum').style.display=t==='cum'?'':'none';
  document.getElementById('tab-day').style.display=t==='day'?'':'none';
  document.querySelectorAll('.tab').forEach((el,i)=>
    el.classList.toggle('active',(i===0&&t==='cum')||(i===1&&t==='day')));
}}
</script>
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
        stocks.append({'code': item['code'], 'name': item['name'], 'data': add_changes(data)})

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
