"""
Nodikar Paints - Automated Evening Director's Briefing

Two reports share this script (see daily_briefing.yml):
  daily  - every evening, about today's numbers
  weekly - every Sunday morning, about the 7 days before (Sunday to Saturday), compared with the week before
  Choose with the REPORT_TYPE setting ("daily" is the default).

Each run:
  1. reads today's numbers from the Neon database (read-only),
  2. asks Claude to write a short plain-English audit from those numbers,
  3. sends it to WhatsApp (WhatsApp Business Cloud API).

Settings come from environment variables (GitHub "Secrets").

FREE setup (no paid services):
  NEON_DATABASE_URL        postgresql://user:password@host/dbname?sslmode=require
  TELEGRAM_BOT_TOKEN       token from Telegram's @BotFather
  TELEGRAM_CHAT_ID         your chat id (several can be separated by commas)
  (no ANTHROPIC_API_KEY: the summary is then written by the script itself, at no cost)

Optional upgrades:
  ANTHROPIC_API_KEY        if set, Claude writes the audit text (paid API)
  CALLMEBOT_APIKEY + WHATSAPP_TO                 free WhatsApp via CallMeBot instead of Telegram
  WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TO   official Meta WhatsApp Business API
  WHATSAPP_TEMPLATE        approved template name (Meta API only)
  WHATSAPP_TEMPLATE_LANG   template language code, default "en"
  ANTHROPIC_MODEL          default "claude-haiku-5-5"
  REPORT_TYPE              "daily" (default) or "weekly"
  REPORT_DATE              YYYY-MM-DD, to re-run a past day; for weekly, the LAST day of the week to report
                           (default: today in Kampala; for weekly, yesterday)
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

        # Money recovered today from earlier credit sales / other debts. Negative entries are corrections
        # (someone edited an earlier payment or recovery downwards), reported separately so they are not mistaken for recoveries.
        cur.execute("""SELECT COALESCE(SUM(CASE WHEN amt > 0 THEN amt END), 0), COALESCE(SUM(CASE WHEN amt < 0 THEN amt END), 0) FROM (
                           SELECT sp.amount AS amt FROM sale_payments sp JOIN sales_ledger s ON s.sale_id = sp.sale_id
                           WHERE sp.payment_date = %s AND s.sale_date < %s
                           UNION ALL
                           SELECT amount AS amt FROM debt_recoveries WHERE recovery_date = %s) t""", (day, day, day))
        rec_pos, rec_neg = cur.fetchone()
        data["recoveries_received_today"] = num(rec_pos)
        data["corrections_to_earlier_payments_today"] = num(rec_neg)  # negative number, 0 if none

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
    "'corrections_to_earlier_payments_today' is a negative number when earlier payments were edited downwards; mention it briefly if not zero. "
    "Plain text only (no markdown, no asterisks, no headings). Keep the whole reply under 800 characters."
)


def fallback_summary(data):
    """Plain summary built without AI, used if the AI call fails so the briefing still goes out."""
    t = data["sales_total"]
    lines = [f"• Sales: {ugx(t['invoiced'])} invoiced; {ugx(t['cash_collected'])} collected, "
             f"{ugx(t['credit_issued'])} on credit. Recoveries received today: {ugx(data['recoveries_received_today'])}."]
    if data["corrections_to_earlier_payments_today"]:
        lines[0] += f" Corrections to earlier payments: {ugx(data['corrections_to_earlier_payments_today'])}."
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


def ask_ai(data, api_key, system_prompt=None):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": env("ANTHROPIC_MODEL", "claude-haiku-5-5"), "max_tokens": 600, "temperature": 0.2,
              "system": system_prompt or SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": "Data (JSON):\n" + json.dumps(data, indent=1)}]},
        timeout=60)
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json()["content"] if b.get("type") == "text").strip()



# --------------------------------------------------------------------------- weekly report
def week_totals(cur, start, end):
    """Headline totals for a date range (used for this week and the week before)."""
    t = {"invoiced": scalar(cur, "SELECT COALESCE(SUM(quantity_sold * actual_price), 0) FROM sales_ledger "
                                 "WHERE sale_date BETWEEN %s AND %s", (start, end))}
    t["cash_received"] = (
        scalar(cur, """SELECT COALESCE(SUM(sp.amount), 0) FROM sale_payments sp JOIN sales_ledger s ON s.sale_id = sp.sale_id
                       WHERE sp.payment_date BETWEEN %s AND %s""", (start, end))
        + scalar(cur, """SELECT COALESCE(SUM(dr.amount), 0) FROM debt_recoveries dr JOIN agent_debts d ON d.debt_id::text = dr.debt_id
                         WHERE dr.recovery_date BETWEEN %s AND %s""", (start, end)))
    t["total_expenses"] = (
        scalar(cur, """SELECT COALESCE(SUM(financial_amount), 0) FROM enterprise_ledger
                       WHERE accounting_date BETWEEN %s AND %s AND entry_type = 'Outflow'
                         AND COALESCE(source_reference_table, '') <> 'procurement'""", (start, end))
        + scalar(cur, """SELECT COALESCE(SUM(pp.amount), 0) FROM purchase_payments pp JOIN purchases p ON p.purchase_id = pp.purchase_id
                         WHERE pp.payment_date BETWEEN %s AND %s""", (start, end)))
    t["net_cash_flow"] = t["cash_received"] - t["total_expenses"]
    return t


def collect_weekly(conn, start, end):
    data = {"period_start": start.isoformat(), "period_end": end.isoformat()}
    with conn.cursor() as cur:
        data["this_week"] = week_totals(cur, start, end)
        data["previous_week"] = week_totals(cur, start - dt.timedelta(days=7), end - dt.timedelta(days=7))

        # Sales by channel: invoiced vs cash collected vs credit issued
        rows = fetch(cur, """
            SELECT s.sales_channel,
                   COALESCE(SUM(s.quantity_sold * s.actual_price), 0) AS invoiced,
                   COALESCE(SUM(p.paid), 0) AS cash_collected
            FROM sales_ledger s
            LEFT JOIN (SELECT sale_id, SUM(amount) AS paid FROM sale_payments
                       WHERE payment_date <= %s GROUP BY sale_id) p ON p.sale_id = s.sale_id
            WHERE s.sale_date BETWEEN %s AND %s
            GROUP BY s.sales_channel ORDER BY 2 DESC""", (end, start, end))
        data["sales_by_channel"] = [
            {"channel": r["sales_channel"], "invoiced": num(r["invoiced"]), "cash_collected": num(r["cash_collected"]),
             "credit_issued": max(num(r["invoiced"]) - num(r["cash_collected"]), 0.0)} for r in rows]
        data["credit_issued_total"] = sum(c["credit_issued"] for c in data["sales_by_channel"])

        data["top_agents"] = [{"agent": r["agent_name"], "invoiced": num(r["invoiced"])} for r in fetch(cur, """
            SELECT a.agent_name, SUM(s.quantity_sold * s.actual_price) AS invoiced
            FROM sales_ledger s JOIN agents a ON a.agent_id = s.agent_id
            WHERE s.sale_date BETWEEN %s AND %s GROUP BY a.agent_name ORDER BY 2 DESC LIMIT 5""", (start, end))]
        data["top_items"] = [{"item": r["item_name"], "units": num(r["units"]), "invoiced": num(r["invoiced"])} for r in fetch(cur, """
            SELECT i.item_name, SUM(s.quantity_sold) AS units, SUM(s.quantity_sold * s.actual_price) AS invoiced
            FROM sales_ledger s JOIN inventory_items i ON i.item_id = s.item_id
            WHERE s.sale_date BETWEEN %s AND %s GROUP BY i.item_name ORDER BY 3 DESC LIMIT 5""", (start, end))]

        # Expenses
        data["expenses_by_category"] = [{"category": r["category"], "total": num(r["total"])} for r in fetch(cur, """
            SELECT COALESCE(c.category_name, 'Uncategorised') AS category, SUM(e.financial_amount) AS total
            FROM enterprise_ledger e LEFT JOIN general_expenditure_categories c ON c.category_id = e.category_id
            WHERE e.accounting_date BETWEEN %s AND %s AND e.entry_type = 'Outflow'
              AND COALESCE(e.source_reference_table, '') NOT IN ('commission', 'procurement')
            GROUP BY 1 ORDER BY 2 DESC LIMIT 6""", (start, end))]
        data["commissions_total"] = scalar(cur, """SELECT COALESCE(SUM(financial_amount), 0) FROM enterprise_ledger
            WHERE accounting_date BETWEEN %s AND %s AND entry_type = 'Outflow' AND source_reference_table = 'commission'""", (start, end))
        data["supplier_payments_total"] = scalar(cur, """SELECT COALESCE(SUM(pp.amount), 0) FROM purchase_payments pp
            JOIN purchases p ON p.purchase_id = pp.purchase_id WHERE pp.payment_date BETWEEN %s AND %s""", (start, end))

        # Truck variances by truck (positive units = missing, negative = surplus)
        trucks = fetch(cur, """
            SELECT vehicle_color, COUNT(*) AS loads,
                   SUM(CASE WHEN variance_units <> 0 THEN 1 ELSE 0 END) AS loads_with_variance,
                   SUM(variance_units) AS net_units,
                   SUM(CASE WHEN variance_units > 0 THEN variance_units * set_price ELSE 0 END) AS missing_value
            FROM (
                SELECT v.vehicle_color, l.set_price,
                       (l.opening_stock + l.added_items - l.physical_final_stock) - l.agent_registered_sold AS variance_units
                FROM daily_run_loading l
                JOIN fleet_daily_runs r ON l.run_id = r.run_id
                JOIN vehicles v ON v.vehicle_id = r.vehicle_id
                WHERE r.run_date BETWEEN %s AND %s
            ) t GROUP BY vehicle_color ORDER BY missing_value DESC""", (start, end))
        data["trucks"] = [{"truck": r["vehicle_color"], "loads": int(num(r["loads"])), "loads_with_variance": int(num(r["loads_with_variance"])),
                           "net_units_missing": num(r["net_units"]), "missing_value": num(r["missing_value"])} for r in trucks]

        # Debts
        data["debts"] = {
            "outstanding_total": scalar(cur, "SELECT COALESCE(SUM(amount_owed - amount_recovered), 0) FROM agent_debts WHERE status != 'Cleared'"),
            "new_debts_this_week": scalar(cur, "SELECT COALESCE(SUM(amount_owed), 0) FROM agent_debts WHERE debt_date BETWEEN %s AND %s", (start, end)),
            "top_debtors": [{"name": r["who"], "agent": r["agent_name"], "balance": num(r["balance"])} for r in fetch(cur, """
                SELECT COALESCE(d.client_name, 'n/a') AS who, a.agent_name, SUM(d.amount_owed - d.amount_recovered) AS balance
                FROM agent_debts d JOIN agents a ON a.agent_id = d.agent_id WHERE d.status != 'Cleared'
                GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5""")],
            "owed_to_suppliers": scalar(cur, """SELECT COALESCE(SUM(p.total_payout - COALESCE(pp.paid, 0)), 0) FROM purchases p
                LEFT JOIN (SELECT purchase_id, SUM(amount) AS paid FROM purchase_payments GROUP BY purchase_id) pp
                ON pp.purchase_id = p.purchase_id""")}
    return data


WEEKLY_SYSTEM_PROMPT = (
    "Act as the Financial Auditor for Nodikar Paints, a paint company in Uganda. Write the Director's weekly report as "
    "6 short bullet points covering: (1) sales and how much was collected versus given on credit, (2) this week compared "
    "with the previous week for sales, cash received and expenses, (3) expenses and commissions, (4) truck shortages by "
    "truck, (5) debts owed to the company and to suppliers, and (6) one clear recommended action for next week.\n"
    "Rules: use ONLY the numbers in the data given; never invent figures. You may calculate simple percentage changes "
    "from the numbers given. Write amounts as 'UGX 1,234,000'. Each bullet starts with '• ' and is one or two short "
    "sentences. For trucks, positive units are missing and negative are surplus; if there were no truck loads, say so. "
    "Plain text only (no markdown, no asterisks, no headings). Keep the whole reply under 1400 characters."
)


def pct_change(now, before):
    if not before:
        return "n/a" if not now else "new"
    return f"{(now - before) / before * 100:+.0f}%"


def weekly_fallback_summary(d):
    """Plain weekly summary built without AI."""
    t, p = d["this_week"], d["previous_week"]
    credit = d["credit_issued_total"]
    lines = [
        f"• Sales: {ugx(t['invoiced'])} invoiced ({pct_change(t['invoiced'], p['invoiced'])} vs previous week); "
        f"{ugx(credit)} of it on credit.",
        f"• Cash received: {ugx(t['cash_received'])} ({pct_change(t['cash_received'], p['cash_received'])} vs previous week).",
        f"• Expenses: {ugx(t['total_expenses'])} ({pct_change(t['total_expenses'], p['total_expenses'])}), including "
        f"commissions {ugx(d['commissions_total'])} and supplier payments {ugx(d['supplier_payments_total'])}. "
        f"Net cash flow: {ugx(t['net_cash_flow'])}.",
    ]
    bad = [x for x in d["trucks"] if x["missing_value"] > 0]
    if bad:
        lines.append("• Fleet: " + "; ".join(f"{x['truck']} truck {x['net_units_missing']:g} units short ({ugx(x['missing_value'])})"
                                             for x in bad[:3]) + ".")
    elif d["trucks"]:
        lines.append("• Fleet: no missing stock on any truck this week.")
    else:
        lines.append("• Fleet: no truck loads were recorded this week.")
    db = d["debts"]
    lines.append(f"• Debts: {ugx(db['outstanding_total'])} owed to the company (new this week: {ugx(db['new_debts_this_week'])}); "
                 f"{ugx(db['owed_to_suppliers'])} owed to suppliers.")
    if d["top_agents"]:
        a = d["top_agents"][0]
        lines.append(f"• Top agent: {a['agent']} with {ugx(a['invoiced'])} in sales.")
    return "\n".join(lines)


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


def send_callmebot(message):
    """Free WhatsApp delivery through the CallMeBot service (personal use)."""
    apikey = env("CALLMEBOT_APIKEY", required=True)
    recipients = [n.strip() for n in env("WHATSAPP_TO", required=True).split(",") if n.strip()]
    failed = False
    for to in recipients:
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": "+" + to.lstrip("+"), "text": message, "apikey": apikey}, timeout=60)
        reply = " ".join(r.text.split())[:200]
        if r.ok and "error" not in reply.lower():
            print(f"Sent to {to}: {reply}")
        else:
            failed = True
            print(f"CallMeBot error for {to}: {r.status_code} {reply}", file=sys.stderr)
    if failed:
        sys.exit(1)


def send_telegram(message):
    """Free delivery through a Telegram bot."""
    token = env("TELEGRAM_BOT_TOKEN", required=True)
    chats = [c.strip() for c in env("TELEGRAM_CHAT_ID", required=True).split(",") if c.strip()]
    failed = False
    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": message[:4000]}, timeout=30)
        if r.ok and r.json().get("ok"):
            print(f"Sent to Telegram chat {chat}")
        else:
            failed = True
            print(f"Telegram error for chat {chat}: {r.status_code} {r.text[:300]}", file=sys.stderr)
    if failed:
        sys.exit(1)


def send_message(message):
    if env("TELEGRAM_BOT_TOKEN"):
        return send_telegram(message)
    return send_whatsapp(message)


def send_whatsapp(message):
    if env("CALLMEBOT_APIKEY"):
        return send_callmebot(message)
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
    weekly_mode = (env("REPORT_TYPE", "daily") or "daily").lower() == "weekly"
    today = dt.datetime.now(TZ).date()
    if weekly_mode:
        end = dt.date.fromisoformat(env("REPORT_DATE")) if env("REPORT_DATE") else today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=6)
        title = f"Nodikar Paints — Weekly Report ({start:%d %b} – {end:%d %b %Y})"
        collect, prompt, fallback = (lambda c: collect_weekly(c, start, end)), WEEKLY_SYSTEM_PROMPT, weekly_fallback_summary
    else:
        day = dt.date.fromisoformat(env("REPORT_DATE")) if env("REPORT_DATE") else today
        title = f"Nodikar Paints — Daily Audit ({day:%d %b %Y})"
        collect, prompt, fallback = (lambda c: collect_data(c, day)), SYSTEM_PROMPT, fallback_summary

    conn = psycopg2.connect(env("NEON_DATABASE_URL", required=True), connect_timeout=30)
    conn.set_session(readonly=True)  # this script can never change your data
    try:
        data = collect(conn)
    finally:
        conn.close()

    api_key = env("ANTHROPIC_API_KEY")
    if api_key:
        try:
            body = ask_ai(data, api_key, prompt)
            if not body:
                raise ValueError("empty AI reply")
        except Exception as e:  # still deliver the numbers if the AI is unavailable
            print(f"AI step failed ({e}); using plain summary.", file=sys.stderr)
            body = fallback(data)
    else:
        body = fallback(data)  # free mode: no AI service needed

    message = f"{title}\n{body}"
    if env("DRY_RUN") == "1":
        print("---- DRY RUN: message not sent ----")
        print(message)
        print("---- data used ----")
        print(json.dumps(data, indent=1))
    else:
        send_message(message)


if __name__ == "__main__":
    main()
