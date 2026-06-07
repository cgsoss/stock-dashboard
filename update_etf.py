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
import numpy as np

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
            import re
            start = re.sub(r'[^\d]', '-', start)
            start = re.sub(r'-+', '-', start).strip('-')
            short = row.get('축약명', '').strip() or name
            items.append({'code': code, 'name': name, 'short': short, 'start': start})
    print(f"[설정] {len(items)}개 종목 (Y): {[x['code']+' '+x['name'] for x in items]}")
    return items

# ── pykrx로 일별 시세 가져오기 ──
def fetch_pykrx(code, start_date_str):
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

# ── 공포탐욕지수 계산 ──
def fetch_yahoo(ticker, period='1y'):
    import urllib.request, json as _json, pandas as pd
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={period}&interval=1d"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = _json.loads(resp.read())
        result = data['chart']['result'][0]
        closes = result['indicators']['quote'][0]['close']
        timestamps = result['timestamp']
        dates = pd.to_datetime(timestamps, unit='s').tz_localize('UTC').tz_convert('Asia/Seoul')
        df = pd.DataFrame({'close': closes}, index=dates).dropna()
        return df
    except Exception as e:
        print(f'  Yahoo fetch 실패 [{ticker}]: {e}')
        return None

def normalize_125(series):
    """feargreed.co.kr 동일 방식: 125거래일 내 min/max 기준 정규화"""
    arr = np.array(series[-125:])
    mn, mx = arr.min(), arr.max()
    if mx == mn:
        return 50.0
    return float(np.clip((arr[-1] - mn) / (mx - mn) * 100, 0, 100))

def calc_fear_greed():
    print('\n[공포탐욕지수] 계산 시작...')
    scores = {}
    history = []

    try:
        # ── 지표1: 시장 모멘텀 (코스피 vs 125일 이동평균) ──
        df_ks = fetch_yahoo('^KS11', '2y')
        if df_ks is not None and len(df_ks) >= 125:
            closes = df_ks['close'].values
            ma125   = np.mean(closes[-125:])
            # 모멘텀 = 코스피 / MA125 비율의 125일 정규화
            momentum_series = [closes[i] / np.mean(closes[max(0,i-125):i]) 
                               for i in range(125, len(closes))]
            if momentum_series:
                mn, mx = min(momentum_series), max(momentum_series[-125:])
                cur = momentum_series[-1]
                scores['momentum'] = float(np.clip((cur - min(momentum_series[-125:])) / 
                                                    (mx - min(momentum_series[-125:]) + 1e-9) * 100, 0, 100))
            else:
                scores['momentum'] = 50.0
            print(f'  모멘텀: {scores["momentum"]:.1f} (코스피={closes[-1]:.0f}, MA125={ma125:.0f})')
        else:
            scores['momentum'] = 50.0

        # ── 지표2: 주가 강도 (52주 신고가 vs 신저가) ──
        try:
            end_today = date.today().strftime('%Y%m%d')
            start_52w = (date.today() - timedelta(days=380)).strftime('%Y%m%d')
            tickers = stock.get_market_ticker_list(end_today, market='KOSPI')
            high52 = 0; low52 = 0
            for t in tickers[:100]:
                try:
                    df_t = stock.get_market_ohlcv(start_52w, end_today, t)
                    if df_t is None or len(df_t) < 2: continue
                    c_today = df_t['종가'].iloc[-1]
                    h_52 = df_t['고가'].max()
                    l_52 = df_t['저가'].min()
                    if c_today >= h_52 * 0.98: high52 += 1
                    elif c_today <= l_52 * 1.02: low52 += 1
                except: continue
            total = high52 + low52
            raw_ratio = high52 / total if total > 0 else 0.5
            # 125일 히스토리 없으니 단순 정규화 (0.5 기준 ±0.5)
            scores['strength'] = float(np.clip(raw_ratio * 100, 0, 100))
            print(f'  주가강도: {scores["strength"]:.1f} (신고가={high52}, 신저가={low52})')
        except Exception as e:
            print(f'  주가강도 오류: {e}')
            scores['strength'] = 50.0

        # ── 지표3: 주가 폭 (상승/하락 종목수) ──
        try:
            end_today = date.today().strftime('%Y%m%d')
            start_5d  = (date.today() - timedelta(days=7)).strftime('%Y%m%d')
            tickers_today = stock.get_market_ticker_list(end_today, market='KOSPI')
            up = 0; down = 0
            for t in tickers_today[:200]:
                try:
                    df_t = stock.get_market_ohlcv(start_5d, end_today, t)
                    if df_t is None or len(df_t) < 2: continue
                    chg = df_t['종가'].iloc[-1] - df_t['종가'].iloc[-2]
                    if chg > 0: up += 1
                    elif chg < 0: down += 1
                except: continue
            total = up + down
            scores['breadth'] = float(np.clip(up / total * 100, 0, 100)) if total > 0 else 50.0
            print(f'  주가폭: {scores["breadth"]:.1f} (상승={up}, 하락={down})')
        except Exception as e:
            print(f'  주가폭 오류: {e}')
            scores['breadth'] = 50.0

        # ── 지표4: 변동성 VKOSPI vs 50일 이동평균 (역방향) ──
        df_vk = fetch_yahoo('^VKOSPI', '1y')
        if df_vk is not None and len(df_vk) >= 50:
            vk_vals = df_vk['close'].values
            # VKOSPI 높을수록 공포 → 역방향 정규화
            vk_inv = [-v for v in vk_vals]  # 부호 뒤집기
            scores['volatility'] = normalize_125(vk_inv)
            print(f'  변동성: {scores["volatility"]:.1f} (VKOSPI={vk_vals[-1]:.2f})')
        else:
            scores['volatility'] = 50.0
            print(f'  변동성: VKOSPI 미수집')

        # ── 지표5: 안전자산 수요 (코스피 vs 국고채 20일 수익률) ──
        try:
            end_today = date.today().strftime('%Y%m%d')
            start_30d = (date.today() - timedelta(days=45)).strftime('%Y%m%d')
            df_ktb = stock.get_market_ohlcv(start_30d, end_today, '148070')
            if df_ks is not None and df_ktb is not None and len(df_ktb) >= 20:
                ks_vals  = df_ks['close'].values
                kospi_ret = (ks_vals[-1] / ks_vals[-20] - 1) * 100
                ktb_ret   = (df_ktb['종가'].iloc[-1] / df_ktb['종가'].iloc[-20] - 1) * 100
                diff = kospi_ret - ktb_ret
                scores['safe_demand'] = float(np.clip((diff + 10) / 20 * 100, 0, 100))
                print(f'  안전자산: {scores["safe_demand"]:.1f} (코스피20d={kospi_ret:.2f}%, 채권={ktb_ret:.2f}%)')
            else:
                scores['safe_demand'] = 50.0
        except Exception as e:
            print(f'  안전자산 오류: {e}')
            scores['safe_demand'] = 50.0

        # ── 최종 점수 ──
        final = round(sum(scores.values()) / len(scores), 1)
        print(f'  최종 공포탐욕지수: {final}')
        print(f'  지표별: {scores}')

        # ── 30일 추이 ──
        if df_ks is not None and len(df_ks) >= 155:
            ks_closes = df_ks['close'].values
            for i in range(30, 0, -1):
                idx = len(ks_closes) - i
                if idx >= 125:
                    sub_momentum = [ks_closes[j] / np.mean(ks_closes[max(0,j-125):j])
                                    for j in range(max(125, idx-124), idx+1)]
                    if sub_momentum:
                        mn2 = min(sub_momentum)
                        mx2 = max(sub_momentum)
                        cur2 = ks_closes[idx-1] / np.mean(ks_closes[idx-125:idx])
                        day_score = float(np.clip((cur2 - mn2) / (mx2 - mn2 + 1e-9) * 100, 0, 100))
                        d = df_ks.index[idx-1].strftime('%m/%d')
                        history.append({'date': d, 'score': round(day_score, 1)})

        return {'score': final, 'scores': scores, 'history': history[-30:] if history else []}

    except Exception as e:
        import traceback
        print(f'[공포탐욕지수] 계산 실패: {e}')
        print(traceback.format_exc())
        return None



# ── HTML 생성 ──
def build_html(stocks, fg=None):
    now = datetime.utcnow().strftime('%Y/%m/%d %H:%M UTC')
    palette = [
        ('#f04f5a','rgba(240,79,90,0.08)'),
        ('#4f9cf0','rgba(79,156,240,0.08)'),
        ('#3ecf8e','rgba(62,207,142,0.08)'),
        ('#f5a623','rgba(245,166,35,0.08)'),
        ('#b57bee','rgba(181,123,238,0.08)'),
        ('#50d8d7','rgba(80,216,215,0.08)'),
    ]
    MAX_ACTIVE = 6

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

    all_cum_js   = ',\n'.join(f'  {json.dumps(cum_vals(s["data"]))}' for s in stocks)
    all_day_js   = ',\n'.join(f'  {json.dumps(align(s["data"],"change"))}' for s in stocks)
    all_close_js = ',\n'.join(f'  {json.dumps(align(s["data"],"close"))}' for s in stocks)

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
            rate_js_lines.append(f'document.getElementById("rateRowWrap{i}").style.display=\'none\';')
    rate_js = '\n'.join(rate_js_lines)

    EYE_ON  = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>'
    EYE_OFF = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'

    chips_html = ''
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
            f'<svg id="eyeIcon{i}" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{EYE_ON if i < MAX_ACTIVE else EYE_OFF}</svg>'
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

    palette_js = json.dumps([p[0] for p in palette])
    names_js   = json.dumps([s['name'] for s in stocks])
    labels_js  = json.dumps(labels)
    stocks_sub = ' vs '.join(s['name'] for s in stocks)

    # ── 공포탐욕지수 HTML 블록 ──
    if fg:
        sc = fg['score']
        sc_scores = fg['scores']
        sc_hist   = fg['history']

        def fg_color(v):
            if v < 25: return '#ef4444'
            if v < 45: return '#f97316'
            if v < 55: return '#9ca3af'
            if v < 75: return '#f59e0b'
            return '#22c55e'

        def fg_label(v):
            if v < 25: return '극단적 공포'
            if v < 45: return '공포'
            if v < 55: return '중립'
            if v < 75: return '탐욕'
            return '극단적 탐욕'

        def fg_badge_style(v):
            if v < 25: return 'background:rgba(239,68,68,.12);color:#ef4444'
            if v < 45: return 'background:rgba(249,115,22,.12);color:#f97316'
            if v < 55: return 'background:rgba(156,163,175,.12);color:#9ca3af'
            if v < 75: return 'background:rgba(245,158,11,.12);color:#f59e0b'
            return 'background:rgba(34,197,94,.12);color:#22c55e'

        fc = fg_color(sc)

        ind_items = [
            ('시장 모멘텀', '코스피가 125일 이동평균선 위에 있을수록 탐욕', sc_scores.get('momentum', 50)),
            ('주가 강도',   '52주 신고가 종목이 신저가보다 많을수록 탐욕',  sc_scores.get('strength', 50)),
            ('주가 폭',     '상승 종목 수가 하락 종목보다 많을수록 탐욕',    sc_scores.get('breadth', 50)),
            ('변동성 VKOSPI','변동성 지수가 50일 평균보다 낮을수록 탐욕',   sc_scores.get('volatility', 50)),
            ('안전자산 수요','코스피 수익률이 국고채보다 높을수록 탐욕',     sc_scores.get('safe_demand', 50)),
        ]

        ind_html = ''
        for idx, (iname, idesc, ival) in enumerate(ind_items):
            ic = fg_color(ival)
            full = idx == 4
            ind_html += (
                f'<div class="ind-card{"  ind-full" if full else ""}">'
                f'<div class="ind-name">{iname}</div>'
                f'<div class="ind-desc">{idesc}</div>'
                f'<div class="ind-bar-wrap"><div class="ind-bar" style="width:{ival:.0f}%;background:{ic}"></div></div>'
                f'<div class="ind-score-row">'
                f'<span class="ind-val" style="color:{ic}">{ival:.0f}</span>'
                f'</div></div>'
            )

        hist_js   = json.dumps([h['score'] for h in sc_hist])
        hist_lbl  = json.dumps([h['date']  for h in sc_hist])
        hist_col  = json.dumps([fg_color(h['score']) for h in sc_hist])

        fg_block = f"""
<div class="fg-section">
  <div class="fg-header">
    <span class="fg-title">코스피 공포탐욕지수</span>
    <span class="fg-updated">{now[:10]} 기준</span>
  </div>

  <div class="fg-gauge-wrap">
    <div style="position:relative;width:200px;height:105px;margin:0 auto">
      <canvas id="gaugeChart" role="img" aria-label="공포탐욕지수 {sc}점 {fg_label(sc)} 구간">{sc}점 {fg_label(sc)}</canvas>
    </div>
    <div class="fg-score" style="color:{fc}">{sc}</div>
    <div class="fg-badge" style="{fg_badge_style(sc)}">{fg_label(sc)}</div>
  </div>

  <div class="fg-hist-title">30일 추이</div>
  <div style="position:relative;width:100%;height:55px;margin-bottom:12px">
    <canvas id="histChart" role="img" aria-label="30일 공포탐욕지수 추이">30일 추이</canvas>
  </div>

  <div class="ind-grid">{ind_html}</div>

  <div class="fg-zones">
    <div class="zone-row" style="background:rgba(239,68,68,.05)">
      <div class="zone-bar" style="background:#ef4444"></div>
      <div><div class="zone-range">0~24</div><div class="zone-name" style="color:#ef4444">극단적 공포</div><div class="zone-desc">패닉 셀링 구간. 역발상 매수 기회일 수 있으나 추가 하락 가능</div></div>
    </div>
    <div class="zone-row" style="background:rgba(249,115,22,.05)">
      <div class="zone-bar" style="background:#f97316"></div>
      <div><div class="zone-range">25~44</div><div class="zone-name" style="color:#f97316">공포</div><div class="zone-desc">매도 압력 우세. 중장기 분할 매수 검토 구간</div></div>
    </div>
    <div class="zone-row" style="background:rgba(107,114,128,.05)">
      <div class="zone-bar" style="background:#6b7280"></div>
      <div><div class="zone-range">45~54</div><div class="zone-name" style="color:#9ca3af">중립</div><div class="zone-desc">방향성 탐색 구간. 변동성 낮고 추세 전환 신호 주시</div></div>
    </div>
    <div class="zone-row" style="background:rgba(245,158,11,.05)">
      <div class="zone-bar" style="background:#f59e0b"></div>
      <div><div class="zone-range">55~74</div><div class="zone-name" style="color:#f59e0b">탐욕</div><div class="zone-desc">상승 모멘텀 강함. 수익 실현 및 리스크 관리 필요</div></div>
    </div>
    <div class="zone-row" style="background:rgba(34,197,94,.05)">
      <div class="zone-bar" style="background:#22c55e"></div>
      <div><div class="zone-range">75~100</div><div class="zone-name" style="color:#22c55e">극단적 탐욕</div><div class="zone-desc">버블 과열 구간. FOMO 매수 폭증. 수익 실현 및 현금 비중 확대 권장</div></div>
    </div>
  </div>
  <div class="fg-note">* 5개 지표 단순 평균 · 참고용 지표 · 투자 결정의 단독 근거로 사용 금지</div>
</div>
"""
        fg_chart_js = f"""
new Chart(document.getElementById('gaugeChart'),{{
  type:'doughnut',
  data:{{datasets:[{{
    data:[{sc},100-{sc}],
    backgroundColor:['{fc}','rgba(30,45,69,0.8)'],
    borderWidth:0,circumference:180,rotation:270
  }}]}},
  options:{{responsive:true,maintainAspectRatio:false,cutout:'72%',
    plugins:{{legend:{{display:false}},tooltip:{{enabled:false}}}}}}
}});

const histData={hist_js};
const histLabels={hist_lbl};
const histColors={hist_col};
new Chart(document.getElementById('histChart'),{{
  type:'line',
  data:{{
    labels:histLabels,
    datasets:[{{
      data:histData,
      borderColor:'{fc}',
      backgroundColor:'rgba(245,158,11,0.05)',
      borderWidth:1.5,pointRadius:0,tension:0.3,fill:true,
      segment:{{borderColor:ctx=>histColors[ctx.p1DataIndex]||'{fc}'}}
    }}]
  }},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{
      mode:'index',intersect:false,
      backgroundColor:'#1a2438',borderColor:'#2a3f5f',borderWidth:1,
      titleColor:'#6b7a99',bodyColor:'#e2e8f0',
      callbacks:{{label:i=>` ${{i.raw}}점`}}
    }}}},
    scales:{{x:{{display:false}},y:{{min:0,max:100,display:false}}}}
  }}
}});
"""
    else:
        fg_block = ''
        fg_chart_js = ''

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
.tp-row{{display:flex;align-items:center;gap:7px;padding:4px 0;border-bottom:1px solid #1e2d45}}.tp-row:last-child{{border-bottom:none}}
.tp-dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0}}
.rate-table{{margin-top:10px;border-top:1px solid var(--border);padding-top:8px}}
.rate-row{{display:flex;gap:5px;align-items:flex-start;margin-bottom:6px}}
.rate-label{{font-size:10px;font-family:var(--mono);width:60px;flex-shrink:0;padding-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.rate-cells{{display:flex;gap:3px;flex-wrap:wrap}}
.rate-cell{{font-size:10px;font-family:var(--mono);padding:2px 5px;border-radius:3px;text-align:center;min-width:50px;cursor:pointer;border:1px solid transparent;transition:border-color .1s,background .1s}}
.rate-cell .rc-d{{color:var(--muted);font-size:9px;display:block}}
.rate-cell .rc-v{{font-weight:600}}
.rate-cell.up{{background:rgba(240,79,90,.1)}}.rate-cell.dn{{background:rgba(79,156,240,.1)}}
.rate-cell.selected{{border-color:rgba(255,255,255,.35)!important;background:rgba(255,255,255,.07)!important}}
.rate-cell.selected .rc-d{{color:var(--text)}}
.rate-cell:hover{{border-color:rgba(255,255,255,.15)}}
.disclaimer{{font-size:10px;color:#3a4a5a;font-family:var(--mono);margin-top:8px;text-align:right}}
.copyright{{font-size:10px;color:#3a4a5a;font-family:var(--mono);text-align:center;margin-top:20px;padding-top:12px;border-top:1px solid var(--border)}}

/* ── 공포탐욕지수 ── */
.fg-section{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px}}
.fg-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}}
.fg-title{{font-size:10px;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.8px}}
.fg-updated{{font-size:10px;color:var(--muted);font-family:var(--mono)}}
.fg-gauge-wrap{{display:flex;flex-direction:column;align-items:center;margin-bottom:12px}}
.fg-score{{font-size:30px;font-weight:600;font-family:var(--mono);margin:2px 0}}
.fg-badge{{font-size:11px;padding:3px 12px;border-radius:12px;font-family:var(--mono);margin-bottom:4px}}
.fg-hist-title{{font-size:10px;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}}
.ind-grid{{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:12px}}
.ind-card{{background:var(--bg);border-radius:8px;padding:9px 10px}}
.ind-full{{grid-column:span 2}}
.ind-name{{font-size:10px;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px}}
.ind-desc{{font-size:10px;color:#8a9ab5;line-height:1.5;margin-bottom:5px}}
.ind-bar-wrap{{height:3px;background:#1e2d45;border-radius:2px;overflow:hidden;margin-bottom:3px}}
.ind-bar{{height:100%;border-radius:2px}}
.ind-score-row{{display:flex;justify-content:space-between}}
.ind-val{{font-size:12px;font-weight:600;font-family:var(--mono)}}
.fg-zones{{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}}
.zone-row{{display:flex;align-items:flex-start;gap:8px;padding:6px 8px;border-radius:6px}}
.zone-bar{{width:3px;min-height:36px;border-radius:2px;flex-shrink:0;margin-top:2px}}
.zone-range{{font-size:9px;color:var(--muted);font-family:var(--mono);margin-bottom:1px}}
.zone-name{{font-size:11px;font-weight:600;font-family:var(--mono);margin-bottom:2px}}
.zone-desc{{font-size:10px;color:#8a9ab5;line-height:1.4}}
.fg-note{{font-size:10px;color:#3a4a5a;font-family:var(--mono);text-align:center}}
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

{fg_block}

<script>
const labels  = {labels_js};
const cumData = [{all_cum_js}];
const dayData = [{all_day_js}];
const closeData = [{all_close_js}];
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
  document.getElementById(which==='cum'?'tpCumDate':'tpDayDate').textContent=labels[idx];
  document.getElementById(which==='cum'?'tpCumRows':'tpDayRows').innerHTML=
    names.map((n,i)=>{{
      if(!active[i]||cumData[i][idx]==null)return'';
      const c=palette[i%palette.length];
      const cumV=cumData[i][idx], dayV=dayData[i][idx];
      const cumC=cumV>=0?'#f04f5a':'#4f9cf0';
      const dayC=dayV>=0?'#f04f5a':'#4f9cf0';
      const dayBg=dayV>=0?'rgba(240,79,90,.15)':'rgba(79,156,240,.15)';
      const price=closeData[i][idx]!=null?closeData[i][idx].toLocaleString()+'원':'';
      return `<div class="tp-row">
        <div class="tp-dot" style="background:${{c}}"></div>
        <div style="flex:1;min-width:0">
          <div style="font-size:11px;color:#dce8f5">${{n}}</div>
          <div style="font-size:9px;color:#6b7a99">${{price}}</div>
        </div>
        <div style="display:flex;gap:5px;align-items:center;flex-shrink:0">
          <span style="font-size:12px;font-weight:700;color:${{cumC}}">${{sign(cumV)}}%</span>
          <span style="font-size:10px;padding:1px 5px;border-radius:3px;font-weight:600;background:${{dayBg}};color:${{dayC}}">${{sign(dayV)}}%</span>
        </div>
      </div>`;
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

names.forEach((_,i)=>{{
  if(!active[i]){{
    const el=document.getElementById('eyeIcon'+i);
    if(el)el.innerHTML=EYE_OFF;
  }}
}});

function toggleDetail(i){{
  const chip=document.getElementById('chip'+i);
  if(!active[i])return;
  const wasOpen=chip.classList.contains('show-detail');
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('show-detail'));
  if(!wasOpen)chip.classList.add('show-detail');
}}

function toggleStock(i){{
  active[i]=!active[i];
  const chip=document.getElementById('chip'+i);
  chip.classList.toggle('off',!active[i]);
  chip.classList.remove('show-detail');
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

let selectedDate=null;
document.addEventListener('click',e=>{{
  const cell=e.target.closest('.rate-cell');
  if(!cell)return;
  const d=cell.querySelector('.rc-d')?.textContent;
  if(!d)return;
  if(selectedDate===d){{
    selectedDate=null;
    document.querySelectorAll('.rate-cell').forEach(c=>c.classList.remove('selected'));
  }}else{{
    selectedDate=d;
    document.querySelectorAll('.rate-cell').forEach(c=>{{
      c.classList.toggle('selected',c.querySelector('.rc-d')?.textContent===d);
    }});
  }}
}});

function setTab(t){{
  document.getElementById('tab-cum').style.display=t==='cum'?'':'none';
  document.getElementById('tab-day').style.display=t==='day'?'':'none';
  document.querySelectorAll('.tab').forEach((el,i)=>
    el.classList.toggle('active',(i===0&&t==='cum')||(i===1&&t==='day')));
}}

{fg_chart_js}
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

    # 공포탐욕지수 계산
    fg = calc_fear_greed()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'etf_dashboard.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(build_html(stocks, fg))

    print(f'\n✓ 완료: {out}')
    for s in stocks:
        cum = (s['data'][-1]['close'] / s['data'][0]['close'] - 1) * 100
        print(f"  {s['name']}: {cum:+.2f}%")
