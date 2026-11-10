"""
Sync and repair all 980 MCQs in MongoDB from 'data/static_gk_1000_clean.json'.
- Identifies corrupted/truncated questions in `db["questions"]` (e.g. truncated Q308 'Constitution of India?',
  or options containing leaked next question lines like 'Right to Housing 309. Which Article...').
- Updates them in-place with verified clean question text, exact 4 options, 0-based correct_answer index,
  and proper category.
- Inserts any missing questions from the 980 set.
- Cleans up any corrupted orphan fragments in DB.
- Updates any user quiz_sets that imported this file.
"""

import sys
import os
import json
import re
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_static_gk")

# Ensure repository root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.database import DatabaseManager

CLEAN_JSON_PATH = os.path.join(REPO_ROOT, "data", "static_gk_1000_clean.json")


def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(s).lower())


def main():
    if not os.path.exists(CLEAN_JSON_PATH):
        logger.error(f"Clean dataset not found at {CLEAN_JSON_PATH}!")
        sys.exit(1)

    with open(CLEAN_JSON_PATH, "r", encoding="utf-8") as f:
        clean_questions = json.load(f)

    logger.info(f"Loaded {len(clean_questions)} verified questions from {CLEAN_JSON_PATH}")

    try:
        db_mgr = DatabaseManager()
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        sys.exit(1)

    questions_col = db_mgr.questions_col
    all_db_q = list(questions_col.find())
    logger.info(f"Currently in MongoDB 'questions' collection: {len(all_db_q)} questions")

    # Map existing DB questions by various lookup keys
    # _id -> doc
    db_by_id = {q["_id"]: q for q in all_db_q}
    matched_db_ids = set()

    updated_count = 0
    inserted_count = 0

    for idx, clean_q in enumerate(clean_questions):
        c_num = clean_q.get("_original_num", idx + 1)
        c_text = clean_q["question"].strip()
        c_norm = norm(c_text)
        c_opts = clean_q["options"]
        c_ans = clean_q["correct_answer"]
        c_cat = clean_q["category"]
        c_ans_text = c_opts[c_ans] if 0 <= c_ans < len(c_opts) else ""

        # Strategy 1: Exact or normalized question match
        best_match_id = None
        for db_id, q in db_by_id.items():
            if db_id in matched_db_ids:
                continue
            db_q_text = str(q.get("question", "")).strip()
            db_norm = norm(db_q_text)

            if db_norm == c_norm:
                best_match_id = db_id
                break

            # Strategy 2: Clean question contains DB question or vice versa (e.g. truncated Q308)
            if len(db_norm) >= 15 and len(c_norm) >= 15:
                if db_norm in c_norm or c_norm in db_norm:
                    # Verify options overlap
                    db_opts = [norm(o) for o in q.get("options", []) if o]
                    overlap = sum(1 for o in c_opts if norm(o) in db_opts)
                    if overlap >= 2 or norm(c_ans_text) in db_opts:
                        best_match_id = db_id
                        break

            # Strategy 3: DB question has leaked question number or options contain next question
            if f"{c_num}." in db_q_text or any(f"{c_num}." in str(o) for o in q.get("options", [])):
                best_match_id = db_id
                break

        # Strategy 4: Fallback - match on at least 3 matching option texts and answer
        if best_match_id is None:
            for db_id, q in db_by_id.items():
                if db_id in matched_db_ids:
                    continue
                db_opts = [norm(o) for o in q.get("options", []) if o]
                overlap = sum(1 for o in c_opts if norm(o) in db_opts)
                if overlap >= 3 and norm(c_ans_text) in db_opts:
                    best_match_id = db_id
                    break

        if best_match_id is not None:
            matched_db_ids.add(best_match_id)
            # Update in MongoDB
            questions_col.update_one(
                {"_id": best_match_id},
                {
                    "$set": {
                        "question": c_text,
                        "options": c_opts,
                        "correct_answer": c_ans,
                        "category": c_cat,
                    }
                }
            )
            updated_count += 1
        else:
            # Insert fresh question
            # Allocate integer id
            last_q = questions_col.find_one(sort=[("id", -1)])
            next_id = (last_q.get("id") or 0) + 1 if last_q else 1
            new_doc = {
                "id": next_id,
                "question": c_text,
                "options": c_opts,
                "correct_answer": c_ans,
                "category": c_cat,
                "explanation": "",
            }
            questions_col.insert_one(new_doc)
            inserted_count += 1

    logger.info(f"Database Sync Complete:")
    logger.info(f"  • Repaired & Updated: {updated_count} questions")
    logger.info(f"  • Inserted as New:   {inserted_count} questions")
    logger.info(f"  • Total Clean Bank:  {len(clean_questions)} questions")

    # Clean up corrupted leftover orphan fragments (< 2 options or corrupt question text)
    orphans_removed = 0
    for db_id, q in db_by_id.items():
        if db_id not in matched_db_ids:
            opts = q.get("options", [])
            q_text = str(q.get("question", "")).strip()
            # If question text is empty or opts < 2 or clearly a severed fragment like 'Constitution of India?'
            if not q_text or len(opts) < 2 or q_text.lower() == "constitution of india?":
                questions_col.delete_one({"_id": db_id})
                orphans_removed += 1

    if orphans_removed > 0:
        logger.info(f"  • Removed Corrupted Orphan Fragments: {orphans_removed}")

    # Also inspect and repair quiz_sets
    quiz_sets_col = db_mgr.quiz_sets_col
    all_sets = list(quiz_sets_col.find())
    sets_fixed = 0
    for qs in all_sets:
        qs_questions = qs.get("questions", [])
        modified = False
        new_qs_list = []
        for q in qs_questions:
            q_text = q.get("question", "").strip()
            # Check if this question is one of our 980
            matched_clean = None
            q_n = norm(q_text)
            for cq in clean_questions:
                cq_n = norm(cq["question"])
                if cq_n == q_n or (len(q_n) >= 15 and (q_n in cq_n or cq_n in q_n)):
                    matched_clean = cq
                    break
            if matched_clean:
                new_qs_list.append({
                    "question": matched_clean["question"],
                    "options": matched_clean["options"],
                    "correct_answer": matched_clean["correct_answer"],
                    "category": matched_clean["category"],
                    "explanation": q.get("explanation", ""),
                })
                modified = True
            else:
                new_qs_list.append(q)
        if modified:
            quiz_sets_col.update_one({"_id": qs["_id"]}, {"$set": {"questions": new_qs_list}})
            sets_fixed += 1

    if sets_fixed > 0:
        logger.info(f"  • Fixed Questions in {sets_fixed} Private Quiz Sets")

    remaining_total = questions_col.count_documents({})
    logger.info(f"Total Active Questions in MongoDB: {remaining_total}")


if __name__ == "__main__":
    main()
