"""Free local scoring, no LLM."""
import re
from config import LEVELS, GER_REQ_RE


def quick_info(profile: dict, job: dict) -> None:
    text = f"{job['title']} {job['description']}".lower()
    skills = [s for s in (profile.get("skills") or []) if s and s.lower() in text]
    words = {w.lower() for t in (profile.get("job_titles") or [])
             for w in re.split(r"\W+", t) if len(w) >= 5}
    title_hits = sum(1 for w in words if w in text)
    job["matched_skills"] = skills[:6]
    job["score"] = len(skills) * 2 + title_hits
    my_level = LEVELS.get(str(profile.get("german_level") or "").upper(), 0)
    job["german_warning"] = bool(GER_REQ_RE.search(job["description"])) and my_level < 5