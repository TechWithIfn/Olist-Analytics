"""
Olist Analytics
End-to-End E-Commerce & Marketing Analytics — local web UI.

Serves the live PostgreSQL results (schemas: ecommerce, marketing) plus the
project's own SQL scripts (insights + data validation) executed on demand.

Run:  python webapp/app.py
Open: http://127.0.0.1:5050
"""

import csv
import io
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, render_template_string, request, send_from_directory

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PSQL = r"C:\Users\ansar\AppData\Local\Programs\Python\Python312\Lib\site-packages\pgserver\pginstall\bin\psql.exe"
DB = {"host": "127.0.0.1", "port": "55432", "user": "postgres", "name": "olist"}
HOST, PORT = "127.0.0.1", 5050
LABELS = {"ecommerce": "E-Commerce", "marketing": "Marketing Funnel"}

SCRIPTS = {
    "ecommerce": {
        "insights": os.path.join(PROJECT, "07_Analytical_insights", "ecommerce", "ecommerce_analytical_insights.sql"),
        "validation": os.path.join(PROJECT, "05_data_validation", "ecommerce", "ecommerce_data_validation.sql"),
    },
    "marketing": {
        "insights": os.path.join(PROJECT, "07_Analytical_insights", "marketing", "marketing_analytical_insights.sql"),
        "validation": os.path.join(PROJECT, "05_data_validation", "marketing", "marketing_data_validation.sql"),
    },
}

app = Flask(__name__)
_cache = {}


# --------------------------------------------------------------------------- db
def run_sql(sql, timeout=120):
    env = os.environ.copy()
    env["PGCLIENTENCODING"] = "UTF8"
    started = time.time()
    try:
        proc = subprocess.run(
            [PSQL, "-h", DB["host"], "-p", DB["port"], "-U", DB["user"], "-d", DB["name"],
             "--csv", "-v", "ON_ERROR_STOP=1", "-f", "-"],
            input=sql.encode("utf-8"), capture_output=True,
            env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "query timed out after %ss" % timeout, "sql": sql, "elapsed": timeout}

    elapsed = round(time.time() - started, 3)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        return {"error": err or "psql failed", "sql": sql, "elapsed": elapsed}

    rows = list(csv.reader(io.StringIO(proc.stdout.decode("utf-8", errors="replace"))))
    if not rows:
        return {"columns": [], "rows": [], "total": 0, "sql": sql, "elapsed": elapsed}
    body = rows[1:]
    return {"columns": rows[0], "rows": body, "total": len(body), "sql": sql, "elapsed": elapsed}


def scalar(sql, default="0"):
    res = run_sql(sql)
    if res.get("error") or not res.get("rows"):
        return default
    return res["rows"][0][0]


# ------------------------------------------------------------------ sql scripts
def split_script(path):
    """Split a psql script into (title, sql) blocks, remembering its search_path."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

    blocks, buf, comments, search_path = [], [], [], None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--"):
            if not buf:
                label = stripped.lstrip("-").strip()
                if label:
                    comments.append(label)
            continue
        # drop a trailing inline comment so "...;  -- note" still terminates
        code = re.sub(r"\s--[^'\"]*$", "", line).strip()
        buf.append(line)
        if code.endswith(";"):
            sql = "\n".join(buf).strip()
            title = " ".join(comments) if comments else "Query"
            buf, comments = [], []
            if re.match(r"(?is)^\s*set\s+search_path", sql):
                m = re.search(r"(?is)to\s+([\w\s,\.]+?)\s*;", sql)
                if m:
                    search_path = m.group(1).strip()
                continue
            blocks.append({"title": title[:160], "sql": sql})
    return search_path or "public", blocks


def run_script(path):
    mtime = os.path.getmtime(path)
    key = (path, mtime)
    if key in _cache:
        return _cache[key]

    search_path, blocks = split_script(path)

    def execute(block):
        return dict(block, **run_sql("SET search_path TO %s;\n%s" % (search_path, block["sql"])))

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(execute, blocks))

    _cache[key] = results
    return results


# ---------------------------------------------------------------------- metrics
KPI_SQL = """
SELECT
  COUNT(DISTINCT o.order_id)                                                              AS orders,
  COUNT(DISTINCT o.customer_id)                                                           AS customers,
  ROUND(SUM(oi.price + oi.freight_value), 2)                                              AS gmv,
  ROUND(SUM(oi.price) / COUNT(DISTINCT o.order_id), 2)                                    AS aov,
  COUNT(DISTINCT oi.seller_id)                                                            AS sellers,
  COUNT(DISTINCT oi.product_id)                                                           AS products
FROM ecommerce.olist_orders o
JOIN ecommerce.olist_order_items oi USING (order_id)
"""

DELIVERY_SQL = """
SELECT
  ROUND(AVG(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp)) / 86400), 2) AS delivery_days,
  ROUND(100.0 * COUNT(*) FILTER (WHERE order_delivered_customer_date > order_estimated_delivery_date)
        / NULLIF(COUNT(*) FILTER (WHERE order_delivered_customer_date IS NOT NULL), 0), 2) AS late_pct
FROM ecommerce.olist_orders
"""

CHARTS = {
    "revenue_by_month": """
        SELECT TO_CHAR(o.order_purchase_timestamp, 'YYYY-MM') AS month,
               ROUND(SUM(oi.price + oi.freight_value), 2) AS revenue
        FROM ecommerce.olist_orders o
        JOIN ecommerce.olist_order_items oi USING (order_id)
        WHERE o.order_purchase_timestamp >= DATE '2017-01-01'
          AND o.order_purchase_timestamp <  DATE '2018-09-01'
        GROUP BY 1 ORDER BY 1
    """,
    "orders_by_status": """
        SELECT order_status AS label, COUNT(*) AS value
        FROM ecommerce.olist_orders GROUP BY 1 ORDER BY 2 DESC
    """,
    "top_categories": """
        SELECT COALESCE(pct.product_category_name_english, p.product_category_name, 'unknown') AS label,
               ROUND(SUM(oi.price), 2) AS value
        FROM ecommerce.olist_order_items oi
        JOIN ecommerce.olist_products p USING (product_id)
        LEFT JOIN ecommerce.product_category_name_translation pct USING (product_category_name)
        GROUP BY 1 ORDER BY value DESC LIMIT 10
    """,
    "review_scores": """
        SELECT review_score::TEXT AS label, COUNT(*) AS value
        FROM ecommerce.olist_order_reviews GROUP BY 1 ORDER BY 1
    """,
    "funnel_origin": """
        SELECT COALESCE(m.origin, 'unknown') AS label,
               ROUND(100.0 * COUNT(DISTINCT c.mql_id) / NULLIF(COUNT(DISTINCT m.mql_id), 0), 1) AS value
        FROM marketing.marketing_qualified_leads m
        LEFT JOIN marketing.marketing_closed_deals c USING (mql_id)
        GROUP BY 1 ORDER BY value DESC
    """,
}


def chart_data(name):
    res = run_sql(CHARTS[name])
    if res.get("error"):
        return {"labels": [], "values": [], "error": res["error"]}
    return {
        "labels": [r[0] for r in res["rows"]],
        "values": [float(r[1]) if r[1] not in (None, "") else 0 for r in res["rows"]],
    }


def table_list():
    res = run_sql("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('ecommerce', 'marketing')
        ORDER BY 1, 2
    """)
    return [(r[0], r[1]) for r in res.get("rows", [])]


def table_rows(schema, table):
    return int(scalar('SELECT COUNT(*) FROM "%s"."%s"' % (schema, table)))


# --------------------------------------------------------------------- templates
LAYOUT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · Olist Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1420; --panel: #171e2e; --panel2: #1e2739; --line: #2a3550;
    --text: #e6ecf7; --muted: #8fa0bd; --accent: #4f8ef7; --accent2: #22c55e;
    --warn: #f59e0b; --danger: #ef4444;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
  a { color: var(--accent); text-decoration: none; }
  header { position: sticky; top: 0; z-index: 20; background: rgba(15,20,32,.94);
           backdrop-filter: blur(8px); border-bottom: 1px solid var(--line); }
  .bar { max-width: 1240px; margin: 0 auto; padding: 14px 22px; display: flex;
         align-items: center; gap: 22px; flex-wrap: wrap; }
  .brand { font-weight: 700; letter-spacing: .2px; font-size: 15px; line-height: 1.15; }
  .brand span { color: var(--accent); }
  .brand small { display: block; font-size: 10px; font-weight: 600; letter-spacing: .09em;
                 text-transform: uppercase; color: var(--muted); }
  nav { display: flex; gap: 4px; flex-wrap: wrap; }
  nav a { padding: 7px 13px; border-radius: 8px; color: var(--muted); font-weight: 600; }
  nav a:hover { background: var(--panel2); color: var(--text); }
  nav a.on { background: var(--accent); color: #fff; }
  .pill { margin-left: auto; font-size: 12px; color: var(--accent2);
          border: 1px solid #1f5138; background: #10231a; padding: 4px 10px; border-radius: 999px; }
  main { max-width: 1240px; margin: 0 auto; padding: 26px 22px 70px; }
  h1 { font-size: 21px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 30px 0 12px; color: var(--muted);
       text-transform: uppercase; letter-spacing: .09em; font-weight: 700; }
  .sub { color: var(--muted); margin: 0 0 22px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 12px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 15px 16px; }
  .card .k { color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
  .card .v { font-size: 23px; font-weight: 700; margin-top: 6px; letter-spacing: -.4px; }
  .card .u { color: var(--muted); font-size: 12px; font-weight: 500; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
  .panel h3 { margin: 0 0 12px; font-size: 13px; color: var(--muted); font-weight: 700;
              text-transform: uppercase; letter-spacing: .06em; }
  .chartbox { position: relative; height: 260px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
       position: sticky; top: 0; background: var(--panel); }
  tbody tr:hover { background: var(--panel2); }
  .scroll { overflow: auto; max-height: 420px; border: 1px solid var(--line); border-radius: 10px; }
  details { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
            margin-bottom: 10px; overflow: hidden; }
  details[open] { border-color: #35507e; }
  summary { cursor: pointer; padding: 12px 16px; font-weight: 600; display: flex; gap: 10px;
            align-items: center; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: "▸"; color: var(--accent); font-size: 12px; }
  details[open] summary::before { content: "▾"; }
  summary .meta { margin-left: auto; color: var(--muted); font-size: 12px; font-weight: 500; }
  .body { padding: 0 16px 16px; }
  pre { background: #0b111c; border: 1px solid var(--line); border-radius: 9px; padding: 12px;
        overflow: auto; font-size: 12px; color: #9fd0ff; margin: 0 0 12px; max-height: 240px; }
  .err { background: #2a1417; border: 1px solid #7f1d1d; color: #fca5a5; padding: 12px;
         border-radius: 9px; white-space: pre-wrap; font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .muted { color: var(--muted); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; }
  .ok { background: #10231a; color: var(--accent2); border: 1px solid #1f5138; }
  .bad { background: #2a1417; color: #fca5a5; border: 1px solid #7f1d1d; }
  form { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
  input, select, textarea, button { font: inherit; }
  input[type=text], select, textarea {
      background: var(--panel); color: var(--text); border: 1px solid var(--line);
      border-radius: 9px; padding: 9px 12px; }
  textarea { width: 100%; min-height: 150px; font-family: ui-monospace, Consolas, monospace;
             font-size: 13px; color: #9fd0ff; }
  button { background: var(--accent); color: #fff; border: 0; border-radius: 9px;
           padding: 9px 18px; font-weight: 700; cursor: pointer; }
  button:hover { filter: brightness(1.1); }
  .gallery { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 14px; }
  .gallery figure { margin: 0; background: var(--panel); border: 1px solid var(--line);
                    border-radius: 12px; overflow: hidden; }
  .gallery img { width: 100%; display: block; }
  .gallery figcaption { padding: 10px 14px; font-weight: 600; font-size: 13px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 18px; }
  .tabs a { padding: 8px 16px; border-radius: 9px; border: 1px solid var(--line);
            color: var(--muted); font-weight: 600; }
  .tabs a.on { background: var(--accent); border-color: var(--accent); color: #fff; }
</style>
</head>
<body>
<header>
  <div class="bar">
    <div class="brand">Olist <span>Analytics</span><small>E-Commerce &amp; Marketing Intelligence</small></div>
    <nav>
      <a href="/" class="{{ 'on' if page == 'overview' }}">Overview</a>
      <a href="/insights/ecommerce" class="{{ 'on' if page == 'ins_ecom' }}">E-commerce Insights</a>
      <a href="/insights/marketing" class="{{ 'on' if page == 'ins_mkt' }}">Marketing Funnel</a>
      <a href="/validation/ecommerce" class="{{ 'on' if page == 'val_ecom' }}">Validation</a>
      <a href="/explore" class="{{ 'on' if page == 'explore' }}">Data Explorer</a>
      <a href="/sql" class="{{ 'on' if page == 'sql' }}">SQL Console</a>
      <a href="/dashboards" class="{{ 'on' if page == 'dash' }}">Dashboards</a>
    </nav>
    <span class="pill">● live · PostgreSQL 16.2</span>
  </div>
</header>
<main>{{ body | safe }}</main>
<footer style="max-width:1240px;margin:0 auto;padding:0 22px 34px;color:#8fa0bd;font-size:12px;
               border-top:1px solid #2a3550;padding-top:16px">
  <strong style="color:#e6ecf7">Olist Analytics</strong> — End-to-End E-Commerce &amp; Marketing Analytics ·
  PostgreSQL 16.2 · 129 SQL queries live
</footer>
</body>
</html>
"""

OVERVIEW = """
<h1>Olist Analytics</h1>
<p class="sub">End-to-end e-commerce &amp; marketing funnel intelligence · live queries against the <code>olist</code> database</p>
<div class="cards">
  {% for c in cards %}
  <div class="card"><div class="k">{{ c.k }}</div><div class="v">{{ c.v }} <span class="u">{{ c.u }}</span></div></div>
  {% endfor %}
</div>

<h2>Revenue trend</h2>
<div class="panel"><div class="chartbox"><canvas id="rev"></canvas></div></div>

<h2>Breakdowns</h2>
<div class="grid2">
  <div class="panel"><h3>Orders by status</h3><div class="chartbox"><canvas id="status"></canvas></div></div>
  <div class="panel"><h3>Top 10 categories by revenue (R$)</h3><div class="chartbox"><canvas id="cats"></canvas></div></div>
  <div class="panel"><h3>Review score distribution</h3><div class="chartbox"><canvas id="reviews"></canvas></div></div>
  <div class="panel"><h3>MQL → closed deal conversion by origin (%)</h3><div class="chartbox"><canvas id="funnel"></canvas></div></div>
</div>

<script>
const D = {{ charts | tojson }};
const money = v => 'R$ ' + Number(v).toLocaleString('pt-BR', {maximumFractionDigits: 0});
const base = { responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { color: '#8fa0bd', boxWidth: 12 } } },
  scales: { x: { ticks: { color: '#8fa0bd' }, grid: { color: '#22304a' } },
            y: { ticks: { color: '#8fa0bd' }, grid: { color: '#22304a' } } } };

new Chart(document.getElementById('rev'), {
  type: 'line',
  data: { labels: D.revenue_by_month.labels,
          datasets: [{ label: 'Revenue', data: D.revenue_by_month.values,
                       borderColor: '#4f8ef7', backgroundColor: 'rgba(79,142,247,.18)',
                       fill: true, tension: .3, pointRadius: 0, borderWidth: 2 }] },
  options: { ...base, plugins: { legend: { display: false } },
             scales: { ...base.scales, y: { ...base.scales.y, ticks: { color: '#8fa0bd', callback: money } } } }
});

new Chart(document.getElementById('status'), {
  type: 'doughnut',
  data: { labels: D.orders_by_status.labels,
          datasets: [{ data: D.orders_by_status.values,
                       backgroundColor: ['#4f8ef7','#22c55e','#f59e0b','#ef4444','#a855f7','#06b6d4','#64748b','#eab308'] }] },
  options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { color: '#8fa0bd', boxWidth: 12 } } } }
});

new Chart(document.getElementById('cats'), {
  type: 'bar',
  data: { labels: D.top_categories.labels,
          datasets: [{ data: D.top_categories.values, backgroundColor: '#4f8ef7', borderRadius: 5 }] },
  options: { ...base, indexAxis: 'y', plugins: { legend: { display: false } },
             scales: { x: { ticks: { color: '#8fa0bd', callback: money }, grid: { color: '#22304a' } },
                       y: { ticks: { color: '#8fa0bd' }, grid: { display: false } } } }
});

new Chart(document.getElementById('reviews'), {
  type: 'bar',
  data: { labels: D.review_scores.labels,
          datasets: [{ data: D.review_scores.values, backgroundColor: '#22c55e', borderRadius: 5 }] },
  options: { ...base, plugins: { legend: { display: false } } }
});

new Chart(document.getElementById('funnel'), {
  type: 'bar',
  data: { labels: D.funnel_origin.labels,
          datasets: [{ data: D.funnel_origin.values, backgroundColor: '#a855f7', borderRadius: 5 }] },
  options: { ...base, plugins: { legend: { display: false } } }
});
</script>
"""

SCRIPT_PAGE = """
<h1>{{ heading }}</h1>
<p class="sub">{{ sub }}</p>
<div class="tabs">
  <a href="/insights/ecommerce" class="{{ 'on' if active == 'ecommerce' }}">E-commerce insights</a>
  <a href="/insights/marketing" class="{{ 'on' if active == 'marketing' }}">Marketing insights</a>
  <a href="/validation/ecommerce" class="{{ 'on' if active == 'ecommerce_val' }}">E-commerce validation</a>
  <a href="/validation/marketing" class="{{ 'on' if active == 'marketing_val' }}">Marketing validation</a>
</div>
<p class="sub">{{ results | length }} queries executed ·
   <span class="badge {{ 'ok' if errors == 0 else 'bad' }}">{{ errors }} errors</span> ·
   source: <code>{{ source_file }}</code></p>

{% for r in results %}
<details {{ 'open' if loop.index <= 2 }}>
  <summary>
    <span>{{ r.title }}</span>
    <span class="meta">
      {% if r.error %}<span class="badge bad">error</span>
      {% else %}{{ r.total }} rows · {{ r.elapsed }}s{% endif %}
    </span>
  </summary>
  <div class="body">
    <pre>{{ r.sql }}</pre>
    {% if r.error %}
      <div class="err">{{ r.error }}</div>
    {% elif r.total == 0 %}
      <p class="muted">No rows returned.</p>
    {% else %}
      <div class="scroll"><table>
        <thead><tr>{% for c in r.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
        <tbody>
          {% for row in r.rows[:200] %}
          <tr>{% for cell in row %}<td>{{ cell if cell is not none else '—' }}</td>{% endfor %}</tr>
          {% endfor %}
        </tbody>
      </table></div>
      {% if r.total > 200 %}<p class="muted">Showing first 200 of {{ r.total }} rows.</p>{% endif %}
    {% endif %}
  </div>
</details>
{% endfor %}
"""

EXPLORE = """
<h1>Data Explorer</h1>
<p class="sub">All {{ tables | length }} tables in the analytical layer.</p>
<form method="get">
  <select name="t">
    {% for s, t in tables %}
    <option value="{{ s }}.{{ t }}" {{ 'selected' if sel == s ~ '.' ~ t }}>{{ s }}.{{ t }}</option>
    {% endfor %}
  </select>
  <input type="text" name="q" value="{{ q }}" placeholder="optional filter, e.g. order_status = 'canceled'">
  <button type="submit">Preview</button>
  <span class="muted">{{ total }} rows</span>
</form>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
{% if columns %}
<div class="scroll"><table>
  <thead><tr>{% for c in columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
  <tbody>
    {% for row in rows %}
    <tr>{% for cell in row %}<td>{{ cell if cell is not none else '—' }}</td>{% endfor %}</tr>
    {% endfor %}
  </tbody>
</table></div>
<p class="muted">Showing {{ rows | length }} of {{ total }} rows (max 500).</p>
{% endif %}
"""

SQL_PAGE = """
<h1>SQL Console</h1>
<p class="sub">Read-only queries against <code>ecommerce</code> and <code>marketing</code>.</p>
<form method="post">
  <textarea name="q">{{ q }}</textarea>
  <div style="display:flex; gap:10px; align-items:center; width:100%">
    <button type="submit">Run query</button>
    <span class="muted">Ctrl+Enter runs · only SELECT / WITH / SHOW / EXPLAIN / TABLE allowed</span>
  </div>
</form>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
{% if columns %}
<div class="scroll"><table>
  <thead><tr>{% for c in columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
  <tbody>
    {% for row in rows %}
    <tr>{% for cell in row %}<td>{{ cell if cell is not none else '—' }}</td>{% endfor %}</tr>
    {% endfor %}
  </tbody>
</table></div>
<p class="muted">{{ total }} rows · {{ elapsed }}s</p>
{% endif %}
"""

DASH = """
<h1>Power BI Dashboards</h1>
<p class="sub">Screenshots shipped with the project (the .pbix files are linked in
   <code>08_power_bi_dashboard/</code>).</p>
<div class="gallery">
  {% for img, label in images %}
  <figure>
    <a href="/screenshots/{{ img }}" target="_blank"><img src="/screenshots/{{ img }}" alt="{{ label }}"></a>
    <figcaption>{{ label }}</figcaption>
  </figure>
  {% endfor %}
</div>
"""


def render(page, title, body, **ctx):
    return render_template_string(LAYOUT, page=page, title=title, body=render_template_string(body, **ctx))


# ----------------------------------------------------------------------- routes
@app.route("/")
def overview():
    kpi = run_sql(KPI_SQL)
    row = kpi["rows"][0] if not kpi.get("error") and kpi["rows"] else [0] * 6
    orders, customers, gmv, aov, sellers, products = row
    delivery = run_sql(DELIVERY_SQL)
    days, late = delivery["rows"][0] if delivery.get("rows") else ("—", "—")

    reviews = run_sql("SELECT ROUND(AVG(review_score), 2) FROM ecommerce.olist_order_reviews")
    avg_review = reviews["rows"][0][0] if reviews.get("rows") else "—"
    funnel = run_sql("""
        SELECT ROUND(100.0 * COUNT(DISTINCT c.mql_id) / NULLIF(COUNT(DISTINCT m.mql_id), 0), 2)
        FROM marketing.marketing_qualified_leads m
        LEFT JOIN marketing.marketing_closed_deals c USING (mql_id)
    """)
    conv = funnel["rows"][0][0] if funnel.get("rows") else "—"

    def n(v):
        try:
            return "{:,}".format(int(float(v)))
        except (TypeError, ValueError):
            return "—"

    def money(v):
        try:
            return "R$ {:,.0f}".format(float(v))
        except (TypeError, ValueError):
            return "—"

    cards = [
        {"k": "Orders", "v": n(orders), "u": ""},
        {"k": "GMV", "v": money(gmv), "u": "items + freight"},
        {"k": "Avg order value", "v": "R$ %s" % aov, "u": ""},
        {"k": "Customers", "v": n(customers), "u": ""},
        {"k": "Sellers", "v": n(sellers), "u": ""},
        {"k": "Products", "v": n(products), "u": ""},
        {"k": "Avg delivery", "v": "%s d" % days, "u": "purchase → customer"},
        {"k": "Late deliveries", "v": "%s%%" % late, "u": "vs estimate"},
        {"k": "Avg review", "v": avg_review, "u": "/ 5"},
        {"k": "MQL → deal", "v": "%s%%" % conv, "u": "8,000 leads"},
    ]
    charts = {name: chart_data(name) for name in CHARTS}
    return render("overview", "Overview", OVERVIEW, cards=cards, charts=charts)


@app.route("/insights/<domain>")
@app.route("/validation/<domain>")
def script_page(domain=None):
    kind = "insights" if request.path.startswith("/insights") else "validation"
    if domain not in SCRIPTS:
        abort(404)
    path = SCRIPTS[domain][kind]
    results = run_script(path)
    errors = sum(1 for r in results if r.get("error"))
    heading = "%s — %s" % (LABELS[domain], kind.title())
    sub = ("Executed from the project's own .sql file, unmodified apart from the fixes needed to run."
           if kind == "insights" else
           "Data-quality and integrity checks defined in the project.")
    page = ("ins_%s" % domain[:4]) if kind == "insights" else "val_%s" % domain[:4]
    return render(page, heading, SCRIPT_PAGE,
                  heading=heading, sub=sub, results=results, errors=errors,
                  source_file=os.path.relpath(path, PROJECT).replace("\\", "/"),
                  active=domain if kind == "insights" else domain + "_val")


@app.route("/explore")
def explore():
    tables = table_list()
    sel = request.args.get("t") or ("%s.%s" % tables[0] if tables else "")
    q = (request.args.get("q") or "").strip()
    columns, rows, total, error = [], [], 0, None

    if sel:
        schema, _, table = sel.partition(".")
        if (schema, table) in tables:
            where = ""
            if q:
                if ";" in q:
                    error = "Filter may not contain ';'."
                else:
                    where = " WHERE " + q
            if not error:
                total = table_rows(schema, table)
                res = run_sql('SELECT * FROM "%s"."%s"%s LIMIT 500' % (schema, table, where))
                if res.get("error"):
                    error = res["error"]
                else:
                    columns, rows = res["columns"], res["rows"]
        else:
            error = "Unknown table."

    return render("explore", "Data Explorer", EXPLORE,
                  tables=tables, sel=sel, q=q, columns=columns, rows=rows, total=total, error=error)


@app.route("/sql", methods=["GET", "POST"])
def sql_console():
    q = (request.form.get("q") or request.args.get("q") or
         "SELECT order_status, COUNT(*) AS orders\nFROM ecommerce.olist_orders\nGROUP BY 1\nORDER BY 2 DESC;")
    columns, rows, total, error, elapsed = [], [], 0, None, 0
    if request.method == "POST":
        stripped = q.strip().rstrip(";").strip()
        if not re.match(r"(?is)^(select|with|show|explain|table|values)\b", stripped):
            error = "Only read-only statements are allowed (SELECT, WITH, SHOW, EXPLAIN, TABLE, VALUES)."
        elif ";" in stripped:
            error = "Please run one statement at a time."
        else:
            res = run_sql(stripped)
            if res.get("error"):
                error = res["error"]
            else:
                columns, rows, total, elapsed = res["columns"], res["rows"], res["total"], res["elapsed"]

    return render("sql", "SQL Console", SQL_PAGE,
                  q=q, columns=columns, rows=rows, total=total, error=error, elapsed=elapsed)


@app.route("/dashboards")
def dashboards():
    root = os.path.join(PROJECT, "08_power_bi_dashboard", "screenshots")
    images = []
    if os.path.isdir(root):
        for folder, prefix in (("ecommerce", "E-commerce · "), ("marketing", "Marketing · ")):
            d = os.path.join(root, folder)
            if os.path.isdir(d):
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith((".png", ".jpg", ".jpeg")):
                        images.append(("%s/%s" % (folder, f), prefix + os.path.splitext(f)[0]))
    return render("dash", "Dashboards", DASH, images=images)


@app.route("/screenshots/<path:sub>")
def screenshots(sub):
    return send_from_directory(os.path.join(PROJECT, "08_power_bi_dashboard", "screenshots"), sub)


@app.route("/api/health")
def health():
    res = run_sql("SELECT 1")
    return jsonify({"db": "ok" if not res.get("error") else "down", "error": res.get("error")})


if __name__ == "__main__":
    print("Olist Analytics -> http://%s:%s" % (HOST, PORT))
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
