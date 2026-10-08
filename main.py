import os, json, time, httpx
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---- Settings ----
BASE = "https://exam.sanand.workers.dev/questionData?email=24f2005600%40ds.study.iitm.ac.in&quizSign=EEvq2lMwEVdSZIk1%2F7mUzJFM0RA4YUgdDp9I%2FJl670EUfNLm%2FzP8DWd%2FhosQtMC%2BwGVE5Lv0d2cifcVOn0JiKrF17c9J91BoJGrf249gkIwiV6DW731dtegn5F%2B2KWjAVjA%2BHHAL8yl%2BDWgyeks%2BKev9OJpcpadpBWW8%2FW%2FHMFvn7UPsqpD6YKddEDXFCjHJk9yzrOA0A0N5crk1yKbLPCzp4XO%2BYXrISkGxm%2F%2BlPwEhKFwZ0%2BStsEvSgXEukhhtdFhLpioQEpfBI9ayy9TAzf1fEjtI%2BE7KB3ZKklHY1KxrAgBqB7b0w4NzgUnCyJ96Kw00ugbyFkuKdV53F4ClkA%3D%3D&questionId=q-ledger-agent-server&path="
AIPIPE_TOKEN = os.environ["AIPIPE_TOKEN"]
MODELS = ["openai/gpt-4.1-mini", "openai/gpt-4.1-nano"]
IST = timezone(timedelta(hours=5, minutes=30))     # India has no daylight saving

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ---- 1. Download everything once, at startup ----
def get_json(suffix, tries=3):
    for i in range(tries):
        try:
            return httpx.get(BASE + suffix, timeout=30).json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1)

pages = get_json("%2Forders&page=1")["pages"]
with ThreadPoolExecutor(8) as ex:
    chunks = list(ex.map(lambda n: get_json(f"%2Forders&page={n}")["orders"],
                         range(1, pages + 1)))
RAW = [o for c in chunks for o in c]
RATES = get_json("%2Frates")["usd_per_unit"]

# ---- 2. Clean it: latest version per id, USD amounts, India-time dates ----
def ts(s):
    return datetime.fromisoformat(s)

latest = {}
for o in RAW:
    cur = latest.get(o["id"])
    if cur is None or ts(o["updated_at"]) > ts(cur["updated_at"]):
        latest[o["id"]] = o

ORDERS = []
for o in latest.values():
    d = ts(o["created_at"]).astimezone(IST)
    row = dict(o)
    row["status"] = str(o["status"]).strip().lower()
    row["region"] = str(o["region"]).strip()
    row["date"] = d.strftime("%Y-%m-%d")
    row["month"] = d.strftime("%Y-%m")
    row["amount_usd"] = Decimal(str(o["amount"])) * Decimal(str(RATES[o["currency"]]))
    ORDERS.append(row)

print(f"Loaded {len(RAW)} raw rows -> {len(ORDERS)} unique orders, {pages} pages")

# ---- 3. Describe the data to the model automatically ----
def describe():
    lines = [f"Example row: {json.dumps(ORDERS[0], default=str)}"]
    for field in ORDERS[0]:
        vals = {str(r.get(field)) for r in ORDERS}
        if 1 < len(vals) <= 30 and field != "id":
            lines.append(f'Values of "{field}": {sorted(vals)}')
    return "\n".join(lines)

DESCRIPTION = describe()
print(DESCRIPTION)

SYSTEM = """You turn a question about an order ledger into a JSON recipe. Reply with JSON only.
Schema:
{"filters": [{"field": str, "op": "=" | ">=" | "<=" | "in", "value": any}],
 "metric": "sum" | "count" | "count_distinct" | "avg" | "max" | "min" | "list",
 "field": <field to sum/average/list/count_distinct, or null for count>,
 "group_by": <field or null>,
 "order": "desc" | "asc",
 "top_n": <int or null>}

The data is ONE list of orders, one row per order (already de-duplicated).
Extra fields: "amount_usd" (amount converted to USD), "date" (YYYY-MM-DD, India time),
"month" (YYYY-MM, India time).

Rules:
- Revenue / sales / total money in USD = metric "sum" of "amount_usd" with a filter status = "paid".
- Refunds: status = "refunded". Refund amount = sum of "amount_usd" with status "refunded";
  number of refunds = count with status "refunded".
- "How many orders" with no status mentioned: count with no status filter.
  "paid orders" means status = "paid".
- Units sold = sum of "qty" (with status "paid" unless the question says otherwise).
- Number of customers = count_distinct of "customer" (with any filters the question implies).
- Time: use "month" = "YYYY-MM" for a month; use "date" >= and "date" <= for other ranges
  (a quarter, a year, specific days). Never filter on created_at or updated_at.
- Region, product, customer values must be written exactly as in the data.
- "Which / top / highest / most": use group_by, top_n 1 (order "desc"). "Lowest / fewest": order "asc".
  "Top 3": top_n 3. A breakdown "by region": group_by "region", top_n null.
- For money, use field "amount_usd".

Data description:
""" + DESCRIPTION

# ---- 4. The model writes a recipe ----
def make_recipe(question):
    deadline = time.monotonic() + 12
    last = {}
    for _ in range(2):
        for model in MODELS:
            left = deadline - time.monotonic()
            if left < 2:
                raise RuntimeError("Out of time. Last reply: " + json.dumps(last)[:400])
            try:
                last = httpx.post(
                    "https://aipipe.org/openrouter/v1/chat/completions",
                    headers={"Authorization": f"Bearer {AIPIPE_TOKEN}"},
                    json={"model": model, "temperature": 0,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": SYSTEM},
                                       {"role": "user", "content": question}]},
                    timeout=min(8, left)).json()
            except httpx.TimeoutException:
                print("TIMEOUT on", model)
                continue
            if "choices" in last:
                text = last["choices"][0]["message"]["content"]
                return json.loads(text.replace("```json", "").replace("```", "").strip())
            print("AIPIPE ERROR on", model, ":", json.dumps(last)[:300])
    raise RuntimeError("All models failed. Last reply: " + json.dumps(last)[:400])

# ---- 5. Python does the maths ----
def matches(row, f):
    v, t = row.get(f["field"]), f["value"]
    if f["op"] == "=":  return str(v).strip().lower() == str(t).strip().lower()
    if f["op"] == ">=": return v >= t
    if f["op"] == "<=": return v <= t
    if f["op"] == "in": return str(v).strip().lower() in [str(x).strip().lower() for x in t]

def tidy(x):
    q = Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return int(q) if q == q.to_integral_value() else float(q)

def agg(rs, m, fld):
    if m == "count":          return len(rs)
    if m == "count_distinct": return len({r[fld] for r in rs})
    if m == "list":           return sorted({r[fld] for r in rs})
    vals = [Decimal(str(r[fld])) for r in rs]
    if not vals: return 0
    out = {"sum": sum(vals), "avg": sum(vals) / len(vals),
           "max": max(vals), "min": min(vals)}[m]
    return tidy(out)

def run(recipe):
    rows = [r for r in ORDERS if all(matches(r, f) for f in recipe.get("filters", []))]
    m, fld, g = recipe["metric"], recipe.get("field"), recipe.get("group_by")
    if not g:
        return agg(rows, m, fld)
    groups = {}
    for r in rows:
        groups.setdefault(r[g], []).append(r)
    ranked = sorted(((k, agg(v, m, fld)) for k, v in groups.items()),
                    key=lambda x: x[1], reverse=(recipe.get("order") != "asc"))
    n = recipe.get("top_n")
    if not ranked: return None
    if n == 1: return ranked[0][0]
    if n:      return [k for k, _ in ranked[:n]]
    return {k: v for k, v in ranked}

# ---- 6. The public endpoint ----
@app.post("/")
def answer(body: dict):
    t0 = time.monotonic()
    recipe = make_recipe(body["question"])
    result = run(recipe)
    print(f'{body["question"]}\n  -> {recipe}\n  -> {result}  ({time.monotonic()-t0:.1f}s)')
    return {"answer": result}

@app.get("/")
def health():
    return {"status": "ok", "orders": len(ORDERS)}