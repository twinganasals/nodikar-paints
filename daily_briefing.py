"""
Nodikar Paints - Automated Evening Director's Briefing

Runs once a day (see daily_briefing.yml):
  1. reads today's numbers from the Neon database (read-only),
  2. asks Claude to write a short plain-English audit from those numbers,
  3. sends it to WhatsApp (WhatsApp Business Cloud API).

Settings come from environment variables (GitHub "Secrets"):
  NEON_DATABASE_URL        postgresql://user:password@host/dbname?sslmode=require
  ANTHROPIC_API_KEY        key from console.anthropic.com
  WHATSAPP_TOKEN           permanent access token from Meta (System User)
  WHATSAPP_PHONE_NUMBER_ID the sender's Phone Number ID from Meta
  WHATSAPP_TO              your number, international format, digits only, e.g. 2567XXXXXXXX
                           (several numbers can be separated by commas)
Optional:
  WHATSAPP_TEMPLATE        approved template name (needed for automatic messages, see README notes)
  WHATSAPP_TEMPLATE_LANG   template language code, default "en"
  ANTHROPIC_MODEL          default "claude-haiku-5-5"
  REPORT_DATE              YYYY-MM-DD, to re-run a past day (default: today in Kampala)
  DRY_RUN                  "1" = print the message instead of sending it
"""
import os
import sys
import json
import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg2
import requests

TZ = ZoneInfo("Africa/Kampala")


# --------------------------------------------------------------------------- helpers
def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required setting: {name}")
    return value


def num(x):
    """Database numbers (Decimal/None) -> plain float."""
    return float(x) if isinstance(x, (Decimal, int, float)) else 0.0


def ugx(x):
    return f"UGX {num(x):,.0f}"


def fetch(cur, query, params=()):
    cur.execute(query, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def scalar(cur, query, params=()):
    cur.execute(query, params)
    row = cur.fetchone()
    return num(row[0]) if row else 0.0


# --------------------------------------------------------------------------- 1. data pull
def collect_data(conn, day):
    data = {"date": day.isoformat()}
    with conn.cursor() as cur:
        # Sales made today, by channel: invoiced vs cash collected vs credit issued
        rows = fetch(cur, """
            SELECT s.sales_channel,
                   COALESCE(SUM(s.quantity_sold * s.actual_price), 0) AS invoiced,
                   COALESCE(SUM(p.paid), 0) AS cash_collected
            FROM sales_ledger s
            LEFT JOIN (SELECT sale_id, SUM(amount) AS paid FROM sale_payments
                       WHERE payment_date <= %s GROUP BY sale_id) p ON p.sale_id = s.sale_id
            WHERE s.sale_date = %s
            GROUP BY s.sales_channel ORDER BY s.sales_channel""", (day, day))
        channels = []
        for r in rows:
            invoiced, cash = num(r["invoiced"]), num(r["cash_collected"])
            channels.append({"channel": r["sales_channel"], "invoiced": invoiced,
                             "cash_collected": cash, "credit_issued": max(invoiced - cash, 0.0)})
        data["sales_by_channel"] = channels
        data["sales_total"] = {
            "invoiced": sum(c["invoiced"] for c in channels),
            "cash_collected": sum(c["cash_collected"] for c in channels),
            "credit_issued": sum(c["credit_issued"] for c in channels),
        }

        # Money recovered today from earlier credit sales / other debts
        data["recoveries_received_today"] = (
            scalar(cur, """SELECT COALESCE(SUM(sp.amount), 0) FROM sale_payments sp
                           JOIN sales_ledger s ON s.sale_id = sp.sale_id
                           WHERE sp.payment_date = %s AND s.sale_date < %s""", (day, day))
            + scalar(cur, "SELECT COALESCE(SUM(amount), 0) FROM debt_recoveries WHERE recovery_date = %s", (day,)))

        # Truck variances (missing / surplus stock) from today's reconciliations
        data["fleet_runs_reconciled_today"] = int(scalar(cur, """
            SELECT COUNT(*) FROM daily_run_loading l JOIN fleet_daily_runs r ON l.run_id = r.run_id
            WHERE r.run_date = %s""", (day,)))
        variances = fetch(cur, """
            SELECT vehicle_color, agent_name, item_name, variance_units, variance_units * set_price AS variance_value
            FROM (
                SELECT v.vehicle_color, a.agent_name, i.item_name, l.set_price,
                       (l.opening_stock + l.added_items - l.physical_final_stock) - l.agent_registered_sold AS variance_units
                FROM daily_run_loading l
                JOIN fleet_daily_runs r ON l.run_id = r.run_id
                JOIN vehicles v ON v.vehicle_id = r.vehicle_id
                JOIN agents a ON a.agent_id = r.agent_id
                JOIN inventory_items i ON i.item_id = l.item_id
                WHERE r.run_date = %s
            ) t
            WHERE variance_units <> 0
            ORDER BY ABS(variance_units * set_price) DESC LIMIT 10""", (day,))
        data["truck_variances"] = [
            {"truck": v["vehicle_color"], "agent": v["agent_name"], "item": v["item_name"],
             "units": num(v["variance_units"]),  # positive = missing, negative = surplus
             "value": num(v["variance_value"])} for v in variances]

        # Expenses, supplier payments and commissions
        exp_rows = fetch(cur, """
            SELECT COALESCE(c.category_name, 'Uncategorised') AS category, SUM(e.financial_amount) AS total
            FROM enterprise_ledger e LEFT JOIN general_expenditure_categories c ON c.category_id = e.category_id
            WHERE e.accounting_date = %s AND e.entry_type = 'Outflow'
              AND COALESCE(e.source_reference_table, '') NOT IN ('commission', 'procurement')
            GROUP BY 1 ORDER BY 2 DESC""", (day,))
        data["expenses_by_category"] = [{"category": r["category"], "total": num(r["total"])} for r in exp_rows]
        data["expenses_total"] = sum(r["total"] for r in data["expenses_by_category"])
        data["commissions_total"] = scalar(cur, """
            SELECT COALESCE(SUM(financial_amount), 0) FROM enterprise_ledger
            WHERE accounting_date = %s AND entry_type = 'Outflow' AND source_reference_table = 'commission'""", (day,))
        data["supplier_payments_total"] = scalar(
            cur, "SELECT COALESCE(SUM(amount), 0) FROM purchase_payments WHERE payment_date = %s", (day,))

        # Outstanding debts owed to the company
        data["outstanding_debts_total"] = scalar(
            cur, "SELECT COALESCE(SUM(amount_owed - amount_recovered), 0) FROM agent_debts WHERE status != 'Cleared'")

        # Raw materials below reorder level - only possible if the database tracks stock and reorder levels
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'inventory_items'")
        cols = {r[0] for r in cur.fetchall()}
        if {"reorder_level", "current_stock"} <= cols:
            low = fetch(cur, """SELECT item_name, current_stock, reorder_level FROM inventory_items
                                WHERE item_type = 'Raw Material' AND current_stock <= reorder_level ORDER BY item_name""")
            data["raw_materials_below_reorder_level"] = [
                {"item": r["item_name"], "stock": num(r["current_stock"]), "reorder_level": num(r["reorder_level"])} for r in low]
        else:
            data["raw_materials_below_reorder_level"] = "not tracked in the database"
    return data


# --------------------------------------------------------------------------- 2. AI audit
SYSTEM_PROMPT = (
    "Act as the Financial Auditor for Nodikar Paints, a paint company in Uganda. Summarize today's performance in "
    "5 short bullet points for the Director, and highlight any truck shortages or urgent raw material reorders.\n"
    "Rules: use ONLY the numbers in the data you are given; never invent or estimate figures. Write amounts as "
    "'UGX 1,234,000'. Each bullet starts with '• ' and is one or two short sentences. If a truck variance has positive "
    "units, those units are missing; negative means surplus. If raw materials are 'not tracked in the database', do not "
    "mention reorders. If no truck reconciliation was done today, say so. If there were no sales, say so plainly. "
    "Plain text only (no markdown, no asterisks, no headings). Keep the whole reply under 800 characters."
)


def fallback_summary(data):
    """Plain summary built without AI, used if the AI call fails so the briefing still goes out."""
    t = data["sales_total"]
    lines = [f"• Sales: {ugx(t['invoiced'])} invoiced; {ugx(t['cash_collected'])} collected, "
             f"{ugx(t['credit_issued'])} on credit. Recoveries received today: {ugx(data['recoveries_received_today'])}."]
    if data["truck_variances"]:
        v = data["truck_variances"][0]
        kind = "missing" if v["units"] > 0 else "surplus"
        lines.append(f"• Fleet alert: {len(data['truck_variances'])} variance(s). Largest: {v['truck']} truck, "
                     f"{abs(v['units']):g} {kind} of {v['item']} ({ugx(abs(v['value']))}).")
    elif data["fleet_runs_reconciled_today"]:
        lines.append("• Fleet: all trucks reconciled with no variance.")
    else:
        lines.append("• Fleet: no truck reconciliation was recorded today.")
    lines.append(f"• Expenses: {ugx(data['expenses_total'])}; agent commissions: {ugx(data['commissions_total'])}; "
                 f"supplier payments: {ugx(data['supplier_payments_total'])}.")
    lines.append(f"• Outstanding debts owed to the company: {ugx(data['outstanding_debts_total'])}.")
    low = data["raw_materials_below_reorder_level"]
    if isinstance(low, list):
        lines.append("• Reorder now: " + ", ".join(i["item"] for i in low) + "." if low else "• No raw materials are below reorder level.")
    return "\n".join(lines)


def ask_ai(data, api_key):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": env("ANTHROPIC_MODEL", "claude-haiku-5-5"), "max_tokens": 600, "temperature": 0.2,
              "system": SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": "Today's data (JSON):\n" + json.dumps(data, indent=1)}]},
        timeout=60)
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json()["content"] if b.get("type") == "text").strip()


# --------------------------------------------------------------------------- 3. WhatsApp
def build_whatsapp_payload(to, message, template=None, lang="en"):
    if template:
        # Template parameters may not contain line breaks, so bullets are joined on one line.
        flat = " | ".join(part.strip() for part in message.splitlines() if part.strip())
        flat = " ".join(flat.split())[:1000]
        return {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": template, "language": {"code": lang},
                             "components": [{"type": "body", "parameters": [{"type": "text", "text": flat}]}]}}
    # Free-text messages are only delivered if you messaged the number in the last 24 hours.
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}}


def send_whatsapp(message):
    token = env("WHATSAPP_TOKEN", required=True)
    phone_id = env("WHATSAPP_PHONE_NUMBER_ID", required=True)
    recipients = [n.strip() for n in env("WHATSAPP_TO", required=True).split(",") if n.strip()]
    failed = False
    for to in recipients:
        payload = build_whatsapp_payload(to, message, env("WHATSAPP_TEMPLATE"), env("WHATSAPP_TEMPLATE_LANG", "en"))
        r = requests.post(f"https://graph.facebook.com/v21.0/{phone_id}/messages",
                          headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=30)
        if r.ok:
            print(f"Sent to {to}")
        else:
            failed = True
            print(f"WhatsApp error for {to}: {r.status_code} {r.text}", file=sys.stderr)
    if failed:
        sys.exit(1)


# --------------------------------------------------------------------------- main
def main():
    day = dt.date.fromisoformat(env("REPORT_DATE")) if env("REPORT_DATE") else dt.datetime.now(TZ).date()

    conn = psycopg2.connect(env("NEON_DATABASE_URL", required=True), connect_timeout=30)
    conn.set_session(readonly=True)  # this script can never change your data
    try:
        data = collect_data(conn, day)
    finally:
        conn.close()

    try:
        body = ask_ai(data, env("ANTHROPIC_API_KEY", required=True))
        if not body:
            raise ValueError("empty AI reply")
    except Exception as e:  # still deliver the numbers if the AI is unavailable
        print(f"AI step failed ({e}); using plain summary.", file=sys.stderr)
        body = fallback_summary(data)

    message = f"Nodikar Paints — Daily Audit ({day:%d %b %Y})\n{body}"
    if env("DRY_RUN") == "1":
        print("---- DRY RUN: message not sent ----")
        print(message)
        print("---- data used ----")
        print(json.dumps(data, indent=1))
    else:
        send_whatsapp(message)


if __name__ == "__main__":
    main()
