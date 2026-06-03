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
    반환: [{'date': 'YYYY/MM/DD', 'close': int}, ...]
    """
    start = start_date_str.replace('-', '')          # '20260101'
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

    rows.sort(key=lambda x: x['date'])
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

    cards_html = ''
    for i, s in enumerate(stocks):
        c = palette[i % len(palette)][0]
        d = s['data']
        last = d[-1]
        cum  = round((last['close'] / d[0]['close'] - 1) * 100, 2)
        cards_html += f"""
  <div class="card" style="--accent:{c}">
    <div class="lbl">{s['code']}</div>
    <div class="tkr" style="color:{c}">{s['name']}</div>
    <div class="stats">
      <div class="stat"><div class="sl">현재가</div><div class="sv">{last['close']:,}원</div></div>
      <div class="stat"><div class="sl">전일대비</div><div class="sv" style="color:{col(last['change'])}">{sign(last['change'])}%</div></div>
      <div class="stat"><div class="sl">누적수익률</div><div class="sv" style="color:{col(cum)}">{sign(cum)}%</div></div>
    </div>
  </div>"""

    labels = [r['date'][5:] for r in stocks[0]['data']]

    def cum_vals(data):
        base = data[0]['close']
        return [round((r['close']/base - 1)*100, 2) for r in data]

    rate_rows_html = ''
    for i, s in enumerate(stocks):
        c = palette[i % len(palette)][0]
        rate_rows_html += f'<div class="rate-row"><span class="rate-label" style="color:{c}">{s["name"][:6]}</span><div class="rate-cells" id="rateRow{i}"></div></div>\n'

    rate_js = '\n'.join(
        f"buildRow('rateRow{i}', {json.dumps(s['data'][-30:])});"
        for i, s in enumerate(stocks)
    )

    legend_html = ''.join(
        f'<div class="leg"><div class="leg-line" style="background:{palette[i%len(palette)][0]}"></div>{s["name"]}</div>'
        for i, s in enumerate(stocks)
    )

    all_cum_js = ',\n'.join(
        f"  {json.dumps(cum_vals(s['data']))}"
        for s in stocks
    )

    datasets_day = ',\n'.join(
        f"""{{label:{json.dumps(s['name'])},data:{json.dumps([r['change'] for r in s['data']])},
          borderColor:'{palette[i%len(palette)][0]}',backgroundColor:'{palette[i%len(palette)][1]}',
          borderWidth:1.6,pointRadius:0,tension:0.2,fill:true,borderDash:{'[]' if i==0 else f'[{5+i},3]'}}}"""
        for i, s in enumerate(stocks)
    )

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
body{{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding:28px 24px 48px}}
header{{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:10px}}
.title-group h1{{font-size:19px;font-weight:700}}
.title-group .sub{{font-size:11px;color:var(--muted);margin-top:4px;font-family:var(--mono)}}
.updated{{font-size:11px;color:var(--muted);font-family:var(--mono);text-align:right;line-height:1.6}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-bottom:20px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 20px;position:relative;overflow:hidden}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent)}}
.card .lbl{{font-size:10px;color:var(--muted);font-family:var(--mono);margin-bottom:5px}}
.card .tkr{{font-size:16px;font-weight:700;font-family:var(--mono)}}
.card .stats{{display:flex;gap:14px;margin-top:12px;flex-wrap:wrap}}
.stat .sl{{font-size:10px;color:var(--muted);font-family:var(--mono)}}
.stat .sv{{font-size:13px;font-weight:600;font-family:var(--mono);margin-top:2px}}
.tabs{{display:flex;gap:4px;margin-bottom:14px}}
.tab{{padding:6px 16px;border-radius:6px;font-size:12px;font-family:var(--mono);cursor:pointer;
  border:1px solid var(--border);background:transparent;color:var(--muted);transition:all 0.15s}}
.tab.active{{background:var(--surface);color:var(--text);border-color:#3a4560}}
.chart-box{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:14px}}
.chart-box h2{{font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:12px;text-transform:uppercase;letter-spacing:.8px}}
.chart-wrap{{position:relative;width:100%;height:260px}}
.chart-wrap-sm{{position:relative;width:100%;height:190px}}
.legend{{display:flex;gap:14px;margin-bottom:10px;flex-wrap:wrap}}
.leg{{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);font-family:var(--mono)}}
.leg-line{{width:16px;height:2px;border-radius:1px}}
.rate-table{{margin-top:12px;border-top:1px solid var(--border);padding-top:10px}}
.rate-row{{display:flex;gap:6px;align-items:flex-start;margin-bottom:7px}}
.rate-label{{font-size:10px;font-family:var(--mono);width:60px;flex-shrink:0;padding-top:4px}}
.rate-cells{{display:flex;gap:4px;flex-wrap:wrap}}
.rate-cell{{font-size:10px;font-family:var(--mono);padding:3px 6px;border-radius:3px;text-align:center;min-width:54px}}
.rate-cell .rc-d{{color:var(--muted);font-size:9px;display:block}}
.rate-cell .rc-v{{font-weight:600}}
.rate-cell.up{{background:rgba(240,79,90,.12)}}.rate-cell.dn{{background:rgba(79,156,240,.12)}}
</style>
</head>
<body>
<header>
  <div class="title-group">
    <h1>주식 비교 대시보드</h1>
    <div class="sub">{' &nbsp;vs&nbsp; '.join(s['name']+' ('+s['code']+')' for s in stocks)}</div>
  </div>
  <div class="updated">마지막 업데이트<br>{now}</div>
</header>

<div class="cards">{cards_html}</div>

<div class="tabs">
  <button class="tab active" onclick="setTab('cum')">누적 수익률</button>
  <button class="tab" onclick="setTab('day')">일별 등락률</button>
</div>

<div class="chart-box" id="tab-cum">
  <h2>누적 수익률 (%)</h2>
  <div class="legend">{legend_html}</div>
  <div class="chart-wrap"><canvas id="chartCum"></canvas></div>
</div>

<div class="chart-box" id="tab-day" style="display:none">
  <h2>일별 등락률 (%)</h2>
  <div class="legend">{legend_html}</div>
  <div class="chart-wrap-sm"><canvas id="chartDay"></canvas></div>
  <div class="rate-table">{rate_rows_html}</div>
</div>

<script>
const labels = {json.dumps(labels)};
const allData = {json.dumps([s['data'] for s in stocks])};
const cumData = [
{all_cum_js}
];
const palette = {json.dumps([p[0] for p in palette])};
const names   = {json.dumps([s['name'] for s in stocks])};

function sign(v){{return(v>=0?'+':'')+v.toFixed(2);}}
Chart.defaults.color='#6b7a99';
Chart.defaults.borderColor='#252d3d';
Chart.defaults.font.family="'JetBrains Mono',monospace";

function mkDatasets(mode){{
  return allData.map((d,i)=>{{
    const c=palette[i%palette.length];
    return {{
      label:names[i],
      data: mode==='cum' ? cumData[i] : d.map(r=>r.change),
      borderColor:c,
      backgroundColor:c.replace('#','').match(/../g).map(h=>parseInt(h,16)),
      borderWidth:1.6,pointRadius:0,tension:0.2,fill:false,
      borderDash:i===0?[]:[5+i,3]
    }};
  }});
}}

const opts = {{
  responsive:true,maintainAspectRatio:false,
  plugins:{{
    legend:{{display:false}},
    tooltip:{{mode:'index',intersect:false,backgroundColor:'#1e2535',
      borderColor:'#3a4560',borderWidth:1,titleColor:'#6b7a99',bodyColor:'#e2e8f0',
      callbacks:{{label:i=>` ${{i.dataset.label}}: ${{sign(i.raw)}}%`}}}}
  }},
  scales:{{
    x:{{ticks:{{font:{{size:10}},maxTicksLimit:14,maxRotation:0}},grid:{{color:'#1e2535'}}}},
    y:{{ticks:{{font:{{size:10}},callback:v=>sign(v)+'%'}},grid:{{color:'#1e2535'}}}}
  }},
  interaction:{{mode:'index',intersect:false}}
}};

const chartCum = new Chart(document.getElementById('chartCum'),{{type:'line',data:{{labels,datasets:mkDatasets('cum')}},options:opts}});
const chartDay = new Chart(document.getElementById('chartDay'),{{type:'line',data:{{labels,datasets:mkDatasets('day')}},options:opts}});

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
