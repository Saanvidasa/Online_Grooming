from gevent import monkey
monkey.patch_all()

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room
import re, json, smtplib, random, time
import gevent
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import sys, os, requests
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)
app.config["SECRET_KEY"] = "safeguard-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

SMTP_EMAIL    = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
PARENT_EMAIL  = os.getenv("PARENT_EMAIL")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = "llama-3.3-70b-versatile"

ROOM              = "safeguard-room"
users             = {}
room_risk         = 0.0
room_history      = []
room_sandbox      = False
sandbox_msg_count = 0
wind_down_started = False
MSG_SCORE_CAP     = 0.35
SANDBOX_THRESHOLD = 1.0
WIND_DOWN_AFTER   = 2
child_sid         = None
predator_sid      = None

SCORING_SYSTEM = """You are a child safety AI scoring system.

Your job: score a single message sent by an adult to a child.
Output how alarming THIS message is on its own, from 0.0 to 1.0.

Score 0.0 = normal conversation — ALL of these must score 0.0:
            "what's your name?", "hi!", "hello", "how are you?", "how was school?",
            "what games do you play?", "do you like music?", "how old are you?",
            "where are you from?", "what grade are you in?", "nice to meet you",
            "what's up?", "do you have pets?", "what's your favourite subject?"

Score 0.1 = very mildly personal, still completely normal:
            "do you live nearby?", "are you home right now?", "do you have siblings?"

Score 0.2 = slightly suspicious only with prior bad context:
            "you seem really mature for your age"

Score 0.3 = moderately suspicious:
            "are you alone right now?", "don't tell your parents about this"

Score 0.5 = clearly inappropriate:
            "you're so hot", "send me a pic of yourself"

Score 0.7 = strongly grooming:
            "send nudes", "come meet me secretly", "home alone tonight?"

Score 0.9+ = explicit sexual content, direct solicitation

CRITICAL — these always score 0.0 no matter what:
- Any greeting or introduction
- Asking someone's name
- Asking age, grade, school, hobbies, favourite things
- Complimenting someone's taste in music/games/food
- Normal friendly small talk

Only return flags if score > 0.3. When in doubt, score LOWER.

Return ONLY valid JSON:
{"score": 0.0, "flags": [], "reasoning": "one sentence"}"""

REGEX_FALLBACK = [
    (r"don'?t tell (your )?(parents?|mom|dad|anyone)", 0.30, "Secrecy"),
    (r"keep (this|it|our|a) secret|just between us",   0.30, "Secrecy"),
    (r"delete (this|the messages?|the chat)",          0.25, "Secrecy"),
    (r"where do you live|what'?s your address",        0.22, "Location"),
    (r"are you alone|home alone|u alone|alone rn",     0.22, "Isolation"),
    (r"parents? (home|away|out)",                      0.20, "Isolation"),
    (r"send (me )?(your )?(nudes?|naked pics?)",       0.35, "Explicit"),
    (r"\bsex\b|have sex|wanna f+u+c+k+",               0.35, "Explicit"),
    (r"\bnaked\b|touch yourself|masturbat",             0.35, "Explicit"),
    (r"\bhorny\b|boner|erection|\bpussy\b|\bdick\b",   0.35, "Explicit"),
    (r"\btits\b|\bboobs?\b|\bcock\b",                  0.35, "Explicit"),
    (r"send (me )?(a )?(pic|photo|selfie) of (you|yourself)", 0.28, "Image request"),
    (r"show me (your body|yourself|ur body)",           0.28, "Image request"),
    (r"(go )?on cam|video call|facetime",              0.20, "Video request"),
    (r"meet (up|in person)|come (over|chill)|pick you up", 0.28, "Meeting"),
    (r"you'?re so (mature|special|hot|sexy)",          0.25, "Manipulation"),
    (r"everyone does it|it'?s normal|don'?t be scared", 0.25, "Normalizing"),
]

REGEX_KEYWORDS = {
    "nudes": 0.35, "nude": 0.30, "naked": 0.28, "sex": 0.30,
    "horny": 0.30, "porn": 0.30, "sexy": 0.22, "hottie": 0.22,
    "undress": 0.28, "strip": 0.25, "fetish": 0.25, "erotic": 0.25,
    "dtf": 0.32, "secret": 0.15, "alone": 0.12, "delete": 0.15,
    "meet": 0.15, "address": 0.20, "addy": 0.22,
}

def call_groq(system, user_msg, max_tokens=200, temperature=0.3):
    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_msg},
                ]
            },
            timeout=10
        )
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[GROQ ERROR] {e}")
        return None

def semantic_score(text):
    context = ""
    for msg in room_history[-5:]:
        label = "CHILD" if msg["role"] == "child" else "ADULT"
        context += f"{label}: {msg['text']}\n"
    prompt = f'Prior context:\n{context or "(none)"}\n\nMessage to score: "{text}"\n\nReturn ONLY the JSON.'
    raw = call_groq(SCORING_SYSTEM, prompt, max_tokens=200, temperature=0.1)
    if raw:
        raw = re.sub(r"```json\s*|```\s*", "", raw).strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group())
                score  = float(result.get("score", 0.0))
                flags  = result.get("flags", [])
                reason = result.get("reasoning", "")
                score  = max(0.0, min(score, 1.0))
                print(f"[SCORE] '{text[:40]}' => {score:.2f} | {flags} | {reason}")
                return round(score, 3), [f.replace("_"," ").title() for f in flags if isinstance(f, str)]
            except (json.JSONDecodeError, ValueError) as e:
                print(f"[SCORE PARSE ERROR] {e}")
    print("[SCORE] Groq unavailable — regex fallback")
    score, flags, t = 0.0, [], text.lower()
    for pattern, weight, label in REGEX_FALLBACK:
        if re.search(pattern, t):
            score += weight
            flags.append(label)
    for word in re.findall(r'\b\w+\b', t):
        if word in REGEX_KEYWORDS:
            score += REGEX_KEYWORDS[word]
    if datetime.now().hour >= 22 or datetime.now().hour <= 5:
        score *= 1.2
    return min(round(score, 3), 1.0), list(set(flags))

def get_stage(risk):
    if risk < 0.25: return "Friendly Interaction"
    if risk < 0.50: return "Suspicious Behaviour"
    if risk < 0.75: return "Escalating Risk"
    if risk < 1.0:  return "High Risk"
    return "Exploitation Attempt"

def update_risk(msg_score):
    global room_risk
    contribution = min(msg_score, MSG_SCORE_CAP)
    room_risk = min(round(room_risk + contribution, 3), 1.0)
    return room_risk

CHILD_SYSTEM = """You are a 14-year-old teenager in a chat. Be casual. Max 2 short sentences.
- where do you live / address → "lol my parents say don't share that"
- how old are you → "why do you ask lol"
- are you alone / home alone → "nah my whole family is home lol"
- send pics / photos / nudes → "lol I don't send pics to people I don't know"
- anything sexual / explicit → "ew wtf no" then change topic
- don't tell parents / keep secret → "lol why would I hide stuff from my parents"
- meet up / come over → "lol my parents would never let me"
- video call / facetime → "lol my wifi is terrible rn"
- I love you / be my gf/bf → "lol we just met 😂"
- your number / snap → "lol I don't give my number to strangers"
Reply naturally as a teen for anything else. Never say you're an AI."""

PREDATOR_SYSTEM = """You are a friendly person having a casual conversation.
Talk about school, music, games, food, weekend plans. Be warm and age-appropriate.
1-2 short sentences. Start gently wrapping up the conversation."""

def build_history():
    lines = ""
    for msg in room_history[-20:]:
        label = "Teen" if msg["role"] == "child" else "Other"
        lines += f"{label}: {msg['text']}\n"
    return lines

def ai_as_child(predator_msg):
    prompt = f'Conversation:\n{build_history()}\nOther said: "{predator_msg}"\nReply as teen, 1-2 sentences.'
    return call_groq(CHILD_SYSTEM, prompt, max_tokens=80, temperature=0.85) or _child_fallback(predator_msg)

def ai_as_predator(child_msg):
    prompt = f'Conversation:\n{build_history()}\nTeen said: "{child_msg}"\nReply naturally, start winding down. 1-2 sentences.'
    return call_groq(PREDATOR_SYSTEM, prompt, max_tokens=80, temperature=0.85) or random.choice([
        "haha yeah lol", "omg same 😂", "lol nice!", "that's cool!", "lol true"
    ])

def _child_fallback(text):
    t = text.lower()
    if re.search(r"how old|your age",                  t): return "why do you ask lol"
    if re.search(r"where.*live|address|addy",          t): return "lol my parents say don't share that"
    if re.search(r"home alone|are you alone|alone rn", t): return "nah my whole family is home lol"
    if re.search(r"pic|photo|selfie|nudes?|pictures?", t): return "lol I don't send pics to strangers"
    if re.search(r"sex|sexy|hot|nude|body|naked",      t): return "ew wtf no"
    if re.search(r"secret|don't tell|delete",          t): return "lol why would I hide stuff from my parents"
    if re.search(r"meet|come over|pick you|hang",      t): return "lol my parents would never let me"
    if re.search(r"video call|facetime|on cam",        t): return "lol my wifi is so bad rn"
    if re.search(r"love|girlfriend|boyfriend",         t): return "lol we just met 😂"
    if re.search(r"number|whatsapp|snap|instagram",    t): return "lol I don't give my number to strangers"
    return random.choice(["lol idk", "umm okay 😅", "haha what", "lol why are you asking that"])

def run_wind_down():
    print("[SANDBOX] Wind-down starting...")
    c_sid  = child_sid
    p_sid  = predator_sid
    c_user = users.get(c_sid, {})
    p_user = users.get(p_sid, {})
    stage  = get_stage(room_risk)
    for i, msg in enumerate(["lol hey I gotta go now", "bye take care"]):
        gevent.sleep(3 + i * 3)
        if p_sid:
            socketio.emit("message", {
                "name": c_user.get("name",""), "avatar": c_user.get("avatar","🐼"),
                "text": msg, "sid": c_sid, "role": "child",
                "risk": room_risk, "msg_score": 0, "stage": stage,
                "flags": [], "flagged": False,
            }, to=p_sid)
    for i, msg in enumerate(["lol yeah I gotta go too", "bye"]):
        gevent.sleep(2 + i * 2)
        if c_sid:
            socketio.emit("message", {
                "name": p_user.get("name",""), "avatar": p_user.get("avatar","🐼"),
                "text": msg, "sid": p_sid, "role": "predator",
                "risk": room_risk, "msg_score": 0, "stage": stage,
                "flags": [], "flagged": False,
            }, to=c_sid)
    gevent.sleep(3)
    terminated_payload = {"risk": room_risk, "stage": stage}
    if c_sid: socketio.emit("chat_terminated", terminated_payload, to=c_sid)
    if p_sid: socketio.emit("chat_terminated", terminated_payload, to=p_sid)
    send_alert(room_risk, stage)
    print("[SANDBOX] Terminated.")

# ═══════════════════════════════════════════════
#  EMAIL — tries Resend first, falls back to Gmail
# ═══════════════════════════════════════════════
def send_alert(risk, stage):
    if not PARENT_EMAIL:
        print("[EMAIL] Skipped — PARENT_EMAIL not set")
        return

    body = (
        f"SafeGuard detected a potential online grooming situation on your child's device.\n\n"
        f"Risk Level : {round(risk * 100)}%\n"
        f"Stage      : {stage}\n"
        f"Time       : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"The AI safety system has already intervened and ended the conversation.\n\n"
        f"If you need help:\n"
        f"  CHILDLINE         : 1098\n"
        f"  Cybercrime Portal : cybercrime.gov.in\n"
        f"  National Helpline : 1930\n\n"
        f"No message content is included to protect your child's privacy.\n"
        f"— SafeGuard AI"
    )

    # ── Method 1: Resend API (recommended — always works) ──────────────
    if RESEND_API_KEY:
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": "SafeGuard <onboarding@resend.dev>",
                    "to": [PARENT_EMAIL],
                    "subject": "SafeGuard Alert — Immediate Attention Needed",
                    "text": body
                },
                timeout=15
            )
            if resp.status_code == 200:
                print(f"[EMAIL] Sent via Resend to {PARENT_EMAIL}")
                return
            else:
                print(f"[EMAIL] Resend failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[EMAIL] Resend error: {e}")

    # ── Method 2: Gmail STARTTLS port 587 ──────────────────────────────
    if SMTP_EMAIL and SMTP_PASSWORD:
        print(f"[EMAIL] Trying Gmail STARTTLS to {PARENT_EMAIL}...")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "SafeGuard Alert — Immediate Attention Needed"
        msg["From"]    = SMTP_EMAIL
        msg["To"]      = PARENT_EMAIL
        msg.attach(MIMEText(body, "plain"))
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(SMTP_EMAIL, SMTP_PASSWORD)
                s.sendmail(SMTP_EMAIL, PARENT_EMAIL, msg.as_string())
            print(f"[EMAIL] Sent via Gmail STARTTLS to {PARENT_EMAIL}")
            return
        except smtplib.SMTPAuthenticationError:
            print("[EMAIL ERROR] Gmail auth failed — need App Password")
            print("  → myaccount.google.com → Security → App Passwords → create one")
        except Exception as e1:
            print(f"[EMAIL] STARTTLS failed: {e1} — trying SSL port 465...")
            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
                    s.login(SMTP_EMAIL, SMTP_PASSWORD)
                    s.sendmail(SMTP_EMAIL, PARENT_EMAIL, msg.as_string())
                print(f"[EMAIL] Sent via Gmail SSL to {PARENT_EMAIL}")
                return
            except Exception as e2:
                print(f"[EMAIL ERROR] Both Gmail methods failed: {e2}")
    else:
        print("[EMAIL] No email method available — set RESEND_API_KEY or SMTP_EMAIL+SMTP_PASSWORD")

SUPPORT_SYSTEM = """You are a warm, compassionate child safety counsellor talking to a child who was just protected from a potentially dangerous online situation by SafeGuard AI.

The child was in a chat that was flagged as dangerous. The AI system intervened and ended the conversation safely. The child may feel confused, scared, embarrassed, or not fully understand what happened.

Your job:
- Be warm, gentle, and non-judgmental. Never blame the child.
- Validate their feelings — it is normal to feel confused or upset.
- Explain simply that the other person's behaviour was not okay and not their fault.
- Encourage them to talk to a trusted adult — parent, teacher, counsellor.
- If they seem distressed, remind them of helplines: CHILDLINE 1098, Cyber Crime 1930.
- Answer their questions honestly but age-appropriately.
- Never say anything scary or dramatic. Stay calm and reassuring.
- Keep replies short — 2-3 sentences max. This is a chat, not an essay.
- Never reveal how the AI system works internally.

You have been given the conversation history so you know exactly what the child experienced. Use this context to give specific, relevant comfort — not generic responses."""

@socketio.on("support_message")
def on_support_message(data):
    sid  = request.sid
    text = data.get("text", "").strip()
    if not text:
        return

    convo_context = ""
    for msg in room_history:
        if msg["role"] == "predator":
            convo_context += f"Stranger said: {msg['text']}\n"
        elif msg["role"] == "child":
            convo_context += f"Child said: {msg['text']}\n"

    support_history = data.get("history", [])
    history_text = ""
    for turn in support_history[-10:]:
        role_label = "Child" if turn["role"] == "child" else "Counsellor"
        history_text += f"{role_label}: {turn['text']}\n"

    prompt = f"""The dangerous conversation that was intercepted:
{convo_context or "(no conversation history available)"}

Risk level reached: {round(room_risk * 100)}% — Stage: {get_stage(room_risk)}

Support conversation so far:
{history_text or "(this is the first message)"}

Child just said: "{text}"

Reply as the counsellor. Be warm, short, and specific to what they experienced."""

    def generate_support_reply():
        reply = call_groq(SUPPORT_SYSTEM, prompt, max_tokens=120, temperature=0.7)
        if not reply:
            reply = "I'm here for you 💚 What you experienced was not your fault at all. You're safe now — would you like to talk about how you're feeling?"
        socketio.emit("support_reply", {"text": reply}, to=sid)
        print(f"[SUPPORT] → '{reply[:60]}'")
    gevent.spawn(generate_support_reply)

def save_evidence():
    path = f"evidence_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(path, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "risk_score":   room_risk,
            "stage":        get_stage(room_risk),
            "conversation": room_history,
        }, f, indent=2)
    print(f"[EVIDENCE] Saved → {path}")

@app.route("/")
def index():
    return send_from_directory("templates", "chat.html")

@socketio.on("join")
def on_join(data):
    global child_sid, predator_sid
    sid    = request.sid
    name   = data.get("name", "User")
    avatar = data.get("avatar", "🐼")
    if child_sid is None:
        role = "child";    child_sid    = sid
    else:
        role = "predator"; predator_sid = sid
    users[sid] = {"name": name, "avatar": avatar, "role": role}
    join_room(ROOM)
    count    = len(users)
    existing = [{"name": u["name"], "avatar": u["avatar"]} for s, u in users.items() if s != sid]
    emit("user_joined", {"name": name, "avatar": avatar, "count": count, "role": role}, to=ROOM, skip_sid=sid)
    emit("room_info",   {"count": count, "existing_users": existing, "my_role": role})
    print(f"[+] {name} joined as {role}")

@socketio.on("message")
def on_message(data):
    global room_sandbox, sandbox_msg_count, wind_down_started
    sid  = request.sid
    user = users.get(sid, {})
    name = user.get("name", "?")
    role = user.get("role", "unknown")
    text = data.get("text", "")
    if role == "predator":
        msg_score, flags = semantic_score(text)
        risk = update_risk(msg_score)
    else:
        msg_score = 0.0; flags = []; risk = room_risk
    stage = get_stage(risk)
    room_history.append({"sender": name, "role": role, "text": text, "score": msg_score, "flags": flags, "ts": datetime.now().isoformat()})
    payload = {"name": name, "avatar": user.get("avatar","🐼"), "text": text, "sid": sid, "risk": risk, "msg_score": msg_score, "stage": stage, "flags": flags, "role": role}
    c_sid  = child_sid;  p_sid  = predator_sid
    c_user = dict(users.get(c_sid, {})); p_user = dict(users.get(p_sid, {}))

    if room_sandbox:
        sandbox_msg_count += 1
        if role == "predator":
            emit("message", {**payload, "flagged": False, "flags": []}, to=p_sid)
            captured_text = text
            def reply_to_predator():
                reply = ai_as_child(captured_text)
                socketio.emit("message", {"name": c_user.get("name",""), "avatar": c_user.get("avatar","🐼"), "text": reply, "sid": c_sid, "risk": risk, "msg_score": 0, "stage": stage, "flags": [], "role": "child", "flagged": False}, to=p_sid)
                print(f"[SANDBOX→predator] '{reply}'")
            gevent.spawn(reply_to_predator)
        elif role == "child":
            emit("message", {**payload, "flagged": False}, to=c_sid)
            captured_text = text
            def reply_to_child():
                reply = ai_as_predator(captured_text)
                socketio.emit("message", {"name": p_user.get("name",""), "avatar": p_user.get("avatar","🐼"), "text": reply, "sid": p_sid, "risk": risk, "msg_score": 0, "stage": stage, "flags": [], "role": "predator", "flagged": False}, to=c_sid)
                print(f"[SANDBOX→child] '{reply}'")
            gevent.spawn(reply_to_child)
        if sandbox_msg_count >= WIND_DOWN_AFTER and not wind_down_started:
            wind_down_started = True
            gevent.spawn(run_wind_down)
        if c_sid: emit("risk_bar", {"risk": risk, "stage": stage}, to=c_sid)
        return

    if role == "predator":
        emit("message", {**payload, "flagged": False, "flags": []}, to=p_sid)
        if c_sid: emit("message", {**payload, "flagged": len(flags) > 0 and msg_score >= 0.3}, to=c_sid)
        if risk >= SANDBOX_THRESHOLD:
            room_sandbox = True; save_evidence()
            if c_sid: emit("sandbox_activated", {"risk": risk, "stage": stage}, to=c_sid)
            print(f"[SANDBOX ACTIVATED] risk={risk:.3f}")
            captured_text = text
            p_user_snap = dict(p_user); c_user_snap = dict(c_user)
            def send_opening_messages():
                gevent.sleep(1)
                opening_to_predator = ai_as_child(captured_text)
                if p_sid:
                    socketio.emit("message", {"name": c_user_snap.get("name",""), "avatar": c_user_snap.get("avatar","🐼"), "text": opening_to_predator, "sid": c_sid, "risk": risk, "msg_score": 0, "stage": stage, "flags": [], "role": "child", "flagged": False}, to=p_sid)
                gevent.sleep(1.5)
                opening_to_child = ai_as_predator(captured_text)
                if c_sid:
                    socketio.emit("message", {"name": p_user_snap.get("name",""), "avatar": p_user_snap.get("avatar","🐼"), "text": opening_to_child, "sid": p_sid, "risk": risk, "msg_score": 0, "stage": stage, "flags": [], "role": "predator", "flagged": False}, to=c_sid)
            gevent.spawn(send_opening_messages)
        elif risk >= 0.75 and c_sid: emit("risk_update", {"risk": risk, "stage": stage, "level": "high"}, to=c_sid)
        elif risk >= 0.50 and c_sid: emit("risk_update", {"risk": risk, "stage": stage, "level": "medium"}, to=c_sid)
        if c_sid: emit("risk_bar", {"risk": risk, "stage": stage}, to=c_sid)
    elif role == "child":
        emit("message", {**payload, "flagged": False}, to=c_sid)
        if p_sid: emit("message", {**payload, "flagged": False}, to=p_sid)
    print(f"[MSG] {name}({role}): '{text[:50]}' | score={msg_score:.2f} | risk={risk:.3f} | sandbox={room_sandbox}")

@socketio.on("image")
def on_image(data):
    global room_sandbox, sandbox_msg_count, wind_down_started
    sid  = request.sid; user = users.get(sid, {}); role = user.get("role", "unknown")
    c_sid = child_sid; p_sid = predator_sid; c_user = dict(users.get(c_sid, {}))
    if role == "predator":
        risk = update_risk(0.25); stage = get_stage(risk)
        room_history.append({"sender": user.get("name","?"), "role": role, "text": "[IMAGE]", "score": 0.25, "flags": ["Image sent"], "ts": datetime.now().isoformat()})
        payload = {"name": user.get("name","?"), "avatar": user.get("avatar","🐼"), "image": data.get("image",""), "filename": data.get("filename","img"), "sid": sid, "risk": risk, "role": role}
        emit("image", {**payload, "flagged": False}, to=p_sid)
        if room_sandbox:
            sandbox_msg_count += 1
            def img_reply():
                reply = ai_as_child("[someone sent me an image]")
                socketio.emit("message", {"name": c_user.get("name",""), "avatar": c_user.get("avatar","🐼"), "text": reply, "sid": c_sid, "risk": risk, "msg_score": 0, "stage": stage, "flags": [], "role": "child", "flagged": False}, to=p_sid)
            gevent.spawn(img_reply)
            if sandbox_msg_count >= WIND_DOWN_AFTER and not wind_down_started:
                wind_down_started = True; gevent.spawn(run_wind_down)
        else:
            if c_sid: emit("image", {**payload, "flagged": True}, to=c_sid)
            if risk >= SANDBOX_THRESHOLD:
                room_sandbox = True; save_evidence()
                if c_sid: emit("sandbox_activated", {"risk": risk, "stage": stage}, to=c_sid)
            elif risk >= 0.75 and c_sid: emit("risk_update", {"risk": risk, "stage": stage, "level": "high"}, to=c_sid)
            elif risk >= 0.50 and c_sid: emit("risk_update", {"risk": risk, "stage": stage, "level": "medium"}, to=c_sid)
        if c_sid: emit("risk_bar", {"risk": risk, "stage": stage}, to=c_sid)
    elif role == "child":
        room_history.append({"sender": user.get("name","?"), "role": role, "text": "[IMAGE]", "score": 0, "flags": [], "ts": datetime.now().isoformat()})
        emit("image", {"name": user.get("name","?"), "avatar": user.get("avatar","🐼"), "image": data.get("image",""), "filename": data.get("filename","img"), "sid": sid, "risk": room_risk, "role": role, "flagged": False}, to=c_sid)
        print("[BLOCKED] Child image — predator did not receive it")

@socketio.on("typing")
def on_typing(data):
    sid = request.sid; user = users.get(sid, {})
    emit("typing", {"name": user.get("name",""), "typing": data.get("typing", False)}, to=ROOM, skip_sid=sid)

def reset_room():
    global room_risk, room_history, room_sandbox, sandbox_msg_count
    global wind_down_started, child_sid, predator_sid
    room_risk         = 0.0
    room_history      = []
    room_sandbox      = False
    sandbox_msg_count = 0
    wind_down_started = False
    child_sid         = None
    predator_sid      = None
    print("[RESET] Room state cleared — ready for new session")

@socketio.on("disconnect")
def on_disconnect():
    global child_sid, predator_sid
    sid = request.sid; user = users.pop(sid, {})
    if user:
        if sid == child_sid:    child_sid    = None
        if sid == predator_sid: predator_sid = None
        emit("user_left", {"name": user.get("name","")}, to=ROOM)
        print(f"[-] {user.get('name')} ({user.get('role')}) left")
        if len(users) == 0:
            reset_room()

if __name__ == "__main__":
    print("=" * 60)
    print("  SafeGuard — http://0.0.0.0:3000")
    print(f"  Sandbox threshold : {SANDBOX_THRESHOLD}")
    print(f"  Per-message cap   : {MSG_SCORE_CAP}")
    print(f"  Email via Resend  : {'YES' if RESEND_API_KEY else 'NO'}")
    print(f"  Email via Gmail   : {'YES' if SMTP_EMAIL else 'NO'}")
    print(f"  Parent email      : {PARENT_EMAIL or 'NOT SET'}")
    print("=" * 60)
    socketio.run(app, host="0.0.0.0", port=3000, debug=False)