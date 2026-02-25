import os
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, timedelta
from flask import Flask, jsonify, render_template, request
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
client = Anthropic()

# Support configurable data directory (set DATA_DIR=/data on Railway with a volume)
DATA_DIR = os.environ.get("DATA_DIR", ".")
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")

LEVEL_NAMES = {
    1: "Explorer",
    2: "Problem Solver",
    3: "AMC8 Challenger",
    4: "Competition Pro",
    5: "Math Champion",
}

LEVEL_DESCRIPTIONS = {
    1: "beginner level with straightforward arithmetic and basic geometry puzzles",
    2: "elementary competition level suitable for MOEMS Division E and Noetic beginners",
    3: "intermediate AMC8 level — comparable to AMC8 problems 1–15",
    4: "advanced AMC8 level (problems 16–25) and Math Kangaroo levels 5–6",
    5: "expert competition level — the hardest AMC8 and challenging Math Kangaroo problems",
}


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {
        "level": 2,
        "streak": 0,
        "last_answered_date": None,
        "history": [],
        "daily_cache": {},
        "weak_areas": [],
    }


def save_progress(data):
    os.makedirs(os.path.dirname(os.path.abspath(PROGRESS_FILE)), exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def generate_questions(level, weak_areas=None):
    # Build the weak-areas section if there are recent mistakes
    weak_str = ""
    if weak_areas:
        recent = weak_areas[-5:]
        lines = "\n".join(f"  - {w['topic']}: {w['hint']}" for w in recent)
        weak_str = f"""
PRIORITY — The student recently got these types of questions WRONG. \
Include at least 1 question that revisits these specific concepts with a fresh problem \
(same concept, new numbers/scenario):
{lines}
"""

    prompt = f"""You are an expert math competition coach creating daily practice problems for a 10-year-old who competes in AMC8, Math Kangaroo, MOEMS, and Noetic Math.

Generate exactly 3 multiple-choice math problems at {LEVEL_DESCRIPTIONS[level]}.
{weak_str}
Choose varied topics from:
- Number theory (factors, primes, divisibility, remainders)
- Geometry (area, perimeter, angles, triangles, circles)
- Counting and combinatorics (arrangements, selections)
- Probability
- Algebra and patterns
- Arithmetic puzzles and logic

Return ONLY a valid JSON object with this exact structure (no markdown, no explanation):
{{
  "questions": [
    {{
      "id": 1,
      "topic": "Number Theory",
      "question": "Full question text here. Use clear, precise mathematical language.",
      "choices": {{
        "A": "first option",
        "B": "second option",
        "C": "third option",
        "D": "fourth option"
      }},
      "correct": "B",
      "solution": "Step-by-step solution. Show all work clearly. Explain WHY each step is taken, not just HOW.",
      "methodology": "Key insight: [The main concept, trick, or strategy that unlocks this type of problem — make it memorable and generalizable]"
    }},
    {{
      "id": 2,
      "topic": "Geometry",
      "question": "...",
      "choices": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
      "correct": "A",
      "solution": "...",
      "methodology": "Key insight: ..."
    }},
    {{
      "id": 3,
      "topic": "Counting",
      "question": "...",
      "choices": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
      "correct": "C",
      "solution": "...",
      "methodology": "Key insight: ..."
    }}
  ]
}}

Guidelines:
- Wrong answer choices should reflect common mistakes students actually make
- Solutions must be educational: explain the reasoning, not just the arithmetic
- The methodology tip should be a generalizable strategy the student can apply to similar problems
- Make problems engaging and varied — no two should feel the same
- Use whole numbers only; avoid fractions unless appropriate for the level"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()

    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    data = json.loads(text)
    return data["questions"]


def get_streak_days(progress):
    """Return the last 14 days as a list of {date, day_label, completed} dicts."""
    cache = progress.get("daily_cache", {})
    today = date.today()
    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        d_str = str(d)
        completed = cache.get(d_str, {}).get("answered", False)
        days.append({
            "date": d_str,
            "day_label": d.strftime("%a")[0],
            "completed": completed,
            "is_today": i == 0,
        })
    return days


def milestone_for(streak):
    milestones = [
        (100, "Legend", "🏆"),
        (50,  "Unstoppable", "🚀"),
        (30,  "One Month", "👑"),
        (21,  "Three Weeks", "💎"),
        (14,  "Two Weeks", "🥇"),
        (7,   "One Week", "🏅"),
        (3,   "On a Roll", "🌟"),
        (1,   "Started", "🔥"),
    ]
    next_milestones = [
        (3, "🌟"), (7, "🏅"), (14, "🥇"), (21, "💎"),
        (30, "👑"), (50, "🚀"), (100, "🏆"),
    ]

    current_label, current_emoji = "", ""
    for threshold, label, emoji in milestones:
        if streak >= threshold:
            current_label, current_emoji = label, emoji
            break

    next_threshold, next_label = None, ""
    for threshold, emoji in next_milestones:
        if streak < threshold:
            next_threshold, next_label = threshold, emoji
            break

    return {
        "current_label": current_label,
        "current_emoji": current_emoji,
        "next_threshold": next_threshold,
        "next_emoji": next_label,
    }


def safe_questions(questions):
    """Strip answers/solutions before sending to the browser."""
    return [
        {"id": q["id"], "topic": q["topic"],
         "question": q["question"], "choices": q["choices"]}
        for q in questions
    ]


# ── Email report ──────────────────────────────────────────

def send_progress_email(score, results, streak, level, level_name, wrong_topics, history):
    """Send a progress report email to the parent after each quiz."""
    gmail_user     = os.environ.get("GMAIL_USER", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    parent_email   = os.environ.get("PARENT_EMAIL", "").strip()

    if not all([gmail_user, gmail_password, parent_email]):
        return  # not configured — skip silently

    # Support comma-separated list of recipient emails
    recipients = [e.strip() for e in parent_email.split(",") if e.strip()]

    today     = date.today().strftime("%A, %B %d %Y")
    wrong     = 3 - score
    streak_fire = "🔥" * min(streak, 5)

    # Score colour
    score_color = "#22c55e" if score == 3 else "#f59e0b" if score >= 2 else "#ef4444"

    # Build per-question rows
    q_rows = ""
    for i, r in enumerate(results):
        icon    = "✅" if r["is_correct"] else "❌"
        verdict = "Correct" if r["is_correct"] else f"Wrong (you chose {r['user_answer']}, answer: {r['correct_answer']})"
        v_color = "#22c55e" if r["is_correct"] else "#ef4444"
        q_rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #1e293b;color:#94a3b8;
                     font-size:13px;white-space:nowrap;">Q{i+1} · {r.get('topic','')}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #1e293b;color:#f1f5f9;font-size:13px;">
            {r['question'][:120]}{'…' if len(r['question'])>120 else ''}
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #1e293b;color:{v_color};
                     font-size:13px;white-space:nowrap;">{icon} {verdict}</td>
        </tr>"""

    # Weak topics section
    if wrong_topics:
        topics_clean = list(dict.fromkeys(t for t in wrong_topics if t and t.strip()))
        topics_html  = "".join(
            f'<span style="background:#1e3a5f;color:#93c5fd;padding:4px 12px;border-radius:999px;'
            f'font-size:13px;margin:3px;display:inline-block;">{t}</span>'
            for t in topics_clean
        )
        weak_section = f"""
        <div style="margin-top:20px;background:#0f172a;border:1px solid #1e3a5f;
                    border-radius:10px;padding:16px 20px;">
          <p style="margin:0 0 10px;color:#60a5fa;font-weight:700;font-size:13px;
                    text-transform:uppercase;letter-spacing:1px;">📌 Focus areas for next time</p>
          <div>{topics_html}</div>
        </div>"""
    else:
        weak_section = ""

    # Recent history mini-chart (last 7 sessions)
    recent = history[-7:]
    history_cells = ""
    for h in recent:
        s = h.get("score", 0)
        c = "#22c55e" if s == 3 else "#f59e0b" if s >= 2 else "#ef4444"
        history_cells += (
            f'<td style="text-align:center;padding:6px 10px;">'
            f'<div style="width:28px;height:28px;border-radius:50%;background:{c};'
            f'color:#000;font-weight:800;font-size:12px;line-height:28px;margin:0 auto;">{s}</div>'
            f'<div style="color:#64748b;font-size:10px;margin-top:3px;">'
            f'{h.get("date","")[-5:]}</div></td>'
        )
    history_section = f"""
        <div style="margin-top:20px;background:#0f172a;border:1px solid #1e293b;
                    border-radius:10px;padding:16px 20px;">
          <p style="margin:0 0 12px;color:#94a3b8;font-weight:700;font-size:13px;
                    text-transform:uppercase;letter-spacing:1px;">📈 Recent sessions</p>
          <table><tr>{history_cells}</tr></table>
        </div>""" if recent else ""

    subject = f"🧙‍♂️ Arhan practiced today — {score}/3 correct  {streak_fire} {streak}-day streak"

    html_body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1e293b,#0f172a);
              border:1px solid #334155;border-radius:14px;padding:28px 28px 20px;
              text-align:center;margin-bottom:20px;">
    <div style="font-size:2.8rem;margin-bottom:8px;">🧙‍♂️</div>
    <div style="color:#f59e0b;font-size:1.4rem;font-weight:800;">Arhan the Math Wizard</div>
    <div style="color:#64748b;font-size:0.82rem;margin-top:4px;">{today}</div>
  </div>

  <!-- Score card -->
  <div style="background:linear-gradient(135deg,#1e293b,#0f172a);
              border:1px solid #334155;border-radius:14px;padding:28px;
              text-align:center;margin-bottom:20px;">
    <div style="font-size:4rem;font-weight:800;color:{score_color};line-height:1;">{score}/3</div>
    <div style="color:#94a3b8;font-size:0.8rem;text-transform:uppercase;
                letter-spacing:1px;margin-top:6px;">Today's Score</div>
    <div style="display:inline-flex;gap:16px;margin-top:16px;">
      <span style="background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.3);
                   color:#22c55e;font-weight:700;font-size:1rem;padding:7px 18px;
                   border-radius:999px;">{score} ✓ correct</span>
      <span style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);
                   color:#ef4444;font-weight:700;font-size:1rem;padding:7px 18px;
                   border-radius:999px;">{wrong} ✗ wrong</span>
    </div>
    <div style="margin-top:16px;color:#fb923c;font-size:1.1rem;font-weight:700;">
      {streak_fire} {streak}-day streak · Level {level}: {level_name}
    </div>
  </div>

  <!-- Question breakdown -->
  <div style="background:#1e293b;border:1px solid #334155;border-radius:14px;
              overflow:hidden;margin-bottom:20px;">
    <div style="padding:14px 20px;border-bottom:1px solid #334155;">
      <span style="color:#f59e0b;font-weight:700;font-size:0.85rem;
                   text-transform:uppercase;letter-spacing:1px;">Question Breakdown</span>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#0f172a;">
          <th style="padding:8px 14px;color:#64748b;font-size:11px;text-align:left;
                     text-transform:uppercase;letter-spacing:1px;">Topic</th>
          <th style="padding:8px 14px;color:#64748b;font-size:11px;text-align:left;
                     text-transform:uppercase;letter-spacing:1px;">Question</th>
          <th style="padding:8px 14px;color:#64748b;font-size:11px;text-align:left;
                     text-transform:uppercase;letter-spacing:1px;">Result</th>
        </tr>
      </thead>
      <tbody>{q_rows}</tbody>
    </table>
  </div>

  {weak_section}
  {history_section}

  <!-- Footer -->
  <div style="text-align:center;color:#334155;font-size:0.72rem;margin-top:24px;">
    Arhan the Math Wizard · Daily Practice · Powered by Claude
  </div>

</div>
</body>
</html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Arhan the Math Wizard <{gmail_user}>"
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, recipients, msg.as_string())
        print(f"Progress email sent to {recipients}")
        return None  # success
    except Exception as e:
        err = str(e)
        print(f"Email send failed (non-fatal): {err}")
        return err  # return error string for debugging


# ── Routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def get_status():
    progress = load_progress()
    today = str(date.today())
    streak = progress.get("streak", 0)
    answered = (
        today in progress.get("daily_cache", {})
        and progress["daily_cache"][today].get("answered", False)
    )
    return jsonify({
        "level": progress["level"],
        "level_name": LEVEL_NAMES[progress["level"]],
        "streak": streak,
        "answered_today": answered,
        "date": today,
        "streak_days": get_streak_days(progress),
        "milestone": milestone_for(streak),
    })


@app.route("/api/questions")
def get_questions():
    progress = load_progress()
    today = str(date.today())

    if today in progress.get("daily_cache", {}):
        cached = progress["daily_cache"][today]
        answered = cached.get("answered", False)
        streak = progress.get("streak", 0)
        response_data = {
            "questions": safe_questions(cached["questions"]),
            "level": progress["level"],
            "level_name": LEVEL_NAMES[progress["level"]],
            "streak": streak,
            "answered": answered,
            "streak_days": get_streak_days(progress),
            "milestone": milestone_for(streak),
        }
        if answered and "results" in cached:
            response_data["results"] = cached["results"]
            response_data["score"] = cached["score"]
        return jsonify(response_data)

    # Generate fresh questions, passing any recent weak areas
    try:
        weak_areas = progress.get("weak_areas", [])
        questions = generate_questions(progress["level"], weak_areas)
    except Exception as e:
        return jsonify({"error": f"Could not generate questions: {str(e)}"}), 500

    if "daily_cache" not in progress:
        progress["daily_cache"] = {}

    progress["daily_cache"][today] = {"questions": questions, "answered": False}

    # Keep only last 30 days in cache
    all_dates = sorted(progress["daily_cache"].keys())
    if len(all_dates) > 30:
        for old in all_dates[:-30]:
            del progress["daily_cache"][old]

    save_progress(progress)

    streak = progress.get("streak", 0)
    return jsonify({
        "questions": safe_questions(questions),
        "level": progress["level"],
        "level_name": LEVEL_NAMES[progress["level"]],
        "streak": streak,
        "answered": False,
        "streak_days": get_streak_days(progress),
        "milestone": milestone_for(streak),
    })


@app.route("/api/submit", methods=["POST"])
def submit_answers():
    data = request.json
    answers = data.get("answers", {})

    progress = load_progress()
    today = str(date.today())

    if today not in progress.get("daily_cache", {}):
        return jsonify({"error": "No questions found for today. Please refresh."}), 400

    if progress["daily_cache"][today].get("answered"):
        cached = progress["daily_cache"][today]
        streak = progress.get("streak", 0)
        return jsonify({
            "already_answered": True,
            "score": cached.get("score", 0),
            "total": 3,
            "results": cached.get("results", []),
            "streak": streak,
            "level": progress["level"],
            "level_name": LEVEL_NAMES[progress["level"]],
            "streak_days": get_streak_days(progress),
            "milestone": milestone_for(streak),
        })

    questions = progress["daily_cache"][today]["questions"]

    # ── Grade ──────────────────────────────────────────
    score = 0
    results = []
    new_weak_areas = list(progress.get("weak_areas", []))

    for q in questions:
        qid = str(q["id"])
        user_answer = answers.get(qid, "").upper()
        correct = q["correct"].upper()
        is_correct = user_answer == correct
        if is_correct:
            score += 1
        else:
            # Record this as a weak area so tomorrow's questions revisit it
            hint = q.get("methodology", "").replace("Key insight:", "").strip()[:100]
            new_weak_areas.append({
                "topic": q.get("topic", "General"),
                "hint": hint,
                "date": today,
            })

        results.append({
            "id": q["id"],
            "topic": q.get("topic", ""),
            "question": q["question"],
            "choices": q["choices"],
            "user_answer": user_answer,
            "correct_answer": correct,
            "is_correct": is_correct,
            "solution": q["solution"],
            "methodology": q["methodology"],
        })

    # Keep only the 10 most recent weak areas
    progress["weak_areas"] = new_weak_areas[-10:]

    # ── Streak ─────────────────────────────────────────
    old_streak = progress.get("streak", 0)
    last = progress.get("last_answered_date")
    yesterday = str(date.today() - timedelta(days=1))
    if last == yesterday:
        progress["streak"] = old_streak + 1
    elif last == today:
        pass  # already counted today
    else:
        progress["streak"] = 1
    progress["last_answered_date"] = today

    # ── Level adjustment ───────────────────────────────
    old_level = progress["level"]
    if score == 3 and progress["level"] < 5:
        progress["level"] += 1
    elif score == 0 and progress["level"] > 1:
        progress["level"] -= 1

    # ── Persist ────────────────────────────────────────
    progress["daily_cache"][today]["answered"] = True
    progress["daily_cache"][today]["score"] = score
    progress["daily_cache"][today]["results"] = results
    progress["history"].append({"date": today, "score": score, "level": old_level})
    save_progress(progress)

    new_streak = progress["streak"]
    wrong_topics = [r["topic"] for r in results if not r["is_correct"]]

    # ── Email report to parents ─────────────────────────
    try:
        send_progress_email(
            score=score,
            results=results,
            streak=new_streak,
            level=progress["level"],
            level_name=LEVEL_NAMES[progress["level"]],
            wrong_topics=wrong_topics,
            history=progress.get("history", []),
        )
    except Exception as e:
        print(f"Email report error (non-fatal): {e}")

    return jsonify({
        "score": score,
        "total": 3,
        "correct": score,
        "wrong": 3 - score,
        "wrong_topics": wrong_topics,
        "results": results,
        "streak": new_streak,
        "level": progress["level"],
        "level_name": LEVEL_NAMES[progress["level"]],
        "old_level": old_level,
        "level_up": progress["level"] > old_level,
        "level_down": progress["level"] < old_level,
        "streak_days": get_streak_days(progress),
        "milestone": milestone_for(new_streak),
        "streak_increased": new_streak > old_streak,
    })


@app.route("/api/test-email")
def test_email():
    """Debug endpoint — tests email config and returns result."""
    gmail_user     = os.environ.get("GMAIL_USER", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    parent_email   = os.environ.get("PARENT_EMAIL", "")

    # Show length + first char hint to diagnose empty vs missing
    def var_status(val, name):
        if val is None:
            return f"❌ NOT IN ENVIRONMENT"
        if val == "":
            return f"❌ SET BUT EMPTY (length=0)"
        return f"✅ set (length={len(val)}, starts with '{val[0]}')"

    config_status = {
        "GMAIL_USER":         var_status(os.environ.get("GMAIL_USER"),         "GMAIL_USER"),
        "GMAIL_APP_PASSWORD": var_status(os.environ.get("GMAIL_APP_PASSWORD"), "GMAIL_APP_PASSWORD"),
        "PARENT_EMAIL":       var_status(os.environ.get("PARENT_EMAIL"),       "PARENT_EMAIL"),
        "all_env_keys_with_G": [k for k in os.environ if k.startswith("G")],
    }

    if not all([gmail_user, gmail_password, parent_email]):
        return jsonify({"status": "error", "config": config_status,
                        "message": "One or more environment variables are missing."})

    # Send a real test email
    fake_results = [
        {"id": 1, "topic": "Number Theory", "question": "Test question 1",
         "user_answer": "A", "correct_answer": "A", "is_correct": True,
         "solution": "test", "methodology": "test"},
        {"id": 2, "topic": "Geometry", "question": "Test question 2",
         "user_answer": "B", "correct_answer": "C", "is_correct": False,
         "solution": "test", "methodology": "test"},
    ]
    error = send_progress_email(
        score=1, results=fake_results, streak=3,
        level=2, level_name="Problem Solver",
        wrong_topics=["Geometry"], history=[]
    )

    if error:
        return jsonify({"status": "error", "config": config_status, "error": error})
    return jsonify({"status": "✅ Email sent successfully!", "config": config_status,
                    "sent_to": parent_email})


@app.route("/api/reset", methods=["POST"])
def reset_today():
    """Clear today's cache so fresh questions are generated on next load."""
    progress = load_progress()
    today = str(date.today())
    if today in progress.get("daily_cache", {}):
        del progress["daily_cache"][today]
    save_progress(progress)
    return jsonify({"ok": True})


@app.route("/api/history")
def get_history():
    progress = load_progress()
    return jsonify({
        "history": progress.get("history", [])[-14:],
        "level": progress["level"],
        "level_name": LEVEL_NAMES[progress["level"]],
        "streak": progress.get("streak", 0),
        "weak_areas": progress.get("weak_areas", [])[-5:],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
