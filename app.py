from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from functools import wraps
from sqlalchemy import func, Index, text
from sqlalchemy.orm import joinedload, selectinload
import os, secrets, hashlib, platform, subprocess, uuid
try:
    import requests as _http
except ImportError:
    _http = None

app = Flask(__name__)
app.url_map.strict_slashes = False
app.config['SECRET_KEY']                     = os.environ.get('SECRET_KEY', 'runx-fixed-secret-key-change-in-production-2024')
_INSTANCE_DIR = os.environ.get(
    'RUNX_INSTANCE_DIR',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
)
os.makedirs(_INSTANCE_DIR, exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(_INSTANCE_DIR, 'shop.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_HTTPONLY']        = True
app.config['SESSION_COOKIE_SAMESITE']        = 'Lax'

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'يجب تسجيل الدخول أولاً'
login_manager.login_message_category = 'warning'

# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id              = db.Column(db.Integer, primary_key=True)
    username        = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash   = db.Column(db.String(256), nullable=False)
    role            = db.Column(db.String(20), default='seller')
    full_name       = db.Column(db.String(120), default='')
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    active          = db.Column(db.Boolean, default=True)
    must_change_pwd = db.Column(db.Boolean, default=False)

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)
    @property
    def is_admin(self): return self.role == 'admin'


class Category(db.Model):
    __tablename__ = 'categories'
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), unique=True, nullable=False)
    products = db.relationship('Product', backref='category_obj', lazy=True)


class Product(db.Model):
    __tablename__  = 'products'
    __table_args__ = (
        Index('ix_product_name',     'name'),
        Index('ix_product_category', 'category_id'),
        Index('ix_product_active',   'active'),
    )
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(200), nullable=False)
    barcode         = db.Column(db.String(100), nullable=True, index=True)
    category_id     = db.Column(db.Integer, db.ForeignKey('categories.id'))
    buy_price       = db.Column(db.Float, default=0.0)
    sell_price      = db.Column(db.Float, default=0.0)
    quantity        = db.Column(db.Float, default=0.0)
    alert_threshold = db.Column(db.Float, default=5.0)
    unit            = db.Column(db.String(30), default='قطعة')
    active          = db.Column(db.Boolean, default=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def profit_margin(self):
        return ((self.sell_price - self.buy_price) / self.buy_price * 100) if self.buy_price > 0 else 0
    @property
    def is_low_stock(self): return self.quantity <= self.alert_threshold
    @property
    def stock_value(self):      return self.quantity * self.buy_price
    @property
    def stock_value_sell(self): return self.quantity * self.sell_price

    def to_dict(self):
        return {
            'id':          self.id,
            'name':        self.name,
            'barcode':     self.barcode or '',
            'sell_price':  self.sell_price,
            'buy_price':   self.buy_price,
            'quantity':    self.quantity,
            'unit':        self.unit,
            'category':    self.category_obj.name if self.category_obj else '',
            'category_id': self.category_id or 0,
            'alert_threshold': self.alert_threshold,
        }


class Sale(db.Model):
    __tablename__  = 'sales'
    __table_args__ = (
        Index('ix_sale_date',      'sale_date'),
        Index('ix_sale_user',      'user_id'),
        Index('ix_sale_invoice',   'invoice_no'),
        Index('ix_sale_type',      'sale_type'),
        Index('ix_sale_date_type', 'sale_date', 'sale_type'),
    )
    id            = db.Column(db.Integer, primary_key=True)
    invoice_no    = db.Column(db.String(30), unique=True, nullable=False)
    customer_name = db.Column(db.String(120), default='زبون نقدي')
    sale_date     = db.Column(db.DateTime, default=datetime.utcnow)
    subtotal      = db.Column(db.Float, default=0.0)
    discount      = db.Column(db.Float, default=0.0)
    total         = db.Column(db.Float, default=0.0)
    cost          = db.Column(db.Float, default=0.0)
    profit        = db.Column(db.Float, default=0.0)
    notes         = db.Column(db.Text, default='')
    sale_type     = db.Column(db.String(20), default='sale')
    user_id       = db.Column(db.Integer, db.ForeignKey('users.id'))
    user          = db.relationship('User', backref='sales')
    items         = db.relationship('SaleItem', backref='sale', lazy=True,
                                    cascade='all, delete-orphan')

    @staticmethod
    def generate_invoice_no():
        today  = date.today()
        prefix = today.strftime('%Y%m%d')
        last   = Sale.query.filter(Sale.invoice_no.like(f'{prefix}%')).order_by(Sale.id.desc()).first()
        seq    = (int(last.invoice_no[8:12]) + 1) if last and last.invoice_no[8:12].isdigit() else 1
        return f'{prefix}{seq:04d}'


class SaleItem(db.Model):
    __tablename__  = 'sale_items'
    __table_args__ = (
        Index('ix_saleitem_sale',         'sale_id'),
        Index('ix_saleitem_product',      'product_id'),
        Index('ix_saleitem_sale_product', 'sale_id', 'product_id'),
    )
    id          = db.Column(db.Integer, primary_key=True)
    sale_id     = db.Column(db.Integer, db.ForeignKey('sales.id'))
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    product     = db.relationship('Product')
    item_type   = db.Column(db.String(20), default='product')
    description = db.Column(db.String(200), default='')
    quantity    = db.Column(db.Float, default=1.0)
    unit_price  = db.Column(db.Float, default=0.0)
    buy_price   = db.Column(db.Float, default=0.0)
    total       = db.Column(db.Float, default=0.0)
    profit      = db.Column(db.Float, default=0.0)

    @property
    def display_name(self):
        if self.item_type == 'service': return self.description or 'خدمة'
        if self.product:                return self.product.name
        return self.description or '—'


class StockAdjustment(db.Model):
    __tablename__  = 'stock_adjustments'
    __table_args__ = (Index('ix_stockadj_date', 'adj_date'),)
    id         = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'))
    product    = db.relationship('Product')
    old_qty    = db.Column(db.Float, default=0.0)
    new_qty    = db.Column(db.Float, default=0.0)
    difference = db.Column(db.Float, default=0.0)
    reason     = db.Column(db.String(200), default='جرد')
    adj_date   = db.Column(db.DateTime, default=datetime.utcnow)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'))
    user       = db.relationship('User')
    session_id = db.Column(db.String(40), nullable=True)


class OilChange(db.Model):
    __tablename__  = 'oil_changes'
    __table_args__ = (
        Index('ix_oilchange_plate', 'car_plate'),
        Index('ix_oilchange_date',  'change_date'),
        Index('ix_oilchange_next',  'next_change_date'),
        Index('ix_oilchange_user',  'user_id'),
    )
    id               = db.Column(db.Integer, primary_key=True)
    customer_name    = db.Column(db.String(120), nullable=False)
    customer_phone   = db.Column(db.String(30), default='')
    car_plate        = db.Column(db.String(30), nullable=False)
    car_model        = db.Column(db.String(100), default='')
    product_id       = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    product          = db.relationship('Product')
    oil_type         = db.Column(db.String(100), default='')
    oil_qty          = db.Column(db.Float, default=0.0)
    km_at_change     = db.Column(db.Integer, default=0)
    km_interval      = db.Column(db.Integer, default=5000)
    next_change_km   = db.Column(db.Integer, default=0)
    change_date      = db.Column(db.Date, default=date.today)
    next_change_date = db.Column(db.Date, nullable=True)
    service_fee      = db.Column(db.Float, default=0.0)
    sale_id          = db.Column(db.Integer, db.ForeignKey('sales.id'), nullable=True)
    sale             = db.relationship('Sale', foreign_keys=[sale_id])
    notes            = db.Column(db.Text, default='')
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'))
    user             = db.relationship('User')


class AuditLog(db.Model):
    __tablename__  = 'audit_logs'
    __table_args__ = (
        Index('ix_audit_date',   'created_at'),
        Index('ix_audit_user',   'user_id'),
        Index('ix_audit_action', 'action'),
    )
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    user       = db.relationship('User')
    action     = db.Column(db.String(50), nullable=False)
    entity     = db.Column(db.String(50), default='')
    entity_id  = db.Column(db.Integer,   nullable=True)
    detail     = db.Column(db.Text,      default='')
    ip         = db.Column(db.String(45), default='')
    created_at = db.Column(db.DateTime,  default=datetime.utcnow)


class Settings(db.Model):
    __tablename__ = 'settings'
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default='')

    @staticmethod
    def get(key, default=''):
        s = Settings.query.filter_by(key=key).first()
        return s.value if s else default

    @staticmethod
    def set(key, value):
        s = Settings.query.filter_by(key=key).first()
        if s: s.value = str(value)
        else: db.session.add(Settings(key=key, value=str(value)))
        db.session.commit()


class Debt(db.Model):
    __tablename__  = 'debts'
    __table_args__ = (
        Index('ix_debt_customer', 'customer_name'),
        Index('ix_debt_date',     'debt_date'),
        Index('ix_debt_status',   'status'),
    )
    id             = db.Column(db.Integer, primary_key=True)
    customer_name  = db.Column(db.String(120), nullable=False)
    customer_phone = db.Column(db.String(30), default='')
    total_amount   = db.Column(db.Float, default=0.0)
    paid_amount    = db.Column(db.Float, default=0.0)
    description    = db.Column(db.Text, default='')
    debt_date      = db.Column(db.DateTime, default=datetime.utcnow)
    due_date       = db.Column(db.Date, nullable=True)
    status         = db.Column(db.String(20), default='unpaid')
    sale_id        = db.Column(db.Integer, db.ForeignKey('sales.id'), nullable=True)
    sale           = db.relationship('Sale')
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'))
    user           = db.relationship('User')
    notes          = db.Column(db.Text, default='')
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    payments       = db.relationship('DebtPayment', backref='debt', lazy=True, cascade='all, delete-orphan')

    @property
    def remaining(self): return max(0.0, self.total_amount - self.paid_amount)
    @property
    def is_paid(self): return self.remaining <= 0.01

    def update_status(self):
        if self.paid_amount <= 0:     self.status = 'unpaid'
        elif self.remaining <= 0.01:  self.status = 'paid'
        else:                         self.status = 'partial'


class License(db.Model):
    __tablename__ = 'license'
    id           = db.Column(db.Integer, primary_key=True, default=1)
    code         = db.Column(db.String(50),  nullable=False)
    device_id    = db.Column(db.String(100), nullable=False)
    server_token = db.Column(db.String(200), nullable=False)
    hash         = db.Column(db.String(100), nullable=False)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)


class DebtPayment(db.Model):
    __tablename__  = 'debt_payments'
    __table_args__ = (Index('ix_debtpay_debt', 'debt_id'),)
    id        = db.Column(db.Integer, primary_key=True)
    debt_id   = db.Column(db.Integer, db.ForeignKey('debts.id'))
    amount    = db.Column(db.Float, default=0.0)
    paid_date = db.Column(db.DateTime, default=datetime.utcnow)
    notes     = db.Column(db.String(200), default='')
    user_id   = db.Column(db.Integer, db.ForeignKey('users.id'))
    user      = db.relationship('User')


class RoadInvoice(db.Model):
    __tablename__  = 'road_invoices'
    __table_args__ = (
        Index('ix_roadinv_date',   'created_at'),
        Index('ix_roadinv_driver', 'driver_name'),
    )
    id            = db.Column(db.Integer, primary_key=True)
    invoice_no    = db.Column(db.String(30), unique=True, nullable=False)
    customer_name = db.Column(db.String(120), default='')
    driver_name   = db.Column(db.String(120), nullable=False)
    destination   = db.Column(db.String(200), nullable=False)
    notes         = db.Column(db.Text, default='')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'))
    user         = db.relationship('User')
    items        = db.relationship('RoadInvoiceItem', backref='road_invoice',
                                   lazy=True, cascade='all, delete-orphan')

    @staticmethod
    def generate_invoice_no():
        today  = date.today()
        prefix = 'RD' + today.strftime('%Y%m%d')
        last   = RoadInvoice.query.filter(
            RoadInvoice.invoice_no.like(f'{prefix}%')
        ).order_by(RoadInvoice.id.desc()).first()
        seq = 1
        if last and last.invoice_no[10:].isdigit():
            seq = int(last.invoice_no[10:]) + 1
        return f'{prefix}{seq:03d}'


class RoadInvoiceItem(db.Model):
    __tablename__  = 'road_invoice_items'
    __table_args__ = (Index('ix_roaditem_inv', 'road_invoice_id'),)
    id              = db.Column(db.Integer, primary_key=True)
    road_invoice_id = db.Column(db.Integer, db.ForeignKey('road_invoices.id'))
    product_id      = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    product         = db.relationship('Product')
    description     = db.Column(db.String(200), default='')
    quantity        = db.Column(db.Float, default=1.0)
    unit            = db.Column(db.String(30), default='قطعة')
    from_inventory  = db.Column(db.Boolean, default=True)

    @property
    def display_name(self):
        if self.product: return self.product.name
        return self.description or '—'


# ─────────────────────────────────────────────
# Activation / License helpers
# ─────────────────────────────────────────────

LICENSE_SERVER_URL = os.environ.get('LICENSE_SERVER_URL', 'https://serverclass4-app.hf.space')
LICENSE_SALT       = 'RUNX_OIL_2025_SECURE_XK9_SALT_BIND'


def _get_device_id():
    """
    Return a stable machine fingerprint.
    Combines ALL available components into one hash — never branches into
    two separate code paths that produce different results.
    This matches the Phone Shop system's proven stable approach.
    """
    _NO_WIN = subprocess.CREATE_NO_WINDOW if platform.system() == 'Windows' else 0
    components = []

    # Component 1: MAC address (always attempt)
    try:
        node = uuid.getnode()
        mac = ':'.join(['{:02x}'.format((node >> (e * 8)) & 0xff)
                        for e in range(6)][::-1])
        components.append(mac)
    except Exception:
        pass

    # Component 2: hostname (always attempt)
    try:
        components.append(platform.node())
    except Exception:
        pass

    # Component 3: Windows MachineGuid — ADD to mix if available, never rely on alone
    if platform.system() == 'Windows':
        try:
            r = subprocess.run(
                ['reg', 'query',
                 r'HKLM\SOFTWARE\Microsoft\Cryptography', '/v', 'MachineGuid'],
                capture_output=True, text=True, timeout=5, creationflags=_NO_WIN)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if 'MachineGuid' in line:
                        components.append(line.strip().split()[-1])
                        break
        except Exception:
            pass

    # Component 4: Disk serial number — ADD if available
    try:
        r = subprocess.run(
            ['wmic', 'diskdrive', 'get', 'SerialNumber'],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WIN)
        serials = [l.strip() for l in r.stdout.splitlines()
                   if l.strip() and 'SerialNumber' not in l]
        if serials:
            components.append(serials[0])
    except Exception:
        pass

    raw = '|'.join(components) if components else platform.platform()
    return hashlib.sha256(raw.encode()).hexdigest()


def _license_hash(code, device_id, token):
    raw = f'{code}|{device_id}|{token}|{LICENSE_SALT}'
    return hashlib.sha256(raw.encode()).hexdigest()


def _call_license_server(code, device_id):
    if _http is None:
        return {'success': False, 'message': 'مكتبة requests غير متوفرة'}
    try:
        r = _http.post(f'{LICENSE_SERVER_URL}/api/verify',
                       json={'code': code, 'device_id': device_id}, timeout=10)
        if r.status_code == 200:
            return r.json()
        return {'success': False, 'message': f'خطأ في الخادم: {r.status_code}'}
    except Exception as ex:
        n = type(ex).__name__
        if 'ConnectionError' in n:
            return {'success': False, 'message': 'تعذّر الاتصال بخادم التراخيص. تحقق من الإنترنت.'}
        if 'Timeout' in n:
            return {'success': False, 'message': 'انتهت مهلة الاتصال بخادم التراخيص.'}
        return {'success': False, 'message': f'خطأ: {str(ex)}'}


def _store_license(code, device_id, token):
    h   = _license_hash(code, device_id, token)
    lic = db.session.get(License, 1)
    if lic:
        lic.code = code; lic.device_id = device_id
        lic.server_token = token; lic.hash = h
        lic.activated_at = datetime.utcnow()
    else:
        db.session.add(License(id=1, code=code, device_id=device_id,
                               server_token=token, hash=h))
    db.session.commit()
    # Invalidate cache so next request re-verifies from fresh DB data
    _lic_cache['ok'] = None


# In-memory cache so we don't hit the DB on every single HTTP request
_lic_cache = {'ok': None, 'ts': 0.0}
_LIC_CACHE_TTL = 60  # seconds


def _verify_license():
    import time
    now = time.monotonic()
    # Return cached result if still fresh
    if _lic_cache['ok'] is not None and (now - _lic_cache['ts']) < _LIC_CACHE_TTL:
        return _lic_cache['ok']

    result = False
    try:
        lic = db.session.get(License, 1)
        if lic:
            did = _get_device_id()
            if lic.device_id == did:
                expected = _license_hash(lic.code, did, lic.server_token)
                result = secrets.compare_digest(str(lic.hash), expected)
    except Exception as e:
        print(f'[LICENSE] verify error: {e}')
        result = False

    _lic_cache['ok'] = result
    _lic_cache['ts'] = now
    return result


# ─────────────────────────────────────────────
# DB Migration helper
# ─────────────────────────────────────────────

def _migrate():
    db.create_all()
    with db.engine.connect() as conn:
        try:
            conn.execute(text('ALTER TABLE products ADD COLUMN barcode VARCHAR(100)'))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text('ALTER TABLE road_invoices ADD COLUMN customer_name VARCHAR(120) DEFAULT ""'))
            conn.commit()
        except Exception:
            pass


# ─────────────────────────────────────────────
# Print context helper
# ─────────────────────────────────────────────

def _print_ctx():
    """Common context variables for print templates."""
    return dict(
        shop_name      = Settings.get('shop_name', 'RUN X'),
        shop_phone     = Settings.get('phone', ''),
        shop_address   = Settings.get('address', ''),
        invoice_footer = Settings.get('invoice_footer', 'شكراً لزيارتكم'),
        currency       = Settings.get('currency', 'د.ل'),
        print_size     = Settings.get('print_size', '80mm'),
    )


# ─────────────────────────────────────────────
# SQL Aggregate Helpers
# ─────────────────────────────────────────────

def _safe_float(v):
    if v is None: return 0.0
    try:    return float(v)
    except: return 0.0


def _sale_agg(d_from, d_to, user_id=None):
    q = db.session.query(
        func.coalesce(func.sum(Sale.total),    0.0).label('revenue'),
        func.coalesce(func.sum(Sale.cost),     0.0).label('cost'),
        func.coalesce(func.sum(Sale.profit),   0.0).label('profit'),
        func.coalesce(func.sum(Sale.discount), 0.0).label('discount'),
        func.count(Sale.id).label('count'),
    ).filter(Sale.sale_date >= d_from, Sale.sale_date <= d_to)
    if user_id:
        q = q.filter(Sale.user_id == user_id)
    row = q.first()
    if not row:
        return 0.0, 0.0, 0.0, 0.0, 0
    return (_safe_float(row.revenue), _safe_float(row.cost),
            _safe_float(row.profit),  _safe_float(row.discount),
            int(row.count or 0))


def _top_products_sql(d_from, d_to, limit=10):
    rows = db.session.query(
        func.coalesce(Product.name, SaleItem.description).label('name'),
        func.sum(SaleItem.quantity).label('total_qty'),
        func.sum(SaleItem.profit).label('total_profit'),
        func.sum(SaleItem.total).label('total_revenue'),
    ).outerjoin(Product, Product.id == SaleItem.product_id
    ).join(Sale, Sale.id == SaleItem.sale_id
    ).filter(Sale.sale_date >= d_from, Sale.sale_date <= d_to
    ).group_by(func.coalesce(Product.name, SaleItem.description)
    ).order_by(func.sum(SaleItem.profit).desc()
    ).limit(limit).all()

    return [
        {
            'name':          r.name or '—',
            'total_qty':     _safe_float(r.total_qty),
            'total_profit':  _safe_float(r.total_profit),
            'total_revenue': _safe_float(r.total_revenue),
        }
        for r in rows
    ]


# ─────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────

@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


def audit(action, entity='', entity_id=None, detail=''):
    try:
        uid = current_user.id if current_user.is_authenticated else None
        ip  = request.remote_addr if request else ''
        db.session.add(AuditLog(user_id=uid, action=action,
            entity=entity, entity_id=entity_id,
            detail=str(detail)[:500], ip=ip))
    except Exception:
        pass


def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('هذه الصفحة للمديرين فقط', 'danger')
            return redirect(url_for('dashboard'))
        return f(*a, **kw)
    return dec


def _only_admin():
    return User.query.filter_by(role='admin', active=True).count() == 1


@app.context_processor
def inject_globals():
    low_stock_count = 0
    oil_due_count   = 0
    debt_count      = 0
    if current_user.is_authenticated:
        low_stock_count = Product.query.filter(
            Product.active==True, Product.quantity<=Product.alert_threshold).count()
        _today = date.today()
        oil_due_count = OilChange.query.filter(
            OilChange.next_change_date!=None,
            OilChange.next_change_date<=_today+timedelta(days=7),
            OilChange.next_change_date>=_today).count()
        debt_count = Debt.query.filter(Debt.status.in_(['unpaid','partial'])).count()
    return dict(
        shop_name      = Settings.get('shop_name', 'محل زيوت وكماليات'),
        currency       = Settings.get('currency', 'د.ل'),
        shop_phone     = Settings.get('phone', ''),
        shop_address   = Settings.get('address', ''),
        invoice_footer = Settings.get('invoice_footer', 'شكراً لزيارتكم'),
        low_stock_count= low_stock_count,
        oil_due_count  = oil_due_count,
        debt_count     = debt_count,
        settings_get   = Settings.get,
        timedelta      = timedelta,
    )


def _date_range():
    today = date.today()
    df = request.args.get('date_from', '')
    dt = request.args.get('date_to', '')
    try:    d_from = datetime.strptime(df, '%Y-%m-%d') if df else datetime(today.year, today.month, 1)
    except: d_from = datetime(today.year, today.month, 1)
    try:    d_to = (datetime.combine(datetime.strptime(dt,'%Y-%m-%d').date(), datetime.max.time())
                    if dt else datetime.combine(today, datetime.max.time()))
    except: d_to = datetime.combine(today, datetime.max.time())
    return d_from, d_to


def _build_oil_change_sale(customer_name, car_plate, oil_product_id, oil_qty,
                            service_fee, discount, notes, user_id):
    subtotal = cost = 0.0
    sale_items = []
    if oil_product_id and oil_qty > 0:
        prod = db.session.get(Product, oil_product_id)
        if prod:
            unit_price = prod.sell_price
            item_total = oil_qty * unit_price
            item_cost  = oil_qty * prod.buy_price
            subtotal  += item_total; cost += item_cost
            sale_items.append(SaleItem(
                product_id=prod.id, item_type='product', description=prod.name,
                quantity=oil_qty, unit_price=unit_price, buy_price=prod.buy_price,
                total=item_total, profit=item_total-item_cost))
            prod.quantity = max(0.0, prod.quantity - oil_qty)
    if service_fee > 0:
        subtotal += service_fee
        sale_items.append(SaleItem(
            product_id=None, item_type='service',
            description=f'أجرة تغيير زيت — {car_plate}',
            quantity=1.0, unit_price=service_fee, buy_price=0.0,
            total=service_fee, profit=service_fee))
    total  = max(0.0, subtotal - discount)
    profit = total - cost
    sale = Sale(invoice_no=Sale.generate_invoice_no(), customer_name=customer_name or 'زبون نقدي',
        subtotal=subtotal, discount=discount, total=total, cost=cost, profit=profit,
        notes=notes, sale_type='oil_change', user_id=user_id)
    db.session.add(sale); db.session.flush()
    for si in sale_items: si.sale_id=sale.id; db.session.add(si)
    return sale


# ─────────────────────────────────────────────
# Error handlers
# ─────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):    return render_template('404.html'), 404
@app.errorhandler(500)
def server_error(e): return render_template('500.html'), 500
@app.errorhandler(403)
def forbidden(e):    return render_template('403.html'), 403


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────

@app.route('/activate', methods=['GET', 'POST'])
def activate():
    if _verify_license():
        return redirect(url_for('dashboard') if current_user.is_authenticated else url_for('login'))
    error     = ''
    device_id = _get_device_id()
    device_id_short = device_id[:8].upper() + '-' + device_id[8:16].upper() + '-' + device_id[16:24].upper()
    if request.method == 'POST':
        code = request.form.get('activation_code', '').strip().upper().replace(' ', '')
        if not code:
            error = 'يرجى إدخال رمز التفعيل.'
        else:
            result = _call_license_server(code, device_id)
            if result.get('success'):
                _store_license(code, device_id, result.get('token', secrets.token_hex(16)))
                flash('✅ تم تفعيل النظام بنجاح! مرحباً بك.', 'success')
                return redirect(url_for('login'))
            else:
                error = result.get('message', 'رمز التفعيل غير صحيح أو منتهي الصلاحية.')
    return render_template('activate.html', error=error, device_id=device_id_short)


@app.route('/api/activate', methods=['POST'])
def api_activate():
    data   = request.get_json() or {}
    code   = data.get('code', '').strip().upper()
    if not code:
        return jsonify({'success': False, 'message': 'رمز التفعيل مطلوب'})
    did    = _get_device_id()
    result = _call_license_server(code, did)
    if result.get('success'):
        _store_license(code, did, result.get('token', secrets.token_hex(16)))
        return jsonify({'success': True, 'message': 'تم التفعيل بنجاح'})
    return jsonify({'success': False, 'message': result.get('message', 'فشل التفعيل')})


@app.route('/')
def index():
    return redirect(url_for('dashboard') if current_user.is_authenticated else url_for('login'))


@app.route('/login', methods=['GET','POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        if not username or not password:
            flash('يرجى إدخال اسم المستخدم وكلمة المرور', 'danger')
            return render_template('login.html')
        u = User.query.filter_by(username=username, active=True).first()
        if u and u.check_password(password):
            login_user(u, remember=True)
            audit('login', 'User', u.id, f'دخول من {request.remote_addr}')
            db.session.commit()
            flash(f'أهلاً {u.full_name or u.username}!', 'success')
            if u.must_change_pwd:
                flash('⚠️ يجب تغيير كلمة المرور الافتراضية', 'warning')
                return redirect(url_for('force_change_password'))
            return redirect(request.args.get('next') or url_for('dashboard'))
        audit('login_fail', 'User', detail=f'فشل: {username}')
        db.session.commit()
        flash('اسم المستخدم أو كلمة المرور غير صحيحة', 'danger')
    return render_template('login.html')


@app.route('/logout', methods=['GET','POST'])
@login_required
def logout():
    audit('logout', 'User', current_user.id)
    db.session.commit()
    logout_user()
    flash('تم تسجيل الخروج', 'info')
    return redirect(url_for('login'))


@app.route('/force-change-password', methods=['GET','POST'])
@login_required
def force_change_password():
    if not current_user.must_change_pwd:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        np = request.form.get('new_password','')
        cp = request.form.get('confirm_password','')
        if len(np) < 6:        flash('كلمة المرور يجب أن تكون 6 أحرف على الأقل', 'danger')
        elif np != cp:         flash('كلمتا المرور غير متطابقتين', 'danger')
        elif np == 'admin123': flash('لا يمكن استخدام كلمة المرور الافتراضية', 'danger')
        else:
            current_user.set_password(np)
            current_user.must_change_pwd = False
            audit('password_change', 'User', current_user.id, 'تغيير إجباري')
            db.session.commit()
            flash('✅ تم تغيير كلمة المرور', 'success')
            return redirect(url_for('dashboard'))
    return render_template('force_change_password.html')


@app.before_request
def check_activation():
    exempt = {'activate', 'api_activate', 'static'}
    if request.endpoint is None or request.endpoint in exempt:
        return
    if not _verify_license():
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'النظام غير مفعّل', 'require_activation': True}), 403
        return redirect(url_for('activate'))


@app.before_request
def check_must_change_pwd():
    allowed = {'login','logout','force_change_password','activate','api_activate','static'}
    if (current_user.is_authenticated
            and current_user.must_change_pwd
            and request.endpoint not in allowed):
        flash('⚠️ يجب تغيير كلمة المرور الافتراضية أولاً', 'warning')
        return redirect(url_for('force_change_password'))


# ─────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    today   = date.today()
    t_start = datetime.combine(today, datetime.min.time())
    t_end   = datetime.combine(today, datetime.max.time())
    m_start = datetime(today.year, today.month, 1)

    uid = None if current_user.is_admin else current_user.id
    today_rev, today_cost, today_profit, _, today_count = _sale_agg(t_start, t_end, uid)
    month_rev, _, month_profit, _, _                    = _sale_agg(m_start, datetime.combine(today, datetime.max.time()), uid)

    if not current_user.is_admin:
        today_profit = None
        month_profit = None

    low_stock = Product.query.options(joinedload(Product.category_obj)).filter(
        Product.active==True, Product.quantity<=Product.alert_threshold
    ).order_by(Product.quantity).limit(8).all()

    sq = Sale.query.options(joinedload(Sale.user))
    if not current_user.is_admin: sq = sq.filter_by(user_id=current_user.id)
    recent_sales = sq.order_by(Sale.sale_date.desc()).limit(8).all()

    recent_oil   = OilChange.query.order_by(OilChange.change_date.desc()).limit(5).all()
    upcoming_oil = OilChange.query.filter(
        OilChange.next_change_date!=None,
        OilChange.next_change_date<=today+timedelta(days=7),
        OilChange.next_change_date>=today
    ).order_by(OilChange.next_change_date).limit(8).all()
    overdue_oil = OilChange.query.filter(
        OilChange.next_change_date!=None,
        OilChange.next_change_date<today
    ).order_by(OilChange.next_change_date).limit(5).all()

    return render_template('dashboard.html',
        today_total=today_rev, today_profit=today_profit,
        today_count=today_count, month_total=month_rev,
        month_profit=month_profit, low_stock=low_stock,
        recent_sales=recent_sales, recent_oil=recent_oil,
        upcoming_oil=upcoming_oil, overdue_oil=overdue_oil, today=today)


# ─────────────────────────────────────────────
# Products
# ─────────────────────────────────────────────

@app.route('/products')
@login_required
def products_list():
    search=request.args.get('q',''); cat_id=request.args.get('cat',''); low=request.args.get('low','')
    q=Product.query.options(joinedload(Product.category_obj)).filter_by(active=True)
    if search: q=q.filter(Product.name.ilike(f'%{search}%'))
    if cat_id: q=q.filter_by(category_id=int(cat_id))
    if low:    q=q.filter(Product.quantity<=Product.alert_threshold)
    return render_template('products_list.html',
        products=q.order_by(Product.name).all(),
        categories=Category.query.order_by(Category.name).all(),
        search=search, cat_id=cat_id, low=low)


@app.route('/products/new', methods=['GET','POST'])
@login_required
@admin_required
def products_new():
    cats=Category.query.order_by(Category.name).all()
    if request.method=='POST':
        name=request.form.get('name','').strip()
        if not name: flash('اسم المنتج مطلوب','danger'); return render_template('products_form.html',categories=cats,product=None)
        cat_id=request.form.get('category_id'); nc=request.form.get('new_category','').strip()
        if nc:
            ex=Category.query.filter_by(name=nc).first()
            if not ex: ex=Category(name=nc); db.session.add(ex); db.session.flush()
            cat_id=ex.id
        barcode=request.form.get('barcode','').strip() or None
        p=Product(name=name, barcode=barcode,
            category_id=int(cat_id) if cat_id else None,
            buy_price=float(request.form.get('buy_price',0)),
            sell_price=float(request.form.get('sell_price',0)),
            quantity=float(request.form.get('quantity',0)),
            alert_threshold=float(request.form.get('alert_threshold',5)),
            unit=request.form.get('unit','قطعة'))
        db.session.add(p); db.session.flush()
        audit('product_create','Product',p.id,f'أضاف: {name}')
        db.session.commit(); flash('تم إضافة المنتج','success'); return redirect(url_for('products_list'))
    return render_template('products_form.html',categories=cats,product=None)


@app.route('/products/<int:pid>/edit', methods=['GET','POST'])
@login_required
@admin_required
def products_edit(pid):
    p=db.session.get(Product, pid)
    if not p: abort(404)
    cats=Category.query.order_by(Category.name).all()
    if request.method=='POST':
        ob=p.buy_price; os_=p.sell_price
        p.name=request.form.get('name','').strip()
        p.barcode=request.form.get('barcode','').strip() or None
        cat_id=request.form.get('category_id'); nc=request.form.get('new_category','').strip()
        if nc:
            ex=Category.query.filter_by(name=nc).first()
            if not ex: ex=Category(name=nc); db.session.add(ex); db.session.flush()
            cat_id=ex.id
        p.category_id=int(cat_id) if cat_id else None
        nb=float(request.form.get('buy_price',0)); ns=float(request.form.get('sell_price',0))
        p.buy_price=nb; p.sell_price=ns
        p.alert_threshold=float(request.form.get('alert_threshold',5))
        p.unit=request.form.get('unit','قطعة')
        detail=f'تعديل {p.name}'
        if ob!=nb: detail+=f' | شراء:{ob}→{nb}'
        if os_!=ns: detail+=f' | بيع:{os_}→{ns}'
        audit('price_change','Product',p.id,detail)
        db.session.commit(); flash('تم تحديث المنتج','success'); return redirect(url_for('products_list'))
    return render_template('products_form.html',categories=cats,product=p)


@app.route('/products/<int:pid>/delete', methods=['POST'])
@login_required
@admin_required
def products_delete(pid):
    p=db.session.get(Product, pid)
    if not p: abort(404)
    p.active=False
    audit('product_delete','Product',p.id,f'حذف: {p.name}')
    db.session.commit(); flash('تم حذف المنتج','success'); return redirect(url_for('products_list'))


# ─────────────────────────────────────────────
# Sales
# ─────────────────────────────────────────────

@app.route('/sales')
@login_required
def sales_list():
    page      = request.args.get('page',1,type=int)
    per_page  = 50
    search    = request.args.get('q','')
    date_from = request.args.get('date_from','')
    date_to   = request.args.get('date_to','')

    def _base_q():
        q = Sale.query
        if not current_user.is_admin: q = q.filter_by(user_id=current_user.id)
        if search:
            q = q.filter((Sale.invoice_no.ilike(f'%{search}%'))|(Sale.customer_name.ilike(f'%{search}%')))
        if date_from: q = q.filter(Sale.sale_date>=datetime.strptime(date_from,'%Y-%m-%d'))
        if date_to:
            dt = datetime.combine(datetime.strptime(date_to,'%Y-%m-%d').date(), datetime.max.time())
            q  = q.filter(Sale.sale_date<=dt)
        return q

    agg_q = db.session.query(
        func.coalesce(func.sum(Sale.total),   0.0).label('total'),
        func.coalesce(func.sum(Sale.profit),  0.0).label('profit'),
        func.coalesce(func.sum(Sale.discount),0.0).label('discount'),
        func.count(Sale.id).label('count'),
    )
    if not current_user.is_admin: agg_q = agg_q.filter(Sale.user_id==current_user.id)
    if search:
        agg_q = agg_q.filter((Sale.invoice_no.ilike(f'%{search}%'))|(Sale.customer_name.ilike(f'%{search}%')))
    if date_from: agg_q = agg_q.filter(Sale.sale_date>=datetime.strptime(date_from,'%Y-%m-%d'))
    if date_to:
        dt = datetime.combine(datetime.strptime(date_to,'%Y-%m-%d').date(), datetime.max.time())
        agg_q = agg_q.filter(Sale.sale_date<=dt)
    agg = agg_q.first()
    filter_total  = _safe_float(agg.total)  if agg else 0.0
    filter_profit = _safe_float(agg.profit) if agg and current_user.is_admin else None
    filter_count  = int(agg.count or 0)     if agg else 0

    sales = _base_q().options(joinedload(Sale.user)).order_by(
        Sale.sale_date.desc()).paginate(page=page, per_page=per_page)

    return render_template('sales_list.html',
        sales=sales, search=search, date_from=date_from, date_to=date_to,
        filter_total=filter_total, filter_profit=filter_profit, filter_count=filter_count)


@app.route('/sales/new', methods=['GET','POST'])
@login_required
def sales_new():
    products_dicts = [p.to_dict() for p in
        Product.query.filter_by(active=True).filter(Product.quantity>0).order_by(Product.name).all()]
    categories = Category.query.order_by(Category.name).all()

    if request.method=='POST':
        items_data=[]; i=0
        while True:
            pid=request.form.get(f'items[{i}][product_id]')
            if pid is None: break
            qty=float(request.form.get(f'items[{i}][quantity]',0) or 0)
            price=float(request.form.get(f'items[{i}][price]',0) or 0)
            if pid and qty>0: items_data.append({'product_id':int(pid),'quantity':qty,'price':price})
            i+=1
        if not items_data:
            flash('أضف منتجاً واحداً على الأقل','danger')
            return render_template('sales_new.html', products=products_dicts, categories=categories)
        customer=request.form.get('customer_name','زبون نقدي').strip() or 'زبون نقدي'
        discount=float(request.form.get('discount',0) or 0); notes=request.form.get('notes','')
        errors=[]
        for d in items_data:
            prod=db.session.get(Product, d['product_id'])
            if not prod: errors.append('منتج غير موجود')
            elif d['quantity']>prod.quantity:
                errors.append(f'الكمية ({d["quantity"]}) أكبر من المتوفر ({prod.quantity}) — {prod.name}')
        if errors:
            db.session.rollback()
            for e in errors: flash(e,'danger')
            return render_template('sales_new.html', products=products_dicts, categories=categories)
        subtotal=cost=0.0; sale_items=[]
        for d in items_data:
            prod=db.session.get(Product, d['product_id']); qty=d['quantity']
            unit_price=d['price'] if d['price']>0 else prod.sell_price
            item_total=qty*unit_price; item_cost=qty*prod.buy_price
            subtotal+=item_total; cost+=item_cost
            sale_items.append(SaleItem(product_id=prod.id,item_type='product',description=prod.name,
                quantity=qty,unit_price=unit_price,buy_price=prod.buy_price,
                total=item_total,profit=item_total-item_cost))
            prod.quantity-=qty
        total=max(0.0,subtotal-discount); profit=total-cost
        sale=Sale(invoice_no=Sale.generate_invoice_no(),customer_name=customer,
            subtotal=subtotal,discount=discount,total=total,cost=cost,profit=profit,
            notes=notes,sale_type='sale',user_id=current_user.id)
        db.session.add(sale); db.session.flush()
        for si in sale_items: si.sale_id=sale.id; db.session.add(si)
        audit('sale_create','Sale',sale.id,f'فاتورة {sale.invoice_no} — {total}')
        db.session.commit()
        flash(f'✅ تم حفظ الفاتورة رقم {sale.invoice_no}','success')
        # Check if print requested
        if request.form.get('save_and_print'):
            return redirect(url_for('print_invoice', sid=sale.id, autoprint='1'))
        return redirect(url_for('sales_detail',sid=sale.id))
    return render_template('sales_new.html', products=products_dicts, categories=categories)


@app.route('/sales/<int:sid>')
@login_required
def sales_detail(sid):
    sale=db.session.get(Sale, sid)
    if not sale: abort(404)
    if not current_user.is_admin and sale.user_id!=current_user.id:
        flash('غير مصرح لك','danger'); return redirect(url_for('sales_list'))
    oil_change=OilChange.query.filter_by(sale_id=sid).first()
    return render_template('sales_detail.html',sale=sale,oil_change=oil_change)


@app.route('/sales/<int:sid>/print')
@login_required
def print_invoice(sid):
    sale=db.session.get(Sale, sid)
    if not sale: abort(404)
    if not current_user.is_admin and sale.user_id!=current_user.id:
        abort(403)
    ctx = _print_ctx()
    return render_template('print_invoice.html', sale=sale, **ctx)


@app.route('/sales/<int:sid>/delete', methods=['POST'])
@login_required
@admin_required
def sales_delete(sid):
    sale=db.session.get(Sale, sid)
    if not sale: abort(404)
    for item in sale.items:
        if item.item_type=='product' and item.product:
            item.product.quantity+=item.quantity
    oc=OilChange.query.filter_by(sale_id=sid).first()
    if oc: oc.sale_id=None
    audit('sale_delete','Sale',sid,f'حذف فاتورة {sale.invoice_no}')
    db.session.delete(sale); db.session.commit()
    flash('تم حذف الفاتورة وإعادة المخزون','success')
    return redirect(url_for('sales_list'))


# ─────────────────────────────────────────────
# Profits
# ─────────────────────────────────────────────

@app.route('/profits')
@login_required
@admin_required
def profits():
    today   = date.today()
    d_from, d_to = _date_range()
    df_str  = request.args.get('date_from', today.strftime('%Y-%m-01'))
    dt_str  = request.args.get('date_to',   today.strftime('%Y-%m-%d'))

    t_start = datetime.combine(today, datetime.min.time())
    t_end   = datetime.combine(today, datetime.max.time())
    m_start = datetime(today.year, today.month, 1)

    today_rev, today_cost, today_profit, _, today_count = _sale_agg(t_start, t_end)
    month_rev, month_cost, month_profit, _, _           = _sale_agg(m_start, t_end)
    range_rev, range_cost, range_profit, range_disc, _  = _sale_agg(d_from, d_to)

    range_page  = request.args.get('rpage', 1, type=int)
    range_sales = Sale.query.options(joinedload(Sale.user)).filter(
        Sale.sale_date>=d_from, Sale.sale_date<=d_to
    ).order_by(Sale.sale_date.desc()).paginate(page=range_page, per_page=100)

    top_products = _top_products_sql(d_from, d_to, limit=10)

    lm_end   = today.replace(day=1) - timedelta(days=1)
    lm_start = lm_end.replace(day=1)

    return render_template('profits.html',
        today_revenue=today_rev,   today_cost=today_cost,
        today_profit=today_profit, today_count=today_count,
        month_revenue=month_rev,   month_cost=month_cost,
        month_profit=month_profit,
        range_sales=range_sales,
        range_revenue=range_rev,   range_cost=range_cost,
        range_profit=range_profit, range_discount=range_disc,
        top_products=top_products,
        df_str=df_str, dt_str=dt_str, today=today,
        today_str  = today.strftime('%Y-%m-%d'),
        month_start= today.strftime('%Y-%m-01'),
        year_start = today.strftime('%Y-01-01'),
        lm_start   = lm_start.strftime('%Y-%m-%d'),
        lm_end     = lm_end.strftime('%Y-%m-%d'),
    )


# ─────────────────────────────────────────────
# Inventory
# ─────────────────────────────────────────────

@app.route('/inventory')
@login_required
@admin_required
def inventory():
    search=request.args.get('q',''); cat_id=request.args.get('cat',''); filter_=request.args.get('filter','all')
    q=Product.query.options(joinedload(Product.category_obj)).filter_by(active=True)
    if search: q=q.filter(Product.name.ilike(f'%{search}%'))
    if cat_id: q=q.filter_by(category_id=int(cat_id))
    if filter_=='low':  q=q.filter(Product.quantity<=Product.alert_threshold)
    elif filter_=='zero': q=q.filter(Product.quantity==0)
    products=q.order_by(Product.name).all()

    agg = db.session.query(
        func.coalesce(func.sum(Product.quantity * Product.buy_price),  0.0).label('val_buy'),
        func.coalesce(func.sum(Product.quantity * Product.sell_price), 0.0).label('val_sell'),
        func.count(db.case((Product.quantity<=Product.alert_threshold, 1))).label('low_cnt'),
        func.count(db.case((Product.quantity==0, 1))).label('zero_cnt'),
    ).filter_by(active=True).first()

    return render_template('inventory.html',
        products=products, categories=Category.query.order_by(Category.name).all(),
        total_value=_safe_float(agg.val_buy),
        total_sell_val=_safe_float(agg.val_sell),
        total_profit_potential=_safe_float(agg.val_sell)-_safe_float(agg.val_buy),
        low_count=int(agg.low_cnt or 0), zero_count=int(agg.zero_cnt or 0),
        recent_adj=StockAdjustment.query.options(
            joinedload(StockAdjustment.product),
            joinedload(StockAdjustment.user)
        ).order_by(StockAdjustment.adj_date.desc()).limit(15).all(),
        search=search, cat_id=cat_id, filter_=filter_)


@app.route('/inventory/adjust', methods=['POST'])
@login_required
@admin_required
def inventory_adjust():
    pid=request.form.get('product_id',type=int); new_qty=request.form.get('new_qty',type=float)
    reason=request.form.get('reason','جرد').strip()
    prod=db.session.get(Product, pid)
    if not prod: abort(404)
    old_qty=prod.quantity; prod.quantity=new_qty
    db.session.add(StockAdjustment(product_id=pid,old_qty=old_qty,new_qty=new_qty,
        difference=new_qty-old_qty,reason=reason,user_id=current_user.id))
    audit('stock_adjust','Product',pid,f'{prod.name}: {old_qty}→{new_qty}')
    db.session.commit(); flash(f'تم تعديل كمية "{prod.name}"','success')
    return redirect(url_for('inventory'))


@app.route('/inventory/bulk-count', methods=['GET','POST'])
@login_required
@admin_required
def inventory_bulk_count():
    products=Product.query.options(joinedload(Product.category_obj)).filter_by(active=True).order_by(Product.name).all()
    if request.method=='POST':
        action=request.form.get('action','preview')
        if action=='preview':
            diffs=[]
            for p in products:
                val=request.form.get(f'qty_{p.id}','')
                if val=='': continue
                try: actual=float(val)
                except: continue
                diffs.append({'product_id':p.id,'name':p.name,'unit':p.unit,
                              'old_qty':p.quantity,'new_qty':actual,'diff':actual-p.quantity})
            return render_template('inventory_count_preview.html',diffs=diffs,products=products)
        elif action=='commit':
            import uuid; session_id=str(uuid.uuid4())[:8]; count=0; i=0
            while True:
                pid_str=request.form.get(f'items[{i}][product_id]')
                if pid_str is None: break
                prod=db.session.get(Product, int(pid_str))
                if prod:
                    old=prod.quantity; prod.quantity=float(request.form.get(f'items[{i}][new_qty]',0))
                    db.session.add(StockAdjustment(product_id=prod.id,old_qty=old,new_qty=prod.quantity,
                        difference=prod.quantity-old,reason='جرد فعلي',
                        user_id=current_user.id,session_id=session_id))
                    count+=1
                i+=1
            audit('bulk_count','StockAdjustment',detail=f'جلسة {session_id} — {count} منتج')
            db.session.commit(); flash(f'✅ تم اعتماد الجرد: {count} منتج','success')
            return redirect(url_for('inventory'))
    return render_template('inventory_bulk_count.html',products=products)


@app.route('/inventory/adjustments')
@login_required
@admin_required
def inventory_adjustments():
    page=request.args.get('page',1,type=int); search=request.args.get('q',''); session=request.args.get('session','')
    q=StockAdjustment.query.options(joinedload(StockAdjustment.product),joinedload(StockAdjustment.user))
    if search: q=q.join(Product).filter(Product.name.ilike(f'%{search}%'))
    if session: q=q.filter_by(session_id=session)
    return render_template('inventory_adjustments.html',
        adjs=q.order_by(StockAdjustment.adj_date.desc()).paginate(page=page,per_page=30),
        search=search, session=session)


# ─────────────────────────────────────────────
# Oil Change
# ─────────────────────────────────────────────

@app.route('/oil-change')
@login_required
def oil_change_list():
    page     = request.args.get('page', 1, type=int)
    per_page = 50
    search   = request.args.get('q','')
    date_from= request.args.get('date_from','')
    date_to  = request.args.get('date_to','')
    filter_  = request.args.get('filter','all')
    today    = date.today()

    q = OilChange.query.options(joinedload(OilChange.product))
    if search:
        q = q.filter((OilChange.customer_name.ilike(f'%{search}%'))
                     |(OilChange.car_plate.ilike(f'%{search}%'))
                     |(OilChange.customer_phone.ilike(f'%{search}%')))
    if date_from: q=q.filter(OilChange.change_date>=datetime.strptime(date_from,'%Y-%m-%d').date())
    if date_to:   q=q.filter(OilChange.change_date<=datetime.strptime(date_to,'%Y-%m-%d').date())
    if filter_=='upcoming':
        q=q.filter(OilChange.next_change_date!=None,
                   OilChange.next_change_date.between(today, today+timedelta(days=14)))
    elif filter_=='overdue':
        q=q.filter(OilChange.next_change_date!=None, OilChange.next_change_date<today)

    records = q.order_by(OilChange.change_date.desc()).paginate(page=page, per_page=per_page)

    upcoming = OilChange.query.filter(
        OilChange.next_change_date!=None,
        OilChange.next_change_date<=today+timedelta(days=14),
        OilChange.next_change_date>=today
    ).order_by(OilChange.next_change_date).limit(20).all()

    overdue = OilChange.query.filter(
        OilChange.next_change_date!=None,
        OilChange.next_change_date<today
    ).order_by(OilChange.next_change_date).limit(20).all()

    return render_template('oil_change_list.html',
        records=records, search=search, upcoming=upcoming,
        overdue=overdue, today=today, filter_=filter_,
        date_from=date_from, date_to=date_to, paginated=True)


@app.route('/oil-change/new', methods=['GET','POST'])
@login_required
def oil_change_new():
    plate=request.args.get('plate','').upper()
    oil_products=Product.query.filter_by(active=True).filter(Product.quantity>0).order_by(Product.name).all()
    last=OilChange.query.filter_by(car_plate=plate).order_by(OilChange.change_date.desc()).first() if plate else None

    if request.method=='POST':
        car_plate=request.form.get('car_plate','').strip().upper()
        ncd=request.form.get('next_change_date',''); product_id=request.form.get('product_id','') or None
        oil_qty=float(request.form.get('oil_qty',0) or 0); service_fee=float(request.form.get('service_fee',0) or 0)
        create_sale=request.form.get('create_sale')=='1'; km_now=int(request.form.get('km_at_change',0) or 0)
        km_interval=int(request.form.get('km_interval',Settings.get('default_km_interval','5000')) or 5000)
        customer=request.form.get('customer_name','').strip(); discount=float(request.form.get('discount',0) or 0)
        oil_type=request.form.get('oil_type','').strip()
        if product_id and oil_qty>0:
            prod=db.session.get(Product, int(product_id))
            if prod and oil_qty>prod.quantity:
                flash(f'الكمية ({oil_qty}) أكبر من المتوفر ({prod.quantity}) — {prod.name}','danger')
                return render_template('oil_change_form.html',record=None,last=last,plate=plate,today=date.today(),oil_products=oil_products)
        if product_id and not oil_type:
            prod=db.session.get(Product, int(product_id))
            if prod: oil_type=prod.name
        sale_obj=None
        if create_sale and (oil_qty>0 or service_fee>0):
            sale_notes=f'تغيير زيت — {car_plate}'
            if request.form.get('notes'): sale_notes+=f' — {request.form.get("notes")}'
            sale_obj=_build_oil_change_sale(customer or 'زبون نقدي',car_plate,
                int(product_id) if product_id else None,oil_qty,service_fee,discount,sale_notes,current_user.id)
            audit('sale_create','Sale',sale_obj.id,f'فاتورة تغيير زيت {car_plate}')
        else:
            if product_id and oil_qty>0:
                prod=db.session.get(Product, int(product_id))
                if prod: prod.quantity=max(0,prod.quantity-oil_qty)
        oc=OilChange(customer_name=customer,customer_phone=request.form.get('customer_phone','').strip(),
            car_plate=car_plate,car_model=request.form.get('car_model','').strip(),
            product_id=int(product_id) if product_id else None,oil_type=oil_type,oil_qty=oil_qty,
            km_at_change=km_now,km_interval=km_interval,next_change_km=km_now+km_interval,
            change_date=datetime.strptime(request.form.get('change_date'),'%Y-%m-%d').date(),
            next_change_date=datetime.strptime(ncd,'%Y-%m-%d').date() if ncd else None,
            service_fee=service_fee,sale_id=sale_obj.id if sale_obj else None,
            notes=request.form.get('notes',''),user_id=current_user.id)
        db.session.add(oc); db.session.flush()
        audit('oil_change_create','OilChange',oc.id,f'{car_plate}')
        db.session.commit()
        msg=f'✅ تم تسجيل تغيير الزيت للسيارة {car_plate}'
        if sale_obj: msg+=f' — فاتورة {sale_obj.invoice_no}'
        flash(msg,'success')
        if request.form.get('save_and_print'):
            return redirect(url_for('print_oil_card', oid=oc.id, autoprint='1'))
        return redirect(url_for('oil_change_detail',oid=oc.id))
    return render_template('oil_change_form.html',record=None,last=last,plate=plate,today=date.today(),oil_products=oil_products)


@app.route('/oil-change/<int:oid>')
@login_required
def oil_change_detail(oid):
    oc=db.session.get(OilChange, oid)
    if not oc: abort(404)
    history=OilChange.query.filter_by(car_plate=oc.car_plate).order_by(OilChange.change_date.desc()).all()
    return render_template('oil_change_detail.html',oc=oc,history=history,today=date.today())


@app.route('/oil-change/<int:oid>/print')
@login_required
def print_oil_card(oid):
    oc=db.session.get(OilChange, oid)
    if not oc: abort(404)
    ctx = _print_ctx()
    return render_template('print_oil_card.html', oc=oc, **ctx)


@app.route('/oil-change/<int:oid>/edit', methods=['GET','POST'])
@login_required
def oil_change_edit(oid):
    oc=db.session.get(OilChange, oid)
    if not oc: abort(404)
    oil_products=Product.query.filter_by(active=True).order_by(Product.name).all()
    if request.method=='POST':
        oc.customer_name=request.form.get('customer_name','').strip()
        oc.customer_phone=request.form.get('customer_phone','').strip()
        oc.car_plate=request.form.get('car_plate','').strip().upper()
        oc.car_model=request.form.get('car_model','').strip()
        oc.oil_type=request.form.get('oil_type','').strip()
        oc.oil_qty=float(request.form.get('oil_qty',0) or 0)
        oc.km_at_change=int(request.form.get('km_at_change',0) or 0)
        oc.km_interval=int(request.form.get('km_interval',5000) or 5000)
        oc.next_change_km=oc.km_at_change+oc.km_interval
        oc.service_fee=float(request.form.get('service_fee',0) or 0)
        oc.change_date=datetime.strptime(request.form.get('change_date'),'%Y-%m-%d').date()
        ncd=request.form.get('next_change_date','')
        oc.next_change_date=datetime.strptime(ncd,'%Y-%m-%d').date() if ncd else None
        oc.notes=request.form.get('notes','')
        audit('oil_change_edit','OilChange',oc.id,f'تعديل {oc.car_plate}')
        db.session.commit(); flash('تم تحديث السجل','success')
        return redirect(url_for('oil_change_detail',oid=oc.id))
    return render_template('oil_change_form.html',record=oc,last=None,plate='',today=date.today(),oil_products=oil_products)


@app.route('/oil-change/<int:oid>/delete', methods=['POST'])
@login_required
@admin_required
def oil_change_delete(oid):
    oc=db.session.get(OilChange, oid)
    if not oc: abort(404)
    audit('oil_change_delete','OilChange',oc.id,f'حذف {oc.car_plate}')
    db.session.delete(oc); db.session.commit()
    flash('تم حذف السجل','success'); return redirect(url_for('oil_change_list'))


@app.route('/oil-change/history/<path:plate>')
@login_required
def oil_change_history(plate):
    page    = request.args.get('page', 1, type=int)
    records = OilChange.query.filter_by(car_plate=plate.upper()).order_by(
        OilChange.change_date.desc()).paginate(page=page, per_page=50)
    return render_template('oil_change_list.html',
        records=records, search=plate, upcoming=[], overdue=[],
        today=date.today(), history_mode=True, plate=plate,
        filter_='all', date_from='', date_to='', paginated=True)


# ─────────────────────────────────────────────
# Audit Log
# ─────────────────────────────────────────────

@app.route('/audit')
@login_required
@admin_required
def audit_log():
    page=request.args.get('page',1,type=int); search=request.args.get('q',''); action_f=request.args.get('action','')
    q=AuditLog.query.options(joinedload(AuditLog.user))
    if search: q=q.join(User,isouter=True).filter((User.username.ilike(f'%{search}%'))|(AuditLog.detail.ilike(f'%{search}%')))
    if action_f: q=q.filter(AuditLog.action==action_f)
    logs=q.order_by(AuditLog.created_at.desc()).paginate(page=page,per_page=30)
    actions=db.session.query(AuditLog.action).distinct().all()
    return render_template('audit_log.html',logs=logs,search=search,action_f=action_f,
        actions=[a[0] for a in actions])


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────

@app.route('/settings', methods=['GET','POST'])
@login_required
@admin_required
def settings():
    if request.method=='POST':
        action=request.form.get('action','save_settings')
        if action=='change_password':
            cur=request.form.get('current_password',''); np=request.form.get('new_password',''); cp=request.form.get('confirm_password','')
            if not current_user.check_password(cur): flash('كلمة المرور الحالية غير صحيحة','danger')
            elif np!=cp: flash('كلمتا المرور غير متطابقتين','danger')
            elif len(np)<6: flash('يجب أن تكون 6 أحرف على الأقل','danger')
            else:
                current_user.set_password(np); current_user.must_change_pwd=False
                audit('password_change','User',current_user.id,'تغيير كلمة المرور')
                db.session.commit(); flash('تم تغيير كلمة المرور','success')
            return redirect(url_for('settings'))
        for k in ['shop_name','shop_tagline','currency','phone','address','invoice_footer',
                  'default_km_interval','shop_logo','print_size']:
            Settings.set(k, request.form.get(k,'').strip())
        audit('settings_update','Settings',detail='تحديث الإعدادات')
        flash('تم حفظ الإعدادات','success'); return redirect(url_for('settings'))
    data={k:Settings.get(k,d) for k,d in [
        ('shop_name','محل زيوت وكماليات'),('shop_tagline',''),('currency','د.ل'),('phone',''),
        ('address',''),('invoice_footer','شكراً لزيارتكم'),
        ('default_km_interval','5000'),('shop_logo',''),('print_size','80mm')]}
    return render_template('settings.html',data=data,users=User.query.order_by(User.username).all())


@app.route('/settings/users/new', methods=['POST'])
@login_required
@admin_required
def settings_users_new():
    username=request.form.get('username','').strip(); password=request.form.get('password','')
    if not username or not password: flash('اسم المستخدم وكلمة المرور مطلوبان','danger'); return redirect(url_for('settings'))
    if User.query.filter_by(username=username).first(): flash('اسم المستخدم موجود مسبقاً','danger'); return redirect(url_for('settings'))
    u=User(username=username,full_name=request.form.get('full_name','').strip(),
           role=request.form.get('role','seller'),must_change_pwd=True)
    u.set_password(password); db.session.add(u); db.session.flush()
    audit('user_create','User',u.id,f'أضاف مستخدم: {username}')
    db.session.commit()
    flash(f'تم إضافة المستخدم {username}','success'); return redirect(url_for('settings'))


@app.route('/settings/users/<int:uid>/edit', methods=['GET','POST'])
@login_required
@admin_required
def settings_users_edit(uid):
    u=db.session.get(User, uid)
    if not u: abort(404)
    if request.method=='POST':
        u.full_name=request.form.get('full_name','').strip()
        new_role=request.form.get('role','seller')
        if u.id==current_user.id:
            pass
        elif u.is_admin and new_role!='admin' and _only_admin():
            flash('لا يمكن تخفيض صلاحية المدير الوحيد','danger')
            return render_template('user_edit.html', u=u)
        else:
            u.role=new_role
        np=request.form.get('new_password','').strip(); cp=request.form.get('confirm_password','').strip()
        if np:
            if len(np)<6:
                flash('كلمة المرور يجب أن تكون 6 أحرف على الأقل','danger')
                return render_template('user_edit.html', u=u)
            if np!=cp:
                flash('كلمتا المرور غير متطابقتين','danger')
                return render_template('user_edit.html', u=u)
            u.set_password(np); u.must_change_pwd=False
        audit('user_edit','User',u.id,f'تعديل {u.username}')
        db.session.commit(); flash('تم تحديث المستخدم','success')
        return redirect(url_for('settings'))
    return render_template('user_edit.html', u=u)


@app.route('/settings/users/<int:uid>/toggle', methods=['POST'])
@login_required
@admin_required
def settings_users_toggle(uid):
    u=db.session.get(User, uid)
    if not u: abort(404)
    if u.id==current_user.id:
        flash('لا يمكن تعطيل حسابك الخاص','danger')
    elif u.is_admin and u.active and _only_admin():
        flash('لا يمكن تعطيل المدير الوحيد في النظام','danger')
    else:
        u.active=not u.active
        audit('user_toggle','User',u.id,f'{"تفعيل" if u.active else "تعطيل"} {u.username}')
        db.session.commit(); flash('تم تحديث حالة المستخدم','success')
    return redirect(url_for('settings'))


# ─────────────────────────────────────────────
# Debts (ديون)
# ─────────────────────────────────────────────

@app.route('/debts')
@login_required
def debts_list():
    page     = request.args.get('page', 1, type=int)
    search   = request.args.get('q', '')
    status_f = request.args.get('status', '')

    q = Debt.query.options(joinedload(Debt.user))
    if not current_user.is_admin: q = q.filter_by(user_id=current_user.id)
    if search:   q = q.filter((Debt.customer_name.ilike(f'%{search}%'))|(Debt.customer_phone.ilike(f'%{search}%')))
    if status_f: q = q.filter_by(status=status_f)

    agg_q = db.session.query(
        func.coalesce(func.sum(Debt.total_amount), 0.0).label('total'),
        func.coalesce(func.sum(Debt.paid_amount),  0.0).label('paid'),
        func.count(Debt.id).label('count'),
    )
    if not current_user.is_admin: agg_q = agg_q.filter(Debt.user_id==current_user.id)
    if search:   agg_q = agg_q.filter((Debt.customer_name.ilike(f'%{search}%'))|(Debt.customer_phone.ilike(f'%{search}%')))
    if status_f: agg_q = agg_q.filter(Debt.status==status_f)
    agg = agg_q.first()
    t = _safe_float(agg.total) if agg else 0.0
    p = _safe_float(agg.paid)  if agg else 0.0

    debts = q.order_by(Debt.debt_date.desc()).paginate(page=page, per_page=50)
    return render_template('debts_list.html',
        debts=debts, search=search, status_f=status_f,
        total_amount=t, total_paid=p, total_remaining=t-p,
        today=date.today(),
        debt_count_filtered=int(agg.count or 0) if agg else 0)


@app.route('/debts/new', methods=['GET','POST'])
@login_required
def debts_new():
    if request.method == 'POST':
        customer = request.form.get('customer_name','').strip()
        if not customer:
            flash('اسم الزبون مطلوب','danger')
            return render_template('debt_form.html', debt=None, today=date.today())
        amount = float(request.form.get('total_amount', 0) or 0)
        if amount <= 0:
            flash('يجب إدخال مبلغ الدين','danger')
            return render_template('debt_form.html', debt=None, today=date.today())
        ncd = request.form.get('due_date','')
        due_date = datetime.strptime(ncd,'%Y-%m-%d').date() if ncd else None
        debt = Debt(
            customer_name=customer,
            customer_phone=request.form.get('customer_phone','').strip(),
            total_amount=amount,
            paid_amount=0.0,
            description=request.form.get('description','').strip(),
            due_date=due_date,
            notes=request.form.get('notes','').strip(),
            user_id=current_user.id,
            status='unpaid'
        )
        db.session.add(debt); db.session.flush()
        audit('debt_create','Debt',debt.id,f'دين جديد: {customer} — {amount}')
        db.session.commit()
        flash(f'✅ تم تسجيل الدين للزبون {customer}','success')
        return redirect(url_for('debt_detail', did=debt.id))
    return render_template('debt_form.html', debt=None, today=date.today())


@app.route('/debts/<int:did>')
@login_required
def debt_detail(did):
    debt = db.session.get(Debt, did)
    if not debt: abort(404)
    if not current_user.is_admin and debt.user_id != current_user.id: abort(403)
    return render_template('debt_detail.html', debt=debt, today=date.today())


@app.route('/debts/<int:did>/pay', methods=['POST'])
@login_required
def debt_pay(did):
    debt = db.session.get(Debt, did)
    if not debt: abort(404)
    if not current_user.is_admin and debt.user_id != current_user.id: abort(403)
    amount = float(request.form.get('amount', 0) or 0)
    if amount <= 0:
        flash('يجب إدخال مبلغ السداد','danger')
        return redirect(url_for('debt_detail', did=did))
    amount = min(amount, debt.remaining)
    if amount <= 0:
        flash('الدين مسدد بالكامل مسبقاً','info')
        return redirect(url_for('debt_detail', did=did))
    db.session.add(DebtPayment(debt_id=debt.id, amount=amount,
        notes=request.form.get('notes','').strip(), user_id=current_user.id))
    debt.paid_amount = min(debt.total_amount, debt.paid_amount + amount)
    debt.update_status()
    audit('debt_payment','Debt',debt.id,f'سداد {amount} — {debt.customer_name}')
    db.session.commit()
    cur = Settings.get('currency','د.ل')
    flash(f'✅ تم تسجيل دفعة {amount:.2f} {cur}','success')
    return redirect(url_for('debt_detail', did=did))


@app.route('/debts/<int:did>/print')
@login_required
def debt_print(did):
    debt = db.session.get(Debt, did)
    if not debt: abort(404)
    if not current_user.is_admin and debt.user_id != current_user.id: abort(403)
    ctx = _print_ctx()
    return render_template('print_debt.html', debt=debt, **ctx)


@app.route('/debts/<int:did>/delete', methods=['POST'])
@login_required
@admin_required
def debt_delete(did):
    debt = db.session.get(Debt, did)
    if not debt: abort(404)
    audit('debt_delete','Debt',did,f'حذف دين: {debt.customer_name}')
    db.session.delete(debt); db.session.commit()
    flash('تم حذف الدين','success')
    return redirect(url_for('debts_list'))


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────

@app.route('/api/products/all')
@login_required
def api_products_all():
    products=Product.query.filter_by(active=True).filter(Product.quantity>0).order_by(Product.name).all()
    return jsonify([p.to_dict() for p in products])


@app.route('/api/oil-change/lookup')
@login_required
def api_oil_lookup():
    q=request.args.get('q','').strip().upper()
    if not q: return jsonify(None)
    last=OilChange.query.filter(
        (OilChange.car_plate.ilike(f'%{q}%'))|(OilChange.customer_name.ilike(f'%{q}%'))
    ).order_by(OilChange.change_date.desc()).first()
    if not last: return jsonify(None)
    return jsonify({'customer_name':last.customer_name,'customer_phone':last.customer_phone,
        'car_plate':last.car_plate,'car_model':last.car_model,'oil_type':last.oil_type,
        'oil_qty':last.oil_qty,'product_id':last.product_id,'km_interval':last.km_interval,
        'last_km':last.km_at_change,'last_date':last.change_date.strftime('%Y-%m-%d')})


# ─────────────────────────────────────────────
# Road Invoice (فاتورة طريق)
# ─────────────────────────────────────────────

@app.route('/road-invoice')
@login_required
def road_invoice_list():
    page   = request.args.get('page', 1, type=int)
    search = request.args.get('q', '')
    q = RoadInvoice.query.options(joinedload(RoadInvoice.user))
    if search:
        q = q.filter(
            (RoadInvoice.driver_name.ilike(f'%{search}%')) |
            (RoadInvoice.destination.ilike(f'%{search}%')) |
            (RoadInvoice.invoice_no.ilike(f'%{search}%'))
        )
    records = q.order_by(RoadInvoice.created_at.desc()).paginate(page=page, per_page=50)
    return render_template('road_invoice_list.html', records=records, search=search)


@app.route('/road-invoice/new', methods=['GET', 'POST'])
@login_required
def road_invoice_new():
    products = Product.query.filter_by(active=True).filter(
        Product.quantity > 0).order_by(Product.name).all()

    if request.method == 'POST':
        customer_name = request.form.get('customer_name', '').strip()
        driver_name   = request.form.get('driver_name', '').strip()
        destination   = request.form.get('destination', '').strip()
        notes         = request.form.get('notes', '').strip()

        if not driver_name:
            flash('اسم السائق مطلوب', 'danger')
            return render_template('road_invoice_form.html', products=products)
        if not destination:
            flash('الوجهة مطلوبة', 'danger')
            return render_template('road_invoice_form.html', products=products)

        # Parse items
        items_data = []
        i = 0
        while True:
            src = request.form.get(f'items[{i}][source]')
            if src is None:
                break
            qty_raw = request.form.get(f'items[{i}][quantity]', '1')
            try:
                qty = float(qty_raw) if qty_raw else 1.0
            except ValueError:
                qty = 1.0
            if qty <= 0:
                i += 1
                continue

            if src == 'inventory':
                pid = request.form.get(f'items[{i}][product_id]', '')
                if pid:
                    items_data.append({
                        'source': 'inventory',
                        'product_id': int(pid),
                        'quantity': qty,
                    })
            else:
                desc = request.form.get(f'items[{i}][description]', '').strip()
                unit = request.form.get(f'items[{i}][unit]', 'قطعة').strip()
                if desc:
                    items_data.append({
                        'source': 'manual',
                        'description': desc,
                        'unit': unit,
                        'quantity': qty,
                    })
            i += 1

        if not items_data:
            flash('أضف صنفاً واحداً على الأقل', 'danger')
            return render_template('road_invoice_form.html', products=products)

        # Validate inventory stock
        errors = []
        for d in items_data:
            if d['source'] == 'inventory':
                prod = db.session.get(Product, d['product_id'])
                if not prod:
                    errors.append('منتج غير موجود')
                elif d['quantity'] > prod.quantity:
                    errors.append(
                        f'الكمية ({d["quantity"]}) أكبر من المتوفر ({prod.quantity}) — {prod.name}')
        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('road_invoice_form.html', products=products)

        # Create road invoice
        inv = RoadInvoice(
            invoice_no    = RoadInvoice.generate_invoice_no(),
            customer_name = customer_name,
            driver_name   = driver_name,
            destination   = destination,
            notes         = notes,
            user_id       = current_user.id,
        )
        db.session.add(inv)
        db.session.flush()

        for d in items_data:
            if d['source'] == 'inventory':
                prod = db.session.get(Product, d['product_id'])
                prod.quantity = max(0.0, prod.quantity - d['quantity'])
                item = RoadInvoiceItem(
                    road_invoice_id = inv.id,
                    product_id      = prod.id,
                    description     = prod.name,
                    quantity        = d['quantity'],
                    unit            = prod.unit,
                    from_inventory  = True,
                )
            else:
                item = RoadInvoiceItem(
                    road_invoice_id = inv.id,
                    product_id      = None,
                    description     = d['description'],
                    quantity        = d['quantity'],
                    unit            = d.get('unit', 'قطعة'),
                    from_inventory  = False,
                )
            db.session.add(item)

        audit('road_invoice_create', 'RoadInvoice', inv.id,
              f'فاتورة طريق {inv.invoice_no} — {destination}')
        db.session.commit()
        flash(f'✅ تم إنشاء فاتورة الطريق {inv.invoice_no}', 'success')

        if request.form.get('save_and_print'):
            return redirect(url_for('road_invoice_print', rid=inv.id, autoprint='1'))
        return redirect(url_for('road_invoice_detail', rid=inv.id))

    return render_template('road_invoice_form.html', products=products)


@app.route('/road-invoice/<int:rid>')
@login_required
def road_invoice_detail(rid):
    inv = db.session.get(RoadInvoice, rid)
    if not inv:
        abort(404)
    return render_template('road_invoice_detail.html', inv=inv)


@app.route('/road-invoice/<int:rid>/print')
@login_required
def road_invoice_print(rid):
    inv = db.session.get(RoadInvoice, rid)
    if not inv:
        abort(404)
    ctx = _print_ctx()
    autoprint = request.args.get('autoprint', '0')
    return render_template('print_road_invoice.html', inv=inv, autoprint=autoprint, **ctx)


@app.route('/road-invoice/<int:rid>/delete', methods=['POST'])
@login_required
@admin_required
def road_invoice_delete(rid):
    inv = db.session.get(RoadInvoice, rid)
    if not inv:
        abort(404)
    # Restore inventory quantities
    for item in inv.items:
        if item.from_inventory and item.product:
            item.product.quantity += item.quantity
    audit('road_invoice_delete', 'RoadInvoice', rid,
          f'حذف فاتورة طريق {inv.invoice_no}')
    db.session.delete(inv)
    db.session.commit()
    flash('تم حذف فاتورة الطريق وإعادة المخزون', 'success')
    return redirect(url_for('road_invoice_list'))


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

@app.cli.command('init-db')
def init_db_cmd():
    from init_db import init
    init()


if __name__=='__main__':
    with app.app_context():
        db.create_all()
        _migrate()
    app.run(debug=True, port=5000)