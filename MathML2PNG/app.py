from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, send_file, Response)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os, subprocess, json, zipfile, shutil, tempfile, uuid, re, base64
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# App & Config
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Secret key — always read from env in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-only-change-in-prod')

# PostgreSQL on Render, SQLite locally
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///mathml.db')
if _db_url.startswith('postgres://'):          # Render still returns old scheme
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI']      = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Upload / ZIP storage — /tmp works on Render (ephemeral is fine for ZIPs)
_outputs = os.environ.get('OUTPUTS_DIR', os.path.join(tempfile.gettempdir(), 'mathml_outputs'))
os.makedirs(_outputs, exist_ok=True)
app.config['UPLOAD_FOLDER']       = _outputs
app.config['MAX_CONTENT_LENGTH']  = 16 * 1024 * 1024   # 16 MB

db = SQLAlchemy(app)

# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    conversions   = db.relationship('Conversion', backref='user', lazy=True,
                                    cascade='all, delete-orphan')

    def set_password(self, p):   self.password_hash = generate_password_hash(p)
    def check_password(self, p): return check_password_hash(self.password_hash, p)


class Conversion(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_id = db.Column(db.String(64),  nullable=False)
    label      = db.Column(db.String(200))
    mode       = db.Column(db.String(20))           # 'single' | 'multiple'
    item_count = db.Column(db.Integer, default=1)
    zip_path   = db.Column(db.String(500))
    svg_data   = db.Column(db.Text)                 # first SVG stored inline
    png_b64    = db.Column(db.Text)                 # first PNG as base64
    alt_texts  = db.Column(db.Text)                 # JSON list of alt strings
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Jinja filter + auth helper
# ─────────────────────────────────────────────────────────────────────────────

@app.template_filter('from_json')
def from_json_filter(v):
    try:    return json.loads(v) if v else []
    except: return []


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
# Node.js conversion helper
# ─────────────────────────────────────────────────────────────────────────────

NODE_BIN    = os.environ.get('NODE_BIN', 'node')
NODE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'convert.js')


def run_node_conversion(mathml_str: str, output_name: str, work_dir: str) -> dict:
    """Pipe MathML to convert.js; return parsed JSON result."""
    try:
        proc = subprocess.run(
            [NODE_BIN, NODE_SCRIPT, output_name],
            input=mathml_str,
            capture_output=True, text=True,
            cwd=work_dir, timeout=120,
        )
        stdout = proc.stdout.strip()
        if not stdout:
            return {'success': False,
                    'error': f'No output from node. stderr: {proc.stderr[-600:]}'}
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'Conversion timed out (120 s)'}
    except json.JSONDecodeError as e:
        return {'success': False, 'error': f'JSON parse error: {e}'}
    except FileNotFoundError:
        return {'success': False, 'error': 'node not found — is Node.js installed?'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def build_zip(work_dir: str, zip_path: str, files: list) -> None:
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            full = os.path.join(work_dir, f)
            if os.path.exists(full):
                zf.write(full, arcname=f)


def read_file_text(work_dir: str, filename: str) -> str:
    p = os.path.join(work_dir, filename)
    try:
        with open(p, 'r', encoding='utf-8') as fh:
            return fh.read()
    except Exception:
        return ''


def read_file_b64(work_dir: str, filename: str) -> str:
    p = os.path.join(work_dir, filename)
    try:
        with open(p, 'rb') as fh:
            data = fh.read()
        if data[:4] == b'\x89PNG':
            return base64.b64encode(data).decode('utf-8')
    except Exception:
        pass
    return ''


def extract_mathml_blocks(content: str) -> list:
    """Return list of {name, mathml} dicts from HTML/XML content."""
    pattern = re.compile(r'(<math[\s\S]*?</math>)', re.IGNORECASE)
    items = []
    for idx, m in enumerate(pattern.findall(content)):
        id_match = re.search(r'\bid\s*=\s*["\']([^"\']+)["\']', m, re.IGNORECASE)
        name = id_match.group(1) if id_match else f'math_{idx + 1}'
        if 'xmlns' not in m:
            m = m.replace('<math', '<math xmlns="http://www.w3.org/1998/Math/MathML"', 1)
        items.append({'name': name, 'mathml': m})
    return items


# ─────────────────────────────────────────────────────────────────────────────
# Routes — auth
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        u = User.query.filter_by(username=username).first()
        if u and u.check_password(password):
            session['user_id']  = u.id
            session['username'] = u.username
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Both fields are required.', 'danger')
            return render_template('register.html')
        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'danger')
            return render_template('register.html')
        u = User(username=username)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash('Account created! Please sign in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ─────────────────────────────────────────────────────────────────────────────
# Routes — main
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    total  = Conversion.query.filter_by(user_id=session['user_id']).count()
    recent = (Conversion.query
              .filter_by(user_id=session['user_id'])
              .order_by(Conversion.created_at.desc())
              .limit(3).all())
    return render_template('dashboard.html',
                           username=session['username'],
                           total=total, recent=recent)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — conversions
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/convert/single', methods=['GET', 'POST'])
@login_required
def convert_single():
    if request.method == 'GET':
        return render_template('convert_single.html')

    mathml = request.form.get('mathml', '').strip()
    label  = request.form.get('label', 'Untitled').strip() or 'Untitled'
    if not mathml:
        return jsonify({'success': False, 'error': 'No MathML provided'}), 400

    sid      = str(uuid.uuid4())
    work_dir = tempfile.mkdtemp(prefix='mathml_')
    name     = 'output'

    result = run_node_conversion(mathml, name, work_dir)
    if not result.get('success'):
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify(result), 500

    base    = result.get('baseFileName', name)
    svg_txt = read_file_text(work_dir, f'{base}.svg')
    png_b64 = read_file_b64(work_dir,  f'{base}.png')

    zip_dest = os.path.join(app.config['UPLOAD_FOLDER'], f'{sid}.zip')
    build_zip(work_dir, zip_dest, list(result['files'].values()))
    shutil.rmtree(work_dir, ignore_errors=True)

    conv = Conversion(
        user_id=session['user_id'], session_id=sid, label=label,
        mode='single', item_count=1, zip_path=zip_dest,
        svg_data=svg_txt, png_b64=png_b64,
        alt_texts=json.dumps([result.get('altText', '')]),
    )
    db.session.add(conv)
    db.session.commit()

    return jsonify({
        'success': True,
        'conv_id': conv.id,
        'altText': result.get('altText', ''),
        'svgData': svg_txt,
        'pngB64':  png_b64,
    })


@app.route('/convert/multiple', methods=['GET', 'POST'])
@login_required
def convert_multiple():
    if request.method == 'GET':
        return render_template('convert_multiple.html')

    if request.is_json:
        data  = request.get_json(force=True)
        items = data.get('items', [])
        label = data.get('label', 'Batch').strip() or 'Batch'
    else:
        label   = request.form.get('label', 'Batch').strip() or 'Batch'
        xmlfile = request.files.get('xmlfile')
        if not xmlfile:
            return jsonify({'success': False, 'error': 'No file uploaded'}), 400
        content = xmlfile.read().decode('utf-8', errors='replace')
        items   = extract_mathml_blocks(content)
        if not items:
            return jsonify({'success': False,
                            'error': 'No <math> elements found in file'}), 400

    if not items:
        return jsonify({'success': False, 'error': 'No items provided'}), 400

    sid      = str(uuid.uuid4())
    work_dir = tempfile.mkdtemp(prefix='mathml_batch_')
    all_files, alt_texts, errors, results_detail = [], [], [], []
    first_svg = ''

    for idx, item in enumerate(items):
        mathml = item.get('mathml', '').strip()
        name   = (item.get('name', '') or f'math_{idx+1}').strip().replace(' ', '_') \
                 or f'math_{idx+1}'
        if not mathml:
            errors.append(f'Item {idx+1}: empty MathML')
            results_detail.append({'name': name, 'ok': False, 'error': 'Empty MathML'})
            continue

        res = run_node_conversion(mathml, name, work_dir)
        if res.get('success'):
            base = res.get('baseFileName', name)
            svg  = read_file_text(work_dir, f'{base}.svg')
            if not first_svg:
                first_svg = svg
            all_files += list(res['files'].values())
            alt = res.get('altText', '')
            alt_texts.append(alt)
            results_detail.append({'name': name, 'ok': True,
                                   'altText': alt, 'svgData': svg})
        else:
            err = res.get('error', 'unknown')
            errors.append(f'Item {idx+1} ({name}): {err}')
            results_detail.append({'name': name, 'ok': False, 'error': err})

    zip_dest = os.path.join(app.config['UPLOAD_FOLDER'], f'{sid}.zip')
    build_zip(work_dir, zip_dest, all_files)
    shutil.rmtree(work_dir, ignore_errors=True)

    conv = Conversion(
        user_id=session['user_id'], session_id=sid, label=label,
        mode='multiple', item_count=len(items), zip_path=zip_dest,
        svg_data=first_svg, alt_texts=json.dumps(alt_texts),
    )
    db.session.add(conv)
    db.session.commit()

    return jsonify({
        'success':  True,
        'conv_id':  conv.id,
        'altTexts': alt_texts,
        'errors':   errors,
        'results':  results_detail,
    })


@app.route('/parse-xml', methods=['POST'])
@login_required
def parse_xml():
    f = request.files.get('xmlfile')
    if not f:
        return jsonify({'success': False, 'error': 'No file'}), 400
    content = f.read().decode('utf-8', errors='replace')
    items   = extract_mathml_blocks(content)
    return jsonify({'success': True, 'items': items, 'count': len(items)})


# ─────────────────────────────────────────────────────────────────────────────
# Routes — download / history / preview
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/download/<int:conv_id>')
@login_required
def download(conv_id):
    conv = db.session.get(Conversion, conv_id)
    if not conv or conv.user_id != session['user_id']:
        flash('Not found.', 'danger')
        return redirect(url_for('history'))
    if not conv.zip_path or not os.path.exists(conv.zip_path):
        flash('File no longer available.', 'danger')
        return redirect(url_for('history'))
    safe_name = re.sub(r'[^\w\-]', '_', conv.label or 'conversion')
    return send_file(conv.zip_path,
                     as_attachment=True,
                     download_name=f'{safe_name}.zip',
                     mimetype='application/zip')


@app.route('/history')
@login_required
def history():
    convs = (Conversion.query
             .filter_by(user_id=session['user_id'])
             .order_by(Conversion.created_at.desc())
             .all())
    return render_template('history.html', conversions=convs)


@app.route('/history/delete/<int:conv_id>', methods=['POST'])
@login_required
def delete_conversion(conv_id):
    conv = db.session.get(Conversion, conv_id)
    if not conv or conv.user_id != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    try:
        if conv.zip_path and os.path.exists(conv.zip_path):
            os.remove(conv.zip_path)
        db.session.delete(conv)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/preview-svg/<int:conv_id>')
@login_required
def preview_svg(conv_id):
    """Return the stored SVG directly as image/svg+xml for inline display."""
    conv = db.session.get(Conversion, conv_id)
    if not conv or conv.user_id != session['user_id']:
        return 'Not found', 404
    if not conv.svg_data:
        return 'No SVG data', 404
    return Response(conv.svg_data, mimetype='image/svg+xml')


# ─────────────────────────────────────────────────────────────────────────────
# Health check (Render pings this)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
