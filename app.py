import streamlit as st
import psycopg2
import pandas as pd
from datetime import date
import os
import hashlib
import random
import ast
import operator
import json
import streamlit.components.v1 as components
from contextlib import contextmanager

# =============================================================================
# 1. PAGE CONFIG & CUSTOM DESIGN
# =============================================================================
st.set_page_config(page_title="Nodikar Paints ERP", page_icon="favicon.png" if os.path.exists("favicon.png") else "🎨", layout="wide")

st.markdown("""
    <style>
    .stApp { background-color: #1e2229; color: #e2e8f0; }
    h1, h2, h3, h4, h5, h6, p, label { color: #f8fafc !important; }
    [data-testid="stSidebar"] { background-color: #0f172a; color: white; }
    .stTextInput>div>div>input, .stSelectbox>div>div>div, .stNumberInput>div>div>input, .stDateInput>div>div>input { background-color: #334155 !important; color: white !important; border: 1px solid #475569 !important; }
    .stButton>button { background-color: #f59e0b !important; color: #0f172a !important; font-weight: bold; border-radius: 6px; border: none; transition: all 0.3s ease; width: 100%; }
    .stButton>button:hover { background-color: #d97706 !important; color: white !important; }
    .stFormSubmitButton>button { background-color: #3b82f6 !important; color: white !important; font-weight: bold; width: 100%; }
    .stFormSubmitButton>button:hover { background-color: #2563eb !important; }
    div[data-testid="metric-container"] { background-color: #1e293b; border-left: 5px solid #3b82f6; padding: 15px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
    .stDataFrame { background-color: #1e293b; }
    .stTabs [data-baseweb="tab-list"] { gap: 5px; }
    .stTabs [data-baseweb="tab"] { background-color: #334155; color: #e2e8f0; border-radius: 4px 4px 0 0; }
    .stTabs [aria-selected="true"] { background-color: #f59e0b !important; color: #0f172a !important; font-weight: bold; }
    .login-container { display: flex; flex-direction: column; align-items: center; justify-content: center; margin-top: 30px; }
    </style>
""", unsafe_allow_html=True)

# =============================================================================
# 2. DB HELPERS
# =============================================================================
def get_secret_db_url():
    """Neon connection URL from Streamlit secrets ([postgres] url = "...") when deployed; None when running locally."""
    try: return st.secrets["postgres"]["url"]
    except Exception: return None

def can_write():
    """Only admins and editors may add, edit or delete. Everyone else is view-only."""
    return st.session_state.get("role") in ("admin", "editor")

@contextmanager
def system_writes():
    """Lets the app's own housekeeping (table setup, back-fills) write even when a view-only user is logged in."""
    st.session_state["_system"] = True
    try: yield
    finally: st.session_state["_system"] = False

def get_connection():
    url = get_secret_db_url()
    if url: conn = psycopg2.connect(url)
    else:
        # Local fallback. The password is read from the DB_PASSWORD environment variable, not stored in the code.
        conn = psycopg2.connect(host="localhost", port="5432", database="enterprise_blueprint", user="postgres", password=os.environ.get("DB_PASSWORD", ""))
    # View-only users get a read-only database session: the database itself refuses any write they attempt.
    if st.session_state.get("logged_in") and not st.session_state.get("_system") and not can_write():
        conn.set_session(readonly=True)
    return conn

VIEW_ONLY_MSG = "You have view-only access. Ask the administrator for permission to add or edit records."

def report_invalid_numbers():
    """Show the error now, and again after any st.rerun() (the script end clears it if no rerun happened)."""
    msg = "Nothing was saved: some number fields are empty or invalid. Enter numbers or calculations like 2500*4 and try again."
    st.error(msg)
    pending = st.session_state.setdefault("flash_transient", [])
    if msg not in pending: pending.append(msg)

def has_invalid_number(params):
    """True if any parameter is NaN (a number field that was blank/invalid)."""
    return params is not None and any(isinstance(v, float) and v != v for v in params)

def run_query(query, params=None, fetch=False):
    if has_invalid_number(params):
        report_invalid_numbers()
        return pd.DataFrame() if fetch else "ERROR"
    conn = get_connection()
    try:
        if fetch: return pd.read_sql_query(query, conn, params=params)
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()
        return "OK"
    except psycopg2.errors.UniqueViolation: conn.rollback(); return "DUPLICATE"
    except psycopg2.errors.ReadOnlySqlTransaction: conn.rollback(); st.error(VIEW_ONLY_MSG); return pd.DataFrame() if fetch else "ERROR"
    except psycopg2.errors.ForeignKeyViolation: conn.rollback(); return "IN_USE"
    except psycopg2.errors.UndefinedTable as e: conn.rollback(); st.error(f"⚠️ Missing Table: {e}"); return pd.DataFrame() 
    except Exception as e: conn.rollback(); st.error(f"DB Error: {e}"); return pd.DataFrame() if fetch else "ERROR"
    finally: conn.close()

def hash_password(password): return hashlib.sha256(password.encode()).hexdigest()
def generate_debt_id(name): return f"{name.replace(' ', '')}-{random.randint(1000, 9999)}"

def insert_returning_id(query, params):
    """Run an INSERT ... RETURNING <id> and return the new id (or None on failure)."""
    if has_invalid_number(params):
        report_invalid_numbers()
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            new_id = cur.fetchone()[0]
            conn.commit()
        return new_id
    except psycopg2.errors.ReadOnlySqlTransaction:
        conn.rollback(); st.error(VIEW_ONLY_MSG); return None
    except Exception as e:
        conn.rollback(); st.error(f"DB Error: {e}"); return None
    finally: conn.close()

def backfill_commission_expenses():
    """Keep commission expenses in step with the commissions table (runs at start of each session):
    removes expenses for commissions that no longer exist / belong to deleted sales / are zero, removes duplicates,
    refreshes amounts and dates, and logs any commission that has no expense yet."""
    cat = get_commission_category_id()
    if cat is None: return
    valid = """(c.total_commission > 0 AND (c.sale_id IS NULL OR EXISTS (SELECT 1 FROM sales_ledger s WHERE s.sale_id = c.sale_id)))"""
    # 1. drop expenses whose commission is gone, orphaned or zero
    run_query(f"""DELETE FROM enterprise_ledger e WHERE e.source_reference_table = 'commission' AND NOT EXISTS
                 (SELECT 1 FROM agent_commissions c WHERE e.bookkeeper_notes LIKE '[COMM:' || c.commission_id || ']%%' AND {valid})""", ())
    # 2. drop duplicate expense rows for the same commission (keep the oldest)
    run_query(r"""DELETE FROM enterprise_ledger WHERE ledger_id IN (
                    SELECT ledger_id FROM (SELECT ledger_id, ROW_NUMBER() OVER (
                        PARTITION BY substring(bookkeeper_notes from '^\[COMM:[0-9]+\]') ORDER BY ledger_id) AS rn
                      FROM enterprise_ledger WHERE source_reference_table = 'commission') t WHERE rn > 1)""")
    # 3. refresh amounts/dates so they always equal the commission
    run_query(f"""UPDATE enterprise_ledger e SET financial_amount = c.total_commission, accounting_date = c.commission_date
                 FROM agent_commissions c WHERE e.source_reference_table = 'commission'
                 AND e.bookkeeper_notes LIKE '[COMM:' || c.commission_id || ']%%' AND {valid}""", ())
    # 4. log commissions that have no expense yet
    run_query(f"""INSERT INTO enterprise_ledger (accounting_date, entry_type, category_id, source_reference_table, financial_amount, bookkeeper_notes)
                 SELECT c.commission_date, 'Outflow', %s, 'commission', c.total_commission, '[COMM:' || c.commission_id || '] Agent commission'
                 FROM agent_commissions c WHERE {valid}
                   AND NOT EXISTS (SELECT 1 FROM enterprise_ledger e WHERE e.source_reference_table = 'commission'
                                   AND e.bookkeeper_notes LIKE '[COMM:' || c.commission_id || ']%%')""", (cat,))

def log_debt_cash(debt_id, reason_text, amount, pay_date):
    """Cash received (or corrected) on a debt becomes revenue immediately.
    Sale-linked debts post to that sale's payments; other debts post to debt_recoveries."""
    s_id = None
    if "Sale ID" in reason_text:
        try: s_id = int(reason_text.split("Sale ID ")[1].split(")")[0])
        except (IndexError, ValueError): s_id = None
    if s_id is not None:
        run_query("UPDATE sales_ledger SET amount_paid = COALESCE(amount_paid, 0) + %s WHERE sale_id = %s", (amount, s_id))
        run_query("INSERT INTO sale_payments (sale_id, payment_date, amount) VALUES (%s, %s, %s)", (s_id, pay_date, amount))
        run_query("UPDATE sales_ledger SET payment_status = 'Paid' WHERE sale_id = %s AND amount_paid >= (quantity_sold * actual_price)", (s_id,))
        run_query("UPDATE sales_ledger SET payment_status = 'Partial' WHERE sale_id = %s AND amount_paid > 0 AND amount_paid < (quantity_sold * actual_price)", (s_id,))
    else:
        run_query("INSERT INTO debt_recoveries (debt_id, recovery_date, amount) VALUES (%s, %s, %s)", (str(debt_id), pay_date, amount))

_CALC_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}

def safe_calc(text):
    """Turn typed text into a number. Accepts plain numbers or simple maths like '2500*4+1000'.
    Empty -> 0.0, invalid -> None. Only digits and + - * / ( ) are allowed."""
    t = (text or "").strip().replace(",", "").replace("x", "*").replace("X", "*").replace("×", "*")
    if not t: return 0.0
    def ev(n):
        if isinstance(n, ast.Expression): return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool): return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in _CALC_OPS: return _CALC_OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _CALC_OPS: return _CALC_OPS[type(n.op)](ev(n.operand))
        raise ValueError("unsupported")
    try: return ev(ast.parse(t, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError): return None

def calc_input(label, value=None, min_value=None, key=None, step=None, **_ignored):
    """Drop-in replacement for st.number_input that starts empty and accepts calculations (e.g. 2500*4+1000).
    Returns a float. Blank -> 0.0 (or NaN if a minimum above 0 is required); invalid/too small -> NaN,
    and database writes refuse NaN, so nothing wrong can be saved."""
    default = "" if value is None else f"{float(value):.2f}".rstrip("0").rstrip(".")
    txt = st.text_input(label, value=default, key=key, placeholder="number or calculation, e.g. 2500*4")
    if not (txt or "").strip():
        return float("nan") if (min_value is not None and min_value > 0) else 0.0
    v = safe_calc(txt)
    if v is None:
        st.markdown(":red[⚠ Not a valid number or calculation (use digits and + - * / ( ) only).]")
        return float("nan")
    if min_value is not None and v < min_value:
        st.markdown(f":red[⚠ Must be at least {min_value:g}.]")
        return float("nan")
    return v

_ARROW_NAV_JS = """
document.addEventListener('keydown', function (e) {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  var el = e.target;
  if (!el || el.tagName !== 'INPUT' || el.type !== 'text') return;
  if (el.closest('[data-baseweb="select"]') || el.closest('[data-testid="stDateInput"]')) return;
  var form = el.closest('[data-testid="stForm"]');
  if (!form) return;
  var fields = Array.prototype.filter.call(form.querySelectorAll('input'), function (i) {
    return !i.disabled && i.type !== 'hidden' && i.offsetParent !== null;
  });
  var idx = fields.indexOf(el);
  if (idx < 0) return;
  var next = fields[idx + (e.key === 'ArrowDown' ? 1 : -1)];
  if (next) { e.preventDefault(); next.focus(); if (next.select) next.select(); }
}, true);
"""

def enable_arrow_navigation():
    """Up/Down arrows move between the fields of a form (installed once into the main page)."""
    components.html(
        "<script>(function(){var d=window.parent.document;if(d.getElementById('arrow-nav-script'))return;"
        "var s=d.createElement('script');s.id='arrow-nav-script';s.textContent=" + json.dumps(_ARROW_NAV_JS) + ";d.head.appendChild(s);})();</script>",
        height=0)

def flash(kind, msg):
    """Queue a message that survives st.rerun()."""
    st.session_state.setdefault("flash", []).append((kind, msg))

def show_flash():
    for kind, msg in st.session_state.pop("flash", []): getattr(st, kind)(msg)

def get_commission_category_id():
    """Expense category used for commissions ('Agent Commissions'); created if missing, else falls back to the first category."""
    q = "SELECT category_id FROM general_expenditure_categories WHERE category_name = %s"
    df = run_query(q, ("Agent Commissions",), fetch=True)
    if not (isinstance(df, pd.DataFrame) and not df.empty):
        run_query("INSERT INTO general_expenditure_categories (category_name) VALUES (%s)", ("Agent Commissions",))
        df = run_query(q, ("Agent Commissions",), fetch=True)
    if not (isinstance(df, pd.DataFrame) and not df.empty):
        df = run_query("SELECT category_id FROM general_expenditure_categories ORDER BY category_id LIMIT 1", fetch=True)
    return int(df.iloc[0]["category_id"]) if isinstance(df, pd.DataFrame) and not df.empty else None

def log_commission_expense(commission_id, amount, comm_date, note):
    """Every commission is automatically logged as an expense (Outflow) in the enterprise ledger."""
    if not commission_id or amount <= 0: return
    cat = get_commission_category_id()
    if cat is None:
        st.warning("Commission saved, but no expense category exists to log it under. Add a category in Master Setup.")
        return
    run_query("INSERT INTO enterprise_ledger (accounting_date, entry_type, category_id, source_reference_table, financial_amount, bookkeeper_notes) VALUES (%s, 'Outflow', %s, 'commission', %s, %s)",
              (comm_date, cat, amount, f"[COMM:{commission_id}] {note}"))

def scalar(df, col):
    """Safely read a single SUM() value from a query result."""
    if isinstance(df, pd.DataFrame) and not df.empty and pd.notnull(df.iloc[0][col]): return float(df.iloc[0][col])
    return 0.0

def ensure_payment_tables():
    """
    Payment tables record WHEN money actually moved (cash received from clients / cash paid to suppliers).
    The Reports tab reads from these, so credit sales/purchases only count for the amount actually settled.
    Existing paid amounts are back-filled once (dated on the sale/purchase date).
    """
    # Every sale now remembers its client. Pick up any client already stored on the sale record (client_id / client text column).
    run_query("ALTER TABLE sales_ledger ADD COLUMN IF NOT EXISTS client_name TEXT")
    cols_df = run_query("SELECT column_name FROM information_schema.columns WHERE table_name = 'sales_ledger'", fetch=True)
    if isinstance(cols_df, pd.DataFrame) and not cols_df.empty:
        for col in cols_df["column_name"].tolist():
            if col.lower() == "client_name" or "client" not in col.lower(): continue
            if col.lower().endswith("_id"):
                run_query(f'UPDATE sales_ledger s SET client_name = cl.client_name FROM clients cl WHERE s.client_name IS NULL AND s."{col}" = cl.client_id')
            else:
                run_query(f'UPDATE sales_ledger SET client_name = "{col}"::text WHERE client_name IS NULL AND "{col}" IS NOT NULL')
    # Sales that already have a client-named debt (e.g. 'Mukasa-4821'): recover the client from the debt ID
    run_query("""UPDATE sales_ledger s SET client_name = cl.client_name FROM agent_debts d, clients cl
                 WHERE s.client_name IS NULL AND d.reason LIKE '%(Sale ID ' || s.sale_id || ')%'
                   AND REPLACE(cl.client_name, ' ', '') = regexp_replace(d.client_name, '-[0-9]+$', '')""")
    run_query("CREATE TABLE IF NOT EXISTS sale_payments (payment_id SERIAL PRIMARY KEY, sale_id INTEGER NOT NULL, payment_date DATE NOT NULL, amount NUMERIC(14,2) NOT NULL)")
    run_query("CREATE TABLE IF NOT EXISTS purchase_payments (payment_id SERIAL PRIMARY KEY, purchase_id INTEGER NOT NULL, payment_date DATE NOT NULL, amount NUMERIC(14,2) NOT NULL)")
    # Reconcile: any paid amount on a sale not yet backed by payment records (e.g. recoveries made before this feature) is added
    run_query("""INSERT INTO sale_payments (sale_id, payment_date, amount)
                 SELECT s.sale_id, s.sale_date, s.amount_paid - COALESCE(sp.paid, 0) FROM sales_ledger s
                 LEFT JOIN (SELECT sale_id, SUM(amount) AS paid FROM sale_payments GROUP BY sale_id) sp ON sp.sale_id = s.sale_id
                 WHERE COALESCE(s.amount_paid, 0) - COALESCE(sp.paid, 0) > 0""")
    run_query("CREATE TABLE IF NOT EXISTS debt_recoveries (recovery_id SERIAL PRIMARY KEY, debt_id TEXT NOT NULL, recovery_date DATE NOT NULL, amount NUMERIC(14,2) NOT NULL)")
    run_query("""INSERT INTO debt_recoveries (debt_id, recovery_date, amount)
                 SELECT d.debt_id::text, d.debt_date, d.amount_recovered - COALESCE(dr.rec, 0) FROM agent_debts d
                 LEFT JOIN (SELECT debt_id, SUM(amount) AS rec FROM debt_recoveries GROUP BY debt_id) dr ON dr.debt_id = d.debt_id::text
                 WHERE COALESCE(d.reason, '') NOT LIKE '%Sale ID%'
                   AND COALESCE(d.amount_recovered, 0) - COALESCE(dr.rec, 0) > 0""")
    # One-off: unpaid sale balances that never got a debt record (e.g. saved without a client) are added to Debts
    run_query("CREATE TABLE IF NOT EXISTS app_flags (flag TEXT PRIMARY KEY)")
    flag_df = run_query("SELECT 1 FROM app_flags WHERE flag = 'unpaid_sales_to_debts'", fetch=True)
    if isinstance(flag_df, pd.DataFrame) and flag_df.empty:
        run_query("""INSERT INTO agent_debts (agent_id, debt_date, reason, amount_owed, client_name)
                     SELECT s.agent_id, s.sale_date, 'Unpaid bal for ' || i.item_name || ' (Sale ID ' || s.sale_id || ')',
                            s.quantity_sold * s.actual_price - COALESCE(s.amount_paid, 0), COALESCE(REPLACE(s.client_name, ' ', '') || '-' || s.sale_id, 'UnknownClient-' || s.sale_id)
                     FROM sales_ledger s JOIN inventory_items i ON i.item_id = s.item_id
                     WHERE s.quantity_sold * s.actual_price - COALESCE(s.amount_paid, 0) > 0
                       AND NOT EXISTS (SELECT 1 FROM agent_debts d WHERE d.reason LIKE '%(Sale ID ' || s.sale_id || ')%')""")
        run_query("INSERT INTO app_flags (flag) VALUES ('unpaid_sales_to_debts') ON CONFLICT DO NOTHING")
    run_query("""UPDATE agent_debts d SET client_name = REPLACE(s.client_name, ' ', '') || '-' || s.sale_id
                 FROM sales_ledger s WHERE d.client_name LIKE 'UnknownClient-%' AND s.client_name IS NOT NULL
                   AND d.reason LIKE '%(Sale ID ' || s.sale_id || ')%'""")
    run_query("""INSERT INTO purchase_payments (purchase_id, payment_date, amount)
                 SELECT p.purchase_id, p.purchase_date, p.total_payout FROM purchases p
                 WHERE p.status = 'Cleared' AND COALESCE(p.total_payout, 0) > 0
                   AND NOT EXISTS (SELECT 1 FROM purchase_payments pp WHERE pp.purchase_id = p.purchase_id)""")

def ensure_user_roles():
    """Adds a role to every user: 'viewer' (view only), 'editor' (can add/edit/delete) or 'admin' (editor + manages users).
    New registrations are viewers until an admin changes them. The admin is the username in secrets ([app] admin_username);
    if no admin exists yet, the earliest registered user becomes admin."""
    run_query("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'viewer'")
    try: admin_user = st.secrets["app"]["admin_username"]
    except Exception: admin_user = None
    if admin_user: run_query("UPDATE users SET role = 'admin' WHERE username = %s", (admin_user,))
    has_admin = run_query("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1", fetch=True)
    if isinstance(has_admin, pd.DataFrame) and has_admin.empty:
        run_query("UPDATE users SET role = 'admin' WHERE user_id = (SELECT MIN(user_id) FROM users)")

if "roles_ready" not in st.session_state:
    with system_writes(): ensure_user_roles()
    st.session_state.roles_ready = True

# =============================================================================
# 3. LOGIN SYSTEM
# =============================================================================
if "logged_in" not in st.session_state: st.session_state.logged_in = False

if not st.session_state.logged_in:
    st.markdown("<div class='login-container'>", unsafe_allow_html=True)
    if os.path.exists("logo.png"):
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2: st.image("logo.png", use_container_width=True)
    else: st.markdown("<h1 style='text-align: center; color: #f59e0b;'>🎨 Nodikar Paints</h1>", unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        tab_login, tab_register = st.tabs(["🔐 Login", "📝 Register"])
        with tab_login:
            with st.form("login_form"):
                login_user = st.text_input("Username"); login_pass = st.text_input("Password", type="password")
                if st.form_submit_button("Sign In"):
                    result = run_query("SELECT user_id, full_name, role FROM users WHERE username = %s AND password_hash = %s", (login_user, hash_password(login_pass)), fetch=True)
                    if isinstance(result, pd.DataFrame) and not result.empty:
                        st.session_state.logged_in = True; st.session_state.user_name = result.iloc[0]['full_name']
                        st.session_state.user_id = int(result.iloc[0]['user_id']); st.session_state.username = login_user
                        st.session_state.role = result.iloc[0]['role'] or "viewer"; st.rerun()
                    else: st.error("Invalid username or password.")
        with tab_register:
            with st.form("register_form"):
                reg_fullname = st.text_input("Full Name"); reg_user = st.text_input("Choose Username")
                reg_pass = st.text_input("Choose Password", type="password"); reg_confirm = st.text_input("Confirm Password", type="password")
                if st.form_submit_button("Create Account"):
                    if not reg_fullname or not reg_user or not reg_pass: st.warning("Fill all fields.")
                    elif reg_pass != reg_confirm: st.error("Passwords do not match.")
                    else:
                        res = run_query("INSERT INTO users (username, password_hash, full_name) VALUES (%s, %s, %s)", (reg_user, hash_password(reg_pass), reg_fullname))
                        if res == "DUPLICATE": st.error("Username taken.")
                        elif res == "OK": st.success("Account created! You can log in now. New accounts are view-only until the administrator grants edit permission.")
    st.markdown("</div>", unsafe_allow_html=True)
    st.stop() 

# =============================================================================
# 4. MAIN APP
# =============================================================================
# Re-read this user's role on every run, so the administrator's changes take effect immediately
_role_df = run_query("SELECT role FROM users WHERE user_id = %s", (int(st.session_state.user_id),), fetch=True) if st.session_state.get("user_id") else pd.DataFrame()
if not isinstance(_role_df, pd.DataFrame) or _role_df.empty:
    st.session_state.logged_in = False; st.rerun()   # account removed, or session from before roles existed: sign in again
st.session_state.role = _role_df.iloc[0]["role"] or "viewer"
is_admin = st.session_state.role == "admin"

head_col1, head_col2, head_col3 = st.columns([1, 7, 2])
with head_col1:
    if os.path.exists("logo.png"): st.image("logo.png", use_container_width=True)
with head_col2: st.title("Nodikar Paints: Enterprise Portal")
with head_col3:
    st.markdown(f"**Welcome, {st.session_state.get('user_name', 'User')}** ({st.session_state.role})")
    if st.button("Logout"):
        for _k in ("logged_in", "role", "user_id", "username"): st.session_state.pop(_k, None)
        st.rerun()
st.markdown("<hr style='border: 1px solid #334155;'>", unsafe_allow_html=True)

# View-only users: every save / delete / edit button is greyed out (and the database refuses writes as a second lock)
if not getattr(st.form_submit_button, "_guarded", False):
    _orig_form_submit, _orig_button = st.form_submit_button, st.button
    def _guarded_form_submit(*a, **k):
        if st.session_state.get("logged_in") and not can_write(): k["disabled"] = True
        return _orig_form_submit(*a, **k)
    def _guarded_button(label, *a, **k):
        if st.session_state.get("logged_in") and not can_write() and label != "Logout": k["disabled"] = True
        return _orig_button(label, *a, **k)
    _guarded_form_submit._guarded = True
    st.form_submit_button = _guarded_form_submit; st.button = _guarded_button
if not can_write(): st.info("👁️ View-only access. Ask the administrator for permission to add or edit records.")

enable_arrow_navigation()
for _m in st.session_state.pop("flash_transient", []): st.error(_m)

if "payment_tables_ready" not in st.session_state:
    with system_writes(): ensure_payment_tables(); backfill_commission_expenses()
    st.session_state.payment_tables_ready = True

try:
    agents_df = run_query("SELECT agent_id, agent_name FROM agents ORDER BY agent_name", fetch=True)
    items_df = run_query("SELECT item_id, item_name, item_type FROM inventory_items ORDER BY item_name", fetch=True)
    vehicles_df = run_query("SELECT vehicle_id, vehicle_color FROM vehicles ORDER BY vehicle_color", fetch=True)
    suppliers_df = run_query("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name", fetch=True)
    clients_df = run_query("SELECT client_id, client_name FROM clients ORDER BY client_name", fetch=True)
    categories_df = run_query("SELECT category_id, category_name FROM general_expenditure_categories ORDER BY category_name", fetch=True)
except Exception as e: st.error(f"DB Error: {e}"); st.stop()

agent_map = dict(zip(agents_df["agent_name"], agents_df["agent_id"])) if not agents_df.empty else {}
item_map = dict(zip(items_df["item_name"], items_df["item_id"])) if not items_df.empty else {}
item_type_map = dict(zip(items_df["item_name"], items_df["item_type"])) if not items_df.empty else {}
vehicle_map = dict(zip(vehicles_df["vehicle_color"], vehicles_df["vehicle_id"])) if not vehicles_df.empty else {}
supplier_map = dict(zip(suppliers_df["supplier_name"], suppliers_df["supplier_id"])) if not suppliers_df.empty else {}
client_map = dict(zip(clients_df["client_name"], clients_df["client_id"])) if not clients_df.empty else {}
category_map = dict(zip(categories_df["category_name"], categories_df["category_id"])) if not categories_df.empty else {}

_tab_names = ["1. Sales", "2. Fleet", "3. Procurement", "4. Debts", "5. Commissions", "6. Expenses", "7. Master Setup", "8. Reports"]
if is_admin: _tab_names.append("9. User Access")
_tabs = st.tabs(_tab_names)
tab_sales, tab_fleet, tab_procure, tab_debts, tab_comm, tab_expenses, tab_master, tab_reports = _tabs[:8]
tab_users = _tabs[8] if is_admin else None

# =============================================================================
# TAB 1: SALES
# =============================================================================
with tab_sales:
    st.subheader("Record Sale")
    show_flash()
    with st.form("sales_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            sale_date = st.date_input("Sale Date", value=date.today())
            channel = st.selectbox("Sales Channel", ["Factory Direct", "Route Fleet"])
            selected_item = st.selectbox("Product", list(item_map.keys()) if item_map else ["No Items"])
            set_price_txt = st.text_input("Company Set Price (UGX)", placeholder="e.g. 25000 or 5000*5")
        with col2:
            selected_agent = st.selectbox("Agent", list(agent_map.keys()) if agent_map else ["No Agents"])
            qty_txt = st.text_input("Quantity Sold", placeholder="e.g. 10 or 6+4")
            actual_price_txt = st.text_input("Agent Actual Selling Price (UGX)", placeholder="e.g. 26000")
            
        sell_on_credit = st.checkbox("Sell on Credit? (Client Debt)")
        selected_client = st.selectbox("Select Client", ["None"] + list(client_map.keys()))
        amount_paid_txt = st.text_input("Amount Paid Now (UGX)", placeholder="e.g. 50000 or 20000+30000")
        st.caption("Number fields accept calculations (+ - * / and brackets). Use the Up/Down arrow keys to move between fields.")

        if st.form_submit_button("Save Sale"):
            set_price = safe_calc(set_price_txt); qty_sold = safe_calc(qty_txt)
            actual_price = safe_calc(actual_price_txt); amount_paid = safe_calc(amount_paid_txt)
            if None in (set_price, qty_sold, actual_price, amount_paid):
                st.error("One of the number fields is not a valid number or calculation. Use digits and + - * / ( ) only.")
            elif qty_sold <= 0:
                st.error("Quantity Sold must be greater than zero.")
            elif min(set_price, actual_price, amount_paid) < 0:
                st.error("Prices and amounts cannot be negative.")
            elif item_map and agent_map:
                total_amount = qty_sold * actual_price
                # Not a credit sale and no client: a blank "Amount Paid" means the sale was paid in full
                if not amount_paid_txt.strip() and not sell_on_credit and selected_client == "None":
                    amount_paid = total_amount
                balance = total_amount - amount_paid
                if amount_paid > total_amount:
                    st.error("Amount paid cannot be more than the sale total.")
                elif balance > 0 and sell_on_credit and selected_client == "None":
                    st.error(f"This is a credit sale with an unpaid balance of UGX {balance:,.2f}. Select the client who owes it.")
                else:
                    status = "Paid" if balance <= 0 else ("Partial" if amount_paid > 0 else "Unpaid")

                    new_sale_id = insert_returning_id("INSERT INTO sales_ledger (sale_date, sales_channel, item_id, agent_id, quantity_sold, unit_rate, actual_price, payment_status, amount_paid, client_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING sale_id",
                              (sale_date, channel, int(item_map[selected_item]), int(agent_map[selected_agent]), qty_sold, set_price, actual_price, status, amount_paid, None if selected_client == "None" else selected_client))

                    if new_sale_id:
                        # Cash received today becomes revenue (dated on the sale date)
                        if amount_paid > 0:
                            run_query("INSERT INTO sale_payments (sale_id, payment_date, amount) VALUES (%s, %s, %s)", (new_sale_id, sale_date, amount_paid))

                        diff = actual_price - set_price
                        if diff != 0:
                            comm_val = max(diff * qty_sold, 0.0)  # negative -> "No commission" (stored as 0)
                            new_comm_id = insert_returning_id("INSERT INTO agent_commissions (sale_id, agent_id, item_id, set_price, actual_price, quantity, total_commission, commission_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING commission_id",
                                      (new_sale_id, int(agent_map[selected_agent]), int(item_map[selected_item]), set_price, actual_price, qty_sold, comm_val, sale_date))
                            log_commission_expense(new_comm_id, comm_val, sale_date, f"Commission: {selected_agent}, {selected_item} (Sale ID {new_sale_id})")

                        if balance > 0:
                            # Unpaid balance is tracked in Debts: against the client if one was chosen, otherwise against the agent
                            who = selected_client if selected_client != "None" else selected_agent
                            debt_id = generate_debt_id(who)
                            run_query("INSERT INTO agent_debts (agent_id, debt_date, reason, amount_owed, client_name) VALUES (%s, %s, %s, %s, %s)",
                                      (int(agent_map[selected_agent]), sale_date, f"Unpaid bal for {selected_item} (Sale ID {new_sale_id})", balance, debt_id))
                            flash("warning", f"{'Client' if selected_client != 'None' else 'Agent'} debt logged! ID: {debt_id} | Balance: UGX {balance:,.2f}")

                        flash("success", f"Sale saved! Total UGX {total_amount:,.2f}"); st.rerun()

    st.divider()
    st.subheader("Manage Saved Sales (Filter & Subtotals)")
    colF1, colF2, colF3 = st.columns(3)
    with colF1: s_start = st.date_input("Start Date", value=None, key="s_start")
    with colF2: s_end = st.date_input("End Date", value=None, key="s_end")
    with colF3: s_agent = st.selectbox("Filter by Agent", ["All"] + list(agent_map.keys()), key="s_agent_filter")
    
    q_s = "SELECT s.sale_id, s.sale_date, s.sales_channel, i.item_name, a.agent_name, s.client_name, s.quantity_sold, s.unit_rate as set_price, s.actual_price, s.amount_paid, s.payment_status FROM sales_ledger s JOIN inventory_items i ON s.item_id = i.item_id JOIN agents a ON s.agent_id = a.agent_id WHERE 1=1"
    p_s = []
    if s_start: q_s += " AND s.sale_date >= %s"; p_s.append(s_start)
    if s_end: q_s += " AND s.sale_date <= %s"; p_s.append(s_end)
    if s_agent != "All": q_s += " AND a.agent_name = %s"; p_s.append(s_agent)
    q_s += " ORDER BY s.sale_id DESC LIMIT 100"
    
    sales_df = run_query(q_s, tuple(p_s), fetch=True)
    
    if isinstance(sales_df, pd.DataFrame) and not sales_df.empty:
        sales_df['total_amount'] = sales_df['quantity_sold'] * sales_df['actual_price']
        raw_comm = (sales_df['actual_price'] - sales_df['set_price']) * sales_df['quantity_sold']
        comm_total = raw_comm.clip(lower=0).sum()
        sales_df['commission'] = ["No commission" if v < 0 else f"{v:,.2f}" for v in raw_comm]
        
        st.dataframe(sales_df, use_container_width=True)
        
        st.markdown(f"**Subtotals for Filtered View:**")
        st.markdown(f"- Total Quantity Sold: **{sales_df['quantity_sold'].sum():,.2f}**")
        st.markdown(f"- **Total Amount Sold (Invoice): UGX {sales_df['total_amount'].sum():,.2f}**")
        st.markdown(f"- **Total Cash Received: UGX {sales_df['amount_paid'].sum():,.2f}**")
        st.markdown(f"- **Total Commission: UGX {comm_total:,.2f}**")
        
        colA, colB = st.columns(2)
        with colA:
            del_sale = st.selectbox("Select Sale ID to Delete", sales_df["sale_id"].tolist(), key="del_sale")
            if st.button("🗑️ Delete Selected Sale"):
                run_query("DELETE FROM sale_payments WHERE sale_id=%s", (int(del_sale),))
                run_query("""DELETE FROM enterprise_ledger e WHERE e.source_reference_table = 'commission' AND EXISTS
                             (SELECT 1 FROM agent_commissions c WHERE c.sale_id = %s AND e.bookkeeper_notes LIKE '[COMM:' || c.commission_id || ']%%')""", (int(del_sale),))
                run_query("DELETE FROM agent_commissions WHERE sale_id=%s", (int(del_sale),))
                run_query("DELETE FROM sales_ledger WHERE sale_id=%s", (int(del_sale),)); st.rerun()
        with colB:
            edit_sale = st.selectbox("Select Sale ID to Edit", sales_df["sale_id"].tolist(), key="edit_sale_widget")
            if st.button("✏️ Edit Selected Sale"): st.session_state.edit_sale_id = int(edit_sale)

        if "edit_sale_id" in st.session_state:
            matches = sales_df[sales_df["sale_id"] == st.session_state.edit_sale_id]
            if not matches.empty:
                rec = matches.iloc[0]
                with st.form("edit_sale_form"):
                    st.markdown(f"### Editing Sale #{st.session_state.edit_sale_id}")
                    e_qty = calc_input("Quantity", value=float(rec["quantity_sold"]))
                    safe_act = float(rec["actual_price"]) if pd.notnull(rec["actual_price"]) else 0.0
                    e_act = calc_input("Actual Selling Price", value=safe_act)
                    
                    e_paid = calc_input("Amount Paid Now (UGX)", value=float(rec["amount_paid"]) if pd.notnull(rec["amount_paid"]) else 0.0, step=1000.0)
                    
                    if st.form_submit_button("Update Sale"):
                        total = e_qty * e_act
                        if e_paid > total:
                            st.error("Amount paid cannot be more than the sale total.")
                        else:
                            new_status = "Paid" if e_paid >= total else ("Partial" if e_paid > 0 else "Unpaid")
                            old_paid = float(rec["amount_paid"]) if pd.notnull(rec["amount_paid"]) else 0.0
                            
                            run_query("UPDATE sales_ledger SET quantity_sold=%s, actual_price=%s, amount_paid=%s, payment_status=%s WHERE sale_id=%s", 
                                      (e_qty, e_act, e_paid, new_status, st.session_state.edit_sale_id))
                            # Any change in cash received is logged as an adjustment dated today
                            if e_paid != old_paid:
                                run_query("INSERT INTO sale_payments (sale_id, payment_date, amount) VALUES (%s, %s, %s)",
                                          (st.session_state.edit_sale_id, date.today(), e_paid - old_paid))
                            del st.session_state.edit_sale_id; st.rerun()
            else:
                del st.session_state.edit_sale_id
                st.warning("Record no longer exists in this view.")

# =============================================================================
# TAB 2: FLEET LOADING
# =============================================================================
with tab_fleet:
    st.subheader("Morning Loading & Evening Reconciliation")
    with st.form("fleet_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            run_date = st.date_input("Route Date", value=date.today())
            selected_vehicle = st.selectbox("Truck", list(vehicle_map.keys()) if vehicle_map else ["No Trucks"])
            fleet_agent = st.selectbox("Driver/Agent", list(agent_map.keys()) if agent_map else ["No Agents"])
        with c2:
            fleet_item = st.selectbox("Item", list(item_map.keys()) if item_map else ["No Items"])
            opening_stock = calc_input("Opening Stock", step=1.0) 
            added_items = calc_input("Added Items (Loaded/Removed)", step=1.0) 
        with c3:
            final_stock = calc_input("Physical Final Stock", step=1.0)
            reg_sold = calc_input("Agent Registered Sold", step=1.0)
            set_price = calc_input("Company Set Price (UGX)", min_value=0.0, step=100.0)
        comments = st.text_input("Route Comments / Credit Notes")
        
        if st.form_submit_button("Save & Reconcile"):
            if has_invalid_number((opening_stock, added_items, final_stock, reg_sold, set_price)):
                report_invalid_numbers()
            elif item_map and agent_map and vehicle_map:
                custom_id = f"{run_date}-{selected_vehicle}-{fleet_agent}"
                conn = get_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO fleet_daily_runs (run_date, vehicle_id, agent_id) VALUES (%s, %s, %s) ON CONFLICT (run_date, vehicle_id) DO UPDATE SET agent_id = EXCLUDED.agent_id RETURNING run_id", (run_date, int(vehicle_map[selected_vehicle]), int(agent_map[fleet_agent])))
                        run_id = cur.fetchone()[0]
                        cur.execute("INSERT INTO daily_run_loading (run_id, item_id, opening_stock, added_items, physical_final_stock, agent_registered_sold, set_price, comments, custom_load_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", (run_id, int(item_map[fleet_item]), opening_stock, added_items, final_stock, reg_sold, set_price, comments, custom_id))
                        conn.commit()
                    expected_sold = (opening_stock + added_items) - final_stock
                    variance = expected_sold - reg_sold
                    if variance == 0: st.success(f"✅ Reconciled perfectly. ID: {custom_id}")
                    elif variance > 0: st.error(f"🚨 VARIANCE: {variance} units MISSING (Value: UGX {variance * set_price:,.2f})")
                    else: st.warning(f"⚠️ SURPLUS: {abs(variance)} units extra.")
                    st.rerun()
                except Exception as e:
                    conn.rollback(); st.error(VIEW_ONLY_MSG if isinstance(e, psycopg2.errors.ReadOnlySqlTransaction) else f"DB Error: {e}")
                finally: conn.close()

    st.divider()
    st.subheader("Manage Fleet Records (Filter & Subtotal)")
    colF1, colF2, colF3, colF4 = st.columns(4)
    with colF1: f_start = st.date_input("Start Date", value=None, key="f_start")
    with colF2: f_end = st.date_input("End Date", value=None, key="f_end")
    with colF3: filter_veh = st.selectbox("Filter by Truck", ["All"] + list(vehicle_map.keys()))
    with colF4: filter_agent = st.selectbox("Filter by Agent", ["All"] + list(agent_map.keys()))

    q = "SELECT l.loading_id, l.custom_load_id, r.run_date, v.vehicle_color, a.agent_name, i.item_name, l.opening_stock, l.added_items, l.physical_final_stock, l.agent_registered_sold, l.set_price, l.comments FROM daily_run_loading l JOIN fleet_daily_runs r ON l.run_id = r.run_id JOIN vehicles v ON r.vehicle_id = v.vehicle_id JOIN agents a ON r.agent_id = a.agent_id JOIN inventory_items i ON l.item_id = i.item_id WHERE 1=1"
    params = []
    if f_start: q += " AND r.run_date >= %s"; params.append(f_start)
    if f_end: q += " AND r.run_date <= %s"; params.append(f_end)
    if filter_veh != "All": q += " AND v.vehicle_color = %s"; params.append(filter_veh)
    if filter_agent != "All": q += " AND a.agent_name = %s"; params.append(filter_agent)
    q += " ORDER BY l.loading_id DESC LIMIT 100"
    
    fleet_df = run_query(q, tuple(params), fetch=True)
    
    if isinstance(fleet_df, pd.DataFrame) and not fleet_df.empty:
        fleet_df['loaded_total'] = fleet_df['opening_stock'] + fleet_df['added_items']
        fleet_df['calculated_sold'] = fleet_df['loaded_total'] - fleet_df['physical_final_stock']
        fleet_df['amount'] = fleet_df['calculated_sold'] * fleet_df['set_price']
        fleet_df['variance'] = fleet_df['calculated_sold'] - fleet_df['agent_registered_sold']
        fleet_df['variance_value'] = fleet_df['variance'] * fleet_df['set_price']
        
        def get_alert(row):
            if row['variance'] == 0: return "✅ OK"
            elif row['variance'] > 0: return f"🚨 Missing ({row['variance']})"
            else: return f"⚠️ Surplus ({abs(row['variance'])})"
        fleet_df['alert'] = fleet_df.apply(get_alert, axis=1)

        display_cols = ['loading_id', 'custom_load_id', 'run_date', 'vehicle_color', 'agent_name', 'item_name', 
                        'opening_stock', 'added_items', 'physical_final_stock', 'calculated_sold', 'set_price', 
                        'amount', 'agent_registered_sold', 'variance', 'alert', 'comments']
        
        st.dataframe(fleet_df[display_cols], use_container_width=True)
        
        st.markdown(f"**Subtotals for Filtered View:**")
        st.markdown(f"- Total Items Loaded: **{fleet_df['loaded_total'].sum():,.2f}**")
        st.markdown(f"- Total Calculated Sold: **{fleet_df['calculated_sold'].sum():,.2f}**")
        st.markdown(f"- **Total Amount Expected: UGX {fleet_df['amount'].sum():,.2f}**")
        st.markdown(f"- Total Registered Sold: **{fleet_df['agent_registered_sold'].sum():,.2f}**")
        st.markdown(f"- **Total Variance Value: UGX {fleet_df['variance_value'].sum():,.2f}**")

        colA, colB = st.columns(2)
        with colA:
            del_fleet = st.selectbox("Select Loading ID to Delete", fleet_df["loading_id"].tolist())
            if st.button("🗑️ Delete Fleet Record"):
                run_query("DELETE FROM daily_run_loading WHERE loading_id=%s", (int(del_fleet),)); st.rerun()
        with colB:
            edit_fleet = st.selectbox("Select Loading ID to Edit", fleet_df["loading_id"].tolist(), key="edit_fleet_widget")
            if st.button("✏️ Edit Fleet Record"):
                st.session_state.edit_fleet_id = int(edit_fleet)
                
        if "edit_fleet_id" in st.session_state:
            matches = fleet_df[fleet_df["loading_id"] == st.session_state.edit_fleet_id]
            if not matches.empty:
                rec = matches.iloc[0]
                with st.form("edit_fleet_form"):
                    st.markdown(f"### Editing Load: {rec['custom_load_id']}")
                    e_open = calc_input("Opening Stock", value=float(rec["opening_stock"]))
                    e_add = calc_input("Added Items", value=float(rec["added_items"]))
                    e_final = calc_input("Final Stock", value=float(rec["physical_final_stock"]))
                    e_sold = calc_input("Registered Sold", value=float(rec["agent_registered_sold"]))
                    if st.form_submit_button("Update Fleet Record"):
                        run_query("UPDATE daily_run_loading SET opening_stock=%s, added_items=%s, physical_final_stock=%s, agent_registered_sold=%s WHERE loading_id=%s", 
                                  (e_open, e_add, e_final, e_sold, st.session_state.edit_fleet_id))
                        del st.session_state.edit_fleet_id; st.rerun()
            else:
                del st.session_state.edit_fleet_id
                st.warning("Record no longer exists in this view.")

# =============================================================================
# TAB 3: PROCUREMENT
# =============================================================================
with tab_procure:
    st.subheader("Record Purchase")
    item_filter = st.radio("Filter Items:", ["Raw Material", "Packaging", "Finished Good"], horizontal=True)
    filtered_items = [name for name, itype in item_type_map.items() if itype == item_filter]
    
    with st.form("procure_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            p_date = st.date_input("Purchase Date", value=date.today())
            p_supplier = st.selectbox("Supplier", list(supplier_map.keys()) if supplier_map else ["No Suppliers"])
            p_item = st.selectbox("Item", filtered_items if filtered_items else ["No Items"])
        with c2:
            p_qty = calc_input("Quantity", min_value=1.0, step=1.0)
            p_price = calc_input("Unit Price (UGX)", min_value=0.0, step=100.0)
        paid_now = st.checkbox("Paid in full now?", value=True)
        part_paid = calc_input("Part-payment now (UGX) - only used if NOT paid in full", min_value=0.0, step=1000.0)
        
        if st.form_submit_button("Save Purchase"):
            if supplier_map and filtered_items:
                total = p_qty * p_price
                paid_amount = total if paid_now else min(part_paid, total)
                status = "Cleared" if paid_amount >= total else "Unpaid"
                
                new_pur_id = insert_returning_id("INSERT INTO purchases (purchase_date, supplier_id, item_id, quantity, unit_price, taxable_income, vat_amount, total_payout, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING purchase_id", 
                          (p_date, int(supplier_map[p_supplier]), int(item_map[p_item]), p_qty, p_price, 0, 0, total, status))
                
                if new_pur_id:
                    # Only cash actually paid out counts as an expense in Reports
                    if paid_amount > 0:
                        run_query("INSERT INTO purchase_payments (purchase_id, payment_date, amount) VALUES (%s, %s, %s)", (new_pur_id, p_date, paid_amount))
                    if total - paid_amount > 0:
                        st.info(f"Credit purchase: UGX {total - paid_amount:,.2f} still owed to {p_supplier}. It will count as an expense only when you record a payment.")
                    st.success(f"Purchase Recorded! Total: UGX {total:,.2f} | Paid: UGX {paid_amount:,.2f}"); st.rerun()

    st.divider()
    st.subheader("Manage Purchases (Filter & Subtotals)")
    colF1, colF2 = st.columns(2)
    with colF1: p_start = st.date_input("Start Date", value=None, key="p_start")
    with colF2: p_end = st.date_input("End Date", value=None, key="p_end")
    
    colF3, colF4 = st.columns(2)
    with colF3: p_filter_sup = st.selectbox("Filter by Supplier", ["All"] + list(supplier_map.keys()), key="p_sup_filter")
    with colF4: p_filter_item = st.selectbox("Filter by Item", ["All"] + list(item_map.keys()), key="p_item_filter")

    q_p = "SELECT p.purchase_id, p.purchase_date, s.supplier_name, i.item_name, p.quantity, p.unit_price, p.total_payout, COALESCE((SELECT SUM(pp.amount) FROM purchase_payments pp WHERE pp.purchase_id = p.purchase_id), 0) AS amount_paid, p.status FROM purchases p JOIN suppliers s ON p.supplier_id = s.supplier_id JOIN inventory_items i ON p.item_id = i.item_id WHERE 1=1"
    p_params = []
    if p_start: q_p += " AND p.purchase_date >= %s"; p_params.append(p_start)
    if p_end: q_p += " AND p.purchase_date <= %s"; p_params.append(p_end)
    if p_filter_sup != "All": q_p += " AND s.supplier_name = %s"; p_params.append(p_filter_sup)
    if p_filter_item != "All": q_p += " AND i.item_name = %s"; p_params.append(p_filter_item)
    q_p += " ORDER BY p.purchase_id DESC LIMIT 100"
    
    purchases_df = run_query(q_p, tuple(p_params), fetch=True)
    
    if isinstance(purchases_df, pd.DataFrame) and not purchases_df.empty:
        purchases_df['amount_paid'] = purchases_df['amount_paid'].astype(float)
        purchases_df['balance'] = purchases_df['total_payout'].astype(float) - purchases_df['amount_paid']
        st.dataframe(purchases_df, use_container_width=True)
        
        st.markdown(f"**Subtotal for Filtered View:**")
        st.markdown(f"- **Total Purchase Amount: UGX {purchases_df['total_payout'].sum():,.2f}**")
        st.markdown(f"- Total Paid to Suppliers: **UGX {purchases_df['amount_paid'].sum():,.2f}**")
        st.markdown(f"- **Total Still Owed to Suppliers: UGX {purchases_df['balance'].sum():,.2f}**")

        # --- Pay off a credit purchase ---
        owing_df = purchases_df[purchases_df['balance'] > 0]
        if not owing_df.empty:
            st.markdown("#### Record Payment to Supplier")
            pay_pur_id = st.selectbox("Select Purchase ID to Pay", owing_df["purchase_id"].tolist(), key="pay_pur_id")
            pay_row = owing_df[owing_df["purchase_id"] == pay_pur_id].iloc[0]
            pay_amt = calc_input("Payment Amount (UGX)", min_value=1.0, value=float(pay_row["balance"]), step=1000.0)
            pay_date = st.date_input("Payment Date", value=date.today(), key="pay_pur_date")
            if st.button("Record Supplier Payment"):
                if pay_amt > float(pay_row["balance"]):
                    st.error(f"Payment exceeds the balance owed (UGX {float(pay_row['balance']):,.2f}).")
                else:
                    run_query("INSERT INTO purchase_payments (purchase_id, payment_date, amount) VALUES (%s, %s, %s)", (int(pay_pur_id), pay_date, pay_amt))
                    if pay_amt >= float(pay_row["balance"]):
                        run_query("UPDATE purchases SET status='Cleared' WHERE purchase_id=%s", (int(pay_pur_id),))
                    st.success("Payment recorded as an expense."); st.rerun()
        
        colA, colB = st.columns(2)
        with colA:
            del_pur = st.selectbox("Select Purchase ID to Delete", purchases_df["purchase_id"].tolist(), key="del_pur")
            if st.button("🗑️ Delete Purchase"):
                run_query("DELETE FROM purchase_payments WHERE purchase_id=%s", (int(del_pur),))
                run_query("DELETE FROM purchases WHERE purchase_id=%s", (int(del_pur),)); st.rerun()
        with colB:
            edit_pur = st.selectbox("Select Purchase ID to Edit", purchases_df["purchase_id"].tolist(), key="edit_pur_widget")
            if st.button("✏️ Edit Selected Purchase"):
                st.session_state.edit_pur_id = int(edit_pur)
            
        if "edit_pur_id" in st.session_state:
            matches = purchases_df[purchases_df["purchase_id"] == st.session_state.edit_pur_id]
            if not matches.empty:
                rec = matches.iloc[0]
                with st.form("edit_pur_form"):
                    st.markdown(f"### Editing Purchase #{st.session_state.edit_pur_id}")
                    e_qty = calc_input("New Quantity", value=float(rec["quantity"]))
                    st.caption("Payment status follows the payments recorded. Use 'Record Payment to Supplier' to settle a balance.")
                    if st.form_submit_button("Update Purchase"):
                        new_total = e_qty * float(rec["unit_price"])
                        e_status = "Cleared" if float(rec["amount_paid"]) >= new_total else "Unpaid"
                        run_query("UPDATE purchases SET quantity=%s, total_payout=%s, status=%s WHERE purchase_id=%s", (e_qty, new_total, e_status, st.session_state.edit_pur_id))
                        del st.session_state.edit_pur_id; st.rerun()
            else:
                del st.session_state.edit_pur_id
                st.warning("Record no longer exists in this view.")

# =============================================================================
# TAB 4: AGENT & CLIENT DEBTS
# =============================================================================
with tab_debts:
    st.subheader("Log Debt Manually")
    with st.form("agent_debt_form"):
        c1, c2 = st.columns(2)
        with c1:
            d_agent = st.selectbox("Agent", list(agent_map.keys()) if agent_map else ["No Agents"])
            d_reason = st.text_input("Reason (e.g., Missing Stock, Client Default)")
        with c2:
            d_amount = calc_input("Amount Owed (UGX)", min_value=1.0, step=1000.0)
            d_date = st.date_input("Date", value=date.today())
        if st.form_submit_button("Log Debt"):
            debt_id = generate_debt_id(d_agent)
            run_query("INSERT INTO agent_debts (agent_id, debt_date, reason, amount_owed, client_name) VALUES (%s, %s, %s, %s, %s)", 
                      (int(agent_map[d_agent]), d_date, d_reason, d_amount, debt_id))
            st.success(f"Debt logged successfully. Debt ID: {debt_id}"); st.rerun()

    st.markdown("#### Outstanding Debts & Recoveries")
    
    colF1, colF2 = st.columns(2)
    with colF1: d_start = st.date_input("Start Date", value=None, key="d_start")
    with colF2: d_end = st.date_input("End Date", value=None, key="d_end")
    
    colF3, colF4 = st.columns(2)
    with colF3: d_filter_agent = st.selectbox("Filter by Agent", ["All"] + list(agent_map.keys()), key="d_agent_filter")
    with colF4: d_search_client = st.text_input("Search Client/Debt ID", value="", key="d_client_filter")

    q_d = "SELECT d.debt_id, a.agent_name, d.client_name, d.debt_date, d.reason, d.amount_owed, d.amount_recovered, (d.amount_owed - d.amount_recovered) as balance FROM agent_debts d JOIN agents a ON d.agent_id = a.agent_id WHERE d.status != 'Cleared'"
    p_d = []
    if d_start: q_d += " AND d.debt_date >= %s"; p_d.append(d_start)
    if d_end: q_d += " AND d.debt_date <= %s"; p_d.append(d_end)
    if d_filter_agent != "All": q_d += " AND a.agent_name = %s"; p_d.append(d_filter_agent)
    if d_search_client: q_d += " AND d.client_name ILIKE %s"; p_d.append(f"%{d_search_client}%")
    q_d += " ORDER BY d.debt_date DESC"
    
    agent_debts_df = run_query(q_d, tuple(p_d), fetch=True)
    
    if isinstance(agent_debts_df, pd.DataFrame) and not agent_debts_df.empty:
        st.dataframe(agent_debts_df, use_container_width=True)
        
        st.markdown(f"**Subtotals for Filtered View:**")
        st.markdown(f"- Total Amount Owed: **UGX {agent_debts_df['amount_owed'].sum():,.2f}**")
        st.markdown(f"- Total Amount Recovered: **UGX {agent_debts_df['amount_recovered'].sum():,.2f}**")
        st.markdown(f"- **Total Outstanding Balance: UGX {agent_debts_df['balance'].sum():,.2f}**")
        
        recover_id = st.selectbox("Select Debt ID to Recover", agent_debts_df["debt_id"].tolist())
        recover_amt = calc_input("Amount Recovered (UGX)", min_value=1.0)
        recover_date = st.date_input("Recovery Date", value=date.today(), key="recover_date")
        
        if st.button("Record Recovery"):
            debt_row = agent_debts_df[agent_debts_df["debt_id"] == recover_id].iloc[0]
            if recover_amt > float(debt_row["balance"]):
                st.error(f"Recovery exceeds the outstanding balance (UGX {float(debt_row['balance']):,.2f}).")
            else:
                # 1. Update Debt Table
                run_query("UPDATE agent_debts SET amount_recovered = amount_recovered + %s WHERE debt_id = %s", (recover_amt, recover_id))
                run_query("UPDATE agent_debts SET status = 'Cleared' WHERE amount_recovered >= amount_owed")
                
                # 2. Add the cleared amount to revenue straight away (and sync the linked sale, if any)
                log_debt_cash(recover_id, str(debt_row["reason"] or ""), recover_amt, recover_date)

                st.success("Recovery recorded and synchronized with Sales Ledger."); st.rerun()
            
        colA, colB = st.columns(2)
        with colA:
            del_debt = st.selectbox("Select Debt ID to Delete", agent_debts_df["debt_id"].tolist(), key="del_debt")
            if st.button("🗑️ Delete Debt Record"):
                run_query("DELETE FROM debt_recoveries WHERE debt_id=%s", (str(del_debt),))
                run_query("DELETE FROM agent_debts WHERE debt_id=%s", (del_debt,)); st.rerun()
        with colB:
            edit_debt = st.selectbox("Select Debt ID to Edit", agent_debts_df["debt_id"].tolist(), key="edit_debt_widget")
            if st.button("✏️ Edit Debt Record"):
                st.session_state.edit_debt_id = edit_debt
                
        if "edit_debt_id" in st.session_state:
            matches = agent_debts_df[agent_debts_df["debt_id"] == st.session_state.edit_debt_id]
            if not matches.empty:
                rec = matches.iloc[0]
                with st.form("edit_debt_form"):
                    st.markdown(f"### Editing Debt: {st.session_state.edit_debt_id}")
                    e_client = st.text_input("Client / Debt ID", value=rec["client_name"] if pd.notnull(rec["client_name"]) else "")
                    e_reason = st.text_input("Reason", value=rec["reason"])
                    e_owed = calc_input("Amount Owed", value=float(rec["amount_owed"]))
                    e_rec = calc_input("Amount Recovered", value=float(rec["amount_recovered"]))
                    if st.form_submit_button("Update Debt"):
                        run_query("UPDATE agent_debts SET client_name=%s, reason=%s, amount_owed=%s, amount_recovered=%s WHERE debt_id=%s", 
                                  (e_client, e_reason, e_owed, e_rec, st.session_state.edit_debt_id))
                        if e_rec != float(rec["amount_recovered"]):
                            log_debt_cash(st.session_state.edit_debt_id, str(rec["reason"] or ""), e_rec - float(rec["amount_recovered"]), date.today())
                        del st.session_state.edit_debt_id; st.rerun()
            else:
                del st.session_state.edit_debt_id
                st.warning("Record no longer exists in this view.")
# =============================================================================
# TAB 5: AGENT COMMISSIONS
# =============================================================================
with tab_comm:
    st.subheader("Calculate Agent Commission Manually")
    with st.form("comm_form"):
        c1, c2 = st.columns(2)
        with c1:
            comm_date = st.date_input("Commission Date", value=date.today())
            comm_agent = st.selectbox("Agent", list(agent_map.keys()) if agent_map else ["No Agents"])
            comm_item = st.selectbox("Item Sold", list(item_map.keys()) if item_map else ["No Items"])
        with c2:
            set_p = calc_input("Company Set Price (UGX)", min_value=0.0, step=100.0)
            actual_p = calc_input("Agent Actual Selling Price (UGX)", min_value=0.0, step=100.0)
            qty = calc_input("Quantity", min_value=1, step=1)
            
        if st.form_submit_button("Calculate & Save Commission"):
            if comm_agent and comm_item:
                diff = actual_p - set_p
                total_comm = max(diff * qty, 0.0)  # negative -> "No commission" (stored as 0)
                new_comm_id = insert_returning_id("INSERT INTO agent_commissions (sale_id, agent_id, item_id, set_price, actual_price, quantity, total_commission, commission_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING commission_id",
                          (None, int(agent_map[comm_agent]), int(item_map[comm_item]), set_p, actual_p, qty, total_comm, comm_date))
                log_commission_expense(new_comm_id, total_comm, comm_date, f"Commission: {comm_agent}, {comm_item}")
                if diff < 0: st.info("Actual price is below the set price: recorded as No commission.")
                else: st.success(f"Commission Saved! UGX {total_comm:,.2f}")
                st.rerun()

    st.divider()
    st.subheader("Recent Commissions (Filtered)")
    colF1, colF2 = st.columns(2)
    with colF1: c_start = st.date_input("Start Date", value=None, key="c_start")
    with colF2: c_end = st.date_input("End Date", value=None, key="c_end")

    q_c = "SELECT c.commission_id, c.commission_date, a.agent_name, i.item_name, c.set_price, c.actual_price, c.quantity, c.total_commission FROM agent_commissions c JOIN agents a ON c.agent_id = a.agent_id JOIN inventory_items i ON c.item_id = i.item_id WHERE (c.sale_id IS NULL OR EXISTS (SELECT 1 FROM sales_ledger s WHERE s.sale_id = c.sale_id))"
    p_c = []
    if c_start: q_c += " AND c.commission_date >= %s"; p_c.append(c_start)
    if c_end: q_c += " AND c.commission_date <= %s"; p_c.append(c_end)
    q_c += " ORDER BY c.commission_id DESC LIMIT 100"
    
    comm_df = run_query(q_c, tuple(p_c), fetch=True)
    
    if isinstance(comm_df, pd.DataFrame) and not comm_df.empty:
        raw_c = comm_df['total_commission'].astype(float)
        no_comm = (comm_df['actual_price'].astype(float) < comm_df['set_price'].astype(float)) | (raw_c < 0)
        show_df = comm_df.copy()
        show_df['total_commission'] = ["No commission" if n else f"{v:,.2f}" for v, n in zip(raw_c, no_comm)]
        st.dataframe(show_df, use_container_width=True)
        
        st.markdown(f"**Subtotal for Filtered View:**")
        st.markdown(f"- **Total Commission: UGX {raw_c.where(~no_comm, 0).clip(lower=0).sum():,.2f}**")
        
        del_comm = st.selectbox("Select Commission ID to Remove", comm_df["commission_id"].tolist())
        if st.button("🗑️ Remove Commission"):
            run_query("DELETE FROM enterprise_ledger WHERE source_reference_table = 'commission' AND bookkeeper_notes LIKE %s", (f"[COMM:{int(del_comm)}]%",))
            run_query("DELETE FROM agent_commissions WHERE commission_id=%s", (int(del_comm),)); st.rerun()

# =============================================================================
# TAB 6: EXPENSES
# =============================================================================
with tab_expenses:
    st.subheader("Record Manual Expenditure")
    with st.form("expense_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            e_date = st.date_input("Expense Date", value=date.today())
            e_cat = st.selectbox("Category", list(category_map.keys()) if category_map else ["No Categories"])
        with c2:
            e_amount = calc_input("Amount (UGX)", min_value=1.0, step=500.0)
            e_notes = st.text_input("Details")
        if st.form_submit_button("Save Expense"):
            run_query("INSERT INTO enterprise_ledger (accounting_date, entry_type, category_id, source_reference_table, financial_amount, bookkeeper_notes) VALUES (%s, 'Outflow', %s, 'manual_portal_entry', %s, %s)", (e_date, int(category_map[e_cat]), e_amount, e_notes))
            st.success("Expense saved."); st.rerun()

    with st.expander("Commission expenses (logged automatically)"):
        comm_exp_df = run_query("SELECT ledger_id, accounting_date, financial_amount, bookkeeper_notes FROM enterprise_ledger WHERE source_reference_table = 'commission' ORDER BY ledger_id DESC LIMIT 100", fetch=True)
        if isinstance(comm_exp_df, pd.DataFrame) and not comm_exp_df.empty:
            st.dataframe(comm_exp_df, use_container_width=True)
            st.markdown(f"- **Total shown: UGX {comm_exp_df['financial_amount'].sum():,.2f}**")
            st.caption("These are created and removed automatically with each commission. Delete the commission to remove its expense.")
        else: st.write("No commission expenses yet.")

    st.divider()
    st.subheader("Manage Saved Expenses")
    colF1, colF2 = st.columns(2)
    with colF1: e_start = st.date_input("Start Date", value=None, key="e_start")
    with colF2: e_end = st.date_input("End Date", value=None, key="e_end")
    
    e_filter_cat = st.selectbox("Filter by Category", ["All"] + list(category_map.keys()), key="e_cat_filter")
    
    q_e = "SELECT e.ledger_id, e.accounting_date, c.category_name, e.financial_amount, e.bookkeeper_notes FROM enterprise_ledger e JOIN general_expenditure_categories c ON e.category_id = c.category_id WHERE e.source_reference_table = 'manual_portal_entry'"
    p_e = []
    if e_start: q_e += " AND e.accounting_date >= %s"; p_e.append(e_start)
    if e_end: q_e += " AND e.accounting_date <= %s"; p_e.append(e_end)
    if e_filter_cat != "All": q_e += " AND c.category_name = %s"; p_e.append(e_filter_cat)
    q_e += " ORDER BY e.ledger_id DESC LIMIT 100"
    
    exp_df = run_query(q_e, tuple(p_e), fetch=True)
    
    if isinstance(exp_df, pd.DataFrame) and not exp_df.empty:
        st.dataframe(exp_df, use_container_width=True)
        
        st.markdown(f"**Subtotal:**")
        st.markdown(f"- **Total for Filtered View: UGX {exp_df['financial_amount'].sum():,.2f}**")
        
        colA, colB = st.columns(2)
        with colA:
            del_exp = st.selectbox("Select Expense ID to Delete", exp_df["ledger_id"].tolist(), key="del_exp")
            if st.button("🗑️ Delete Selected Expense"):
                run_query("DELETE FROM enterprise_ledger WHERE ledger_id=%s", (int(del_exp),)); st.rerun()
        with colB:
            edit_exp = st.selectbox("Select Expense ID to Edit", exp_df["ledger_id"].tolist(), key="edit_exp_widget")
            if st.button("✏️ Edit Selected Expense"):
                st.session_state.edit_exp_id = int(edit_exp)

        if "edit_exp_id" in st.session_state:
            matches = exp_df[exp_df["ledger_id"] == st.session_state.edit_exp_id]
            if not matches.empty:
                rec = matches.iloc[0]
                with st.form("edit_exp_form"):
                    st.markdown(f"### Editing Expense #{st.session_state.edit_exp_id}")
                    e_date_new = st.date_input("New Date", value=rec["accounting_date"])
                    e_amt_new = calc_input("New Amount (UGX)", value=float(rec["financial_amount"]))
                    if st.form_submit_button("Update Expense"):
                        run_query("UPDATE enterprise_ledger SET accounting_date=%s, financial_amount=%s WHERE ledger_id=%s", (e_date_new, e_amt_new, st.session_state.edit_exp_id))
                        del st.session_state.edit_exp_id; st.rerun()
            else:
                del st.session_state.edit_exp_id
                st.warning("Record no longer exists in this view.")

# =============================================================================
# TAB 7: MASTER SETUP
# =============================================================================
with tab_master:
    st.subheader("Master Data Registration")
    c1, c2 = st.columns(2)
    
    with c1:
        st.markdown("#### Add Supplier")
        with st.form("add_sup"):
            sup_name = st.text_input("Supplier Name")
            sup_contact = st.text_input("Contact")
            if st.form_submit_button("Save Supplier"):
                if not sup_name:
                    st.warning("Supplier Name cannot be empty.")
                else:
                    conn = get_connection()
                    try:
                        with conn.cursor() as cur:
                            cur.execute("INSERT INTO suppliers (supplier_name, contact_info) VALUES (%s, %s)", (sup_name, sup_contact))
                            conn.commit()
                        st.success(f"Supplier '{sup_name}' added successfully!")
                        st.rerun()
                    except Exception as e:
                        conn.rollback()
                        st.error(f"DB Error: {e}")
                    finally:
                        conn.close()
                
        st.markdown("#### Add Product / Material")
        with st.form("add_item"):
            i_name = st.text_input("Item Name")
            i_type = st.selectbox("Type", ["Raw Material", "Packaging", "Finished Good"])
            i_uom = st.text_input("Unit (e.g., Kg, 20L)")
            
            if st.form_submit_button("Save Item"):
                if not i_name or not i_uom:
                    st.warning("Please enter both Item Name and Unit.")
                else:
                    conn = get_connection()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO inventory_items (item_name, item_type, unit_of_measure) VALUES (%s, %s, %s)", 
                                (i_name, i_type, i_uom)
                            )
                            conn.commit()
                        st.success(f"Item '{i_name}' added successfully!")
                        st.rerun()
                    except Exception as e:
                        conn.rollback()
                        st.error(f"DB Error: {e}")
                    finally:
                        conn.close()

    with c2:
        st.markdown("#### Add Agent")
        with st.form("add_agent"):
            a_name = st.text_input("Agent Name")
            a_phone = st.text_input("Phone")
            if st.form_submit_button("Save Agent"):
                if not a_name:
                    st.warning("Agent Name cannot be empty.")
                else:
                    conn = get_connection()
                    try:
                        with conn.cursor() as cur:
                            cur.execute("INSERT INTO agents (agent_name, contact_number) VALUES (%s, %s)", (a_name, a_phone))
                            conn.commit()
                        st.success(f"Agent '{a_name}' added successfully!")
                        st.rerun()
                    except Exception as e:
                        conn.rollback()
                        st.error(f"DB Error: {e}")
                    finally:
                        conn.close()

        st.markdown("#### Add Client")
        with st.form("add_client"):
            c_name = st.text_input("Client Name")
            c_loc = st.text_input("Location")
            if st.form_submit_button("Save Client"):
                if not c_name:
                    st.warning("Client Name cannot be empty.")
                else:
                    conn = get_connection()
                    try:
                        with conn.cursor() as cur:
                            cur.execute("INSERT INTO clients (client_name, location) VALUES (%s, %s)", (c_name, c_loc))
                            conn.commit()
                        st.success(f"Client '{c_name}' added successfully!")
                        st.rerun()
                    except Exception as e:
                        conn.rollback()
                        st.error(f"DB Error: {e}")
                    finally:
                        conn.close()

    # --- MANAGE EXISTING ITEMS ---
    st.divider()
    st.subheader("Manage Saved Products / Materials")
    
    current_items_df = run_query("SELECT item_id, item_name, item_type, unit_of_measure FROM inventory_items ORDER BY item_name", fetch=True)
    
    if isinstance(current_items_df, pd.DataFrame) and not current_items_df.empty:
        st.dataframe(current_items_df, use_container_width=True)
        
        colA, colB = st.columns(2)
        
        with colA:
            del_item_id = st.selectbox("Select Item ID to Delete", current_items_df["item_id"].tolist(), key="del_item_id_widget")
            if st.button("🗑️ Delete Selected Item"):
                conn = get_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM inventory_items WHERE item_id = %s", (int(del_item_id),))
                        conn.commit()
                    st.success("Item deleted successfully.")
                    st.rerun()
                except psycopg2.errors.ForeignKeyViolation:
                    conn.rollback()
                    st.error("Cannot delete this item because it has already been used in Sales, Fleet, or Purchases. (Data Integrity Protection).")
                except Exception as e:
                    conn.rollback()
                    st.error(f"DB Error: {e}")
                finally:
                    conn.close()
                    
        with colB:
            edit_item_id = st.selectbox("Select Item ID to Edit", current_items_df["item_id"].tolist(), key="edit_item_id_widget")
            if st.button("✏️ Edit Selected Item"):
                st.session_state.edit_item_id = int(edit_item_id)
                
        if "edit_item_id" in st.session_state:
            matches = current_items_df[current_items_df["item_id"] == st.session_state.edit_item_id]
            if not matches.empty:
                rec = matches.iloc[0]
                with st.form("edit_item_form"):
                    st.markdown(f"### Editing: {rec['item_name']}")
                    
                    e_name = st.text_input("Item Name", value=rec["item_name"])
                    
                    type_options = ["Raw Material", "Packaging", "Finished Good"]
                    current_type = rec["item_type"] if rec["item_type"] in type_options else "Finished Good"
                    e_type = st.selectbox("Type", type_options, index=type_options.index(current_type))
                    
                    e_uom = st.text_input("Unit of Measure", value=rec["unit_of_measure"] if rec["unit_of_measure"] else "")
                    
                    if st.form_submit_button("Update Item"):
                        conn = get_connection()
                        try:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    UPDATE inventory_items 
                                    SET item_name=%s, item_type=%s, unit_of_measure=%s 
                                    WHERE item_id=%s
                                """, (e_name, e_type, e_uom, st.session_state.edit_item_id))
                                conn.commit()
                            st.success("Item updated successfully!")
                            del st.session_state.edit_item_id
                            st.rerun()
                        except Exception as e:
                            conn.rollback()
                            st.error(f"DB Error: {e}")
                        finally:
                            conn.close()
            else:
                del st.session_state.edit_item_id
                st.warning("Record no longer exists.")
    else:
        st.info("No items have been saved yet.")

# =============================================================================
# TAB 8: REPORTS (CASH BASIS)
# =============================================================================
with tab_reports:
    st.subheader("Executive P&L Dashboard (Cash Basis)")
    
    colR1, colR2 = st.columns(2)
    with colR1: rep_start = st.date_input("Report Start Date", value=date.today(), key="rep_start")
    with colR2: rep_end = st.date_input("Report End Date", value=date.today(), key="rep_end")
    
    if rep_start and rep_end:
        # Revenue: cash actually received in the period (full payments + credit-sale recoveries, by date received)
        rev_df = run_query("""SELECT SUM(sp.amount) AS total_rev FROM sale_payments sp
                              JOIN sales_ledger s ON s.sale_id = sp.sale_id
                              WHERE sp.payment_date >= %s AND sp.payment_date <= %s""", (rep_start, rep_end), fetch=True)
        sales_cash = scalar(rev_df, 'total_rev')
        rec_df = run_query("""SELECT SUM(dr.amount) AS total_rec FROM debt_recoveries dr
                              JOIN agent_debts d ON d.debt_id::text = dr.debt_id
                              WHERE dr.recovery_date >= %s AND dr.recovery_date <= %s""", (rep_start, rep_end), fetch=True)
        debt_cash = scalar(rec_df, 'total_rec')
        total_revenue = sales_cash + debt_cash
        
        # Expenses: manual expenses + cash actually paid to suppliers in the period (by date paid)
        man_df = run_query("""SELECT SUM(financial_amount) AS total_exp FROM enterprise_ledger
                              WHERE accounting_date >= %s AND accounting_date <= %s AND entry_type = 'Outflow'
                              AND COALESCE(source_reference_table, '') <> 'procurement'""", (rep_start, rep_end), fetch=True)
        proc_df = run_query("""SELECT SUM(pp.amount) AS total_exp FROM purchase_payments pp
                               JOIN purchases p ON p.purchase_id = pp.purchase_id
                               WHERE pp.payment_date >= %s AND pp.payment_date <= %s""", (rep_start, rep_end), fetch=True)
        manual_expenses = scalar(man_df, 'total_exp')  # includes commissions
        comm_exp_total = scalar(run_query("""SELECT SUM(financial_amount) AS t FROM enterprise_ledger
                                             WHERE accounting_date >= %s AND accounting_date <= %s AND entry_type = 'Outflow' AND source_reference_table = 'commission'""", (rep_start, rep_end), fetch=True), 't')
        procurement_paid = scalar(proc_df, 'total_exp')
        total_expenses = manual_expenses + procurement_paid
        
        # Still owed TO you (client/agent debts) and still owed BY you (unpaid supplier balances) - not in the figures above
        debt_df = run_query("SELECT SUM(amount_owed - amount_recovered) as total_debt FROM agent_debts WHERE debt_date >= %s AND debt_date <= %s", (rep_start, rep_end), fetch=True)
        total_debt = scalar(debt_df, 'total_debt')
        payable_df = run_query("""SELECT SUM(p.total_payout - COALESCE(pp.paid, 0)) AS owed FROM purchases p
                                  LEFT JOIN (SELECT purchase_id, SUM(amount) AS paid FROM purchase_payments GROUP BY purchase_id) pp ON pp.purchase_id = p.purchase_id
                                  WHERE p.purchase_date >= %s AND p.purchase_date <= %s""", (rep_start, rep_end), fetch=True)
        total_payable = scalar(payable_df, 'owed')

        # Billed / invoiced figures for the period (cash + the portion still recoverable)
        inv_df = run_query("""SELECT SUM(quantity_sold * actual_price) AS invoiced, SUM(COALESCE(amount_paid, 0)) AS paid
                              FROM sales_ledger WHERE sale_date >= %s AND sale_date <= %s""", (rep_start, rep_end), fetch=True)
        invoiced = scalar(inv_df, 'invoiced')
        sales_recoverable = max(invoiced - scalar(inv_df, 'paid'), 0.0)
        # Debts not tied to a sale (e.g. missing stock). Sale-linked debts are already inside sales_recoverable.
        other_debt_df = run_query("""SELECT SUM(amount_owed - amount_recovered) AS d FROM agent_debts
                                     WHERE debt_date >= %s AND debt_date <= %s AND COALESCE(reason, '') NOT LIKE '%%Sale ID%%'""", (rep_start, rep_end), fetch=True)
        other_recoverable = scalar(other_debt_df, 'd')
        pur_df = run_query("SELECT SUM(total_payout) AS t FROM purchases WHERE purchase_date >= %s AND purchase_date <= %s", (rep_start, rep_end), fetch=True)
        purchases_billed = scalar(pur_df, 't')

        total_recoverable = sales_recoverable + other_recoverable
        revenue_incl = invoiced + other_recoverable
        expenses_incl = manual_expenses + purchases_billed

        st.markdown("#### Cash Basis (money actually received / paid)")
        m1, m2, m3 = st.columns(3)
        m1.metric("Cash Received (Revenue)", f"UGX {total_revenue:,.2f}")
        m2.metric("Total Expenses (Cash Paid)", f"UGX {total_expenses:,.2f}")
        m3.metric("Net Cash Flow", f"UGX {total_revenue - total_expenses:,.2f}")
        st.caption(f"Revenue = sales cash (incl. credit-sale recoveries) UGX {sales_cash:,.2f} + other debts cleared UGX {debt_cash:,.2f}. The unrecovered balance stays in Debts and is not counted here.")
        st.caption(f"Expenses = manual expenses UGX {manual_expenses - comm_exp_total:,.2f} + commissions UGX {comm_exp_total:,.2f} + supplier payments UGX {procurement_paid:,.2f}")

        with st.expander("🔍 Cash receipts included in this period's revenue"):
            det_s = run_query("""SELECT sp.payment_date, 'Sale #' || sp.sale_id AS source, sp.amount FROM sale_payments sp
                                 JOIN sales_ledger s ON s.sale_id = sp.sale_id
                                 WHERE sp.payment_date >= %s AND sp.payment_date <= %s ORDER BY sp.payment_date DESC""", (rep_start, rep_end), fetch=True)
            det_d = run_query("""SELECT dr.recovery_date AS payment_date, 'Debt ' || dr.debt_id AS source, dr.amount FROM debt_recoveries dr
                                 JOIN agent_debts d ON d.debt_id::text = dr.debt_id
                                 WHERE dr.recovery_date >= %s AND dr.recovery_date <= %s ORDER BY dr.recovery_date DESC""", (rep_start, rep_end), fetch=True)
            det = pd.concat([x for x in (det_s, det_d) if isinstance(x, pd.DataFrame) and not x.empty], ignore_index=True) if any(isinstance(x, pd.DataFrame) and not x.empty for x in (det_s, det_d)) else pd.DataFrame()
            if det.empty: st.write("No cash receipts fall inside the selected dates. Widen the Start/End dates (they default to today only).")
            else: st.dataframe(det, use_container_width=True)
            all_s = scalar(run_query("SELECT SUM(sp.amount) AS t FROM sale_payments sp JOIN sales_ledger s ON s.sale_id = sp.sale_id", fetch=True), 't')
            all_d = scalar(run_query("SELECT SUM(dr.amount) AS t FROM debt_recoveries dr JOIN agent_debts d ON d.debt_id::text = dr.debt_id", fetch=True), 't')
            st.caption(f"All-time cash received: UGX {all_s + all_d:,.2f}")

        st.markdown("#### Including Amounts Still Recoverable / Payable")
        n1, n2, n3 = st.columns(3)
        n1.metric("Revenue incl. Recoverable", f"UGX {revenue_incl:,.2f}")
        n2.metric("Expenses incl. Payable", f"UGX {expenses_incl:,.2f}")
        n3.metric("Net incl. Recoverable", f"UGX {revenue_incl - expenses_incl:,.2f}")
        st.caption(f"Recoverable from clients on sales in this period: UGX {sales_recoverable:,.2f} | Other agent debts: UGX {other_recoverable:,.2f} | Unpaid supplier balances: UGX {total_payable:,.2f}. "
                   "Sales are counted by sale date and purchases by purchase date, so a later recovery or payment is not counted twice.")
    else:
        st.warning("Please select both a start and end date to generate the report.")

# =============================================================================
# TAB 9: USER ACCESS (admin only)
# =============================================================================
if tab_users is not None:
    with tab_users:
        st.subheader("User Access")
        st.caption("Viewer: can only look. Editor: can add, edit and delete records. Admin: editor, plus manages this page. New registrations start as viewers.")
        users_df = run_query("SELECT user_id, username, full_name, role FROM users ORDER BY user_id", fetch=True)
        if isinstance(users_df, pd.DataFrame) and not users_df.empty:
            st.dataframe(users_df, use_container_width=True)
            c1, c2 = st.columns(2)
            with c1: target_user = st.selectbox("User", users_df["username"].tolist(), key="role_user")
            with c2: new_role = st.selectbox("New Access Level", ["viewer", "editor", "admin"], key="role_new")
            if st.button("Update Access"):
                if target_user == st.session_state.get("username") and new_role != "admin":
                    st.error("You cannot remove your own admin access. Ask another admin, or make someone else admin first.")
                else:
                    run_query("UPDATE users SET role = %s WHERE username = %s", (new_role, target_user))
                    st.success(f"{target_user} is now: {new_role}"); st.rerun()

# A run that reached the end did not rerun, so any error was already shown above; don't repeat it
st.session_state.pop("flash_transient", None)
