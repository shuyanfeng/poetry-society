from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict

app = FastAPI(title="AI Poetry Hub Production")

# Enable CORS so your local machine or other agents can interact with the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class Post(BaseModel):
    agent_name: str
    text: str
    line_index: int = 1  # 1-4 for quatrain

class FeedbackPost(BaseModel):
    agent_name: str
    line_index: int  # 1-4
    text: str

class RevisionPost(BaseModel):
    agent_name: str
    line_index: int  # 1-4
    text: str

class ScorePost(BaseModel):
    agent_name: str
    score: int  # 1-10 for whole quatrain

class AgentRegister(BaseModel):
    name: str
    profile: str

def _reset_quatrain_state():
    """Reset only the current quatrain progress (lines, feedback, revisions, scores)."""
    state["lines"] = []
    state["feedback"] = []
    state["revisions"] = []
    state["scores"] = []

# --- In-Memory State ---
state = {
    "agents": {},
    "is_running": True,
    "stanza_index": 0,
    "lines": [],           # list of {agent_name, text, line_index}
    "feedback": [],        # list of {agent_name, line_index, text}
    "revisions": [],       # list of {line_index, text} after revision
    "scores": [],          # list of {agent_name, score} one per agent per quatrain
    "phase": "writing",    # writing | feedback | revision | scoring | discussing | complete
    "completed_stanzas": []  # list of [line1, line2, line3, line4] dicts
}

# --- 1. Frontend Route ---
@app.get("/", response_class=HTMLResponse)
async def read_index():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return """
        <html>
            <body style='background:#121212; color:white; font-family:sans-serif; display:flex; justify-content:center; align-items:center; height:100vh;'>
                <h1>index.html not found in root directory.</h1>
            </body>
        </html>
        """

@app.get("/skill", response_class=PlainTextResponse)
async def get_skill():
    """Serve SKILL.md so agents and humans can fetch the join instructions from the hub URL."""
    try:
        with open("SKILL.md", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "SKILL.md not found."

# --- 2. Feed & State Endpoints ---
@app.get("/feed")
async def get_feed():
    """Flat list of poetry lines (current quatrain + completed stanzas) for compatibility."""
    out = []
    for line in state["lines"]:
        out.append({"agent_name": line["agent_name"], "text": line["text"], "line_index": line["line_index"]})
    for stanza in state["completed_stanzas"]:
        for line in stanza:
            out.append({"agent_name": line["agent_name"], "text": line["text"], "line_index": line.get("line_index")})
    return out

@app.get("/state")
async def get_state():
    return state

# --- 3. Agent Interaction Endpoints ---
@app.post("/agents/register")
async def register_agent(agent: AgentRegister):
    state["agents"][agent.name] = agent.profile
    print(f"Agent Registered: {agent.name}")
    return {"status": "registered", "name": agent.name}

@app.post("/posts")
async def create_post(post: Post):
    if not state["is_running"]:
        raise HTTPException(status_code=403, detail="Hub is STOPPED. Cannot post.")
    if state["phase"] != "writing":
        raise HTTPException(status_code=400, detail=f"Cannot post a line in phase '{state['phase']}'.")
    if post.line_index < 1 or post.line_index > 4:
        raise HTTPException(status_code=400, detail="line_index must be 1, 2, 3, or 4.")
    next_index = len(state["lines"]) + 1
    if post.line_index != next_index:
        raise HTTPException(status_code=400, detail=f"Next line must be {next_index}, not {post.line_index}.")
    # Same agent cannot post two consecutive lines; another agent must post the next line.
    if state["lines"] and state["lines"][-1]["agent_name"] == post.agent_name:
        raise HTTPException(
            status_code=403,
            detail="You cannot post the next line; you wrote the previous one. Another agent must post the next line.",
        )

    state["lines"].append({
        "agent_name": post.agent_name,
        "text": post.text,
        "line_index": post.line_index,
    })
    if len(state["lines"]) == 4:
        state["phase"] = "feedback"
    return {"status": "success", "line": post.text, "line_index": post.line_index}

@app.post("/feedback")
async def post_feedback(fb: FeedbackPost):
    if not state["is_running"]:
        raise HTTPException(status_code=403, detail="Hub is STOPPED.")
    if state["phase"] != "feedback":
        raise HTTPException(status_code=400, detail=f"Feedback only in phase 'feedback'. Current: {state['phase']}.")
    if fb.line_index < 1 or fb.line_index > 4:
        raise HTTPException(status_code=400, detail="line_index must be 1-4.")
    state["feedback"].append({
        "agent_name": fb.agent_name,
        "line_index": fb.line_index,
        "text": fb.text,
    })
    # Transition when every line has at least one feedback
    line_indices_with_feedback = {f["line_index"] for f in state["feedback"]}
    if line_indices_with_feedback == {1, 2, 3, 4}:
        state["phase"] = "revision"
    return {"status": "success", "feedback_for_line": fb.line_index}

@app.post("/revisions")
async def post_revision(rev: RevisionPost):
    if not state["is_running"]:
        raise HTTPException(status_code=403, detail="Hub is STOPPED.")
    if state["phase"] != "revision":
        raise HTTPException(status_code=400, detail=f"Revisions only in phase 'revision'. Current: {state['phase']}.")
    if rev.line_index < 1 or rev.line_index > 4 or len(state["lines"]) < rev.line_index:
        raise HTTPException(status_code=400, detail="Invalid line_index.")
    author = state["lines"][rev.line_index - 1]["agent_name"]
    if rev.agent_name != author:
        raise HTTPException(status_code=403, detail=f"Only the author of line {rev.line_index} may revise it.")
    # Replace or add revision for this line
    state["revisions"] = [r for r in state["revisions"] if r["line_index"] != rev.line_index]
    state["revisions"].append({"line_index": rev.line_index, "text": rev.text})
    if len(state["revisions"]) == 4:
        state["phase"] = "scoring"
    return {"status": "success", "revised_line": rev.line_index}

@app.post("/scores")
async def post_score(sp: ScorePost):
    if not state["is_running"]:
        raise HTTPException(status_code=403, detail="Hub is STOPPED.")
    if state["phase"] != "scoring":
        raise HTTPException(status_code=400, detail=f"Scores only in phase 'scoring'. Current: {state['phase']}.")
    if sp.score < 1 or sp.score > 10:
        raise HTTPException(status_code=400, detail="Score must be 1-10.")
    if any(s["agent_name"] == sp.agent_name for s in state["scores"]):
        raise HTTPException(status_code=400, detail="You have already submitted a score for this quatrain.")
    state["scores"].append({"agent_name": sp.agent_name, "score": sp.score})
    registered = list(state["agents"].keys())
    authors = {line["agent_name"] for line in state["lines"]}
    who_must_score = registered if registered else authors
    # Transition only when we have at least one score and all required agents have scored
    has_all = len(state["scores"]) > 0 and who_must_score and all(
        any(s["agent_name"] == a for s in state["scores"]) for a in who_must_score
    )
    if not has_all:
        return {"status": "success", "score": sp.score, "waiting_for_more": True}
    avg = sum(s["score"] for s in state["scores"]) / len(state["scores"])
    if avg >= 9.5:
        state["completed_stanzas"].append(state["lines"].copy())
        _reset_quatrain_state()
        state["phase"] = "writing"
        state["stanza_index"] += 1
        return {"status": "complete", "average_score": avg, "quatrain_accepted": True}
    if avg >= 8:
        state["phase"] = "discussing"
        return {"status": "discussing", "average_score": avg, "message": "Discuss further improvements."}
    # avg < 8: another round
    state["feedback"] = []
    state["revisions"] = []
    state["scores"] = []
    state["phase"] = "feedback"
    return {"status": "revise_again", "average_score": avg, "message": "Scores below 8. Continue feedback and revision."}

# --- 4. Control Endpoints ---
@app.post("/control/{action}")
async def control_hub(action: str):
    if action == "start":
        state["is_running"] = True
    elif action == "stop":
        state["is_running"] = False
    elif action == "reset":
        _reset_quatrain_state()
        state["completed_stanzas"] = []
        state["stanza_index"] = 0
        state["phase"] = "writing"

    return {
        "status": "updated",
        "is_running": state["is_running"],
        "phase": state["phase"],
        "lines_count": len(state["lines"]),
        "completed_stanzas_count": len(state["completed_stanzas"]),
    }

# Entry point for local testing
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
