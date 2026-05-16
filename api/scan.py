from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.error
from html.parser import HTMLParser

# ── HTML Parser ───────────────────────────────────────────────
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta_desc = ""
        self.headings = []
        self.json_ld = []
        self.word_count = 0
        self._in_title = False
        self._in_body = False
        self._capture_json = False
        self._json_buf = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and attrs.get("name", "").lower() == "description":
            self.meta_desc = attrs.get("content", "")
        if tag in ("h1", "h2", "h3"):
            self.headings.append({"tag": tag, "text": ""})
            self._current_heading = tag
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self._capture_json = True
            self._json_buf = ""
        if tag == "body":
            self._in_body = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._capture_json:
            self._capture_json = False
            try:
                self.json_ld.append(json.loads(self._json_buf))
            except Exception:
                pass

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._capture_json:
            self._json_buf += data
        if self.headings and data.strip():
            self.headings[-1]["text"] += data.strip() + " "
        if self._in_body:
            self.word_count += len(data.split())

# ── Checks ────────────────────────────────────────────────────
QUESTION_WORDS = ("who", "what", "when", "where", "why", "how", "which", "can", "do", "does", "is", "are")
SPECIFICITY_KEYWORDS = (
    "specializ", "focus", "expert", "niche", "only", "exclusively",
    "years experience", "certified", "accredited", "serving"
)

def check_faq_schema(json_ld):
    for block in json_ld:
        t = block.get("@type", "")
        if isinstance(t, str) and "FAQ" in t:
            return True
        if isinstance(t, list) and any("FAQ" in x for x in t):
            return True
    return False

def check_local_business_schema(json_ld):
    for block in json_ld:
        t = block.get("@type", "")
        types = [t] if isinstance(t, str) else t
        if any(x in ("LocalBusiness", "ProfessionalService", "FinancialService", "MedicalBusiness") for x in types):
            return True
    return False

def check_entity_definition(headings, meta_desc):
    # H1 or meta desc should contain who/what/where signal
    h1s = [h["text"].lower() for h in headings if h["tag"] == "h1"]
    combined = " ".join(h1s) + " " + meta_desc.lower()
    has_location = any(w in combined for w in (" in ", " based in ", " serving ", " london", " new york", " chicago"))
    has_role = any(w in combined for w in ("advisor", "consultant", "broker", "coach", "planner", "specialist", "therapist", "accountant"))
    return has_location and has_role

def check_specificity(headings, meta_desc):
    text = " ".join(h["text"].lower() for h in headings) + " " + meta_desc.lower()
    return any(kw in text for kw in SPECIFICITY_KEYWORDS)

def check_question_headings(headings):
    h2h3 = [h["text"].lower().strip() for h in headings if h["tag"] in ("h2", "h3")]
    return any(h.startswith(QUESTION_WORDS) or h.endswith("?") for h in h2h3)

def check_meta_desc(meta_desc):
    return len(meta_desc) >= 120

def check_word_count(word_count):
    return word_count >= 500

# ── Scorer ────────────────────────────────────────────────────
CHECKS = [
    ("faq_schema",          25, "FAQ schema (FAQPage JSON-LD)"),
    ("local_business_schema", 15, "LocalBusiness / ProfessionalService schema"),
    ("entity_definition",   20, "Entity definition in H1 or meta (who + where)"),
    ("specificity",         15, "Specificity signals in headings / meta"),
    ("question_headings",   10, "Question-format H2/H3 headings"),
    ("meta_desc",           10, "Meta description ≥ 120 characters"),
    ("word_count",           5, "Page word count ≥ 500"),
]

def score(url):
    # Fetch
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AEOCheck/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return {"error": f"Could not fetch URL: {e}"}

    # Parse
    p = PageParser()
    p.feed(html)

    # Run checks
    results = {
        "faq_schema":             check_faq_schema(p.json_ld),
        "local_business_schema":  check_local_business_schema(p.json_ld),
        "entity_definition":      check_entity_definition(p.headings, p.meta_desc),
        "specificity":            check_specificity(p.headings, p.meta_desc),
        "question_headings":      check_question_headings(p.headings),
        "meta_desc":              check_meta_desc(p.meta_desc),
        "word_count":             check_word_count(p.word_count),
    }

    total = sum(w for k, w, _ in CHECKS if results[k])
    breakdown = [
        {"key": k, "label": lbl, "weight": w, "passed": results[k]}
        for k, w, lbl in CHECKS
    ]

    band = "Invisible" if total < 40 else "Partial visibility" if total < 70 else "Citable"

    return {
        "url": url,
        "score": total,
        "band": band,
        "breakdown": breakdown,
        "meta": {
            "title": p.title.strip(),
            "meta_desc": p.meta_desc,
            "word_count": p.word_count,
            "headings_found": len(p.headings),
            "json_ld_blocks": len(p.json_ld),
        }
    }

# ── Vercel handler ────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        url = qs.get("url", [None])[0]

        if not url:
            self._respond(400, {"error": "Missing ?url= parameter"})
            return
        if not url.startswith("http"):
            url = "https://" + url

        self._respond(200, score(url))

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")