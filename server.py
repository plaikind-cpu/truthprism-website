from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
import os
import re
import sqlite3
import secrets
import string
from datetime import datetime
from html.parser import HTMLParser

app = Flask(__name__)
CORS(app)

FAMILY_ACCESS_CODE = os.environ.get('LEGACY_ACCESS_CODE', 'TruthPrism2026')
FAMILY_API_KEY = os.environ.get('WEBAPP_API_KEY', '')

# --- SQLite / per-user access codes ---
DB_PATH = os.environ.get('SQLITE_PATH', '/data/truthlens.db')
ADMIN_SECRET = os.environ.get('ADMIN_SECRET', 'TruthPrismAdmin2026')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception:
        pass
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS access_codes (
            code TEXT PRIMARY KEY,
            label TEXT,
            created_at TEXT,
            last_used TEXT,
            use_count INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            max_uses INTEGER DEFAULT NULL
        )
    ''')
    # Add max_uses column if upgrading from older schema
    try:
        conn.execute('ALTER TABLE access_codes ADD COLUMN max_uses INTEGER DEFAULT NULL')
    except Exception:
        pass  # Column already exists
    # Seed the legacy family code into DB if not already there
    existing = conn.execute(
        'SELECT code FROM access_codes WHERE code = ?', (FAMILY_ACCESS_CODE,)
    ).fetchone()
    if not existing:
        conn.execute(
            'INSERT INTO access_codes (code, label, created_at, use_count, active, max_uses) VALUES (?, ?, ?, 0, 1, NULL)',
            (FAMILY_ACCESS_CODE, 'Family (unlimited)', datetime.utcnow().isoformat())
        )
    # Trial emails table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trial_emails (
            email TEXT PRIMARY KEY,
            code TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def send_trial_email(email, code):
    """Send trial code via email. Uses SendGrid when configured, logs to console otherwise."""
    sendgrid_key = os.environ.get('SENDGRID_API_KEY')
    if sendgrid_key:
        import urllib.request
        payload = {
            'personalizations': [{'to': [{'email': email}]}],
            'from': {'email': os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@truthprism.app'), 'name': 'TruthPrism'},
            'subject': 'Your TruthPrism Free Trial Code',
            'content': [{'type': 'text/plain', 'value': f'''Welcome to TruthPrism!

Your free trial access code is:

    {code}

This code gives you 5 free fact-checks. Enter it in the Authentication section of the app at:
https://truthlens-laikind.up.railway.app/app

After your trial, visit the app to purchase an access code and continue checking articles.

— The TruthPrism Team
'''}]
        }
        req = urllib.request.Request(
            'https://api.sendgrid.com/v3/mail/send',
            data=__import__('json').dumps(payload).encode(),
            headers={'Authorization': f'Bearer {sendgrid_key}', 'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            urllib.request.urlopen(req)
            print(f"[TRIAL] Email sent to {email} with code {code}")
        except Exception as e:
            print(f"[TRIAL] Email failed: {e} — code: {code}")
    else:
        # No SendGrid yet — log to console for testing
        print(f"[TRIAL] *** EMAIL NOT SENT (no SendGrid key) *** Email: {email} | Code: {code}")

def generate_code(length=12):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def validate_user_code(code, count_use=True):
    """Returns (valid: bool, error_message: str or None)"""
    code = code.strip()
    try:
        conn = get_db()
        row = conn.execute(
            'SELECT * FROM access_codes WHERE code = ? AND active = 1', (code,)
        ).fetchone()
        if not row:
            conn.close()
            return False, 'Invalid access code'
        # Check usage limit
        max_uses = row['max_uses']
        use_count = row['use_count'] or 0
        if max_uses is not None and use_count >= max_uses:
            conn.close()
            return False, 'You have reached your usage limit. To continue using TruthPrism, please obtain your own Anthropic API key at console.anthropic.com.'
        # Only increment counter for actual fact-check calls
        if count_use:
            conn.execute(
                'UPDATE access_codes SET last_used = ?, use_count = use_count + 1 WHERE code = ?',
                (datetime.utcnow().isoformat(), code)
            )
            conn.commit()
        conn.close()
        return True, None
    except Exception:
        return False, 'Invalid access code'

init_db()

@app.route('/')
def index():
    return send_from_directory('.', 'fact-checker-app.html')

@app.route('/<path:filename>')
def static_files(filename):
    allowed = ['.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.webp']
    import os
    ext = os.path.splitext(filename)[1].lower()
    if ext in allowed:
        return send_from_directory('.', filename)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/check-access', methods=['POST'])
def check_access():
    data = request.json
    code = data.get('access_code', '').strip()
    valid, err = validate_user_code(code, count_use=False)
    if valid:
        # Return remaining uses so webapp can show counter
        conn = get_db()
        row = conn.execute('SELECT use_count, max_uses FROM access_codes WHERE code = ?', (code,)).fetchone()
        conn.close()
        remaining = None
        if row and row['max_uses']:
            remaining = max(0, row['max_uses'] - row['use_count'])
        return jsonify({'success': True, 'remaining': remaining})
    return jsonify({'success': False, 'error': err}), 401

@app.route('/api/request-trial', methods=['POST'])
def request_trial():
    data = request.json
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'error': 'Please enter a valid email address'}), 400

    conn = get_db()
    # Check if email already has a trial
    existing = conn.execute('SELECT code FROM trial_emails WHERE email = ?', (email,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'A trial code has already been sent to this email address'}), 409

    # Generate trial code and insert into both tables
    code = 'TRIAL-' + generate_code(8).upper()
    now = datetime.utcnow().isoformat()
    conn.execute(
        'INSERT INTO access_codes (code, label, created_at, use_count, active, max_uses) VALUES (?, ?, ?, 0, 1, 5)',
        (code, f'Trial: {email}', now)
    )
    conn.execute(
        'INSERT INTO trial_emails (email, code, created_at) VALUES (?, ?, ?)',
        (email, code, now)
    )
    conn.commit()
    conn.close()

    # Send email (or log to console if SendGrid not configured)
    send_trial_email(email, code)

    return jsonify({'success': True, 'message': 'Trial code sent! Check your email.'})

@app.route('/api/check-facts-family', methods=['POST'])
def check_facts_family():
    data = request.json
    code = data.get('access_code', '').strip()
    valid, err = validate_user_code(code)
    if not valid:
        return jsonify({'error': err}), 401
    claim_text = data.get('claim_text') or data.get('text')
    if not claim_text:
        return jsonify({'error': 'Missing text'}), 400
    result = run_fact_check(FAMILY_API_KEY, claim_text)
    # Append remaining uses for trial codes
    conn = get_db()
    row = conn.execute('SELECT use_count, max_uses FROM access_codes WHERE code = ?', (code,)).fetchone()
    conn.close()
    if row and row['max_uses']:
        remaining = max(0, row['max_uses'] - row['use_count'])
        result_data = result.get_json()
        result_data['remaining'] = remaining
        return jsonify(result_data)
    return result

@app.route('/api/fetch-url-family', methods=['POST'])
def fetch_url_family():
    data = request.json
    code = data.get('access_code', '').strip()
    valid, err = validate_user_code(code)
    if not valid:
        return jsonify({'error': err}), 401
    url = data.get('url')
    if not url:
        return jsonify({'error': 'Missing URL'}), 400
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; TruthPrism/1.0)'}
        page = requests.get(url, headers=headers, timeout=10)
        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
                self.skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'nav', 'header', 'footer'):
                    self.skip = True
            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'nav', 'header', 'footer'):
                    self.skip = False
            def handle_data(self, data):
                if not self.skip and data.strip():
                    self.text.append(data.strip())
        parser = TextExtractor()
        parser.feed(page.text)
        raw_text = ' '.join(parser.text)
        truncated = len(raw_text) > 7500
        text = raw_text[:7500]
    except Exception as e:
        return jsonify({'error': f'Could not fetch URL: {str(e)}'}), 400
    return run_fact_check(FAMILY_API_KEY, text, truncated=truncated)

@app.route('/admin')
def admin():
    from flask import make_response
    response = make_response(send_from_directory('.', 'admin.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.route('/app')
@app.route('/app/v2')
def webapp():
    from flask import make_response
    response = make_response(send_from_directory('.', 'webapp.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def condense_analysis(full_text):
    """Extract a 4-section condensed summary from full analysis text."""
    import re
    condensed = []

    def clean(text):
        return re.sub(r'\*\*([^*]+)\*\*', r'\1', text).replace('**', '').strip()

    def first_sentences(text, n=2):
        sentences = re.split(r'(?<![A-Z])(?<![A-Z][a-z])(?<=[.!?])\s+(?=[A-Z])', text.strip())
        return ' '.join(sentences[:n])

    # 1. ARTICLE OVERVIEW - full summary as-is
    summary_match = re.search(r'SUMMARY:\s*(.*?)(?=\n\n|\n---|\nACCURATE)', full_text, re.IGNORECASE | re.DOTALL)
    if summary_match:
        condensed.append("ARTICLE OVERVIEW:\n" + clean(summary_match.group(1).strip()))

    # 2. FACTUAL FINDINGS - synthesize accurate + inaccurate into a paragraph
    accurate_match = re.search(r'ACCURATE CLAIMS:\s*(.*?)(?=\n\nINACCURATE|\n---)', full_text, re.IGNORECASE | re.DOTALL)
    inaccurate_match = re.search(r'INACCURATE CLAIMS:\s*(.*?)(?=\n\nMISLEADING|\n---)', full_text, re.IGNORECASE | re.DOTALL)
    
    factual_parts = []
    if accurate_match:
        bullets = re.findall(r'\*\s+(.+?)(?=\n\*|\Z)', accurate_match.group(1), re.DOTALL)
        count = len(bullets)
        if count > 0:
            # First bullet as example claim
            example = clean(first_sentences(bullets[0], 1))
            if count == 1:
                factual_parts.append(f"One verifiable claim was confirmed accurate: {example}")
            else:
                factual_parts.append(f"{count} verifiable claims were confirmed accurate. For example: {example}")

    if inaccurate_match:
        bullets = re.findall(r'\*\s+(.+?)(?=\n\*|\Z)', inaccurate_match.group(1), re.DOTALL)
        none_found = any('none' in b.lower() or 'no inaccurate' in b.lower() or 'no false' in b.lower() or 'no outright' in b.lower() for b in bullets)
        if none_found or not bullets:
            factual_parts.append("No outright false claims were identified.")
        else:
            count = len(bullets)
            example = clean(first_sentences(bullets[0], 1))
            factual_parts.append(f"{count} inaccurate or unsubstantiated claim{'s were' if count > 1 else ' was'} identified. For example: {example}")

    if factual_parts:
        condensed.append("FACTUAL FINDINGS:\n" + " ".join(factual_parts))

    # 3. CONTEXTUAL FINDINGS - synthesize misleading + contextualization
    misleading_match = re.search(r'MISLEADING ELEMENTS:\s*(.*?)(?=\n\nCONTEXTUALIZATION|\n\nSCORE|\n---)', full_text, re.IGNORECASE | re.DOTALL)
    context_match = re.search(r'CONTEXTUALIZATION ISSUES:\s*(.*?)(?=\n\nSCORE|\n---)', full_text, re.IGNORECASE | re.DOTALL)

    context_parts = []
    for match in [misleading_match, context_match]:
        if match:
            bullets = re.findall(r'\*\s+(.+?)(?=\n\*|\Z)', match.group(1), re.DOTALL)
            for b in bullets:
                bc = clean(b.strip())
                if 'none' not in bc.lower()[:30] and 'no significant' not in bc.lower()[:30]:
                    context_parts.append(first_sentences(bc, 1))

    if context_parts:
        condensed.append("CONTEXTUAL FINDINGS:\n" + " ".join(context_parts[:3]))
    else:
        condensed.append("CONTEXTUAL FINDINGS:\nNo significant contextualization issues identified.")

    # 4. SCORE EXPLANATION - as before
    score_match = re.search(r'(?:SCORE EXPLANATION|SCORE RATIONALE|SCORING RATIONALE|EXPLANATION):\s*(.*?)(?=\n\nSOURCES|\n---|\Z)', full_text, re.IGNORECASE | re.DOTALL)
    if score_match:
        score_text = score_match.group(1).strip()
        factual_s = re.search(r'(?:Factual(?:\s+Score)?(?:\s+Explanation)?|FACTUAL).*?:(.*?)(?=(?:Context|CONTEXT)|\Z)', score_text, re.IGNORECASE | re.DOTALL)
        context_s = re.search(r'(?:Context(?:\s+Score)?(?:\s+Explanation)?|CONTEXT).*?:(.*?)(?=\n\n|\Z)', score_text, re.IGNORECASE | re.DOTALL)
        score_lines = []
        if factual_s:
            ft = clean(factual_s.group(1).strip())
            sentences = re.split(r'(?<![A-Z])(?<![A-Z][a-z])(?<=[.!?])\s+(?=[A-Z])', ft)
            score_lines.append("Factual Score: " + (sentences[0] if sentences else ft))
        if context_s:
            ct = clean(context_s.group(1).strip())
            sentences = re.split(r'(?<![A-Z])(?<![A-Z][a-z])(?<=[.!?])\s+(?=[A-Z])', ct)
            score_lines.append("Context Score: " + (sentences[0] if sentences else ct))
        if score_lines:
            condensed.append("SCORE EXPLANATION:\n" + "\n\n".join(score_lines))

    # SOURCES
    sources_match = re.search(r'SOURCES:\s*(.*?)(?=\n---|\Z)', full_text, re.IGNORECASE | re.DOTALL)
    if sources_match:
        sources_text = sources_match.group(1).strip()
        # Count all sources mentioned
        other_sources = re.findall(r'\n-\s*(.+)', sources_text)
        num_other = len(other_sources)
        fc = "not covered" if "not covered" in sources_text[:300].lower() else "covered"
        pt_text = sources_text[150:400] if len(sources_text) > 150 else sources_text
        pt = "not covered" if "not covered" in pt_text.lower() else "covered"
        
        source_summary = "Multiple sources were consulted including FactCheck.org"
        if fc == "covered":
            source_summary = "FactCheck.org fact-checks were found relevant to this article"
        if pt == "covered":
            source_summary += ", PolitiFact"
        if num_other > 0:
            source_summary += f", and {num_other} additional credible sources including news outlets, government records, and academic references"
        source_summary += ". See full report for complete sourcing."
        condensed.append("SOURCES: " + source_summary)

    return "\n\n".join(condensed)


def run_fact_check(api_key, claim_text, truncated=False):
    prompt = '''You are a rigorous fact-checker. Your job is to verify the factual claims in any text, including opinion pieces, editorials, news articles, and social media posts.

IMPORTANT RULES:
1. ALWAYS fact-check the text, regardless of whether it is a news article, opinion piece, editorial, or commentary.
2. Opinion pieces and editorials often contain verifiable factual claims - check ALL of them.
3. Never refuse to fact-check or assign a score because the text is an opinion piece or analysis.
4. Never invert or reinterpret the scoring scale. A score of 1-3 means the factual claims are mostly false. A score of 8-10 means the factual claims are mostly accurate.
5. Score the accuracy of the verifiable factual claims in the text, not the author's opinions or conclusions.

Text to verify:
"""
''' + claim_text + '''
"""

Instructions:
1. Identify ALL verifiable factual claims (dates, events, statistics, quotes, attributions, legal claims, scientific assertions)
2. Perform a maximum of 4 web searches total. Prioritize searches wisely:
   - Use search 1 to check FactCheck.org or PolitiFact if the article contains political or policy claims
   - Use searches 2-3 to verify the most significant or questionable factual claims only
   - Use search 4 (if needed) to confirm any claim you intend to flag as inaccurate
   Focus on claims that materially affect the overall assessment. Do not search for minor details, background facts, or claims you can assess from your training knowledge.
3. For any claim you intend to rate as INACCURATE or HIGHLY MISLEADING, confirm it with at least one search result before reporting it. Do not flag a claim as inaccurate without search confirmation.
4. For each claim, note which source(s) verified or debunked it
5. Note whether the piece is opinion/editorial vs. straight news, but fact-check it either way
6. Assign TWO scores:

   FACTUAL ACCURACY SCORE (1-10): Rate only the accuracy of verifiable factual claims
   - 10 = All factual claims verified accurate by multiple credible sources
   - 7-9 = Mostly accurate, minor errors or lacks some context
   - 4-6 = Mixed accuracy, some true and some false claims
   - 1-3 = Mostly or completely false claims, debunked by fact-checkers

   CONTEXTUALIZATION SCORE (1-10): Rate how fairly and completely the piece presents the broader picture
   - 10 = Balanced, complete context provided, no significant omissions
   - 7-9 = Mostly fair, minor omissions or framing issues
   - 4-6 = Notable omissions, one-sided framing, or cherry-picked data
   - 1-3 = Highly misleading framing, major context omitted, or strongly biased presentation

7. SCORING WEIGHTS for Factual Accuracy - apply these when determining score impact:
   - HIGH IMPACT (can lower score significantly): False central claims, fabricated quotes, wrong attribution, deliberate misrepresentation of data, debunked core assertions
   - LOW IMPACT (minor score reduction or none): Rounding of ages or numbers (e.g. "80" vs "79.5"), approximate figures that are directionally correct, statistics that were accurate at time of publication but have since changed (e.g. death tolls, poll numbers, case counts), minor name or title variations
   - NO IMPACT (do not penalize): Figures that were accurate when the article was written but are now outdated due to the passage of time. Judge the article against what was known at the time of publication, not current data. Population figures rounded to the nearest million (e.g. "90 million" vs "93 million") are NOT inaccurate claims — do NOT list these under Inaccurate Claims. Any numerical approximation within 10% that is directionally correct should NOT be listed as an inaccurate claim.
   - CRITICAL RULE: If you find yourself noting that a discrepancy is minor, inconsequential, or of no practical significance — DO NOT LIST IT. Only report errors that materially affect the reader's understanding of the facts. When in doubt, leave it out.

8. SCORING WEIGHTS for Contextualization - consider these factors:
   - NEGATIVE factors (lower the score): Omitting important opposing data, attributing trends to one cause while ignoring others, presenting cumulative figures as single-period results, failing to disclose conflicts of interest, using emotionally loaded framing unsupported by evidence
   - NEUTRAL (do not penalize): Normal editorial framing, clearly labeled opinion, emphasis on one side when the piece is transparently advocacy

Format your response EXACTLY as follows:

FACTUAL SCORE: [number from 1-10]
CONTEXT SCORE: [number from 1-10]

ANALYSIS:

SUMMARY:
[1-2 sentence overall assessment, noting if the piece is opinion/editorial]


ACCURATE CLAIMS:
* [List factual claims that are true or mostly true]
* [Include sources]


INACCURATE CLAIMS:
* [List factual claims that are false or mostly false — confirmed by at least two independent sources]
* [Include what fact-checkers found]
* NOTE: Do NOT list any discrepancy you would describe as minor, trivial, inconsequential, or of no practical significance. If it doesn't materially affect the reader's understanding, omit it entirely.


MISLEADING ELEMENTS:
* [Claims that are technically true but lack important context]
* [Cherry-picked statistics or incomplete information]
* NOTE: Do NOT list here: rounded numbers, approximate ages, statistics that changed after publication date, population figures rounded to nearest million, or any approximation within 10% that is directionally correct. Only list claims that are materially and substantively misleading.


CONTEXTUALIZATION ISSUES:
* [Omitted context that materially changes the picture]
* [One-sided framing or attribution]
* [Undisclosed conflicts of interest]
* [If none: state "No significant contextualization issues identified"]


SCORE EXPLANATION:
Factual Score Explanation: [One sentence explaining the factual score]
Context Score Explanation: [One sentence explaining the context score]


SOURCES:
FactCheck.org: [findings or "not covered"]
PolitiFact: [findings or "not covered"]
Snopes: [findings or "not covered"]
Other sources: [list additional sources consulted]

Be thorough but concise. Use bullet points for easy reading. Add blank lines between sections.'''

    response = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'anthropic-beta': 'prompt-caching-2024-07-31',
            'content-type': 'application/json'
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 2500,
            'tools': [{'type': 'web_search_20250305', 'name': 'web_search'}],
            'system': [{'type': 'text', 'text': prompt[:prompt.find('Text to verify:')].strip(), 'cache_control': {'type': 'ephemeral'}}],
            'messages': [{'role': 'user', 'content': 'Text to verify:\n"""\n' + claim_text + '\n"""\n\n' + prompt[prompt.find('Instructions:'):]}]
        }
    )

    if response.status_code != 200:
        return jsonify({'error': response.json()}), response.status_code

    result = response.json()
    full_text = ''
    for block in result.get('content', []):
        if block.get('type') == 'text':
            full_text += block.get('text', '')

    factual_match = re.search(r'FACTUAL SCORE:\s*(\d+)', full_text, re.IGNORECASE) or \
                    re.search(r'Factual Score\s*\((\d+)/10\)', full_text, re.IGNORECASE)
    context_match = re.search(r'CONTEXT SCORE:\s*(\d+)', full_text, re.IGNORECASE) or \
                    re.search(r'Context Score\s*\((\d+)/10\)', full_text, re.IGNORECASE)
    analysis_match = re.search(r'ANALYSIS:\s*([\s\S]+)', full_text, re.IGNORECASE)

    factual_score = int(factual_match.group(1)) if factual_match else None
    context_score = int(context_match.group(1)) if context_match else None
    analysis = analysis_match.group(1).strip() if analysis_match else full_text.strip()

    analysis = re.sub(r'.*?(?=SUMMARY:)', '', analysis, flags=re.IGNORECASE | re.DOTALL)
    analysis = re.sub(r'^FACTUAL SCORE:\s*\d+[^\n]*\n?', '', analysis, flags=re.IGNORECASE | re.MULTILINE)
    analysis = re.sub(r'^CONTEXT SCORE:\s*\d+[^\n]*\n?', '', analysis, flags=re.IGNORECASE | re.MULTILINE)
    analysis = re.sub(r'^SCORE:\s*\d+[^\n]*\n?', '', analysis, flags=re.IGNORECASE | re.MULTILINE)
    analysis = analysis.strip()

    if factual_score is None:
        single = re.search(r'SCORE:\s*(\d+)', full_text, re.IGNORECASE)
        factual_score = int(single.group(1)) if single else 5

    condensed = condense_analysis(analysis)

    return jsonify({
        'score': factual_score,
        'context_score': context_score,
        'analysis': condensed,
        'full_analysis': analysis,
        'truncated': truncated
    })

@app.route('/api/check-facts', methods=['POST'])
@app.route('/fact-check', methods=['POST'])
def fact_check():
    data = request.json
    api_key = data.get('api_key')
    claim_text = data.get('claim_text') or data.get('text')
    if not api_key or not claim_text:
        return jsonify({'error': 'Missing API key or text'}), 400
    return run_fact_check(api_key, claim_text)

@app.route('/api/fetch-url', methods=['POST'])
def fetch_url():
    data = request.json
    api_key = data.get('api_key')
    url = data.get('url')
    if not api_key or not url:
        return jsonify({'error': 'Missing API key or URL'}), 400
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; TruthPrism/1.0)'}
        page = requests.get(url, headers=headers, timeout=10)
        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
                self.skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'nav', 'header', 'footer'):
                    self.skip = True
            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'nav', 'header', 'footer'):
                    self.skip = False
            def handle_data(self, data):
                if not self.skip and data.strip():
                    self.text.append(data.strip())
        parser = TextExtractor()
        parser.feed(page.text)
        raw_text = ' '.join(parser.text)
        truncated = len(raw_text) > 7500
        text = raw_text[:7500]
    except Exception as e:
        return jsonify({'error': f'Could not fetch URL: {str(e)}'}), 400
    return run_fact_check(api_key, text, truncated=truncated)

@app.route('/api/test-key', methods=['POST'])
@app.route('/test', methods=['POST'])
def test_key():
    data = request.json
    api_key = data.get('api_key')
    if not api_key:
        return jsonify({'error': 'Missing API key'}), 400
    response = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json'
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 50,
            'messages': [{'role': 'user', 'content': 'Say "API key works!" and nothing else.'}]
        }
    )
    if response.status_code == 200:
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Invalid API key'}), 401


# --- Admin endpoints (protected by ADMIN_SECRET) ---

def require_admin(data):
    return data.get('admin_secret', '').strip() == ADMIN_SECRET

@app.route('/api/admin/create-code', methods=['POST'])
def admin_create_code():
    data = request.json
    if not require_admin(data):
        return jsonify({'error': 'Unauthorized'}), 401
    label = data.get('label', '').strip()
    code = data.get('code', '').strip() or generate_code()
    max_uses = data.get('max_uses')  # None = unlimited
    if max_uses is not None:
        try:
            max_uses = int(max_uses)
        except (ValueError, TypeError):
            max_uses = None
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO access_codes (code, label, created_at, use_count, active, max_uses) VALUES (?, ?, ?, 0, 1, ?)',
            (code, label, datetime.utcnow().isoformat(), max_uses)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'code': code, 'label': label, 'max_uses': max_uses})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Code already exists'}), 409

@app.route('/api/admin/list-codes', methods=['POST'])
def admin_list_codes():
    data = request.json
    if not require_admin(data):
        return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    rows = conn.execute('SELECT code, label, created_at, last_used, use_count, active, max_uses FROM access_codes ORDER BY created_at DESC').fetchall()
    conn.close()
    return jsonify({'codes': [dict(r) for r in rows]})

@app.route('/api/admin/deactivate-code', methods=['POST'])
def admin_deactivate_code():
    data = request.json
    if not require_admin(data):
        return jsonify({'error': 'Unauthorized'}), 401
    code = data.get('code', '').strip()
    conn = get_db()
    conn.execute('UPDATE access_codes SET active = 0 WHERE code = ?', (code,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/admin/delete-code', methods=['POST'])
def admin_delete_code():
    data = request.json
    if not require_admin(data):
        return jsonify({'error': 'Unauthorized'}), 401
    code = data.get('code', '').strip()
    conn = get_db()
    conn.execute('DELETE FROM access_codes WHERE code = ?', (code,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


if __name__ == '__main__':
    print("=" * 59)
    print("AI Fact Checker Server Starting...")
    print("=" * 59)
    print()
    print("Open your browser and go to:")
    print("   http://localhost:5000")
    print()
    print("Keep this window open while using the app")
    print("Press Ctrl+C to stop the server")
    print("=" * 59)
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
