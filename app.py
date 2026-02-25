import os
import json
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
