# MathML Forge — Setup Guide

## Stack
- **Python / Flask** — web server, auth, DB
- **SQLite** — user accounts + conversion history
- **Node.js** — `convert.js` (mathjax-node-sre + sharp)

---

## 1. Python Setup

```bash
cd mathml_app
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. Node.js Setup

Replace the stub `convert.js` with your full script, then:

```bash
npm init -y
npm install mathjax-node-sre sharp jsdom
```

---

## 3. Place your Node.js script

Copy your full `convert.js` (the one with mathjax-node-sre) into:
```
mathml_app/convert.js
```

The Flask app calls:
```
node convert.js <output_name>
```
with MathML piped on **stdin** and expects JSON on **stdout**:
```json
{
  "success": true,
  "files": { "svg": "name.svg", "png": "name.png", "txt": "name.txt" },
  "altText": "..."
}
```

---

## 4. Run

```bash
python app.py
```

Open **http://localhost:5000** — register, log in, convert!

---

## File layout

```
mathml_app/
├── app.py               # Flask app
├── convert.js           # YOUR Node.js script
├── requirements.txt
├── outputs/             # ZIP files stored here
└── templates/
    ├── base.html
    ├── login.html
    ├── register.html
    ├── dashboard.html
    ├── convert_single.html
    ├── convert_multiple.html
    └── history.html
```

---

## Routes

| Route | Description |
|-------|-------------|
| `GET /login` | Login page |
| `GET /register` | Register page |
| `GET /logout` | Logout |
| `GET /dashboard` | Home dashboard |
| `GET/POST /convert/single` | Single MathML conversion |
| `GET /convert/multiple` | Batch conversion page |
| `POST /convert/multiple` | Batch conversion API (JSON body) |
| `GET /download/<id>` | Download ZIP |
| `GET /history` | Conversion history |
| `POST /history/delete/<id>` | Delete a record |
